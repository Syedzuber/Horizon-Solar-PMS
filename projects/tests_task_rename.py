"""
Rename task — a PM or coordinator corrects a task's name; on a location copy, its location.

THE RULES PINNED HERE, each by a test named for it
    Eligibility   permissions.can_rename_task(): task_add's gate (PM / Project
                  Coordinator who manages the project), Active, not soft-deleted, any
                  project type; the task is not Done, not awaiting approval, not a
                  mirror, not a payment milestone, and its name is not reserved.
                  Refused on BOTH the GET panel and the POST; the POST writes nothing.
    Validation    empty, over-length, reserved TO, a case-insensitive duplicate in the
                  phase, and a checklist-link name are refused; unchanged is a no-op.
    Checklist     a task whose checklist is found by name may not be renamed; a task
                  whose checklist is found by template code keeps it after a rename.
    Location      both name formats rebuild; a name without the suffix updates the label
                  only; a label a sibling copy already has is refused.
    Write         exactly the named fields; one ActivityLog row (task_renamed); no
                  notification, no StatusTransition.
    Icon          rendered iff eligible, on project_overview and on the HTMX row.

Run with:
    python manage.py test projects.tests_task_rename --settings=solarpms.test_settings
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.core import mail
from django.template import Context, Template
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    ActivityLog, Checklist, ChecklistItem, ChecklistTaskLink, NotificationLog, Project,
    ProjectPhase, StatusTransition, Task,
)
from .permissions import can_rename_task, reserved_task_names
from .tests_checklist_task_link import _client_for, _profile, _seed_opex
from .utils import (
    INVOICE_TASK_NAMES, RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL,
    RESIDENTIAL_FINANCE_CONFIRMATION_TASK_NAMES,
)
from .views import TASK_NAME_MAX_LENGTH, _FINANCE_TASK_TO_MILESTONE, _checklist_for_task

SOURCE_NAME  = 'Module Installation'       # OPEX Installation phase
SIBLING_NAME = 'Inverter Installation'     # same phase
MIRROR_NAME  = 'Delivery — MMS'
ICON_MARK    = 'aria-label="Rename '


def _logs():
    """ActivityLog rows, less the user_login row every force_login writes."""
    return ActivityLog.objects.exclude(action_code='user_login').count()


class RenameFixture(TestCase):

    @classmethod
    def setUpTestData(cls):
        _seed_opex()
        cls.pm       = _profile('ren_pm', 'PM')
        cls.other_pm = _profile('ren_other_pm', 'PM')
        cls.coord    = _profile('ren_coord', 'Project Coordinator')
        cls.se       = _profile('ren_se', 'Site Engineer')
        cls.finance  = _profile('ren_fin', 'Finance')
        cls.scm      = _profile('ren_scm', 'SCM')

    def setUp(self):
        self.site = self._activate_opex('Rename Site A')
        self.task = self._task(SOURCE_NAME)

    # -- fixture helpers ------------------------------------------------------

    def _activate_opex(self, name):
        site = Project.objects.create(
            customer_name=name, customer_phone='9876543211', site_address='1 Ren Road',
            city='Lucknow', project_type='OPEX', dc_capacity_kw=Decimal('100.00'),
            status='Draft', assigned_pm=self.pm,
        )
        response = _client_for(self.pm).post(reverse('opex_site_activate', args=[site.project_id]))
        self.assertEqual(response.status_code, 302, 'OPEX activation did not redirect')
        site.refresh_from_db()
        self.assertEqual(site.status, 'Active')
        return site

    def _task(self, name, project=None):
        task = Task.objects.filter(phase__project=project or self.site, task_name=name).first()
        self.assertIsNotNone(task, f'no task named {name!r}')
        return task

    def _bare_project(self, project_type, name='Bare'):
        project = Project.objects.create(
            customer_name=name, customer_phone='9876543219', site_address='9 Ren Road',
            city='Lucknow', project_type=project_type, dc_capacity_kw=Decimal('50.00'),
            status='Active', assigned_pm=self.pm,
        )
        phase = ProjectPhase.objects.create(project=project, phase_name='Installation', phase_order=1)
        task = Task.objects.create(phase=phase, task_name='Hand Task', task_order=1,
                                   assigned_role=Task.SITE_ENGINEER)
        return project, task

    def _panel(self, profile=None, task=None):
        task = task or self.task
        return _client_for(profile or self.pm).get(
            reverse('task_rename', args=[task.phase.project.project_id, task.pk]),
            HTTP_HX_REQUEST='true')

    def _save(self, value, profile=None, task=None, hx=True):
        task = task or self.task
        headers = {'HTTP_HX_REQUEST': 'true'} if hx else {}
        return _client_for(profile or self.pm).post(
            reverse('task_rename_save', args=[task.phase.project.project_id, task.pk]),
            {'value': value}, **headers)

    def _snapshot(self, task):
        return Task.objects.filter(pk=task.pk).values().get()

    def assertRefusedBothWays(self, profile=None, task=None):
        task = task or self.task
        before = self._snapshot(task)
        self.assertIn(self._panel(profile, task).status_code, (403, 404))
        self.assertIn(self._save('Something Else', profile, task).status_code, (403, 404))
        self.assertEqual(self._snapshot(task), before, 'a refused POST changed the task')

    def assertRefusedWith(self, value, text, task=None):
        task = task or self.task
        before = self._snapshot(task)
        logs = _logs()
        response = self._save(value, task=task)
        self.assertEqual(response.status_code, 200)
        self.assertIn(text, response.content.decode())
        self.assertEqual(self._snapshot(task), before, 'a refused rename changed the task')
        self.assertEqual(_logs(), logs)


# ---------------------------------------------------------------------------
# 1 — Eligibility, on the GET panel AND the POST.
# ---------------------------------------------------------------------------

class EligibilityTests(RenameFixture):

    def test_the_owning_pm_is_admitted(self):
        self.assertTrue(can_rename_task(self.pm.user, self.task))
        self.assertEqual(self._panel().status_code, 200)

    def test_a_coordinator_on_the_project_is_admitted(self):
        self.site.coordinators.add(self.coord)
        self.assertEqual(self._panel(self.coord).status_code, 200)

    def test_refused_when_done(self):
        Task.objects.filter(pk=self.task.pk).update(status=Task.DONE)
        self.assertRefusedBothWays()

    def test_refused_when_awaiting_approval(self):
        Task.objects.filter(pk=self.task.pk).update(
            status=Task.IN_PROGRESS, submitted_at=timezone.now(), submitted_by=self.se,
            submission_remarks='done')
        self.assertRefusedBothWays()

    def test_refused_for_a_mirror(self):
        self.assertRefusedBothWays(task=self._task(MIRROR_NAME))

    def test_refused_for_a_payment_milestone(self):
        Task.objects.filter(pk=self.task.pk).update(is_payment_milestone=True)
        self.assertRefusedBothWays()

    def test_refused_from_every_reserved_name_in_any_case(self):
        for name in INVOICE_TASK_NAMES + RESIDENTIAL_FINANCE_CONFIRMATION_TASK_NAMES:
            with self.subTest(name=name):
                Task.objects.filter(pk=self.task.pk).update(task_name=name.upper())
                self.assertRefusedBothWays()

    def test_refused_for_an_inactive_project(self):
        Project.objects.filter(pk=self.site.pk).update(status='On Hold')
        self.assertRefusedBothWays()

    def test_refused_for_a_soft_deleted_project(self):
        Project.objects.filter(pk=self.site.pk).update(is_deleted=True)
        self.assertRefusedBothWays()

    def test_refused_for_a_pm_who_does_not_own_the_project(self):
        self.assertRefusedBothWays(self.other_pm)

    def test_refused_for_a_site_engineer(self):
        self.assertRefusedBothWays(self.se)

    def test_refused_for_finance(self):
        self.assertRefusedBothWays(self.finance)

    def test_refused_for_scm(self):
        self.assertRefusedBothWays(self.scm)

    def test_a_get_to_the_save_route_writes_nothing(self):
        before = self._snapshot(self.task)
        _client_for(self.pm).get(reverse('task_rename_save',
                                         args=[self.site.project_id, self.task.pk]))
        self.assertEqual(self._snapshot(self.task), before)


class AllowedTests(RenameFixture):

    def test_allowed_in_each_open_status(self):
        for status in (Task.NOT_STARTED, Task.IN_PROGRESS, Task.BLOCKED):
            with self.subTest(status=status):
                Task.objects.filter(pk=self.task.pk).update(status=status)
                new = f'Module Installation {status}'
                self._save(new)
                self.assertEqual(Task.objects.get(pk=self.task.pk).task_name, new)

    def test_allowed_on_opex(self):
        self._save('Module Installation (Roof)')
        self.assertEqual(Task.objects.get(pk=self.task.pk).task_name, 'Module Installation (Roof)')

    def test_allowed_on_capex(self):
        _, task = self._bare_project('CAPEX')
        self._save('Hand Task Renamed', task=task)
        self.assertEqual(Task.objects.get(pk=task.pk).task_name, 'Hand Task Renamed')

    def test_a_rejected_back_task_can_be_renamed(self):
        """task_reject's end state: In Progress, submission cleared, remarks kept."""
        Task.objects.filter(pk=self.task.pk).update(
            status=Task.IN_PROGRESS, submitted_at=None, submitted_by=None,
            approval_remarks='redo the earthing')
        self.task.refresh_from_db()
        self.assertFalse(self.task.is_awaiting_approval)
        self.assertTrue(can_rename_task(self.pm.user, self.task))
        self._save('Module Installation Redo')
        self.assertEqual(Task.objects.get(pk=self.task.pk).task_name, 'Module Installation Redo')


class ResidentialTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.pm      = _profile('ren_res_pm', 'PM')
        cls.design  = _profile('ren_res_design', 'Design')
        cls.finance = _profile('ren_res_fin', 'Finance', email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)

    def setUp(self):
        self.project = Project.objects.create(
            customer_name='Rename Residential', customer_phone='9876543210',
            site_address='1 Res Road', city='Lucknow', project_type='Residential',
            dc_capacity_kw=Decimal('5.00'), status='Draft', assigned_pm=self.pm,
        )
        response = _client_for(self.pm).post(
            reverse('project_activate', args=[self.project.project_id]),
            {'assigned_design_id': self.design.pk})
        self.assertEqual(response.status_code, 302)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, 'Active')

    def _task(self, name):
        return Task.objects.get(phase__project=self.project, task_name=name)

    def test_allowed_on_residential(self):
        task = self._task('Earthing Work')
        self.assertTrue(can_rename_task(self.pm.user, task))
        _client_for(self.pm).post(reverse('task_rename_save', args=[self.project.project_id, task.pk]),
                                  {'value': 'Earthing & Lightning Arrestor'})
        self.assertEqual(Task.objects.get(pk=task.pk).task_name, 'Earthing & Lightning Arrestor')

    def test_the_finance_confirmation_tasks_are_refused(self):
        for name in RESIDENTIAL_FINANCE_CONFIRMATION_TASK_NAMES + INVOICE_TASK_NAMES:
            with self.subTest(name=name):
                self.assertFalse(can_rename_task(self.pm.user, self._task(name)))

    def test_icon_rendered_iff_eligible_on_residential_overview(self):
        html = _client_for(self.pm).get(
            reverse('project_overview', args=[self.project.project_id])).content.decode()
        eligible = [t for t in Task.objects.filter(phase__project=self.project).select_related('phase__project')
                    if can_rename_task(self.pm.user, t)]
        self.assertGreater(len(eligible), 0)
        self.assertEqual(html.count(ICON_MARK), len(eligible))
        self.assertNotIn('aria-label="Rename Advance Payment Confirmation"', html)


class ReservedNamesTests(TestCase):

    def test_finance_confirmation_names_equal_the_milestone_map_keys(self):
        """The views' sync map and utils' tuple name the same three tasks; the reserved
        list reads the tuple, so a drift would leave a synced name renamable."""
        self.assertEqual(set(RESIDENTIAL_FINANCE_CONFIRMATION_TASK_NAMES),
                         set(_FINANCE_TASK_TO_MILESTONE))

    def test_reserved_names_are_both_lists_casefolded(self):
        self.assertEqual(reserved_task_names(), {
            n.casefold() for n in INVOICE_TASK_NAMES + RESIDENTIAL_FINANCE_CONFIRMATION_TASK_NAMES})


# ---------------------------------------------------------------------------
# 2 — Validation.
# ---------------------------------------------------------------------------

class ValidationTests(RenameFixture):

    def test_refused_to_a_reserved_name_in_any_case(self):
        self.assertRefusedWith('advance payment   CONFIRMATION', 'finance workflow')
        self.assertRefusedWith('Send Invoice - Final Payment', 'finance workflow')

    def test_refused_to_a_duplicate_in_the_phase_case_insensitively(self):
        self.assertRefusedWith(SIBLING_NAME.lower(), 'already exists in this phase')

    def test_a_same_name_in_another_phase_is_allowed(self):
        other = Task.objects.filter(phase__project=self.site).exclude(phase=self.task.phase).first()
        self._save(other.task_name)
        self.assertEqual(Task.objects.get(pk=self.task.pk).task_name, other.task_name)

    def test_refused_empty(self):
        self.assertRefusedWith('   ', 'Enter a task name.')

    def test_refused_over_max_length(self):
        self.assertRefusedWith('x' * (TASK_NAME_MAX_LENGTH + 1), 'longer than a task name')

    def test_exactly_max_length_is_allowed(self):
        self._save('y' * TASK_NAME_MAX_LENGTH)
        self.assertEqual(len(Task.objects.get(pk=self.task.pk).task_name), TASK_NAME_MAX_LENGTH)

    def test_whitespace_is_trimmed_and_collapsed(self):
        self._save('  Module    Installation   Roof  ')
        self.assertEqual(Task.objects.get(pk=self.task.pk).task_name, 'Module Installation Roof')

    def test_a_case_only_change_is_a_rename(self):
        self._save(SOURCE_NAME.upper())
        self.assertEqual(Task.objects.get(pk=self.task.pk).task_name, SOURCE_NAME.upper())

    def test_unchanged_is_a_no_op_with_a_message(self):
        before = self._snapshot(self.task)
        logs = _logs()
        response = self._save(f'  {SOURCE_NAME} ')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Nothing changed', response.content.decode())
        self.assertEqual(response['HX-Trigger'], 'taskFormDone')
        self.assertEqual(self._snapshot(self.task), before)
        self.assertEqual(_logs(), logs)

    def test_a_non_htmx_refusal_redirects_with_the_message(self):
        response = self._save('', hx=False)
        self.assertRedirects(response, reverse('project_overview', args=[self.site.project_id]),
                             fetch_redirect_response=False)


# ---------------------------------------------------------------------------
# 3 — Checklist identity.
# ---------------------------------------------------------------------------

class ChecklistTests(RenameFixture):

    def _checklist(self, name='QMS'):
        checklist = Checklist.objects.create(name=name, created_by=self.pm.user)
        ChecklistItem.objects.create(checklist=checklist, label='Torque checked', order=1)
        checklist.activate()
        return checklist

    def test_a_template_linked_task_keeps_its_checklist_after_a_rename(self):
        checklist = self._checklist()
        ChecklistTaskLink.objects.create(checklist=checklist, template_task=self.task.template_task)
        self.assertEqual(_checklist_for_task(self.task, self.site), checklist)
        self._save('Module Installation (Block B)')
        renamed = Task.objects.get(pk=self.task.pk)
        self.assertEqual(renamed.task_name, 'Module Installation (Block B)')
        self.assertEqual(_checklist_for_task(renamed, self.site), checklist)

    def test_a_task_whose_checklist_is_found_by_name_is_refused(self):
        project, task = self._bare_project('OPEX', 'Name Path')
        ChecklistTaskLink.objects.create(checklist=self._checklist(), task_name='Hand Task',
                                         project_type='OPEX')
        panel = self._panel(task=task).content.decode()
        self.assertIn('attached through its name', panel)
        self.assertNotIn('name="value"', panel)
        before = self._snapshot(task)
        response = self._save('Hand Task Two', task=task)
        self.assertIn('attached through its name', response.content.decode())
        self.assertEqual(self._snapshot(task), before)

    def test_a_task_without_template_may_not_take_a_linked_name(self):
        project, task = self._bare_project('OPEX', 'To Link')
        ChecklistTaskLink.objects.create(checklist=self._checklist(), task_name='Legacy QMS Task',
                                         project_type='OPEX')
        self.assertRefusedWith('legacy qms task',
                               'This name is used to attach a checklist. Choose a different name.',
                               task=task)

    def test_a_template_task_with_no_code_link_may_not_take_a_linked_name(self):
        ChecklistTaskLink.objects.create(checklist=self._checklist(), task_name='Legacy QMS Task',
                                         project_type='OPEX')
        self.assertRefusedWith('Legacy QMS Task', 'used to attach a checklist')

    def test_a_task_found_by_code_may_take_a_linked_name(self):
        ChecklistTaskLink.objects.create(checklist=self._checklist('A'),
                                         template_task=self.task.template_task)
        ChecklistTaskLink.objects.create(checklist=self._checklist('B'), task_name='Legacy QMS Task',
                                         project_type='OPEX')
        self._save('Legacy QMS Task')
        self.assertEqual(Task.objects.get(pk=self.task.pk).task_name, 'Legacy QMS Task')

    def test_a_linked_name_of_another_project_type_does_not_refuse(self):
        project, task = self._bare_project('OPEX', 'Other Type')
        ChecklistTaskLink.objects.create(checklist=self._checklist(), task_name='Legacy QMS Task',
                                         project_type='CAPEX')
        self._save('Legacy QMS Task', task=task)
        self.assertEqual(Task.objects.get(pk=task.pk).task_name, 'Legacy QMS Task')


# ---------------------------------------------------------------------------
# 4 — Location copies.
# ---------------------------------------------------------------------------

class LocationTests(RenameFixture):

    def _copy(self, name, label, template_task=True):
        return Task.objects.create(
            phase=self.task.phase, task_name=name, task_order=99, location_label=label,
            template_task=self.task.template_task if template_task else None,
            assigned_role=self.task.assigned_role)

    def test_the_panel_edits_the_location_and_previews_the_name(self):
        copy = self._copy(f'{SOURCE_NAME} — Block A', 'Block A')
        html = self._panel(task=copy).content.decode()
        self.assertIn('Change location', html)
        self.assertIn('>Location<', html)
        self.assertIn('value="Block A"', html)
        self.assertIn(f'{SOURCE_NAME} — Block A', html)

    def test_the_dash_format_rebuilds(self):
        copy = self._copy(f'{SOURCE_NAME} — Block A', 'Block A')
        self._save('Block  B ', task=copy)
        copy.refresh_from_db()
        self.assertEqual((copy.task_name, copy.location_label), (f'{SOURCE_NAME} — Block B', 'Block B'))

    def test_the_space_format_rebuilds(self):
        copy = self._copy(f'{SOURCE_NAME} Food Court', 'Food Court')
        self._save('Food Court 2', task=copy)
        copy.refresh_from_db()
        self.assertEqual((copy.task_name, copy.location_label), (f'{SOURCE_NAME} Food Court 2', 'Food Court 2'))

    def test_a_name_without_the_suffix_updates_the_label_only_and_says_so(self):
        copy = self._copy('Roof panels, east side', 'Block A')
        response = self._save('Block C', task=copy)
        copy.refresh_from_db()
        self.assertEqual((copy.task_name, copy.location_label), ('Roof panels, east side', 'Block C'))
        self.assertIn('does not end with the old location', response.content.decode())

    def test_a_label_a_sibling_copy_has_is_refused(self):
        self._copy(f'{SOURCE_NAME} — Block B', 'Block B')
        copy = self._copy(f'{SOURCE_NAME} — Block A', 'Block A')
        self.assertRefusedWith('block b', 'already exists at', task=copy)

    def test_empty_location_is_refused(self):
        copy = self._copy(f'{SOURCE_NAME} — Block A', 'Block A')
        self.assertRefusedWith(' ', 'Enter a location.', task=copy)

    def test_an_over_long_location_is_refused(self):
        copy = self._copy(f'{SOURCE_NAME} — Block A', 'Block A')
        self.assertRefusedWith('z' * 101, 'at most 100', task=copy)

    def test_an_unchanged_location_is_a_no_op(self):
        copy = self._copy(f'{SOURCE_NAME} — Block A', 'Block A')
        before = self._snapshot(copy)
        response = self._save('Block A', task=copy)
        self.assertIn('Nothing changed: the location is the same.', response.content.decode())
        self.assertEqual(self._snapshot(copy), before)

    def test_the_location_log_text(self):
        copy = self._copy(f'{SOURCE_NAME} — Block A', 'Block A')
        self._save('Block D', task=copy)
        log = ActivityLog.objects.get(action_code='task_renamed', entity_id=copy.pk)
        self.assertEqual(log.action, "Location changed from 'Block A' to 'Block D'")


# ---------------------------------------------------------------------------
# 5 — What the write touches.
# ---------------------------------------------------------------------------

class WriteTests(RenameFixture):

    def test_exactly_one_log_no_notification_no_transition_no_other_field(self):
        before = self._snapshot(self.task)
        logs, notes = _logs(), NotificationLog.objects.count()
        transitions = StatusTransition.objects.count()
        response = self._save('Module Installation (Roof)')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(_logs(), logs + 1)
        log = ActivityLog.objects.latest('pk')
        self.assertEqual(log.action_code, 'task_renamed')
        self.assertEqual(log.action, f"Renamed from '{SOURCE_NAME}' to 'Module Installation (Roof)'")
        self.assertEqual((log.entity_type, log.entity_id), ('Task', self.task.pk))
        self.assertEqual(NotificationLog.objects.count(), notes)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(StatusTransition.objects.count(), transitions)
        after = self._snapshot(self.task)
        self.assertEqual({k for k in before if before[k] != after[k]}, {'task_name'})

    def test_the_hx_response_swaps_the_row_and_closes_the_modal(self):
        response = self._save('Module Installation (Roof)')
        self.assertEqual(response['HX-Retarget'], f'#task-row-{self.task.pk}')
        self.assertEqual(response['HX-Reswap'], 'outerHTML')
        self.assertEqual(response['HX-Trigger'], 'taskFormDone')
        html = response.content.decode()
        self.assertIn(f'id="task-row-{self.task.pk}"', html)
        self.assertIn('Renamed to', html)

    def test_a_non_htmx_save_redirects(self):
        response = self._save('Module Installation (Roof)', hx=False)
        self.assertRedirects(response, reverse('project_overview', args=[self.site.project_id]),
                             fetch_redirect_response=False)


# ---------------------------------------------------------------------------
# 6 — The icon.
# ---------------------------------------------------------------------------

class IconTests(RenameFixture):

    def _overview(self, profile):
        return _client_for(profile).get(
            reverse('project_overview', args=[self.site.project_id])).content.decode()

    def _eligible(self, profile):
        return [t for t in Task.objects.filter(phase__project=self.site).select_related('phase__project')
                if can_rename_task(profile.user, t)]

    def test_icon_rendered_iff_eligible_on_the_overview(self):
        Task.objects.filter(pk=self.task.pk).update(status=Task.DONE)
        html = self._overview(self.pm)
        eligible = self._eligible(self.pm)
        self.assertGreater(len(eligible), 0)
        self.assertEqual(html.count(ICON_MARK), len(eligible))
        self.assertIn(f'aria-label="Rename {SIBLING_NAME}"', html)
        self.assertNotIn(f'aria-label="Rename {SOURCE_NAME}"', html)
        self.assertNotIn(f'aria-label="Rename {MIRROR_NAME}"', html)
        self.assertIn('>Rename</span>', html, 'the tooltip text is missing')

    def test_no_icon_for_a_viewer_who_may_not_rename(self):
        self.assertEqual(self._overview(self.se).count(ICON_MARK), 0)

    def test_the_rename_row_rerender_carries_the_icon(self):
        html = self._save('Module Installation (Roof)').content.decode()
        self.assertIn('aria-label="Rename Module Installation (Roof)"', html)

    def test_each_htmx_row_responder_carries_the_icon(self):
        """task_assign and the status swap re-render through _render_task_row_hx too."""
        client = _client_for(self.pm)
        html = client.post(reverse('task_assign', args=[self.site.project_id, self.task.pk]),
                           {'assigned_to': self.se.pk}, HTTP_HX_REQUEST='true').content.decode()
        self.assertIn(f'aria-label="Rename {SOURCE_NAME}"', html)
        html = client.post(reverse('task_set_due_date', args=[self.site.project_id, self.task.pk]),
                           {'due_date': timezone.localdate().isoformat()},
                           HTTP_HX_REQUEST='true').content.decode()
        self.assertIn(f'aria-label="Rename {SOURCE_NAME}"', html)

    def test_the_status_swap_row_carries_the_icon(self):
        Task.objects.filter(pk=self.task.pk).update(assigned_to=self.se, due_date=timezone.localdate())
        html = _client_for(self.pm).post(
            reverse('task_status_update', args=[self.site.project_id, self.task.pk]),
            {'status': Task.IN_PROGRESS}, HTTP_HX_REQUEST='true').content.decode()
        self.assertEqual(Task.objects.get(pk=self.task.pk).status, Task.IN_PROGRESS)
        self.assertIn(f'aria-label="Rename {SOURCE_NAME}"', html)

    def test_the_project_half_is_asked_once_per_request_not_per_row(self):
        tasks = list(Task.objects.filter(phase__project=self.site).select_related('phase'))
        template = Template('{% load duplicate_tags %}{% for t in tasks %}'
                            '{% if t|can_rename:user %}x{% endif %}{% endfor %}')
        with patch('projects.permissions.user_can_manage_project', return_value=True) as manage:
            template.render(Context({'tasks': tasks, 'user': self.pm.user}))
        self.assertEqual(manage.call_count, 1)

    def test_the_filter_refuses_an_anonymous_user(self):
        template = Template('{% load duplicate_tags %}{% if t|can_rename:user %}x{% endif %}')
        self.assertEqual(template.render(Context({'t': self.task, 'user': AnonymousUser()})), '')
