"""
B-2b verification — the formula engine runs on BOQ saves.

WHAT THIS FILE PROVES
---------------------
  * NO RULES, NO CHANGE. The picker save, the upload, and the reviewer's add_line and
    set_quantity give the same lines and quantities as before, write no recalculation
    record, and cost exactly one extra query each (the exists() gate). The counts are pinned
    against figures measured on 6765ef8 (before B-2b) with this file's own fixture.
  * WITH RULES. A save adds a computed line; a changed driver updates it with one record
    (old, new, source); an unchanged driver writes none; a chain two deep follows in order.
    The picker's posted quantity for a computed line is ignored, the line is never deleted
    by the picker, and a computed item posted but not on the BOQ is not created (Q1).
  * D1. A computed line whose rule falls to 0 is removed (with a record that survives it),
    none is added at 0, a line with an ordered quantity is kept at 0, and the reviewer path
    sets 0 instead of deleting (Q4).
  * Q9. A computed item on two lines is not written, and the save says so.
  * OVERRIDE. Blank note refused; an overridden line is left alone by later saves; later
    rules read the overridden value; clearing recalculates in the same request; reviewers,
    non-computed lines and locked BOQs are refused; both log entries are written.
  * D2. A reviewer cannot set or add a computed item; nothing is saved.
  * Z2. A reviewer correction to a driver writes exactly one BOQCorrection and links the
    recalculations to it; the reviewer-corrections queryset and the change-request evidence
    see one row.
  * D3. An upload's figure for a computed item is replaced and listed; an override survives
    the upload; the activity text counts D1 removals.
  * NOT HOOKED. The ACCB endpoint and fix_tin_shade_ballast --apply-counts write no record
    and move no computed line.
  * PROJECT TYPE (Q3). A Residential rule never touches a RESCO BOQ.
  * THE LOCK (Q2). Taken with rules, not without; on Postgres the SQL carries FOR UPDATE.
  * CONSTRAINTS (0153). A duplicate (rule, item) or (rule, group) is refused by the
    database; a blank override note too.
  * THE SEED COMMAND refuses without DEBUG and off a local database.

RUNNER: `manage.py test projects.tests_boq_formula_apply --settings=solarpms.test_settings`.
The catalogue codes here are FX-*, so they never collide with the OPX rows migrations seed;
PostgresLockTests also runs under the real settings (it is skipped on SQLite).
"""
from decimal import Decimal
from io import BytesIO, StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import boq_formulas
from . import tests_tin_shade_ballast_fix as tsb
from .accb import ACCB_CONFIGURED_CODE
from .boq_formulas import FORMULA_REVIEWER_MESSAGE, load_rules
from .design_metrics import reviewer_corrections
from .design_views import _uncited_corrections_since
from .models import (
    ActivityLog, BOQ, BOQCorrection, BOQFormulaDriver, BOQFormulaRule, BOQItem,
    BOQItemGroup, BOQItemGroupMember, BOQItemMaster, BOQLineOverride, BOQRecalculation,
    DesignAssignment, DesignAttempt, Program, Project,
    ATTEMPT_REASON_INITIAL, DESIGN_IN_DESIGN, DESIGN_IN_QC, FORMULA_SOURCE_CORRECTION,
    FORMULA_SOURCE_OVERRIDE_CLEARED, FORMULA_SOURCE_PICKER, FORMULA_SOURCE_UPLOAD,
)
from .views import BOQ_DOWNLOAD_COLUMNS, BOQ_DOWNLOAD_SHEET

D = Decimal

#: (code, description, unit, category, sort_order). FX-* never collides with seeded rows.
CATALOGUE = [
    ('FX-LA1',   'Conventional lightning arrester', 'Nos',   'Earthing',   9001),
    ('FX-LA2',   'ESE lightning arrester',          'Nos',   'Earthing',   9002),
    ('FX-EARTH', 'Earthing cable 16 sq mm',         'Meter', 'Earthing',   9003),
    ('FX-INV1',  '20 kW inverter',                  'Nos',   'Inverter',   9004),
    ('FX-INV2',  '50 kW inverter',                  'Nos',   'Inverter',   9005),
    ('FX-LOG',   'Data logger',                     'Nos',   'Monitoring', 9006),
    ('FX-RS485', 'RS485 cable',                     'Meter', 'Monitoring', 9007),
    ('FX-PANEL', 'Solar module',                    'Nos',   'Modules',    9008),
]

#: Measured on 6765ef8 (before B-2b) with this file's fixture and requests, each POST plus
#: the page it redirects to (follow=True), under solarpms.test_settings. B-2b adds exactly
#: one query to each with no rules: formula_rules_exist()'s exists().
BASELINE = {'picker': 36, 'upload': 19, 'add_line': 40, 'set_quantity': 42}


def _profile(username, role, **flags):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.capitalize())
    profile = user.profile
    profile.role = role
    profile.is_active = True
    for name, value in flags.items():
        setattr(profile, name, value)
    profile.save()
    return profile


class FormulaBase(TestCase):

    def setUp(self):
        self.items = {}
        for code, description, unit, category, sort_order in CATALOGUE:
            self.items[code] = BOQItemMaster.objects.create(
                code=code, description=description, unit=unit, category=category,
                project_type='OPEX', sort_order=sort_order, is_active=True)
        # Migration 0149 writes OPX-273 on a migrated database; with migrations off, here.
        self.accb_master, _ = BOQItemMaster.objects.get_or_create(
            code=ACCB_CONFIGURED_CODE,
            defaults={'description': 'ACCB (configured)', 'unit': 'Nos', 'category': 'ACCB',
                      'project_type': 'OPEX', 'sort_order': 273})

        self.designer = _profile('fxdes', 'Design')
        self.qc       = _profile('fxqc', 'Design', is_design_qc=True)
        self.head     = _profile('fxhead', 'Design', is_design_head=True)
        self.pm       = _profile('fxpm', 'PM')

        self.program = Program.objects.create(
            name='Test-FX', program_type='OPEX', client_name='FXClient', status='Active',
            short_tender_code='FX')
        self.site, self.assignment = self._opex_site('FX-A')

    # ── fixtures ────────────────────────────────────────────────────────────

    def _opex_site(self, code, status=DESIGN_IN_DESIGN):
        site = Project(
            project_id=code, customer_name='FXClient', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code=code, dc_capacity_kw=D('100.00'),
            status='Draft', assigned_design=self.designer, assigned_pm=self.pm)
        site.save()
        assignment = DesignAssignment.objects.create(
            project=site, status=status, assigned_to=self.designer,
            survey_file_bucket='b', survey_file_path=f'{code}/survey/x.pdf')
        return site, assignment

    def _review_site(self, code='FX-QC'):
        """A site in Design QC with the BOQ stamped: the reviewer's correction window."""
        site, assignment = self._opex_site(code, status=DESIGN_IN_QC)
        BOQ.objects.create(project=site)
        DesignAttempt.objects.create(
            assignment=assignment, attempt_number=1, opened_reason=ATTEMPT_REASON_INITIAL,
            boq_submitted_at=timezone.now(), boq_submitted_by=self.designer)
        assignment.current_attempt_number = 1
        assignment.save(update_fields=['current_attempt_number'])
        return site

    def _boq(self, site=None):
        boq, _ = BOQ.objects.get_or_create(project=site or self.site)
        return boq

    def _line(self, code, quantity, site=None):
        master = self.items.get(code) or BOQItemMaster.objects.get(code=code)
        return BOQItem.objects.create(
            boq=self._boq(site), item_master=master, serial_no=master.sort_order,
            category=master.category, description=master.description, uom=master.unit,
            boq_quantity=None if quantity is None else D(str(quantity)),
            is_standard_item=True)

    def _lines(self, code, site=None):
        return list(BOQItem.objects.filter(boq__project=site or self.site,
                                           item_master__code=code).order_by('pk'))

    def _qty(self, code, site=None):
        """The quantity of the one line on `code`, or 'absent'."""
        lines = self._lines(code, site)
        self.assertLessEqual(len(lines), 1, f'{code} is on the BOQ more than once')
        return lines[0].boq_quantity if lines else 'absent'

    def _group(self, name, label, codes):
        group = BOQItemGroup(name=name, label=label)
        group.save()
        for code in codes:
            BOQItemGroupMember(group=group, item=self.items[code]).save()
        return group

    def _rule(self, target, summary, drivers, **fields):
        rule = BOQFormulaRule(target=self.items[target], summary=summary, **fields)
        rule.save()
        for driver in drivers:
            BOQFormulaDriver(rule=rule, **driver).save()
        return rule

    def _rules(self):
        """R1-, R13- and R14-shaped rules on the FX items.

            FX-EARTH = 5 m x (all lightning arresters)          group
            FX-LOG   = all inverters / 10, up, at least 1       group
            FX-RS485 = 5 m x FX-LOG                             chained
        """
        arresters = self._group('fx arresters', 'lightning arresters', ['FX-LA1', 'FX-LA2'])
        inverters = self._group('fx inverters', 'inverters', ['FX-INV1', 'FX-INV2'])
        self.earth_rule = self._rule('FX-EARTH', '5 m per lightning arrester',
                                     [{'group': arresters}], multiplier=5)
        self.logger_rule = self._rule('FX-LOG', '1 data logger per 10 inverters',
                                      [{'group': inverters}], divisor=10,
                                      floor_one_when_driven=True)
        self.rs485_rule = self._rule('FX-RS485', '5 m RS485 per data logger',
                                     [{'item': self.items['FX-LOG'], 'label': 'data loggers'}],
                                     multiplier=5)

    def _login(self, profile):
        self.assertTrue(self.client.login(username=profile.user.username, password='x'))

    # ── the four paths ──────────────────────────────────────────────────────

    def _picker(self, quantities, site=None, extra=None):
        """Save the picker with exactly these {code: qty} rows (absence = removal)."""
        data = {'action': 'save_draft', 'item': []}
        for code, quantity in quantities.items():
            pk = self.items[code].pk
            data['item'].append(str(pk))
            data[f'qty_{pk}'] = '' if quantity is None else str(quantity)
        data.update(extra or {})
        return self.client.post(reverse('opex_boq_entry', kwargs={
            'project_id': (site or self.site).project_id}), data, follow=True)

    def _file(self, rows):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = BOQ_DOWNLOAD_SHEET
        ws.append([header for header, _key in BOQ_DOWNLOAD_COLUMNS])
        for code, quantity in rows:
            ws.append([code, '', '', '', quantity])
        out = BytesIO()
        wb.save(out)
        return SimpleUploadedFile('boq.xlsx', out.getvalue())

    def _upload_rows_json(self, rows):
        url = reverse('opex_boq_upload', kwargs={'project_id': self.site.project_id})
        preview = self.client.post(url, {'phase': 'preview', 'file': self._file(rows)})
        self.assertEqual(preview.context['stage'], 'preview')
        return url, preview.context['rows_json']

    def _upload(self, rows):
        url, rows_json = self._upload_rows_json(rows)
        return self.client.post(url, {'phase': 'commit', 'rows_json': rows_json})

    def _correct(self, site, action, **data):
        return self.client.post(reverse('boq_correct', kwargs={'project_id': site.project_id}),
                                {'action': action, **data}, follow=True)

    def _override(self, action, line, site=None, **data):
        return self.client.post(
            reverse('opex_boq_override', kwargs={'project_id': (site or self.site).project_id}),
            {'action': action, 'item_id': str(line.pk), **data}, follow=True)

    def _messages(self, response):
        return [str(m) for m in response.context['messages']]


# ===========================================================================
# No rules: every hooked path behaves exactly as before, for one extra query
# ===========================================================================

class NoRulesTests(FormulaBase):

    def setUp(self):
        super().setUp()
        self.review = self._review_site()
        self._line('FX-PANEL', 10, site=self.review)

    def _count(self, call):
        call()   # warm the session and the content-type cache
        with CaptureQueriesContext(connection) as ctx:
            call()
        return len(ctx)

    def test_picker_save_is_unchanged(self):
        self._login(self.designer)
        self._picker({'FX-LA1': 2, 'FX-EARTH': 7, 'FX-LOG': ''})
        self.assertEqual((self._qty('FX-LA1'), self._qty('FX-EARTH'), self._qty('FX-LOG')),
                         (D(2), D(7), None))
        self.assertFalse(BOQRecalculation.objects.exists())

    def test_upload_is_unchanged(self):
        self._login(self.designer)
        response = self._upload([('FX-LA1', 3), ('FX-EARTH', 11)])
        self.assertEqual(response.context['stage'], 'result')
        self.assertEqual((self._qty('FX-LA1'), self._qty('FX-EARTH')), (D(3), D(11)))
        self.assertEqual((response.context['added'], response.context['removed'],
                          response.context['replaced']), (2, 0, []))
        self.assertFalse(BOQRecalculation.objects.exists())

    def test_reviewer_paths_are_unchanged(self):
        self._login(self.qc)
        self._correct(self.review, 'add_line', master_id=str(self.items['FX-EARTH'].pk),
                      quantity='9')
        line = self._lines('FX-PANEL', self.review)[0]
        self._correct(self.review, 'set_quantity', item_id=str(line.pk), quantity='12')
        self.assertEqual((self._qty('FX-EARTH', self.review), self._qty('FX-PANEL', self.review)),
                         (D(9), D(12)))
        self.assertEqual(BOQCorrection.objects.count(), 2)
        self.assertFalse(BOQRecalculation.objects.exists())

    # -- query counts: the baseline plus exactly the exists() gate --------------

    def test_picker_query_count(self):
        self._login(self.designer)
        n = self._count(lambda: self._picker({'FX-LA1': 2, 'FX-EARTH': 7}))
        self.assertEqual(n, BASELINE['picker'] + 1)

    def test_upload_query_count(self):
        self._login(self.designer)
        url, rows_json = self._upload_rows_json([('FX-LA1', 3), ('FX-EARTH', 11)])
        n = self._count(lambda: self.client.post(url, {'phase': 'commit',
                                                        'rows_json': rows_json}))
        self.assertEqual(n, BASELINE['upload'] + 1)

    def test_add_line_query_count(self):
        self._login(self.qc)
        codes = iter(['FX-EARTH', 'FX-LOG'])
        n = self._count(lambda: self._correct(
            self.review, 'add_line', master_id=str(self.items[next(codes)].pk), quantity='9'))
        self.assertEqual(n, BASELINE['add_line'] + 1)

    def test_set_quantity_query_count(self):
        self._login(self.qc)
        line = self._lines('FX-PANEL', self.review)[0]
        quantities = iter(['12', '13'])
        n = self._count(lambda: self._correct(self.review, 'set_quantity',
                                              item_id=str(line.pk), quantity=next(quantities)))
        self.assertEqual(n, BASELINE['set_quantity'] + 1)

    def test_no_rules_takes_no_lock_and_loads_nothing(self):
        self._login(self.designer)
        with mock.patch.object(boq_formulas, 'lock_boq', wraps=boq_formulas.lock_boq) as lock, \
                mock.patch.object(boq_formulas, 'load_rules',
                                  wraps=boq_formulas.load_rules) as load:
            self._picker({'FX-LA1': 2})
        self.assertEqual((lock.call_count, load.call_count), (0, 0))


# ===========================================================================
# With rules: the picker
# ===========================================================================

class PickerWithRulesTests(FormulaBase):

    def setUp(self):
        super().setUp()
        self._rules()
        self._login(self.designer)

    def test_a_save_adds_the_computed_line_with_the_right_quantity(self):
        self._picker({'FX-LA1': 2, 'FX-LA2': 1})
        line = self._lines('FX-EARTH')[0]
        self.assertEqual(line.boq_quantity, D(15))
        # Z1: the catalogue's current wording, unit and category.
        master = self.items['FX-EARTH']
        self.assertEqual((line.description, line.uom, line.category, line.serial_no),
                         (master.description, master.unit, master.category,
                          master.sort_order))
        record = BOQRecalculation.objects.get()
        self.assertEqual((record.line, record.quantity_before, record.quantity_after,
                          record.source, record.rule, record.actor, record.item_code,
                          record.item_unit),
                         (line, None, D(15), FORMULA_SOURCE_PICKER, self.earth_rule,
                          self.designer, 'FX-EARTH', 'Meter'))
        self.assertIn('2 FX-LA1 + 1 FX-LA2', record.explanation)

    def test_a_changed_driver_updates_the_line_with_one_record(self):
        self._picker({'FX-LA1': 2, 'FX-LA2': 1})
        earth = self._lines('FX-EARTH')[0]
        response = self._picker({'FX-LA1': 4, 'FX-LA2': 1, 'FX-EARTH': None})
        self.assertEqual(self._qty('FX-EARTH'), D(25))
        records = list(BOQRecalculation.objects.filter(line=earth).order_by('pk'))
        self.assertEqual([(r.quantity_before, r.quantity_after, r.source) for r in records],
                         [(None, D(15), FORMULA_SOURCE_PICKER),
                          (D(15), D(25), FORMULA_SOURCE_PICKER)])
        self.assertIn('Calculated lines updated: FX-EARTH 15 → 25.', self._messages(response))

    def test_an_unchanged_driver_writes_no_record(self):
        self._picker({'FX-LA1': 2, 'FX-LA2': 1})
        self._picker({'FX-LA1': 2, 'FX-LA2': 1, 'FX-EARTH': 15, 'FX-PANEL': 40})
        self.assertEqual(BOQRecalculation.objects.count(), 1)

    def test_a_chain_two_deep_updates_in_order(self):
        self._picker({'FX-INV1': 8, 'FX-INV2': 4})        # 12 inverters
        self.assertEqual((self._qty('FX-LOG'), self._qty('FX-RS485')), (D(2), D(10)))
        self._picker({'FX-INV1': 8, 'FX-INV2': 17, 'FX-LOG': None, 'FX-RS485': None})
        self.assertEqual((self._qty('FX-LOG'), self._qty('FX-RS485')), (D(3), D(15)))
        last = list(BOQRecalculation.objects.order_by('-pk')[:2])[::-1]
        self.assertEqual([(r.item_code, r.quantity_before, r.quantity_after) for r in last],
                         [('FX-LOG', D(2), D(3)), ('FX-RS485', D(10), D(15))])

    def test_a_posted_quantity_for_a_computed_line_is_ignored(self):
        self._picker({'FX-LA1': 2})
        self._picker({'FX-LA1': 2, 'FX-EARTH': 999})
        self.assertEqual(self._qty('FX-EARTH'), D(10))
        self.assertEqual(BOQRecalculation.objects.count(), 1)

    def test_the_picker_never_deletes_a_computed_line(self):
        self._picker({'FX-LA1': 2})
        earth = self._lines('FX-EARTH')[0]
        self._picker({'FX-LA1': 2})           # FX-EARTH absent from the POST
        self.assertEqual(self._lines('FX-EARTH'), [earth])
        self.assertEqual(BOQRecalculation.objects.count(), 1)

    def test_a_computed_item_posted_but_not_on_the_boq_is_not_created(self):
        # No arresters, so the rule gives 0: the posted line is neither created nor kept.
        self._picker({'FX-EARTH': 12, 'FX-PANEL': 5})
        self.assertEqual(self._qty('FX-EARTH'), 'absent')
        self.assertFalse(BOQRecalculation.objects.exists())

    def test_two_lines_on_a_computed_item_are_skipped_and_reported(self):
        self._line('FX-EARTH', 3)
        self._line('FX-EARTH', 4)
        response = self._picker({'FX-LA1': 2})
        self.assertEqual([l.boq_quantity for l in self._lines('FX-EARTH')], [D(3), D(4)])
        self.assertFalse(BOQRecalculation.objects.filter(item_code='FX-EARTH').exists())
        self.assertIn('FX-EARTH is on this BOQ more than once, so its calculated quantity '
                      'was not written. Remove the extra line.', self._messages(response))

    def test_a_residential_rule_never_touches_a_resco_boq(self):
        res = BOQItemMaster.objects.create(code='FX-RES', description='Res item', unit='Nos',
                                           category='BOS', project_type='Residential',
                                           sort_order=9100)
        BOQFormulaRule(target=res, summary='fixed', constant=2).save()
        self._picker({'FX-PANEL': 5})
        self.assertFalse(BOQItem.objects.filter(item_master=res).exists())
        self.assertEqual(len(load_rules('OPEX').rules), 3)
        self.assertEqual(len(load_rules().rules), 4)


# ===========================================================================
# D1 — a computed line that works out to 0
# ===========================================================================

class ZeroResultTests(FormulaBase):

    def setUp(self):
        super().setUp()
        self._rules()
        self._login(self.designer)

    def test_a_driver_dropping_to_0_removes_the_computed_line(self):
        self._picker({'FX-LA1': 2})
        earth = self._lines('FX-EARTH')[0]
        response = self._picker({'FX-LA1': 0})
        self.assertEqual(self._qty('FX-EARTH'), 'absent')
        record = BOQRecalculation.objects.order_by('-pk').first()
        self.assertEqual((record.line, record.quantity_before, record.quantity_after,
                          record.item_code, record.item_description),
                         (None, D(10), None, 'FX-EARTH', earth.description))
        self.assertIn('Calculated lines removed because they now work out to 0: FX-EARTH.',
                      self._messages(response))

    def test_no_line_is_added_at_0(self):
        self._picker({'FX-LA1': 0, 'FX-PANEL': 3})
        self.assertEqual(self._qty('FX-EARTH'), 'absent')
        self.assertFalse(BOQRecalculation.objects.exists())

    def test_a_line_with_an_ordered_quantity_is_kept_at_0(self):
        self._picker({'FX-LA1': 2})
        BOQItem.objects.filter(item_master__code='FX-EARTH').update(ordered_quantity=D(10))
        self._picker({'FX-LA1': 0})
        self.assertEqual(self._qty('FX-EARTH'), D(0))
        record = BOQRecalculation.objects.order_by('-pk').first()
        self.assertEqual((record.quantity_before, record.quantity_after), (D(10), D(0)))

    def test_the_reviewer_path_sets_0_and_never_deletes(self):
        review = self._review_site()
        la = self._line('FX-LA1', 2, site=review)
        self._line('FX-EARTH', 10, site=review)
        self._login(self.qc)
        self._correct(review, 'set_quantity', item_id=str(la.pk), quantity='0')
        self.assertEqual(self._qty('FX-EARTH', review), D(0))
        record = BOQRecalculation.objects.get()
        self.assertEqual((record.quantity_before, record.quantity_after, record.source),
                         (D(10), D(0), FORMULA_SOURCE_CORRECTION))


# ===========================================================================
# The designer's override
# ===========================================================================

class OverrideTests(FormulaBase):

    def setUp(self):
        super().setUp()
        self._rules()
        self._login(self.designer)
        self._picker({'FX-INV1': 12})          # FX-LOG 2, FX-RS485 10
        self.logger = self._lines('FX-LOG')[0]

    def test_a_blank_note_is_refused_and_nothing_is_saved(self):
        response = self._override('set', self.logger, quantity='4', note='   ')
        self.assertIn('An override needs a note saying why the calculated figure is wrong '
                      'for this site.', self._messages(response))
        self.assertEqual(self._qty('FX-LOG'), D(2))
        self.assertFalse(BOQLineOverride.objects.exists())

    def test_a_missing_quantity_is_refused(self):
        self._override('set', self.logger, quantity='', note='site survey')
        self.assertFalse(BOQLineOverride.objects.exists())

    def test_set_writes_the_quantity_the_note_and_a_log_entry(self):
        self._override('set', self.logger, quantity='4', note='two roofs, two loggers each')
        override = BOQLineOverride.objects.get()
        self.assertEqual((override.line, override.note, override.set_by),
                         (self.logger, 'two roofs, two loggers each', self.designer))
        self.assertEqual(self._qty('FX-LOG'), D(4))
        log = ActivityLog.objects.get(action_code='boq_formula_override_set')
        self.assertEqual(log.action, 'Fxdes overrode FX-LOG on FX-A: 2 → 4 '
                                     '(two roofs, two loggers each)')
        self.assertEqual((log.entity_type, log.entity_id), ('BOQItem', self.logger.pk))

    def test_later_rules_read_the_overridden_value(self):
        self._override('set', self.logger, quantity='4', note='two roofs')
        self.assertEqual(self._qty('FX-RS485'), D(20))
        record = BOQRecalculation.objects.order_by('-pk').first()
        self.assertEqual((record.item_code, record.quantity_before, record.quantity_after),
                         ('FX-RS485', D(10), D(20)))

    def test_an_overridden_line_is_untouched_by_later_saves(self):
        self._override('set', self.logger, quantity='4', note='two roofs')
        before = BOQRecalculation.objects.count()
        self._picker({'FX-INV1': 55, 'FX-LOG': 9})     # the rule would give 6; 9 is posted
        self.assertEqual((self._qty('FX-LOG'), self._qty('FX-RS485')), (D(4), D(20)))
        self.assertEqual(BOQRecalculation.objects.count(), before)
        self.assertTrue(BOQLineOverride.objects.filter(line=self.logger).exists())

    def test_clearing_recalculates_in_the_same_request(self):
        self._override('set', self.logger, quantity='4', note='two roofs')
        self._picker({'FX-INV1': 55})
        response = self._override('clear', self.logger)
        self.assertFalse(BOQLineOverride.objects.exists())
        self.assertEqual((self._qty('FX-LOG'), self._qty('FX-RS485')), (D(6), D(30)))
        record = BOQRecalculation.objects.filter(
            item_code='FX-LOG', source=FORMULA_SOURCE_OVERRIDE_CLEARED).get()
        self.assertEqual((record.quantity_before, record.quantity_after, record.source),
                         (D(4), D(6), FORMULA_SOURCE_OVERRIDE_CLEARED))
        self.assertIn('Override on FX-LOG cleared; it is calculated again (6).',
                      self._messages(response))
        log = ActivityLog.objects.get(action_code='boq_formula_override_cleared')
        self.assertEqual(log.action, 'Fxdes cleared the override on FX-LOG on FX-A '
                                     '(calculated: 6)')

    def test_a_reviewer_cannot_set_one(self):
        self._login(self.qc)
        response = self._override('set', self.logger, quantity='4', note='x')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(BOQLineOverride.objects.exists())

    def test_a_line_that_is_not_computed_cannot_be_overridden(self):
        inverter = self._lines('FX-INV1')[0]
        response = self._override('set', inverter, quantity='4', note='x')
        self.assertIn('FX-INV1 is not a calculated line, so there is nothing to override. '
                      'Change its quantity on the BOQ.', self._messages(response))
        self.assertFalse(BOQLineOverride.objects.exists())

    def test_a_design_locked_boq_refuses_an_override(self):
        DesignAttempt.objects.create(
            assignment=self.assignment, attempt_number=1, opened_reason=ATTEMPT_REASON_INITIAL,
            boq_submitted_at=timezone.now(), boq_submitted_by=self.designer)
        self.assignment.current_attempt_number = 1
        self.assignment.save(update_fields=['current_attempt_number'])
        self._override('set', self.logger, quantity='4', note='x')
        self.assertFalse(BOQLineOverride.objects.exists())
        self.assertEqual(self._qty('FX-LOG'), D(2))


# ===========================================================================
# D2 and Z2 — the reviewer
# ===========================================================================

class ReviewerTests(FormulaBase):

    def setUp(self):
        super().setUp()
        self._rules()
        self.review = self._review_site()
        self.la = self._line('FX-LA1', 2, site=self.review)
        self.earth = self._line('FX-EARTH', 10, site=self.review)
        self._login(self.qc)

    def test_setting_a_computed_quantity_is_refused_and_nothing_is_saved(self):
        response = self._correct(self.review, 'set_quantity', item_id=str(self.earth.pk),
                                 quantity='14')
        self.assertIn(FORMULA_REVIEWER_MESSAGE, self._messages(response))
        self.assertEqual(self._qty('FX-EARTH', self.review), D(10))
        self.assertFalse(BOQCorrection.objects.exists())
        self.assertFalse(BOQRecalculation.objects.exists())

    def test_adding_a_computed_item_by_hand_is_refused(self):
        response = self._correct(self.review, 'add_line',
                                 master_id=str(self.items['FX-LOG'].pk), quantity='1')
        self.assertIn(FORMULA_REVIEWER_MESSAGE, self._messages(response))
        self.assertEqual(self._qty('FX-LOG', self.review), 'absent')
        self.assertFalse(BOQCorrection.objects.exists())

    def test_a_driver_correction_writes_one_correction_and_linked_recalculations(self):
        since = timezone.now()
        self._correct(self.review, 'set_quantity', item_id=str(self.la.pk), quantity='3')
        self._correct(self.review, 'add_line', master_id=str(self.items['FX-INV1'].pk),
                      quantity='12')
        self.assertEqual((self._qty('FX-EARTH', self.review), self._qty('FX-LOG', self.review),
                          self._qty('FX-RS485', self.review)), (D(15), D(2), D(10)))
        corrections = list(BOQCorrection.objects.order_by('pk'))
        self.assertEqual([c.item_code for c in corrections], ['FX-LA1', 'FX-INV1'])
        recalcs = list(BOQRecalculation.objects.order_by('pk'))
        self.assertEqual([(r.item_code, r.driving_correction_id, r.actor) for r in recalcs],
                         [('FX-EARTH', corrections[0].pk, self.qc),
                          ('FX-LOG', corrections[1].pk, self.qc),
                          ('FX-RS485', corrections[1].pk, self.qc)])
        # The two readers that decide or count: one row per reviewer edit, nothing more.
        boq = self.review.boq
        self.assertEqual(reviewer_corrections().filter(boq=boq).count(), 2)
        self.assertEqual(len(_uncited_corrections_since(self.review, since)), 2)


# ===========================================================================
# D3 — the upload
# ===========================================================================

class UploadTests(FormulaBase):

    def setUp(self):
        super().setUp()
        self._rules()
        self._login(self.designer)

    def test_a_computed_figure_in_the_file_is_replaced_and_listed(self):
        response = self._upload([('FX-LA1', 2), ('FX-LA2', 1), ('FX-EARTH', 12)])
        self.assertEqual(self._qty('FX-EARTH'), D(15))
        self.assertEqual(response.context['replaced'],
                         [{'code': 'FX-EARTH', 'file': D(12), 'calculated': D(15)}])
        self.assertEqual(response.context['added'], 2)
        self.assertContains(response, 'id="boqUploadReplaced"')
        record = BOQRecalculation.objects.get()
        self.assertEqual((record.quantity_before, record.quantity_after, record.source),
                         (None, D(15), FORMULA_SOURCE_UPLOAD))
        log = ActivityLog.objects.get(action_code='boq_uploaded')
        self.assertEqual(log.action, 'BOQ spreadsheet uploaded — 2 row(s) added, 0 '
                                     'quantity(ies) changed, 0 removed, 1 calculated line(s) '
                                     'replaced')

    def test_an_override_survives_an_upload(self):
        self._picker({'FX-INV1': 12})
        logger = self._lines('FX-LOG')[0]
        self._override('set', logger, quantity='4', note='two roofs')
        response = self._upload([('FX-INV1', 30), ('FX-LOG', 1)])
        self.assertEqual(self._qty('FX-LOG'), D(4))
        self.assertTrue(BOQLineOverride.objects.filter(line=logger).exists())
        self.assertEqual(response.context['kept_override'],
                         [{'code': 'FX-LOG', 'file': D(1), 'calculated': D(4)}])
        self.assertEqual(response.context['replaced'], [])

    def test_a_removal_is_counted_in_the_activity_text(self):
        self._picker({'FX-LA1': 2})
        response = self._upload([('FX-LA1', 0)])
        self.assertEqual(self._qty('FX-EARTH'), 'absent')
        self.assertEqual(response.context['removed'], 1)
        self.assertContains(response, '<strong>1 removed</strong>')
        log = ActivityLog.objects.get(action_code='boq_uploaded')
        self.assertEqual(log.action, 'BOQ spreadsheet uploaded — 0 row(s) added, 1 '
                                     'quantity(ies) changed, 1 removed')


# ===========================================================================
# Not hooked: the ACCB endpoint and fix_tin_shade_ballast
# ===========================================================================

class NotHookedACCBTests(FormulaBase):

    def test_the_accb_endpoint_writes_no_record_and_moves_no_computed_line(self):
        self._rules()
        # Arresters written straight to the database: the computed line is now stale.
        self._line('FX-LA1', 2)
        self._login(self.designer)
        self.client.post(reverse('opex_boq_accb', kwargs={'project_id': self.site.project_id}),
                         {'action': 'add', 'accb_type': 'Type-1',
                          'in_breaker': ['MCCB'], 'in_rating': ['100'],
                          'out_breaker': ['MCCB'], 'out_rating': ['100']})
        self.assertEqual(BOQItem.objects.filter(item_master=self.accb_master).count(), 1)
        self.assertEqual(self._qty('FX-EARTH'), 'absent')
        self.assertFalse(BOQRecalculation.objects.exists())


class NotHookedTinShadeTests(tsb.TsbBase):

    def test_apply_counts_writes_no_record_and_moves_no_computed_line(self):
        # INC work Tin shade computed from the Tin Shade count (R24's shape).
        BOQFormulaRule(target=self.inc, summary='0.64 kWp per tin shade', multiplier='0.64',
                       rounding='none').save()
        BOQFormulaDriver(rule=BOQFormulaRule.objects.get(), item=self.tin).save()
        path = self.filled({self.line_a.pk: '12'})
        self.run_cmd('--apply-counts', path, '--apply')
        self.line_a.refresh_from_db()
        self.line_inc.refresh_from_db()
        self.assertEqual((self.line_a.boq_quantity, self.line_inc.boq_quantity),
                         (D('12'), D('186.88')))
        self.assertFalse(BOQRecalculation.objects.exists())


# ===========================================================================
# The BOQ row lock (Q2)
# ===========================================================================

class LockTests(FormulaBase):

    def test_the_lock_is_taken_with_rules_and_not_without(self):
        self._login(self.designer)
        with mock.patch.object(boq_formulas, 'lock_boq', wraps=boq_formulas.lock_boq) as lock:
            self._picker({'FX-LA1': 2})
            self.assertEqual(lock.call_count, 0)
            self._rules()
            self._picker({'FX-LA1': 3})
            self.assertEqual(lock.call_count, 1)
        self.assertEqual(lock.call_args.args[0], self.site.boq)


class PostgresLockTests(FormulaBase):
    """The SQL itself. Django drops FOR UPDATE on SQLite, so this runs on Postgres only:
    `manage.py test projects.tests_boq_formula_apply.PostgresLockTests` (real settings)."""

    def setUp(self):
        if not connection.features.has_select_for_update:
            self.skipTest('the database has no row locks (SQLite)')
        super().setUp()

    def _for_update(self, quantities):
        with CaptureQueriesContext(connection) as ctx:
            self._picker(quantities)
        return [q['sql'] for q in ctx.captured_queries
                if 'FOR UPDATE' in q['sql'] and 'projects_boq' in q['sql']]

    def test_for_update_appears_with_rules_and_not_without(self):
        self._login(self.designer)
        self.assertEqual(self._for_update({'FX-LA1': 2}), [])
        self._rules()
        locked = self._for_update({'FX-LA1': 3})
        self.assertEqual(len(locked), 1)
        self.assertIn('"projects_boq"', locked[0])


# ===========================================================================
# 0153's constraints, and the seed command's guard
# ===========================================================================

class ConstraintTests(FormulaBase):

    def setUp(self):
        super().setUp()
        self._rules()

    def test_a_duplicate_item_driver_is_refused_by_the_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            BOQFormulaDriver.objects.bulk_create([
                BOQFormulaDriver(rule=self.rs485_rule, item=self.items['FX-LOG'])])

    def test_a_duplicate_group_driver_is_refused_by_the_database(self):
        group = BOQItemGroup.objects.get(name='fx arresters')
        with self.assertRaises(IntegrityError), transaction.atomic():
            BOQFormulaDriver.objects.bulk_create([
                BOQFormulaDriver(rule=self.earth_rule, group=group)])

    def test_save_refuses_a_duplicate_before_the_database_does(self):
        with self.assertRaises(ValidationError):
            BOQFormulaDriver(rule=self.rs485_rule, item=self.items['FX-LOG']).save()

    def test_the_same_item_on_two_rules_is_allowed(self):
        BOQFormulaDriver.objects.bulk_create([
            BOQFormulaDriver(rule=self.earth_rule, item=self.items['FX-PANEL'])])
        BOQFormulaDriver.objects.bulk_create([
            BOQFormulaDriver(rule=self.logger_rule, item=self.items['FX-PANEL'])])

    def test_a_blank_override_note_is_refused_by_the_database(self):
        line = self._line('FX-EARTH', 5)
        with self.assertRaises(IntegrityError), transaction.atomic():
            BOQLineOverride.objects.create(line=line, note='')


class SeedCommandTests(TestCase):

    @override_settings(DEBUG=False)
    def test_it_refuses_when_debug_is_off(self):
        with self.assertRaisesMessage(CommandError, 'runs only with DEBUG on'):
            call_command('seed_formula_demo', '--site', 'X', stdout=StringIO())
        self.assertFalse(BOQFormulaRule.objects.exists())

    @override_settings(DEBUG=True)
    def test_it_refuses_off_a_local_database(self):
        with mock.patch('projects.management.commands.seed_formula_demo.database_is_local',
                        return_value=False):
            with self.assertRaisesMessage(CommandError, 'runs only against a local database'):
                call_command('seed_formula_demo', '--remove', stdout=StringIO())
