"""
DW-1 (ruling DW-R5) — the minimum an OPEX BOQ must carry: at least one inverter, and at
least one DCDB, ACDB or ACCB, each with a quantity above 0.

DW-1a: the helper (boq_minimum_content_gaps and friends, models.py) and the gate on
"Mark BOQ complete" (design_boq_complete, both of its routes). DW-1b's group-screen
warning is tested in tests_design_boq_group_snapshots / tests_design_boq_groups.

The fixture builds its own small catalogue and the three B-2c formula groups, because the
suite runs with migrations off and no group exists until a test makes one, which is also
how the "groups not loaded" path is tested.
"""
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from . import models as models_module
from .accb import ACCB_CONFIGURED_CODE
from .models import (
    ACCBConfig, BOQ, BOQItem, BOQItemGroup, BOQItemGroupMember, BOQItemMaster, DesignAssignment,
    DesignAttempt, Program, Project, DESIGN_IN_DESIGN,
    boq_minimum_content_gaps, boq_minimum_content_rules, minimum_content_gaps,
)

#: (code, description, category) — one item per group, one spare of each kind, and a cable.
CATALOGUE = [
    ('OPX-001', 'Solar PV Module', 'Module'),
    ('OPX-010', 'String inverter 50 kW', 'Inverter'),
    ('OPX-011', 'String inverter 30 kW (retired)', 'Inverter'),
    ('OPX-020', 'DCDB 2 in 2 out', 'DCDB'),
    ('OPX-030', 'ACDB 63 A', 'ACDB'),
    ('OPX-059', 'DC cable 4 sq mm', 'Cable'),
    (ACCB_CONFIGURED_CODE, 'ACCB (configured)', 'ACCB'),
]

BOTH_GAPS = ['no_inverter', 'no_dcdb_acdb_accb']


def _profile(username, role='Design'):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.save()
    return profile


class MinimumContentBase(TestCase):

    def setUp(self):
        # The log-once memory is per process; clear it so each test sees its own warning.
        models_module._MINIMUM_CONTENT_LOGGED.clear()
        for i, (code, description, category) in enumerate(CATALOGUE, start=1):
            BOQItemMaster.objects.create(
                code=code, description=description, unit='Nos', category=category,
                project_type='OPEX', is_active=code != 'OPX-011', sort_order=i)
        self.designer = _profile('dw1des')
        self.pm = _profile('dw1pm', 'PM')
        self.program = Program.objects.create(
            name='DW1 tender', program_type='OPEX', client_name='C', status='Active',
            short_tender_code='DW1')
        self.site = Project.objects.create(
            project_id='DW1-A', customer_name='C', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code='DW1-A', dc_capacity_kw=Decimal('100.00'),
            status='Draft', assigned_design=self.designer, assigned_pm=self.pm)
        self.assignment = DesignAssignment.objects.create(
            project=self.site, status=DESIGN_IN_DESIGN, assigned_to=self.designer,
            survey_file_bucket='b', survey_file_path='DW1-A/survey/x.pdf')
        self.boq = BOQ.objects.create(project=self.site)

    def load_groups(self, names=('all inverters', 'all DCDBs', 'all ACDBs')):
        """The three B-2c groups, as load_formula_rules would make them. The retired
        inverter is a member too: membership ignores is_active (B-2a)."""
        members = {'all inverters': ['OPX-010', 'OPX-011'], 'all DCDBs': ['OPX-020'],
                   'all ACDBs': ['OPX-030']}
        for name in names:
            group = BOQItemGroup.objects.create(name=name, label=name.split(' ', 1)[1])
            for code in members[name]:
                BOQItemGroupMember(group=group, item=self.m(code)).save()

    def m(self, code):
        return BOQItemMaster.objects.get(code=code)

    def line(self, code, quantity, boq=None):
        master = self.m(code)
        return BOQItem.objects.create(
            boq=boq or self.boq, item_master=master, serial_no=master.sort_order,
            category=master.category, description=master.description, uom='Nos',
            boq_quantity=None if quantity is None else Decimal(str(quantity)),
            is_standard_item=True)

    def accb(self, boq=None):
        """A configured ACCB line, Qty 1, as the ACCB section writes it."""
        boq = boq or self.boq
        item = self.line(ACCB_CONFIGURED_CODE, 1, boq=boq)
        ACCBConfig.objects.create(
            boq=boq, boq_item=item, number=1, accb_type='Type-2', has_spd=False,
            feeders={'in': [{'breaker': 'MCCB', 'rating': 100}],
                     'out': [{'breaker': 'MCCB', 'rating': 63}]},
            config_key='Type-2|IN:MCCB-100|OUT:MCCB-63')
        return item


# ═════════════════════════════════════════════════════════════════════════════
# T1 — the helper
# ═════════════════════════════════════════════════════════════════════════════

class HelperTests(MinimumContentBase):

    def setUp(self):
        super().setUp()
        self.load_groups()
        self.line('OPX-001', 10)

    def gaps(self):
        return boq_minimum_content_gaps(self.boq)

    def test_module_only_has_both_gaps(self):
        self.assertEqual(self.gaps(), BOTH_GAPS)

    def test_inverter_and_acdb_pass(self):
        self.line('OPX-010', 2)
        self.line('OPX-030', 1)
        self.assertEqual(self.gaps(), [])

    def test_inverter_alone_leaves_only_the_board_gap(self):
        self.line('OPX-010', 2)
        self.assertEqual(self.gaps(), ['no_dcdb_acdb_accb'])

    def test_dcdb_alone_leaves_only_the_inverter_gap(self):
        self.line('OPX-020', 1)
        self.assertEqual(self.gaps(), ['no_inverter'])

    def test_blank_and_zero_quantities_count_as_missing(self):
        self.line('OPX-010', None)
        self.line('OPX-020', 0)
        self.line('OPX-030', None)
        self.assertEqual(self.gaps(), BOTH_GAPS)

    def test_several_inverters_and_accbs_pass(self):
        self.line('OPX-010', 3)
        self.line('OPX-011', 1)
        self.accb()
        second = BOQ.objects.get(pk=self.boq.pk)
        self.assertEqual(boq_minimum_content_gaps(second), [])

    def test_accb_without_dcdb_or_acdb_passes_the_board_test(self):
        self.line('OPX-010', 1)
        self.accb()
        self.assertEqual(self.gaps(), [])

    def test_a_bare_opx_273_line_counts_as_an_accb(self):
        """The spec counts lines on OPX-273; a configuration is not required."""
        self.line('OPX-010', 1)
        self.line(ACCB_CONFIGURED_CODE, 1)
        self.assertEqual(self.gaps(), [])

    def test_a_line_on_a_retired_inverter_counts(self):
        self.line('OPX-011', 1)
        self.line('OPX-020', 1)
        self.assertEqual(self.gaps(), [])

    def test_a_line_with_no_catalogue_item_never_counts(self):
        BOQItem.objects.create(boq=self.boq, item_master=None, serial_no=99,
                               category='Inverter', description='String inverter (ad hoc)',
                               uom='Nos', boq_quantity=Decimal('2'))
        BOQItem.objects.create(boq=self.boq, item_master=None, serial_no=100,
                               category='ACDB', description='ACDB (ad hoc)',
                               uom='Nos', boq_quantity=Decimal('1'))
        self.assertEqual(self.gaps(), BOTH_GAPS)

    def test_no_boq_at_all_has_both_gaps(self):
        self.assertEqual(boq_minimum_content_gaps(None), BOTH_GAPS)

    def test_pure_form_matches(self):
        rules = boq_minimum_content_rules()
        inverter, acdb = self.m('OPX-010').pk, self.m('OPX-030').pk
        self.assertEqual(minimum_content_gaps({inverter, acdb}, False, rules), [])
        self.assertEqual(minimum_content_gaps({inverter}, True, rules), [])
        self.assertEqual(minimum_content_gaps(set(), True, rules), ['no_inverter'])
        self.assertEqual(minimum_content_gaps(set(), False, rules), BOTH_GAPS)

    def test_rules_are_one_query(self):
        with self.assertNumQueries(1):
            boq_minimum_content_rules()


class MissingGroupTests(MinimumContentBase):
    """Q7: a test whose group is missing is dropped, logged once, never enforced."""

    def setUp(self):
        super().setUp()
        self.line('OPX-001', 10)

    def test_no_groups_loaded_refuses_nothing_and_logs_once(self):
        with self.assertLogs('projects.models', 'WARNING') as logs:
            self.assertEqual(boq_minimum_content_gaps(self.boq), [])
            self.assertEqual(boq_minimum_content_gaps(self.boq), [])
        self.assertEqual(len(logs.records), 1)
        self.assertIn('all ACDBs, all DCDBs, all inverters', logs.output[0])

    def test_one_board_group_missing_drops_that_whole_test(self):
        """Without "all ACDBs", an ACDB-only site would be refused for a line it has."""
        self.load_groups(('all inverters', 'all DCDBs'))
        with self.assertLogs('projects.models', 'WARNING') as logs:
            self.assertEqual(boq_minimum_content_gaps(self.boq), ['no_inverter'])
        self.assertIn('all ACDBs', logs.output[0])

    def test_inverter_group_missing_keeps_the_board_test(self):
        self.load_groups(('all DCDBs', 'all ACDBs'))
        with self.assertLogs('projects.models', 'WARNING'):
            self.assertEqual(boq_minimum_content_gaps(self.boq), ['no_dcdb_acdb_accb'])

    def test_an_empty_group_counts_as_missing(self):
        self.load_groups(('all inverters', 'all DCDBs'))
        BOQItemGroup.objects.create(name='all ACDBs', label='ACDBs')
        with self.assertLogs('projects.models', 'WARNING'):
            self.assertEqual(boq_minimum_content_gaps(self.boq), ['no_inverter'])


# ═════════════════════════════════════════════════════════════════════════════
# T2 — "Mark BOQ complete" is refused, on both routes
# ═════════════════════════════════════════════════════════════════════════════

class MarkCompleteGateTests(MinimumContentBase):

    def setUp(self):
        super().setUp()
        self.load_groups()
        self.line('OPX-001', 10)
        self.assertTrue(self.client.login(username='dw1des', password='x'))

    def workspace_post(self):
        return self.client.post(
            reverse('design_boq_complete', kwargs={'project_id': self.site.project_id}),
            {'boq_remarks': ''}, follow=True)

    def picker_post(self, codes_and_quantities):
        data = {'action': 'mark_complete', 'item': [], 'keep_row': [], 'boq_remarks': ''}
        for code, quantity in codes_and_quantities:
            master = self.m(code)
            data['item'].append(str(master.pk))
            data[f'qty_{master.pk}'] = str(quantity)
        return self.client.post(
            reverse('opex_boq_entry', kwargs={'project_id': self.site.project_id}),
            data, follow=True)

    def stamped(self):
        attempt = DesignAttempt.objects.filter(assignment=self.assignment).first()
        return attempt is not None and attempt.boq_submitted_at is not None

    def errors(self, response):
        # By level, not tag: settings.MESSAGE_TAGS maps ERROR to Bootstrap's 'danger'.
        return [str(m) for m in response.context['messages'] if m.level == messages.ERROR]

    def test_workspace_route_names_both_gaps_and_does_not_stamp(self):
        response = self.workspace_post()
        self.assertEqual(self.errors(response), [
            "DW1-A: can't mark this BOQ complete: it has no inverter line with a "
            "quantity, and no DCDB, ACDB or ACCB line with a quantity. Add them and save "
            "first. An ACCB saves from its own card."])
        self.assertFalse(self.stamped())
        # Refused before the lazy attempt-open, so no stray attempt row either.
        self.assertFalse(DesignAttempt.objects.filter(assignment=self.assignment).exists())

    def test_inverter_only_names_the_board_gap_alone(self):
        self.line('OPX-010', 2)
        response = self.workspace_post()
        self.assertEqual(self.errors(response), [
            "DW1-A: can't mark this BOQ complete: it has no DCDB, ACDB or ACCB line with a "
            "quantity. Add it and save first. An ACCB saves from its own card."])
        self.assertFalse(self.stamped())

    def test_board_only_names_the_inverter_gap_without_the_accb_sentence(self):
        self.line('OPX-030', 1)
        response = self.workspace_post()
        self.assertEqual(self.errors(response), [
            "DW1-A: can't mark this BOQ complete: it has no inverter line with a quantity. "
            "Add it and save first."])

    def test_complete_boq_stamps(self):
        self.line('OPX-010', 2)
        self.line('OPX-030', 1)
        response = self.workspace_post()
        self.assertEqual(self.errors(response), [])
        self.assertTrue(self.stamped())

    def test_picker_route_says_the_sheet_was_saved(self):
        response = self.picker_post([('OPX-001', 12), ('OPX-010', 2)])
        self.assertEqual(self.errors(response), [
            "Your sheet was saved, but DW1-A can't be marked complete: it has no DCDB, ACDB "
            "or ACCB line with a quantity. Add it and save first. An ACCB saves from its own "
            "card."])
        self.assertFalse(self.stamped())
        # The sheet itself was kept: the inverter and the new module quantity are on it.
        saved = dict(BOQItem.objects.filter(boq=self.boq)
                     .values_list('item_master__code', 'boq_quantity'))
        self.assertEqual(saved, {'OPX-001': Decimal('12'), 'OPX-010': Decimal('2')})
        self.assertRedirects(response, reverse('design_site_workspace',
                                               kwargs={'project_id': self.site.project_id}))

    def test_picker_route_with_an_unsaved_figure_is_saved_then_checked(self):
        """The picker's Mark complete posts the whole sheet, so a figure typed and not yet
        saved is saved first and the check reads it: a 0 typed over the inverter refuses."""
        self.line('OPX-010', 2)
        self.line('OPX-030', 1)
        response = self.picker_post([('OPX-001', 10), ('OPX-010', 0), ('OPX-030', 1)])
        self.assertEqual(self.errors(response), [
            "Your sheet was saved, but DW1-A can't be marked complete: it has no inverter "
            "line with a quantity. Add it and save first."])
        self.assertFalse(self.stamped())

    def test_picker_route_complete_sheet_stamps(self):
        response = self.picker_post([('OPX-001', 10), ('OPX-010', 2), ('OPX-020', 1)])
        self.assertEqual(self.errors(response), [])
        self.assertTrue(self.stamped())

    def test_accb_only_board_stamps(self):
        self.line('OPX-010', 2)
        self.accb()
        self.workspace_post()
        self.assertTrue(self.stamped())


class MarkCompleteWithoutGroupsTests(MinimumContentBase):
    """A database with no formula rules loaded keeps today's behaviour (Q7)."""

    def test_module_only_still_stamps_when_no_group_is_loaded(self):
        self.line('OPX-001', 10)
        self.assertTrue(self.client.login(username='dw1des', password='x'))
        with self.assertLogs('projects.models', 'WARNING'):
            self.client.post(reverse('design_boq_complete',
                                     kwargs={'project_id': self.site.project_id}))
        attempt = DesignAttempt.objects.get(assignment=self.assignment)
        self.assertIsNotNone(attempt.boq_submitted_at)
