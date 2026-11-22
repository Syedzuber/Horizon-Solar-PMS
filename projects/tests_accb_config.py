"""
B-3a verification — configured ACCBs: one configured BOQ line per ACCB.

WHAT THIS FILE PROVES
---------------------
  * The wording. accb_description() reproduces the two approved examples word for word,
    and each rule behind them (1 vs N feeders, "2×" repeats, MCCB before MCB, without SPD).
  * The grouping identity. accb_config_key() ignores feeder order and changes with any
    change to type, breaker, rating or SPD. The SCM aggregate depends on exactly that.
  * Validation is server-side and refuses with NOTHING saved.
  * The designer's add / edit / duplicate / remove each touch exactly one line and its
    configuration, Qty is always 1, and the BOQ's own write rule (author, no group lock,
    not submitted) applies.
  * EVERY OTHER WRITE PATH LEAVES ACCB LINES ALONE: the picker save that omits them, the
    spreadsheet upload, the reviewer's correction screen.
  * SCM keeps different configurations apart and changes no other item's total; the order
    raise lists ACCBs as orderable rows, one per configuration (B-3b; the raise itself is
    in tests_accb_order).
  * OPX-273 is locked on both catalogue screens and in the form.

RUNNER: like tests_boq_upload, this seeds catalogue codes in setUp, so it runs with
migrations disabled — `manage.py test projects --settings=solarpms.test_settings`. Migration
0149 creates OPX-273 on a migrated database; here setUp creates it.
"""
import json
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from .accb import (
    ACCB_CATALOGUE_LOCKED_MESSAGE, ACCB_CONFIGURED_CODE, ACCB_REVIEWER_MESSAGE,
    ACCB_UPLOAD_REFUSED_MESSAGE, accb_config_key, accb_description, accb_summary,
    validate_accb_config,
)
from .design_views import _boq_review_panel, aggregate_group_boq
from .forms import BOQItemMasterForm
from .models import (
    ACCBConfig, ActivityLog, BOQ, BOQCorrection, BOQItem, BOQItemMaster,
    DesignAssignment, DesignAttempt, Program, Project, SiteGroup, SiteGroupMembership,
    ATTEMPT_REASON_INITIAL, DESIGN_IN_DESIGN, DESIGN_IN_QC, SITE_GROUP_LOCKED,
    get_opex_boq_catalogue, get_opex_mandatory_items, opex_catalogue_category_order,
)
from .order_views import _parse_requirement, _picker_context, accb_amount_field
from .views import (
    BOQ_DOWNLOAD_ACCB_SHEET, BOQ_DOWNLOAD_COLUMNS, BOQ_DOWNLOAD_SHEET, _accb_create,
    accb_log_text,
)


def cfg(accb_type, spd, incomers, outgoings):
    """A configuration dict from (breaker, rating) pairs — the shape accb.py takes."""
    return {'type': accb_type, 'spd': spd,
            'in': [{'breaker': b, 'rating': r} for b, r in incomers],
            'out': [{'breaker': b, 'rating': r} for b, r in outgoings]}


#: The two approved examples (B-3 rulings), configuration and exact text.
EX_TYPE2 = cfg('Type-2', True, [('MCCB', 100), ('MCCB', 63)], [('MCCB', 150)])
EX_TYPE2_TEXT = ('ACCB Type-2, I/C Feeder 2 Nos. with MCCB 4P 415V 100A and 63A; '
                 'O/G Feeder 1 No. with MCCB 4P 415V 150A, with SPD')
EX_TYPE1 = cfg('Type-1', False,
               [('MCCB', 100), ('MCB', 32), ('MCB', 32), ('MCB', 25)], [('MCCB', 100)])
EX_TYPE1_TEXT = ('ACCB Type-1, I/C Feeder 4 Nos. with MCCB 4P 415V 100A + MCB 4P 415V '
                 '2×32A and 25A; O/G Feeder 1 No. with MCCB 4P 415V 100A, without SPD')


def _longest_config():
    """20 + 20 feeders, mixed breakers, all-different 4-digit ratings (pre-flight P2)."""
    return cfg('Type-1', False,
               [('MCCB', 1600 - i) for i in range(10)] + [('MCB', 1500 - i) for i in range(10)],
               [('MCCB', 1400 - i) for i in range(10)] + [('MCB', 1300 - i) for i in range(10)])


# ===========================================================================
# The pure rules — wording, key, summary, validation
# ===========================================================================

class DescriptionTests(SimpleTestCase):

    def test_approved_type2_example_word_for_word(self):
        self.assertEqual(accb_description(EX_TYPE2), EX_TYPE2_TEXT)

    def test_approved_type1_example_word_for_word(self):
        self.assertEqual(accb_description(EX_TYPE1), EX_TYPE1_TEXT)

    def test_one_feeder_is_1_No_and_several_are_N_Nos(self):
        text = accb_description(cfg('Type-1', True, [('MCCB', 63)],
                                    [('MCCB', 100), ('MCCB', 80), ('MCCB', 50)]))
        self.assertIn('I/C Feeder 1 No. with', text)
        self.assertIn('O/G Feeder 3 Nos. with', text)

    def test_repeats_are_written_as_N_times(self):
        text = accb_description(cfg('Type-1', True,
                                    [('MCB', 32), ('MCB', 32), ('MCB', 32), ('MCB', 16)],
                                    [('MCCB', 100)]))
        self.assertIn('MCB 4P 415V 3×32A and 16A', text)

    def test_mccb_group_comes_before_mcb_whatever_the_entry_order(self):
        text = accb_description(cfg('Type-2', False, [('MCB', 25), ('MCCB', 125)],
                                    [('MCCB', 160)]))
        self.assertIn('with MCCB 4P 415V 125A + MCB 4P 415V 25A;', text)

    def test_ratings_within_a_group_run_high_to_low(self):
        text = accb_description(cfg('Type-2', False,
                                    [('MCCB', 63), ('MCCB', 250), ('MCCB', 100)],
                                    [('MCCB', 400)]))
        self.assertIn('MCCB 4P 415V 250A, 100A and 63A;', text)

    def test_without_spd(self):
        self.assertTrue(accb_description(EX_TYPE1).endswith(', without SPD'))
        self.assertTrue(accb_description(EX_TYPE2).endswith(', with SPD'))

    def test_the_longest_description_is_416_characters(self):
        # Pre-flight P2: fits BOQItem.description (unbounded text).
        self.assertEqual(len(accb_description(_longest_config())), 416)


class ConfigKeyTests(SimpleTestCase):

    def test_feeder_order_does_not_change_the_key(self):
        shuffled = cfg('Type-1', False,
                       [('MCB', 25), ('MCB', 32), ('MCCB', 100), ('MCB', 32)], [('MCCB', 100)])
        self.assertEqual(accb_config_key(shuffled), accb_config_key(EX_TYPE1))

    def test_same_key_means_same_description(self):
        shuffled = cfg('Type-1', False,
                       [('MCB', 32), ('MCB', 25), ('MCB', 32), ('MCCB', 100)], [('MCCB', 100)])
        self.assertEqual(accb_description(shuffled), accb_description(EX_TYPE1))

    def test_any_change_to_type_breaker_rating_or_spd_changes_the_key(self):
        base = accb_config_key(EX_TYPE2)
        variants = {
            'type':    cfg('Type-1', True, [('MCCB', 100), ('MCCB', 63)], [('MCCB', 150)]),
            'breaker': cfg('Type-2', True, [('MCCB', 100), ('MCB', 63)], [('MCCB', 150)]),
            'rating':  cfg('Type-2', True, [('MCCB', 100), ('MCCB', 80)], [('MCCB', 150)]),
            'spd':     cfg('Type-2', False, [('MCCB', 100), ('MCCB', 63)], [('MCCB', 150)]),
            'side':    cfg('Type-2', True, [('MCCB', 100)], [('MCCB', 150), ('MCCB', 63)]),
            'count':   cfg('Type-2', True, [('MCCB', 100), ('MCCB', 63), ('MCCB', 63)],
                           [('MCCB', 150)]),
        }
        for name, variant in variants.items():
            with self.subTest(change=name):
                self.assertNotEqual(accb_config_key(variant), base)

    def test_the_longest_key_fits_the_column(self):
        limit = ACCBConfig._meta.get_field('config_key').max_length
        self.assertLessEqual(len(accb_config_key(_longest_config())), limit)


class SummaryAndLogTests(SimpleTestCase):

    def test_summary_wording(self):
        self.assertEqual(accb_summary(EX_TYPE2), 'Type-2 · I/C 2 · O/G 1 · SPD')
        self.assertEqual(accb_summary(EX_TYPE1), 'Type-1 · I/C 4 · O/G 1 · no SPD')

    def test_the_longest_log_line_fits_in_255(self):
        """Q6: no truncation in normal use. Built from the worst case of every part: a
        301-character name, a 30-character site code (Project.project_id's max), the
        largest ACCB number the column holds, and the longest change detail."""
        user = SimpleNamespace(get_full_name=lambda: 'N' * 301, username='u')
        profile = SimpleNamespace(user=user)
        project = SimpleNamespace(project_id='S' * Project._meta.get_field('project_id').max_length)
        longest = accb_summary(_longest_config())
        detail = f'{longest} → {longest}'
        text = accb_log_text(profile, 'changed', 32767, project, detail)
        self.assertLessEqual(len(text), ActivityLog._meta.get_field('action').max_length)


class ValidationTests(SimpleTestCase):

    def _ok(self, **overrides):
        args = {'accb_type': 'Type-2', 'spd': '1',
                'in_breakers': ['MCCB'], 'in_ratings': ['100'],
                'out_breakers': ['MCCB'], 'out_ratings': ['150']}
        args.update(overrides)
        return validate_accb_config(args['accb_type'], args['spd'],
                                    args['in_breakers'], args['in_ratings'],
                                    args['out_breakers'], args['out_ratings'])

    def test_a_valid_configuration_is_normalised(self):
        self.assertEqual(self._ok(in_breakers=['mccb'], in_ratings=[' 100 ']),
                         cfg('Type-2', True, [('MCCB', 100)], [('MCCB', 150)]))

    def test_the_bounds_are_accepted(self):
        self.assertEqual(self._ok(in_ratings=['1'])['in'][0]['rating'], 1)
        self.assertEqual(self._ok(in_ratings=['1600'])['in'][0]['rating'], 1600)
        config = self._ok(in_breakers=['MCB'] * 20, in_ratings=['32'] * 20)
        self.assertEqual(len(config['in']), 20)

    def test_unticked_spd_is_false(self):
        self.assertFalse(self._ok(spd=None)['spd'])

    def test_each_rule_is_refused(self):
        cases = {
            'type':            {'accb_type': 'Type-3'},
            'blank type':      {'accb_type': ''},
            'no I/C feeder':   {'in_breakers': [], 'in_ratings': []},
            '21 O/G feeders':  {'out_breakers': ['MCB'] * 21, 'out_ratings': ['32'] * 21},
            'breaker':         {'in_breakers': ['RCCB']},
            'rating 0':        {'in_ratings': ['0']},
            'rating 1601':     {'in_ratings': ['1601']},
            'rating decimal':  {'in_ratings': ['12.5']},
            'rating text':     {'in_ratings': ['abc']},
            'rating blank':    {'in_ratings': ['']},
            'rating negative': {'in_ratings': ['-5']},
            'spd':             {'spd': 'maybe'},
            'mismatched':      {'in_breakers': ['MCCB', 'MCB'], 'in_ratings': ['100']},
        }
        for name, override in cases.items():
            with self.subTest(rule=name):
                with self.assertRaises(ValidationError):
                    self._ok(**override)

    def test_every_fault_is_named_at_once(self):
        with self.assertRaises(ValidationError) as caught:
            self._ok(accb_type='X', in_ratings=['0'], out_breakers=['RCCB'])
        self.assertEqual(len(caught.exception.messages), 3)


# ===========================================================================
# Shared fixtures for the database tests
# ===========================================================================

CATALOGUE = [
    # (code, description, unit, category, sort_order, is_mandatory)
    ('OPX-001', 'Solar PV Module', 'Nos', 'Module', 1, True),
    ('OPX-002', 'DCDB 5 In 5 Out', 'Nos', 'DCDB',   2, False),
    ('OPX-003', 'AC Cable 4 sqmm', 'Mtr', 'Cable',  3, False),
    ('OPX-039', 'ACDB Type-1 (For 12KW INV)', 'Nos', 'ACDB', 39, False),
    (ACCB_CONFIGURED_CODE, 'ACCB (configured)', 'Nos', 'ACCB', 273, False),
]


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


class ACCBBase(TestCase):

    def setUp(self):
        BOQItemMaster.objects.bulk_create([
            BOQItemMaster(code=code, description=description, unit=unit, category=category,
                          project_type='OPEX', sort_order=sort_order,
                          is_mandatory=is_mandatory, is_active=True)
            for code, description, unit, category, sort_order, is_mandatory in CATALOGUE
        ])
        self.accb_master = BOQItemMaster.objects.get(code=ACCB_CONFIGURED_CODE)

        self.designer = _profile('desacc',  'Design')
        self.other    = _profile('desacc2', 'Design')
        self.qc       = _profile('qcacc',   'Design', is_design_qc=True)
        self.head     = _profile('headacc', 'Design', is_design_head=True)
        self.pm       = _profile('pmacc',   'PM')
        self.admin    = _profile('admacc',  'Admin')

        self.program = Program.objects.create(
            name='Test-ACCB', program_type='OPEX', client_name='ACCBClient',
            status='Active', short_tender_code='AC')
        self.site, self.assignment = self._opex_site('AC-A')

    # ── fixtures ────────────────────────────────────────────────────────────

    def _opex_site(self, code, status=DESIGN_IN_DESIGN):
        site = Project(
            project_id=code, customer_name='ACCBClient', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code=code,
            dc_capacity_kw=Decimal('100.00'), status='Draft',
            assigned_design=self.designer, assigned_pm=self.pm)
        site.save()
        assignment = DesignAssignment.objects.create(
            project=site, status=status, assigned_to=self.designer,
            survey_file_bucket='b', survey_file_path=f'{code}/survey/x.pdf')
        return site, assignment

    def _master(self, code):
        return BOQItemMaster.objects.get(code=code)

    def _boq(self, site=None):
        boq, _ = BOQ.objects.get_or_create(project=site or self.site)
        return boq

    def _line(self, code, quantity, site=None):
        master = self._master(code)
        return BOQItem.objects.create(
            boq=self._boq(site), item_master=master, serial_no=master.sort_order,
            category=master.category, description=master.description, uom=master.unit,
            boq_quantity=Decimal(str(quantity)), is_standard_item=True)

    def _seed_accb(self, config, site=None):
        """An ACCB written by the view's own create helper."""
        return _accb_create(self._boq(site), self.accb_master, config, self.designer)

    def _design_lock(self, assignment=None):
        assignment = assignment or self.assignment
        DesignAttempt.objects.create(
            assignment=assignment, attempt_number=1,
            opened_reason=ATTEMPT_REASON_INITIAL, boq_submitted_at=timezone.now(),
            boq_submitted_by=assignment.assigned_to)
        assignment.current_attempt_number = 1
        assignment.save(update_fields=['current_attempt_number'])

    def _group_lock(self, site=None):
        group = SiteGroup.objects.create(program=self.program, name='G1',
                                         status=SITE_GROUP_LOCKED, created_by=self.pm)
        SiteGroupMembership.objects.create(group=group, project=site or self.site,
                                           added_by=self.pm)

    def _login(self, profile):
        self.assertTrue(self.client.login(username=profile.user.username, password='x'))

    def _accb_post(self, action, config=None, accb_id=None, site=None, extra=None):
        data = {'action': action}
        if accb_id is not None:
            data['accb_id'] = str(accb_id)
        if config is not None:
            data['accb_type'] = config['type']
            if config['spd']:
                data['has_spd'] = '1'
            for side in ('in', 'out'):
                data[f'{side}_breaker'] = [f['breaker'] for f in config[side]]
                data[f'{side}_rating'] = [str(f['rating']) for f in config[side]]
        data.update(extra or {})
        return self.client.post(
            reverse('opex_boq_accb', kwargs={'project_id': (site or self.site).project_id}),
            data)

    def _accb_lines(self, site=None):
        return list(BOQItem.objects.filter(boq__project=site or self.site,
                                           accb_config__isnull=False)
                    .select_related('accb_config').order_by('accb_config__number'))


# ===========================================================================
# The designer's ACCB section
# ===========================================================================

class DesignerACCBTests(ACCBBase):

    def setUp(self):
        super().setUp()
        self._login(self.designer)

    def test_add_creates_exactly_one_line_and_configuration(self):
        response = self._accb_post('add', EX_TYPE2)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].endswith('#accb'))
        lines = self._accb_lines()
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(line.description, EX_TYPE2_TEXT)
        self.assertEqual(line.uom, 'Nos')
        self.assertEqual(line.boq_quantity, Decimal('1'))
        self.assertEqual(line.item_master, self.accb_master)
        self.assertEqual(line.accb_config.number, 1)
        self.assertEqual(line.accb_config.config_key, accb_config_key(EX_TYPE2))
        self.assertEqual(line.accb_config.as_config(), EX_TYPE2)
        self.assertEqual(BOQItem.objects.filter(boq__project=self.site).count(), 1)
        log = ActivityLog.objects.get(action_code='boq_accb_added')
        self.assertEqual(log.action,
                         'Desacc added ACCB 1 on AC-A (Type-2 · I/C 2 · O/G 1 · SPD)')

    def test_both_approved_examples_become_two_lines_nos_qty_1(self):
        self._accb_post('add', EX_TYPE2)
        self._accb_post('add', EX_TYPE1)
        lines = self._accb_lines()
        self.assertEqual([l.description for l in lines], [EX_TYPE2_TEXT, EX_TYPE1_TEXT])
        self.assertEqual([l.accb_config.number for l in lines], [1, 2])
        self.assertTrue(all(l.uom == 'Nos' and l.boq_quantity == 1 for l in lines))

    def test_edit_updates_the_same_line(self):
        self._accb_post('add', EX_TYPE2)
        line = self._accb_lines()[0]
        changed = cfg('Type-2', True, [('MCCB', 125), ('MCCB', 63)], [('MCCB', 150)])
        self._accb_post('save', changed, accb_id=line.accb_config.pk)
        lines = self._accb_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].pk, line.pk)
        self.assertEqual(lines[0].description, accb_description(changed))
        self.assertEqual(lines[0].accb_config.config_key, accb_config_key(changed))
        self.assertEqual(lines[0].boq_quantity, Decimal('1'))
        log = ActivityLog.objects.get(action_code='boq_accb_changed')
        self.assertEqual(log.action, 'Desacc changed ACCB 1 on AC-A '
                                     '(Type-2 · I/C 2 · O/G 1 · SPD; '
                                     'feeders changed)')

    def test_an_edit_that_changes_the_counts_logs_old_and_new_summary(self):
        self._accb_post('add', EX_TYPE2)
        accb_id = self._accb_lines()[0].accb_config.pk
        self._accb_post('save', EX_TYPE1, accb_id=accb_id)
        log = ActivityLog.objects.get(action_code='boq_accb_changed')
        self.assertIn('(Type-2 · I/C 2 · O/G 1 · SPD → '
                      'Type-1 · I/C 4 · O/G 1 · no SPD)', log.action)

    def test_a_save_that_changes_nothing_logs_nothing(self):
        self._accb_post('add', EX_TYPE2)
        accb_id = self._accb_lines()[0].accb_config.pk
        self._accb_post('save', EX_TYPE2, accb_id=accb_id)
        self.assertFalse(ActivityLog.objects.filter(action_code='boq_accb_changed').exists())

    def test_duplicate_creates_one_new_line_with_the_next_number(self):
        self._accb_post('add', EX_TYPE2)
        self._accb_post('add', EX_TYPE1)
        first = self._accb_lines()[0]
        self._accb_post('duplicate', accb_id=first.accb_config.pk)
        lines = self._accb_lines()
        self.assertEqual(len(lines), 3)
        copy = lines[2]
        self.assertEqual(copy.accb_config.number, 3)
        self.assertNotEqual(copy.pk, first.pk)
        self.assertEqual(copy.description, EX_TYPE2_TEXT)
        self.assertEqual(copy.accb_config.as_config(), EX_TYPE2)
        self.assertEqual(copy.boq_quantity, Decimal('1'))

    def test_remove_deletes_the_line_and_its_configuration_and_numbers_stay(self):
        self._accb_post('add', EX_TYPE2)
        self._accb_post('add', EX_TYPE1)
        first = self._accb_lines()[0]
        self._accb_post('remove', accb_id=first.accb_config.pk)
        self.assertFalse(BOQItem.objects.filter(pk=first.pk).exists())
        self.assertFalse(ACCBConfig.objects.filter(pk=first.accb_config.pk).exists())
        self.assertEqual([l.accb_config.number for l in self._accb_lines()], [2])
        # Q5: numbers are kept; a new ACCB takes max+1, so the gap at 1 stays.
        self._accb_post('add', EX_TYPE2)
        self.assertEqual([l.accb_config.number for l in self._accb_lines()], [2, 3])
        self.assertTrue(ActivityLog.objects.filter(action_code='boq_accb_removed').exists())

    def test_quantity_is_always_1_whatever_is_posted(self):
        self._accb_post('add', EX_TYPE2, extra={'quantity': '5', 'boq_quantity': '5'})
        accb_id = self._accb_lines()[0].accb_config.pk
        self._accb_post('save', EX_TYPE1, accb_id=accb_id, extra={'quantity': '7'})
        self.assertEqual(self._accb_lines()[0].boq_quantity, Decimal('1'))

    def test_an_invalid_configuration_saves_nothing(self):
        bad = {
            'type':    dict(EX_TYPE2, type='Type-9'),
            'feeders': dict(EX_TYPE2, **{'in': []}),
            '21':      dict(EX_TYPE2, out=[{'breaker': 'MCB', 'rating': 32}] * 21),
            'breaker': dict(EX_TYPE2, **{'in': [{'breaker': 'RCCB', 'rating': 63}]}),
            'rating':  dict(EX_TYPE2, **{'in': [{'breaker': 'MCB', 'rating': 1601}]}),
            'decimal': dict(EX_TYPE2, **{'in': [{'breaker': 'MCB', 'rating': '12.5'}]}),
        }
        for name, config in bad.items():
            with self.subTest(rule=name):
                response = self._accb_post('add', config)
                self.assertEqual(response.status_code, 302)
                self.assertFalse(BOQ.objects.filter(project=self.site).exists())
                self.assertEqual(ACCBConfig.objects.count(), 0)
                self.assertEqual(BOQItem.objects.count(), 0)
        response = self._accb_post('add', EX_TYPE2, extra={'has_spd': 'maybe'})
        self.assertEqual(ACCBConfig.objects.count(), 0)

    def test_an_invalid_edit_leaves_the_saved_accb_unchanged(self):
        self._accb_post('add', EX_TYPE2)
        line = self._accb_lines()[0]
        self._accb_post('save', dict(EX_TYPE2, type='Type-9'), accb_id=line.accb_config.pk)
        line.refresh_from_db()
        self.assertEqual(line.description, EX_TYPE2_TEXT)
        self.assertEqual(ACCBConfig.objects.get().as_config(), EX_TYPE2)

    def test_an_accb_from_another_site_is_a_404(self):
        other_site, _ = self._opex_site('AC-B')
        theirs = self._seed_accb(EX_TYPE2, site=other_site)
        self._accb_post('add', EX_TYPE1)
        response = self._accb_post('remove', accb_id=theirs.pk)
        self.assertEqual(response.status_code, 404)
        self.assertTrue(ACCBConfig.objects.filter(pk=theirs.pk).exists())

    def test_a_residential_project_404s(self):
        house = Project(project_id='HOUSE1', customer_name='House',
                        customer_phone='9876543211', site_address='2 Sun Rd', city='Delhi',
                        project_type='Residential', dc_capacity_kw=Decimal('10.00'),
                        status='Draft', assigned_design=self.designer, assigned_pm=self.pm)
        house.save()
        self.assertEqual(self._accb_post('add', EX_TYPE2, site=house).status_code, 404)

    def test_the_page_draws_the_section_and_loads_alpine_once(self):
        self._seed_accb(EX_TYPE2)
        response = self.client.get(reverse('opex_boq_entry',
                                           kwargs={'project_id': self.site.project_id}))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertEqual(html.count('alpinejs@'), 1)
        self.assertIn('id="accbData"', html)
        self.assertIn(reverse('opex_boq_accb', kwargs={'project_id': self.site.project_id}),
                      html)
        data = json.loads(html.split('id="accbData" type="application/json">', 1)[1]
                          .split('</script>', 1)[0])
        self.assertEqual(data['accbs'][0]['description'], EX_TYPE2_TEXT)
        self.assertNotIn(self.accb_master.pk,
                         [item['id'] for item in json.loads(response.context['catalogue_json'])])


class DesignerWriteRuleTests(ACCBBase):
    """P6: the BOQ's own write rule — author, no group lock, not submitted."""

    def setUp(self):
        super().setUp()
        self.accb = self._seed_accb(EX_TYPE2)

    def _assert_nothing_changed(self):
        self.assertEqual(ACCBConfig.objects.count(), 1)
        self.accb.boq_item.refresh_from_db()
        self.assertEqual(self.accb.boq_item.description, EX_TYPE2_TEXT)

    def test_refused_when_the_boq_is_submitted(self):
        self._design_lock()
        self._login(self.designer)
        for action, config in (('add', EX_TYPE1), ('save', EX_TYPE1),
                               ('duplicate', None), ('remove', None)):
            with self.subTest(action=action):
                self._accb_post(action, config, accb_id=self.accb.pk)
                self._assert_nothing_changed()

    def test_refused_under_a_group_lock(self):
        self._group_lock()
        self._login(self.designer)
        for action, config in (('add', EX_TYPE1), ('save', EX_TYPE1),
                               ('duplicate', None), ('remove', None)):
            with self.subTest(action=action):
                self._accb_post(action, config, accb_id=self.accb.pk)
                self._assert_nothing_changed()

    def test_refused_for_anyone_but_the_assigned_designer(self):
        for profile in (self.other, self.qc, self.head, self.pm, self.admin):
            with self.subTest(user=profile.user.username):
                self._login(profile)
                response = self._accb_post('remove', accb_id=self.accb.pk)
                self.assertEqual(response.status_code, 403)
                response = self._accb_post('add', EX_TYPE1)
                self.assertEqual(response.status_code, 403)
                self._assert_nothing_changed()

    def test_a_locked_page_shows_accbs_read_only(self):
        self._design_lock()
        self._login(self.designer)
        response = self.client.get(reverse('opex_boq_entry',
                                           kwargs={'project_id': self.site.project_id}))
        html = response.content.decode()
        self.assertIn(EX_TYPE2_TEXT, html)
        self.assertNotIn(reverse('opex_boq_accb', kwargs={'project_id': self.site.project_id}),
                         html)


# ===========================================================================
# The other write paths leave ACCB lines alone
# ===========================================================================

class PickerSaveTests(ACCBBase):

    def setUp(self):
        super().setUp()
        self._login(self.designer)
        self.mandatory = self._line('OPX-001', 10)
        self.accb_a = self._seed_accb(EX_TYPE2)
        self.accb_b = self._seed_accb(EX_TYPE1)

    def _save(self, data):
        return self.client.post(reverse('opex_boq_entry',
                                        kwargs={'project_id': self.site.project_id}), data)

    def test_a_sheet_save_that_omits_the_accbs_leaves_them_intact(self):
        dcdb = self._master('OPX-002')
        response = self._save({'action': 'save_draft', 'item': [str(dcdb.pk)],
                               f'qty_{dcdb.pk}': '3'})
        self.assertEqual(response.status_code, 302)
        lines = self._accb_lines()
        self.assertEqual([l.pk for l in lines], [self.accb_a.boq_item_id, self.accb_b.boq_item_id])
        self.assertEqual([l.description for l in lines], [EX_TYPE2_TEXT, EX_TYPE1_TEXT])
        self.assertTrue(all(l.boq_quantity == 1 for l in lines))
        self.assertEqual([l.accb_config.as_config() for l in lines], [EX_TYPE2, EX_TYPE1])
        # The ordinary items still work as before: OPX-002 added, mandatory OPX-001 kept.
        self.assertEqual(BOQItem.objects.get(boq__project=self.site,
                                             item_master=dcdb).boq_quantity, Decimal('3'))
        self.assertTrue(BOQItem.objects.filter(pk=self.mandatory.pk).exists())

    def test_posting_the_accb_code_as_a_picker_item_adds_nothing(self):
        self._save({'action': 'save_draft', 'item': [str(self.accb_master.pk)],
                    f'qty_{self.accb_master.pk}': '9',
                    f'qty_row_{self.accb_a.boq_item_id}': '9',
                    'keep_row': [str(self.accb_a.boq_item_id)]})
        self.assertEqual(BOQItem.objects.filter(item_master=self.accb_master).count(), 2)
        self.assertTrue(all(l.boq_quantity == 1 for l in self._accb_lines()))


class UploadDownloadTests(ACCBBase):

    def setUp(self):
        super().setUp()
        self._login(self.designer)
        self._line('OPX-001', 10)
        self.accb = self._seed_accb(EX_TYPE2)

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

    def _url(self):
        return reverse('opex_boq_upload', kwargs={'project_id': self.site.project_id})

    def test_a_file_carrying_the_accb_code_is_refused_with_the_message(self):
        response = self.client.post(self._url(), {
            'phase': 'preview', 'file': self._file([('OPX-001', 12), (ACCB_CONFIGURED_CODE, 5)])})
        self.assertEqual(response.context['stage'], 'errors')
        self.assertIn(f'Row 3: {ACCB_UPLOAD_REFUSED_MESSAGE}', response.context['errors'])
        self.assertEqual(self._accb_lines()[0].boq_quantity, Decimal('1'))

    def test_a_crafted_commit_carrying_the_accb_code_is_refused(self):
        response = self.client.post(self._url(), {
            'phase': 'commit',
            'rows_json': json.dumps([{'code': ACCB_CONFIGURED_CODE, 'quantity': '5'}])})
        self.assertEqual(response.context['stage'], 'errors')
        self.assertEqual(self._accb_lines()[0].boq_quantity, Decimal('1'))

    def test_an_ordinary_upload_leaves_the_accb_line_at_1(self):
        preview = self.client.post(self._url(), {
            'phase': 'preview', 'file': self._file([('OPX-001', 12), ('OPX-002', 4)])})
        self.assertEqual(preview.context['stage'], 'preview')
        self.client.post(self._url(), {'phase': 'commit',
                                       'rows_json': preview.context['rows_json']})
        line = self._accb_lines()[0]
        self.assertEqual(line.boq_quantity, Decimal('1'))
        self.assertEqual(line.description, EX_TYPE2_TEXT)
        self.assertEqual(BOQItem.objects.filter(boq__project=self.site).count(), 3)

    def test_the_download_carries_the_accbs_on_their_own_sheet(self):
        from openpyxl import load_workbook
        response = self.client.get(reverse('opex_boq_download',
                                           kwargs={'project_id': self.site.project_id}))
        wb = load_workbook(BytesIO(response.content))
        data_codes = [row[0] for row in wb[BOQ_DOWNLOAD_SHEET].iter_rows(min_row=2, values_only=True)]
        self.assertNotIn(ACCB_CONFIGURED_CODE, data_codes)
        accb_rows = list(wb[BOQ_DOWNLOAD_ACCB_SHEET].iter_rows(min_row=5, values_only=True))
        self.assertEqual(accb_rows, [(1, ACCB_CONFIGURED_CODE, EX_TYPE2_TEXT, 'Nos', 1)])


class ReviewerTests(ACCBBase):
    """P7: a reviewer sees ACCB lines read-only and cannot change them."""

    def setUp(self):
        super().setUp()
        self.site, self.assignment = self._opex_site('AC-QC', status=DESIGN_IN_QC)
        self._line('OPX-001', 10)
        self.accb = self._seed_accb(EX_TYPE2)
        self._design_lock()
        self._login(self.qc)
        self.url = reverse('boq_correct', kwargs={'project_id': self.site.project_id})

    def test_the_correction_screen_shows_accbs_read_only_with_the_message(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn(EX_TYPE2_TEXT, html)
        self.assertIn(ACCB_REVIEWER_MESSAGE.replace("'", '&#x27;'), html)
        self.assertNotIn(f'name="item_id" value="{self.accb.boq_item_id}"', html)

    def test_a_crafted_quantity_change_is_refused(self):
        response = self.client.post(self.url, {'action': 'set_quantity',
                                               'item_id': str(self.accb.boq_item_id),
                                               'quantity': '4'}, follow=True)
        self.assertIn(ACCB_REVIEWER_MESSAGE,
                      [str(m) for m in response.context['messages']])
        self.assertEqual(self._accb_lines()[0].boq_quantity, Decimal('1'))
        self.assertFalse(BOQCorrection.objects.exists())

    def test_a_reviewer_cannot_add_a_bare_accb_line(self):
        self.client.post(self.url, {'action': 'add_line',
                                    'master_id': str(self.accb_master.pk), 'quantity': '1'})
        self.assertEqual(BOQItem.objects.filter(item_master=self.accb_master).count(), 1)

    def test_the_review_panel_carries_the_accbs(self):
        panel = _boq_review_panel(self.site.boq)
        self.assertEqual([row.pk for row in panel['boq_accb_rows']], [self.accb.boq_item_id])
        self.assertEqual(panel['boq_row_count'], 2)


# ===========================================================================
# SCM — kept apart by configuration, nothing else moves
# ===========================================================================

class SCMGroupingTests(ACCBBase):

    def setUp(self):
        super().setUp()
        self.site_b, _ = self._opex_site('AC-B')
        for site in (self.site, self.site_b):
            self._line('OPX-001', 10, site=site)
            self._line('OPX-002', 2, site=site)
        self.ids = [self.site.pk, self.site_b.pk]

    def test_same_configuration_in_a_different_feeder_order_is_one_row_count_2(self):
        reordered = cfg('Type-1', False,
                        [('MCB', 25), ('MCB', 32), ('MCB', 32), ('MCCB', 100)], [('MCCB', 100)])
        self._seed_accb(EX_TYPE1, site=self.site)
        self._seed_accb(reordered, site=self.site_b)
        rows = aggregate_group_boq(self.ids)['accb_lines']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['total_quantity'], Decimal('2'))
        self.assertEqual(rows[0]['site_count'], 2)
        self.assertEqual(rows[0]['description'], EX_TYPE1_TEXT)
        self.assertEqual(rows[0]['contributions'], [('AC-A', Decimal('1')), ('AC-B', Decimal('1'))])

    def test_a_different_configuration_gets_its_own_row(self):
        self._seed_accb(EX_TYPE1, site=self.site)
        self._seed_accb(EX_TYPE1, site=self.site)
        self._seed_accb(EX_TYPE2, site=self.site_b)
        rows = {row['description']: row for row in aggregate_group_boq(self.ids)['accb_lines']}
        self.assertEqual(set(rows), {EX_TYPE1_TEXT, EX_TYPE2_TEXT})
        self.assertEqual(rows[EX_TYPE1_TEXT]['total_quantity'], Decimal('2'))
        self.assertEqual(rows[EX_TYPE1_TEXT]['site_count'], 1)
        self.assertEqual(rows[EX_TYPE2_TEXT]['total_quantity'], Decimal('1'))

    def test_non_accb_totals_are_unchanged_by_adding_accbs(self):
        before = aggregate_group_boq(self.ids)
        self._seed_accb(EX_TYPE1, site=self.site)
        self._seed_accb(EX_TYPE2, site=self.site_b)
        after = aggregate_group_boq(self.ids)
        self.assertEqual(after['lines'], before['lines'])
        self.assertEqual(after['item_count'], before['item_count'])
        self.assertEqual(after['unlinked'], before['unlinked'])
        self.assertNotIn(self.accb_master.pk, [line['item_master'] for line in after['lines']])

    def test_the_order_raise_lists_accbs_as_orderable_rows(self):
        """B-3b (rewritten from B-3a's read-only test): one orderable row per
        configuration, under its own amount field. tests_accb_order covers the raise."""
        self._seed_accb(EX_TYPE1, site=self.site)
        self._seed_accb(EX_TYPE1, site=self.site_b)
        context = _picker_context(self.program, {})
        self.assertEqual(len(context['accb_requirement']), 1)
        row = context['accb_requirement'][0]
        self.assertEqual(json.loads(row['contrib']),
                         {str(self.site.pk): '1.00', str(self.site_b.pk): '1.00'})
        self.assertEqual(row['field'], accb_amount_field(accb_config_key(EX_TYPE1)))
        html = render_to_string('projects/partials/vendor_order_requirement.html', context)
        self.assertNotIn('ACCBs are not ordered from here yet', html)
        self.assertNotIn('voAccbCard', html)
        self.assertIn(f'name="{row["field"]}"', html)
        # Never amount_<OPX-273 pk>: that one field would be shared by every ACCB.
        self.assertNotIn(f'name="amount_{self.accb_master.pk}"', html)
        errors = []
        lines, priced = _parse_requirement(
            {row['field']: '5000', f'amount_{self.accb_master.pk}': '9999'}, self.ids, errors)
        self.assertEqual(errors, [])
        accb = [line for line in lines if line['accb_config_key']]
        self.assertEqual(len(accb), 1)
        self.assertEqual((accb[0]['item_master_id'], accb[0]['description'],
                          accb[0]['quantity'], accb[0]['amount'], accb[0]['accb_config_key']),
                         (self.accb_master.pk, EX_TYPE1_TEXT, Decimal('2'), Decimal('5000'),
                          accb_config_key(EX_TYPE1)))
        # The catalogue-row amount for OPX-273 is ignored; it is not how an ACCB is priced.
        self.assertEqual(priced, Decimal('5000'))
        self.assertEqual(sorted(line['code'] for line in lines if not line['accb_config_key']),
                         ['OPX-001', 'OPX-002'])

    def test_the_group_screen_draws_the_accb_rows(self):
        from .models import SITE_GROUP_DRAFT
        self._seed_accb(EX_TYPE1, site=self.site)
        scm = _profile('scmacc', 'SCM')
        group = SiteGroup.objects.create(program=self.program, name='G2',
                                         status=SITE_GROUP_DRAFT, created_by=scm)
        for site in (self.site, self.site_b):
            SiteGroupMembership.objects.create(group=group, project=site, added_by=scm)
        self._login(scm)
        response = self.client.get(reverse('site_group_detail', kwargs={'pk': group.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="groupAccbs"', response.content.decode())
        self.assertIn(EX_TYPE1_TEXT, response.content.decode())


# ===========================================================================
# The catalogue: OPX-273 locked, everything else as before
# ===========================================================================

class CatalogueTests(ACCBBase):

    def test_the_picker_catalogue_leaves_out_only_the_accb_row(self):
        codes = [m.code for m in get_opex_boq_catalogue()]
        self.assertEqual(codes, ['OPX-001', 'OPX-002', 'OPX-003', 'OPX-039'])
        self.assertNotIn('ACCB', opex_catalogue_category_order())

    def test_the_mandatory_items_are_unchanged(self):
        self.assertEqual([m.code for m in get_opex_mandatory_items()], ['OPX-001'])

    def test_the_existing_acdb_item_still_works_in_the_picker(self):
        self._login(self.designer)
        acdb = self._master('OPX-039')
        self.client.post(reverse('opex_boq_entry', kwargs={'project_id': self.site.project_id}),
                         {'action': 'save_draft', 'item': [str(acdb.pk)], f'qty_{acdb.pk}': '2'})
        line = BOQItem.objects.get(boq__project=self.site, item_master=acdb)
        self.assertEqual((line.boq_quantity, line.description),
                         (Decimal('2'), 'ACDB Type-1 (For 12KW INV)'))

    def _assert_unchanged(self):
        self.accb_master.refresh_from_db()
        self.assertTrue(self.accb_master.is_active)
        self.assertFalse(self.accb_master.is_mandatory)
        self.assertEqual(self.accb_master.description, 'ACCB (configured)')

    def _form_data(self, **overrides):
        data = {'description': 'Edited', 'unit': 'Nos', 'category': 'ACCB',
                'is_active': 'on', 'sort_order': '273', 'project_type': 'OPEX'}
        data.update(overrides)
        return data

    def test_the_head_cannot_edit_it(self):
        self._login(self.head)
        url = reverse('design_boq_catalogue_edit', kwargs={'item_id': self.accb_master.pk})
        self.assertEqual(self.client.get(url).status_code, 302)
        response = self.client.post(url, self._form_data(is_mandatory='on'), follow=True)
        self.assertIn(ACCB_CATALOGUE_LOCKED_MESSAGE, [str(m) for m in response.context['messages']])
        self._assert_unchanged()

    def test_the_head_cannot_deactivate_it(self):
        self._login(self.head)
        self.client.post(reverse('design_boq_catalogue_toggle',
                                 kwargs={'item_id': self.accb_master.pk}))
        self._assert_unchanged()

    def test_the_admin_cannot_edit_it(self):
        self._login(self.admin)
        url = reverse('admin_boq_item_edit', kwargs={'item_id': self.accb_master.pk})
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.post(url, self._form_data(is_mandatory='on'))
        self._assert_unchanged()

    def test_the_admin_cannot_deactivate_it(self):
        self._login(self.admin)
        self.client.post(reverse('admin_boq_item_toggle', kwargs={'item_id': self.accb_master.pk}))
        self._assert_unchanged()

    def test_the_form_refuses_mandatory_on_it(self):
        form = BOQItemMasterForm(self._form_data(description='ACCB (configured)',
                                                 is_mandatory='on'),
                                 instance=self.accb_master)
        del form.fields['code']
        self.assertFalse(form.is_valid())
        self.assertIn(ACCB_CATALOGUE_LOCKED_MESSAGE, form.non_field_errors())
        self._assert_unchanged()

    def test_both_catalogue_lists_draw_it_locked(self):
        self._login(self.head)
        html = self.client.get(reverse('design_boq_catalogue')).content.decode()
        self.assertIn('Locked — used by configured ACCBs', html)
        self.assertNotIn(reverse('design_boq_catalogue_toggle',
                                 kwargs={'item_id': self.accb_master.pk}), html)
        self._login(self.admin)
        html = self.client.get(reverse('admin_boq_items')).content.decode()
        self.assertIn('Locked — used by configured ACCBs', html)
        self.assertNotIn(reverse('admin_boq_item_toggle',
                                 kwargs={'item_id': self.accb_master.pk}), html)

    def test_other_items_stay_editable(self):
        self._login(self.head)
        other = self._master('OPX-002')
        url = reverse('design_boq_catalogue_edit', kwargs={'item_id': other.pk})
        self.assertEqual(self.client.get(url).status_code, 200)


# ===========================================================================
# B-3a.1 — numbers are never reused; the group screen counts ACCBs
# ===========================================================================

class NumberCounterTests(ACCBBase):

    def setUp(self):
        super().setUp()
        self._login(self.designer)

    def _numbers(self):
        return [line.accb_config.number for line in self._accb_lines()]

    def test_a_removed_highest_number_is_not_reused(self):
        for _ in range(3):
            self._accb_post('add', EX_TYPE2)
        third = self._accb_lines()[2]
        self._accb_post('remove', accb_id=third.accb_config.pk)
        self.assertEqual(self._numbers(), [1, 2])
        self._accb_post('add', EX_TYPE1)
        self.assertEqual(self._numbers(), [1, 2, 4])
        self.assertEqual(BOQ.objects.get(project=self.site).next_accb_number, 5)

    def test_duplicate_takes_its_number_from_the_counter(self):
        self._accb_post('add', EX_TYPE2)
        self._accb_post('add', EX_TYPE1)
        lines = self._accb_lines()
        self._accb_post('remove', accb_id=lines[1].accb_config.pk)
        self._accb_post('duplicate', accb_id=lines[0].accb_config.pk)
        self.assertEqual(self._numbers(), [1, 3])

    def test_a_counter_behind_the_numbers_in_use_cannot_collide(self):
        self._accb_post('add', EX_TYPE2)
        self._accb_post('add', EX_TYPE2)
        BOQ.objects.filter(project=self.site).update(next_accb_number=1)
        self._accb_post('add', EX_TYPE1)
        self.assertEqual(self._numbers(), [1, 2, 3])
        self.assertEqual(BOQ.objects.get(project=self.site).next_accb_number, 4)

    def test_the_backfill_sets_max_plus_one_and_leaves_empty_boqs_at_1(self):
        import importlib
        from django.apps import apps
        backfill = importlib.import_module(
            'projects.migrations.0150_boq_next_accb_number').backfill_next_accb_number
        gapped = self._boq()
        for number in (1, 3):   # ACCB 2 was removed
            line = BOQItem.objects.create(boq=gapped, item_master=self.accb_master,
                                          serial_no=273, category='ACCB',
                                          description=EX_TYPE2_TEXT, uom='Nos',
                                          boq_quantity=Decimal('1'))
            ACCBConfig.objects.create(boq=gapped, boq_item=line, number=number,
                                      accb_type='Type-2', has_spd=True,
                                      feeders={'in': EX_TYPE2['in'], 'out': EX_TYPE2['out']},
                                      config_key=accb_config_key(EX_TYPE2))
        other_site, _ = self._opex_site('AC-EMPTY')
        empty = self._boq(other_site)
        BOQ.objects.filter(pk__in=[gapped.pk, empty.pk]).update(next_accb_number=1)
        backfill(apps, None)
        self.assertEqual(BOQ.objects.get(pk=gapped.pk).next_accb_number, 4)
        self.assertEqual(BOQ.objects.get(pk=empty.pk).next_accb_number, 1)


class GroupScreenCountTests(ACCBBase):

    def setUp(self):
        super().setUp()
        from .models import SITE_GROUP_DRAFT
        self.site_b, _ = self._opex_site('AC-B')
        self.scm = _profile('scmacc2', 'SCM')
        self.group = SiteGroup.objects.create(program=self.program, name='G3',
                                              status=SITE_GROUP_DRAFT, created_by=self.scm)
        for site in (self.site, self.site_b):
            SiteGroupMembership.objects.create(group=self.group, project=site,
                                               added_by=self.scm)
        self._login(self.scm)
        self.url = reverse('site_group_detail', kwargs={'pk': self.group.pk})

    def _page(self):
        import re
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return re.sub(r'\s+', ' ', response.content.decode())

    def test_nothing_at_all_says_nothing_to_aggregate(self):
        html = self._page()
        self.assertIn('id="groupNothingToAggregate"', html)
        self.assertIn('2 sites, 0 items </span>', html)
        self.assertNotIn('ACCB configuration', html)

    def test_catalogue_quantities_only_omit_the_accb_count(self):
        self._line('OPX-001', 10)
        html = self._page()
        self.assertIn('2 sites, 1 item </span>', html)
        self.assertNotIn('ACCB configuration', html)
        self.assertNotIn('id="groupNothingToAggregate"', html)

    def test_accbs_only_are_counted_and_not_called_nothing(self):
        self._seed_accb(EX_TYPE2)
        self._seed_accb(EX_TYPE2, site=self.site_b)
        html = self._page()
        self.assertIn('2 sites, 0 items, 1 ACCB configuration </span>', html)
        self.assertNotIn('id="groupNothingToAggregate"', html)
        self.assertIn('id="groupAccbsOnly"', html)

    def test_the_header_counts_each_configuration(self):
        self._line('OPX-001', 10)
        self._seed_accb(EX_TYPE2)
        self._seed_accb(EX_TYPE1, site=self.site_b)
        html = self._page()
        self.assertIn('2 sites, 1 item, 2 ACCB configurations </span>', html)

    def test_accbs_add_no_query_to_the_group_screen(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self._line('OPX-001', 10)
        self.client.get(self.url)   # warm the session
        with CaptureQueriesContext(connection) as without:
            self.client.get(self.url)
        self._seed_accb(EX_TYPE2)
        self._seed_accb(EX_TYPE1, site=self.site_b)
        with CaptureQueriesContext(connection) as with_accbs:
            self.client.get(self.url)
        self.assertEqual(len(with_accbs), len(without))
