"""
FIN-M-RN — rename protection for tasks found by name, and for Not Applicable tasks.

WHY. Several features find a task by its name, not by a key: the "Invoice due" alert
and the Finance work page (utils.INVOICE_DUE_TRIGGERS), the invoice recording and the
Finance back-assignment (INVOICE_TASK_NAMES), the payment-milestone sync
(RESIDENTIAL_FINANCE_CONFIRMATION_TASK_NAMES). A rename broke them silently. Before
this session the two R8 trigger names were renamable; so was any Not Applicable task.

THE RULES PINNED HERE, each by a test named for it
    Triggers      "PO for B & C Class Items" and "Customer Handover" cannot be renamed:
                  no Rename button, GET panel and POST both 404, nothing written; and
                  no task may be renamed TO either.
    No regression every previously reserved name is still refused.
    Derived       the reserved list is built from the lookup tables: a name added to any
                  of the three is reserved with no edit to the list, and every key of the
                  views' name maps is reserved.
    N/A           a Not Applicable task cannot be renamed, by anyone; once the PM
                  un-marks it, it renames as before, unless its name is reserved.
    Ordinary      an ordinary task still renames for the same users as before.

Run with:
    python manage.py test projects.tests_rename_protection --settings=solarpms.test_settings
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from . import utils
from .models import Project, Task
from .permissions import can_rename_task, reserved_task_names
from .tests_checklist_task_link import _client_for, _profile
from .tests_task_rename import RenameFixture
from .utils import (
    INVOICE_DUE_TRIGGERS, INVOICE_TASK_NAMES, RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL,
    RESIDENTIAL_FINANCE_CONFIRMATION_TASK_NAMES,
)
from .views import (
    _FINANCE_TASK_TO_MILESTONE, _INVOICE_DUE_TRIGGER_TO_MILESTONE, _INVOICE_TASK_TO_MILESTONE,
)
from .views_finance_work import TRIGGER_TASKS

M2_TRIGGER = 'PO for B & C Class Items'
M3_TRIGGER = 'Customer Handover'
SIBLING_NAME_FOR_CONTROL = 'Inverter Installation'   # same OPEX phase as the fixture task
PREVIOUSLY_RESERVED = INVOICE_TASK_NAMES + RESIDENTIAL_FINANCE_CONFIRMATION_TASK_NAMES


def _rename_icon(name):
    """The row's Rename button, as the template renders it (the name autoescaped)."""
    return 'aria-label="Rename ' + name.replace('&', '&amp;') + '"'


def _set_not_applicable(pm, task, marking):
    """Mark or un-mark through the VIEW, the only way production produces either state."""
    response = _client_for(pm).post(
        reverse('task_set_not_applicable', args=[task.phase.project.project_id, task.pk]),
        {'not_applicable': '1' if marking else '0',
         'not_applicable_reason': 'Ruled out of scope' if marking else 'Back in scope'})
    task.refresh_from_db()
    assert task.is_not_applicable == marking, (
        f'fixture failed to {"mark" if marking else "un-mark"} N/A: {response.status_code}')
    return task


# ---------------------------------------------------------------------------
# Residential: the two trigger tasks on a real activated site.
# ---------------------------------------------------------------------------

class ResidentialTriggerTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.pm      = _profile('rn_res_pm', 'PM')
        cls.coord   = _profile('rn_res_coord', 'Project Coordinator')
        cls.design  = _profile('rn_res_design', 'Design')
        cls.finance = _profile('rn_res_fin', 'Finance', email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)

    def setUp(self):
        self.project = Project.objects.create(
            customer_name='Rename Protection Residential', customer_phone='9876543210',
            site_address='1 Guard Road', city='Lucknow', project_type='Residential',
            dc_capacity_kw=Decimal('5.00'), status='Draft', assigned_pm=self.pm,
        )
        response = _client_for(self.pm).post(
            reverse('project_activate', args=[self.project.project_id]),
            {'assigned_design_id': self.design.pk})
        self.assertEqual(response.status_code, 302)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, 'Active')
        self.project.coordinators.add(self.coord)

    def _task(self, name):
        return Task.objects.select_related('phase__project').get(
            phase__project=self.project, task_name=name)

    def _get_panel(self, task, profile=None):
        return _client_for(profile or self.pm).get(
            reverse('task_rename', args=[self.project.project_id, task.pk]),
            HTTP_HX_REQUEST='true')

    def _post(self, task, value, profile=None):
        return _client_for(profile or self.pm).post(
            reverse('task_rename_save', args=[self.project.project_id, task.pk]),
            {'value': value}, HTTP_HX_REQUEST='true')

    def assertNotRenamable(self, task):
        for profile in (self.pm, self.coord):
            with self.subTest(task=task.task_name, user=profile.user.username):
                self.assertFalse(can_rename_task(profile.user, task))
                self.assertEqual(self._get_panel(task, profile).status_code, 404)
                self.assertEqual(self._post(task, 'Something Else', profile).status_code, 404)
                self.assertEqual(Task.objects.get(pk=task.pk).task_name, task.task_name,
                                 'a refused POST changed the name')

    def test_the_two_trigger_tasks_cannot_be_renamed(self):
        for name in (M2_TRIGGER, M3_TRIGGER):
            self.assertNotRenamable(self._task(name))

    def test_the_trigger_tasks_show_no_rename_button(self):
        html = _client_for(self.pm).get(
            reverse('project_overview', args=[self.project.project_id])).content.decode()
        # Positive control: an ordinary row on the same page does carry the button.
        self.assertIn(_rename_icon('Earthing Work'), html)
        for name in (M2_TRIGGER, M3_TRIGGER):
            with self.subTest(name=name):
                self.assertNotIn(_rename_icon(name), html)

    def test_every_previously_reserved_task_is_still_refused(self):
        for name in PREVIOUSLY_RESERVED:
            self.assertNotRenamable(self._task(name))

    def test_a_trigger_task_un_marked_from_not_applicable_is_still_refused(self):
        """Ruling 4: un-marking restores the rename UNLESS the name is reserved."""
        task = _set_not_applicable(self.pm, self._task(M3_TRIGGER), marking=True)
        _set_not_applicable(self.pm, task, marking=False)
        self.assertNotRenamable(task)

    def test_an_ordinary_residential_task_still_renames(self):
        task = self._task('Earthing Work')
        self.assertTrue(can_rename_task(self.pm.user, task))
        self._post(task, 'Earthing & Lightning Arrestor')
        self.assertEqual(Task.objects.get(pk=task.pk).task_name, 'Earthing & Lightning Arrestor')


# ---------------------------------------------------------------------------
# OPEX: renaming TO a trigger name, Not Applicable, ordinary tasks.
# ---------------------------------------------------------------------------

class RenameToTriggerTests(RenameFixture):

    def test_refused_to_either_trigger_name_in_any_case(self):
        self.assertRefusedWith(M2_TRIGGER.lower(), 'finance workflow')
        self.assertRefusedWith(M3_TRIGGER.upper(), 'finance workflow')

    def test_a_task_carrying_a_trigger_name_is_refused_on_any_project_type(self):
        Task.objects.filter(pk=self.task.pk).update(task_name=M3_TRIGGER)
        self.task.refresh_from_db()
        self.assertRefusedBothWays()


class NotApplicableTests(RenameFixture):

    def setUp(self):
        super().setUp()
        self.site.coordinators.add(self.coord)

    def test_a_not_applicable_task_cannot_be_renamed_by_the_pm_or_a_coordinator(self):
        _set_not_applicable(self.pm, self.task, marking=True)
        self.assertFalse(can_rename_task(self.pm.user, self.task))
        self.assertRefusedBothWays()
        self.assertRefusedBothWays(self.coord)

    def test_a_not_applicable_task_shows_no_rename_button(self):
        _set_not_applicable(self.pm, self.task, marking=True)
        html = _client_for(self.pm).get(
            reverse('project_overview', args=[self.site.project_id])).content.decode()
        self.assertIn(_rename_icon(SIBLING_NAME_FOR_CONTROL), html)
        self.assertNotIn(_rename_icon(self.task.task_name), html)

    def test_after_un_marking_it_renames_as_before(self):
        """Ruling 4: the flag was the only reason, so un-marking restores the rename."""
        _set_not_applicable(self.pm, self.task, marking=True)
        _set_not_applicable(self.pm, self.task, marking=False)
        self.assertTrue(can_rename_task(self.pm.user, self.task))
        self._save('Module Installation (Roof)')
        self.assertEqual(Task.objects.get(pk=self.task.pk).task_name, 'Module Installation (Roof)')


class OrdinaryTaskTests(RenameFixture):
    """The same users as before: the owning PM and a coordinator on the project; not
    another PM, an SE, Finance or SCM."""

    def test_the_owning_pm_and_a_coordinator_still_rename(self):
        self.site.coordinators.add(self.coord)
        self._save('Module Installation (Roof)')
        self.assertEqual(Task.objects.get(pk=self.task.pk).task_name, 'Module Installation (Roof)')
        self._save('Module Installation (Ground)', profile=self.coord)
        self.assertEqual(Task.objects.get(pk=self.task.pk).task_name, 'Module Installation (Ground)')

    def test_other_roles_are_still_refused(self):
        for profile in (self.other_pm, self.se, self.finance, self.scm):
            with self.subTest(user=profile.user.username):
                self.assertRefusedBothWays(profile)


# ---------------------------------------------------------------------------
# The reserved list is derived, not copied.
# ---------------------------------------------------------------------------

class DerivedReservedNamesTests(TestCase):

    def test_both_trigger_names_are_reserved(self):
        self.assertIn(M2_TRIGGER.casefold(), reserved_task_names())
        self.assertIn(M3_TRIGGER.casefold(), reserved_task_names())

    def test_every_key_of_every_views_name_map_is_reserved(self):
        """The views' maps cannot be read by permissions (views imports it), so this is
        the tripwire: a name added only in views fails here instead of going renamable."""
        reserved = reserved_task_names()
        maps = {
            '_FINANCE_TASK_TO_MILESTONE':        _FINANCE_TASK_TO_MILESTONE,
            '_INVOICE_TASK_TO_MILESTONE':        _INVOICE_TASK_TO_MILESTONE,
            '_INVOICE_DUE_TRIGGER_TO_MILESTONE': _INVOICE_DUE_TRIGGER_TO_MILESTONE,
            'views_finance_work.TRIGGER_TASKS':  {v: k for k, v in TRIGGER_TASKS.items()},
        }
        for label, mapping in maps.items():
            for name in mapping:
                with self.subTest(map=label, name=name):
                    self.assertIn(name.casefold(), reserved)

    def test_a_new_trigger_entry_is_reserved_with_no_edit_to_the_list(self):
        new = 'Retention Release Trigger'
        self.assertNotIn(new.casefold(), reserved_task_names())
        with patch.dict(utils.INVOICE_DUE_TRIGGERS, {'M9': new}):
            self.assertIn(new.casefold(), reserved_task_names())
        self.assertNotIn(new.casefold(), reserved_task_names())

    def test_a_new_invoice_or_confirmation_name_is_reserved_with_no_edit_to_the_list(self):
        for attr in ('INVOICE_TASK_NAMES', 'RESIDENTIAL_FINANCE_CONFIRMATION_TASK_NAMES'):
            with self.subTest(table=attr):
                new = f'New {attr} Entry'
                with patch.object(utils, attr, getattr(utils, attr) + (new,)):
                    self.assertIn(new.casefold(), reserved_task_names())

    def test_the_gantt_display_names_are_not_reserved(self):
        """Ruling 3: a rename only loses the friendly client label."""
        self.assertNotIn('earthing work', reserved_task_names())
        self.assertNotIn('plant commissioning', reserved_task_names())

    def test_the_list_is_exactly_the_three_tables(self):
        self.assertEqual(reserved_task_names(), {
            n.casefold() for n in
            INVOICE_TASK_NAMES + RESIDENTIAL_FINANCE_CONFIRMATION_TASK_NAMES
            + tuple(INVOICE_DUE_TRIGGERS.values())})
