"""
Soft-delete correctness — prompt 0.2c.

WHY THIS FILE EXISTS
--------------------
`project_delete` sets `is_deleted=True` and `deleted_at` and **leaves `status`
untouched**. A soft-deleted project therefore keeps `status='Draft'` or `'Active'` and
still satisfies every status-based precondition in the codebase. There are no custom
model managers (`docs/execution-model.md` §6), so nothing filters it for us: before 0.2c,
55 of 72 object resolutions did not carry the filter, and 42 of those were writes.

Three of those writes are why this is more than tidiness, and each has a test below:

  * `project_activate` checks only `status == 'Draft'`, so it would activate a DELETED
    draft — 52 tasks, 3 milestones and six tasks back-assigned to the Finance user, on a
    record the Admin believes is gone. (The audit adds "and fires an assignment
    notification"; it does not — see the correction on
    test_activate_on_a_deleted_draft_back_assigns_nothing_to_finance.)
  * `enable_cascade_scheduling` would set a flag documented as irreversible.
  * The Supabase upload paths would store real objects against a deleted project, which
    `purge_deleted_files` has no reason to look for and would never clean up.

THE POSITIVE CONTROL IS HALF THE FILE
-------------------------------------
Every refusal test below has a twin that drives the SAME endpoint against a LIVE project
and asserts it still works. A filter applied slightly wrong — on the wrong side of a
join, or with the sense inverted — blocks everything, and a suite that only checks
refusals would report that as success. `LiveProjectStillWorksTests` is the half of this
file that would fail if 0.2c over-filtered.

WHAT IS DELIBERATELY NOT TESTED
-------------------------------
`purge_deleted_files` and `project_delete` itself must still see deleted rows — finding
them is the entire purpose of the former. Neither was touched by 0.2c and neither is
pinned here.

Run with:
    python manage.py test projects.tests_soft_delete --settings=solarpms.test_settings

See ACCESS_ISOLATION_AUDIT.md §G for the resolution inventory this file closes out.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from .models import (
    BOQItemMaster, Checklist, ChecklistItem, ChecklistItemCompletion,
    ChecklistTaskLink, DCLineItem, DeliveryChallan, Issue, PaymentMilestone,
    Project, ProjectDocument, SystemSettings, Task, TaskAttachment, UserProfile,
)
from .utils import RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL, assign_tasks_to


# ---------------------------------------------------------------------------
# fixture helpers — same shapes as tests_residential_baseline.py
# ---------------------------------------------------------------------------

def _profile(username, role, email=''):
    """Create a User and set its auto-created UserProfile's role.

    A post_save signal on User creates the UserProfile, so we fetch and mutate rather
    than creating a second one.
    """
    user = User.objects.create_user(username=username, password='pw12345', email=email)
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.save()
    return profile


def _client_for(profile):
    """A logged-in Client for this profile."""
    client = Client()
    client.force_login(profile.user)
    return client


def _photo(name='site.jpg'):
    """A JPEG upload that passes the real _validate_and_upload() checks."""
    return SimpleUploadedFile(name, b'\xff\xd8\xff\xe0fake-jpeg-bytes',
                              content_type='image/jpeg')


def _pdf(name='handover.pdf'):
    return SimpleUploadedFile(name, b'%PDF-1.4 fake', content_type='application/pdf')


class SoftDeleteBase(TestCase):
    """One activated Residential project and one Draft, plus their full cast.

    Both are LIVE in setUp. Each test deletes what it needs to, so the positive-control
    class can use the identical fixture without a second setup path — the only
    difference between a refusal test and its twin is the `_delete()` call.
    """

    def setUp(self):
        # The account without which no Residential project can be activated:
        # attach_residential_template() raises and rolls the whole activation back.
        self.finance = _profile('fin_sd', 'Finance',
                                email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)

        self.pm      = _profile('pm_sd',    'PM')
        self.se      = _profile('se_sd',    'Site Engineer')
        self.design  = _profile('design_sd', 'Design')
        self.scm     = _profile('scm_sd',   'SCM')
        self.admin   = _profile('admin_sd', 'Admin')

        # Migrations are disabled under test_settings, so the data migration that seeds
        # the 37 production catalogue rows never runs. Three rows are enough here.
        for order, (code, desc, cat, unit) in enumerate([
            ('ITM-001', 'Solar Module 540Wp',        'Solar Modules', 'Nos'),
            ('ITM-002', 'Module Mounting Structure', 'Structure',     'Nos'),
            ('ITM-003', 'String Inverter 5kW',       'Inverter',      'Nos'),
        ], start=1):
            BOQItemMaster.objects.create(
                code=code, description=desc, category=cat, unit=unit,
                project_type='Residential', is_active=True, sort_order=order,
            )

        self.project = self._make_project('Deleted Residence')
        self._activate(self.project)

        # A second project left in Draft, because project_activate is the highest
        # severity path and can only be exercised against a project that never left it.
        self.draft = self._make_project('Draft Residence')

    # -- fixture helpers -----------------------------------------------------

    def _make_project(self, customer_name):
        return Project.objects.create(
            customer_name=customer_name,
            customer_phone='9876543210',
            site_address='1 Sun Road',
            city='Lucknow',
            project_type='Residential',
            capacity_kw=Decimal('5.00'),
            contract_value=Decimal('300000.00'),
            status='Draft',
            assigned_pm=self.pm,
            target_commissioning_date=date.today() + timedelta(days=90),
        )

    def _activate(self, project):
        """Activate through the real view, then hand the SE their tasks.

        Activation leaves every Site Engineer task unassigned (utils.py), and
        user_can_view_project()'s Site Engineer branch keys on holding a task — so
        without this the SE has no relationship to the project and the GRN and
        checklist tests would 404 for the RIGHT reason and prove nothing.
        """
        response = _client_for(self.pm).post(
            reverse('project_activate', args=[project.project_id]),
            {'assigned_design_id': self.design.pk},
        )
        self.assertEqual(response.status_code, 302, 'activation did not redirect')
        project.refresh_from_db()
        self.assertEqual(project.status, 'Active',
                         'fixture did not activate — is the Finance assignee present?')
        assign_tasks_to(
            Task.objects.filter(phase__project=project,
                                assigned_role=Task.SITE_ENGINEER),
            self.se,
        )
        return project

    def _delete(self, project):
        """Soft-delete through the real Admin view, not by setting the flag directly.

        Driving the real endpoint is the point: it is what proves `status` survives
        deletion, which is the whole reason every filter in 0.2c was needed.
        """
        response = _client_for(self.admin).post(
            reverse('project_delete', args=[project.project_id]))
        self.assertEqual(response.status_code, 302)
        project.refresh_from_db()
        self.assertTrue(project.is_deleted)
        return project

    def _task(self, project, task_name):
        return Task.objects.get(phase__project=project, task_name=task_name)

    def _make_dc(self, project, dc_number='DC-001'):
        challan = DeliveryChallan.objects.create(
            project=project, dc_number=dc_number, dc_date=date.today(),
            status=DeliveryChallan.EXPECTED, created_by=self.scm,
        )
        DCLineItem.objects.create(
            challan=challan, boq_category='Solar Modules',
            item_description='Solar Module 540Wp',
            ordered_quantity=Decimal('10.00'), unit='Nos',
        )
        return challan

    def _make_checklist(self, task):
        # Draft, then items, then activate() — R-7 refuses an item added to an active
        # version, so the fixture has to build one the way the product does.
        checklist = Checklist.objects.create(name='Pre-commissioning')
        item = ChecklistItem.objects.create(
            checklist=checklist, label='Earth resistance < 5 ohm', order=1)
        checklist.activate()
        ChecklistTaskLink.objects.create(
            checklist=checklist, task_name=task.task_name, project_type='Residential')
        return item

    def _issue_in_progress(self, project, title='Inverter arrived scratched'):
        """Raise an issue and move it to In Progress — resolve_issue's precondition."""
        client = _client_for(self.pm)
        client.post(reverse('create_project_issue', args=[project.project_id]),
                    {'title': title, 'description': 'noted at GRN',
                     'severity': Issue.HIGH, 'assigned_to': str(self.se.pk)})
        issue = Issue.objects.get(project=project, title=title)
        client.post(reverse('update_issue_status', args=[issue.pk]))
        issue.refresh_from_db()
        self.assertEqual(issue.status, Issue.IN_PROGRESS,
                         'fixture issue is not In Progress; resolve would no-op anyway')
        return issue


# ---------------------------------------------------------------------------
# 1 — deletion does not change status. Everything else follows from this.
# ---------------------------------------------------------------------------

class DeletionLeavesStatusAloneTests(SoftDeleteBase):
    """The premise the whole prompt rests on, asserted rather than assumed.

    If a future session ever makes project_delete write a terminal status, these two
    tests fail and the filters below become belt-and-braces rather than load-bearing —
    which is worth knowing loudly, not silently.
    """

    def test_a_deleted_active_project_keeps_status_active(self):
        self._delete(self.project)
        self.assertEqual(self.project.status, 'Active')
        self.assertIsNotNone(self.project.deleted_at)

    def test_a_deleted_draft_project_keeps_status_draft(self):
        self._delete(self.draft)
        self.assertEqual(self.draft.status, 'Draft')


# ---------------------------------------------------------------------------
# 2 — the three named high-severity write paths
# ---------------------------------------------------------------------------

class DeletedProjectWriteRefusalTests(SoftDeleteBase):
    """Each of these mutated a soft-deleted project before 0.2c."""

    def test_activate_refuses_a_deleted_draft_and_seeds_nothing(self):
        """The highest-severity path in the audit.

        `status` is untouched by deletion, so this Draft still satisfies
        project_activate's only precondition. Asserting the 404 alone would be weak —
        the point is that no phases, tasks or milestones exist afterwards.
        """
        self._delete(self.draft)

        response = _client_for(self.pm).post(
            reverse('project_activate', args=[self.draft.project_id]),
            {'assigned_design_id': self.design.pk},
        )
        self.assertEqual(response.status_code, 404)

        self.draft.refresh_from_db()
        self.assertEqual(self.draft.status, 'Draft', 'a deleted project was activated')
        self.assertIsNone(self.draft.activated_at)
        self.assertEqual(self.draft.phases.count(), 0)
        self.assertEqual(Task.objects.filter(phase__project=self.draft).count(), 0)
        self.assertEqual(
            PaymentMilestone.objects.filter(project=self.draft).count(), 0)

    def test_activate_on_a_deleted_draft_back_assigns_nothing_to_finance(self):
        """Activation back-assigns six named tasks to the Finance assignee.

        CORRECTION TO ACCESS_ISOLATION_AUDIT.md §G.2, recorded here because the test it
        implies cannot be written: the audit says activation "fires an assignment
        notification to the Finance assignee". It does not. Activation reaches the
        Finance user through `assign_tasks_to()`, which is documented as "always silent,
        and there is no notify parameter by design" (utils.py) -- so a
        send_notification() assertion here would pass against the UNFIXED code too and
        prove nothing. The observable effect is the back-assignment itself, so that is
        what this asserts.
        """
        self._delete(self.draft)
        _client_for(self.pm).post(
            reverse('project_activate', args=[self.draft.project_id]),
            {'assigned_design_id': self.design.pk},
        )
        self.assertEqual(
            Task.objects.filter(assigned_to=self.finance,
                                phase__project=self.draft).count(), 0,
            'a deleted project back-assigned tasks to the Finance assignee',
        )
        self.draft.refresh_from_db()
        self.assertIsNone(self.draft.assigned_design,
                          'a deleted project was stamped with a designer')

    def test_enable_cascade_scheduling_refuses_a_deleted_project(self):
        """The flag is documented as irreversible, so writing it to a deleted project
        is not recoverable by un-deleting."""
        settings_obj = SystemSettings.get()
        settings_obj.cascade_scheduling_enabled = True
        settings_obj.save()

        self._delete(self.project)
        response = _client_for(self.pm).post(
            reverse('enable_cascade_scheduling', args=[self.project.project_id]))
        self.assertEqual(response.status_code, 404)

        self.project.refresh_from_db()
        self.assertFalse(self.project.cascade_scheduling,
                         'an irreversible flag was set on a deleted project')

    def test_upload_project_document_refuses_a_deleted_project(self):
        """A stored Supabase object that purge_deleted_files would never queue."""
        self._delete(self.project)
        with patch('projects.supabase_storage.get_supabase_client',
                   return_value=MagicMock()) as supabase:
            response = _client_for(self.pm).post(
                reverse('upload_project_document', args=[self.project.project_id]),
                {'files': [_pdf('handover.pdf')]},
            )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(ProjectDocument.objects.count(), 0)
        # The 404 must land BEFORE the storage client is reached — otherwise the row is
        # refused but the object is already in the bucket.
        supabase.assert_not_called()

    def test_upload_task_attachment_refuses_a_deleted_project(self):
        task = self._task(self.project, 'AC Cable Work')
        self._delete(self.project)
        with patch('projects.supabase_storage.get_supabase_client',
                   return_value=MagicMock()) as supabase:
            response = _client_for(self.se).post(
                reverse('upload_task_attachment',
                        args=[self.project.project_id, task.pk]),
                {'files': [_photo('roof.jpg')]},
            )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(TaskAttachment.objects.count(), 0)
        supabase.assert_not_called()

    def test_checklist_item_complete_refuses_a_deleted_project(self):
        task = self._task(self.project, 'Pre Commissioning Check List')
        item = self._make_checklist(task)
        self._delete(self.project)

        with patch('projects.supabase_storage.get_supabase_client',
                   return_value=MagicMock()):
            response = _client_for(self.se).post(
                reverse('checklist_item_complete',
                        args=[self.project.project_id, task.pk, item.pk]),
                {'photo': _photo()},
            )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(ChecklistItemCompletion.objects.count(), 0)


# ---------------------------------------------------------------------------
# 3 — the children: Issue and DeliveryChallan carry no is_deleted of their own
# ---------------------------------------------------------------------------

class DeletedProjectChildRefusalTests(SoftDeleteBase):
    """Issue and DeliveryChallan have no is_deleted field, and deletion is soft so the
    CASCADE on their project FK never fires. The filter has to sit on the parent."""

    def test_resolve_issue_refuses_an_issue_on_a_deleted_project(self):
        issue = self._issue_in_progress(self.project)
        self._delete(self.project)

        response = _client_for(self.pm).post(
            reverse('resolve_issue', args=[issue.pk]),
            {'resolution_note': 'Sorted on site.'},
        )
        self.assertEqual(response.status_code, 404)

        issue.refresh_from_db()
        self.assertEqual(issue.status, Issue.IN_PROGRESS)
        self.assertIsNone(issue.resolved_at)

    def test_resolving_an_issue_on_a_deleted_project_sends_no_notification(self):
        """The escaping half of the defect, and the reason this is not tidiness.

        resolve_issue sends WhatsApp and email. Before 0.2c it would notify managers,
        the assignee and the raiser about an issue on a project deleted months ago.
        """
        issue = self._issue_in_progress(self.project)
        self._delete(self.project)

        with patch('projects.views.send_notification') as sender:
            _client_for(self.pm).post(
                reverse('resolve_issue', args=[issue.pk]),
                {'resolution_note': 'Sorted on site.'},
            )
        sender.assert_not_called()

    def test_the_other_issue_write_endpoints_refuse_a_deleted_project(self):
        """All six bare Issue resolutions now route through _issue_base_qs()."""
        issue = self._issue_in_progress(self.project)
        self._delete(self.project)
        client = _client_for(self.pm)

        for url_name, payload in [
            ('update_issue_status', {}),
            ('close_issue',         {}),
            ('reopen_issue',        {}),
            ('assign_issue',        {'assigned_to': str(self.se.pk)}),
            ('create_issue_comment', {'comment_text': 'still broken'}),
        ]:
            with self.subTest(endpoint=url_name):
                response = client.post(reverse(url_name, args=[issue.pk]), payload)
                self.assertEqual(response.status_code, 404)

        issue.refresh_from_db()
        self.assertEqual(issue.status, Issue.IN_PROGRESS)

    def test_confirm_grn_refuses_a_challan_on_a_deleted_project(self):
        challan = self._make_dc(self.project)
        line = challan.line_items.first()
        self._delete(self.project)

        response = _client_for(self.se).post(
            reverse('confirm_grn', args=[self.project.project_id, challan.pk]),
            {f'received_qty_{line.pk}': '10', f'damaged_qty_{line.pk}': '0'},
        )
        self.assertEqual(response.status_code, 404)

        line.refresh_from_db()
        challan.refresh_from_db()
        self.assertIsNone(line.received_quantity)
        self.assertEqual(challan.status, DeliveryChallan.EXPECTED)


# ---------------------------------------------------------------------------
# 4 — reads
# ---------------------------------------------------------------------------

class DeletedProjectReadRefusalTests(SoftDeleteBase):
    """A deleted project must not be readable either — the pages carry customer name,
    address, contract value and the whole task list."""

    def test_project_overview_returns_404(self):
        self._delete(self.project)
        response = _client_for(self.pm).get(
            reverse('project_overview', args=[self.project.project_id]))
        self.assertEqual(response.status_code, 404)

    def test_task_detail_returns_404(self):
        task = self._task(self.project, 'AC Cable Work')
        self._delete(self.project)
        response = _client_for(self.se).get(
            reverse('task_detail', args=[self.project.project_id, task.pk]))
        self.assertEqual(response.status_code, 404)

    def test_project_timeline_returns_404(self):
        self._delete(self.project)
        response = _client_for(self.pm).get(
            reverse('project_timeline', args=[self.project.project_id]))
        self.assertEqual(response.status_code, 404)

    def test_delivery_challan_detail_returns_404(self):
        challan = self._make_dc(self.project)
        self._delete(self.project)
        response = _client_for(self.se).get(
            reverse('delivery_challan_detail',
                    args=[self.project.project_id, challan.pk]))
        self.assertEqual(response.status_code, 404)

    def test_my_documents_stops_listing_a_deleted_projects_files(self):
        """my_documents resolves nothing by project_id — it lists by uploader, so the
        filter had to go on each queryset's parent traversal instead."""
        with patch('projects.supabase_storage.get_supabase_client',
                   return_value=MagicMock()):
            _client_for(self.pm).post(
                reverse('upload_project_document', args=[self.project.project_id]),
                {'files': [_pdf('handover.pdf')]},
            )
        self.assertEqual(ProjectDocument.objects.filter(is_deleted=False).count(), 1)

        self._delete(self.project)
        response = _client_for(self.pm).get(reverse('my_documents'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['project_docs']), [],
                         'my_documents still lists a deleted project\'s documents')


# ---------------------------------------------------------------------------
# 5 — THE POSITIVE CONTROL. Half the file, and the half that matters most.
# ---------------------------------------------------------------------------

class LiveProjectStillWorksTests(SoftDeleteBase):
    """Every path above, driven against a LIVE project by someone with a real
    relationship to it.

    A filter on the wrong side of a join, or with its sense inverted, refuses
    everything — and a suite of refusal tests would pass on that and call it a fix.
    These are the tests that fail if 0.2c over-filtered.
    """

    def test_a_live_draft_still_activates(self):
        self._activate(self.draft)
        self.assertEqual(self.draft.status, 'Active')
        self.assertEqual(self.draft.phases.count(), 9)
        self.assertEqual(Task.objects.filter(phase__project=self.draft).count(), 52)
        self.assertEqual(PaymentMilestone.objects.filter(project=self.draft).count(), 3)

    def test_a_live_project_still_takes_cascade_scheduling(self):
        settings_obj = SystemSettings.get()
        settings_obj.cascade_scheduling_enabled = True
        settings_obj.save()

        response = _client_for(self.pm).post(
            reverse('enable_cascade_scheduling', args=[self.project.project_id]))
        self.assertEqual(response.status_code, 302)
        self.project.refresh_from_db()
        self.assertTrue(self.project.cascade_scheduling)

    def test_a_live_project_still_takes_a_document_upload(self):
        with patch('projects.supabase_storage.get_supabase_client',
                   return_value=MagicMock()):
            response = _client_for(self.pm).post(
                reverse('upload_project_document', args=[self.project.project_id]),
                {'files': [_pdf('handover.pdf')]},
            )
        self.assertEqual(response.status_code, 302)
        doc = ProjectDocument.objects.get(project=self.project)
        self.assertEqual(doc.uploaded_by, self.pm)
        self.assertFalse(doc.is_deleted)

    def test_a_live_project_still_takes_a_task_attachment(self):
        task = self._task(self.project, 'AC Cable Work')
        with patch('projects.supabase_storage.get_supabase_client',
                   return_value=MagicMock()):
            response = _client_for(self.se).post(
                reverse('upload_task_attachment',
                        args=[self.project.project_id, task.pk]),
                {'files': [_photo('roof.jpg')]},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(TaskAttachment.objects.filter(task=task).count(), 1)

    def test_a_live_project_still_takes_a_checklist_completion(self):
        task = self._task(self.project, 'Pre Commissioning Check List')
        item = self._make_checklist(task)
        with patch('projects.supabase_storage.get_supabase_client',
                   return_value=MagicMock()):
            response = _client_for(self.se).post(
                reverse('checklist_item_complete',
                        args=[self.project.project_id, task.pk, item.pk]),
                {'photo': _photo()},
            )
        self.assertEqual(response.status_code, 302)
        completion = ChecklistItemCompletion.objects.get(item=item, task=task)
        self.assertTrue(completion.is_checked)

    def test_a_live_issue_still_resolves_and_still_notifies(self):
        """Both halves. The refusal twin asserts no notification goes out; if the filter
        were inverted this would silently stop notifying and no other test would notice.
        """
        issue = self._issue_in_progress(self.project)
        with patch('projects.views.send_notification') as sender:
            response = _client_for(self.pm).post(
                reverse('resolve_issue', args=[issue.pk]),
                {'resolution_note': 'Sorted on site.'},
            )
        self.assertEqual(response.status_code, 302)
        issue.refresh_from_db()
        self.assertEqual(issue.status, Issue.RESOLVED)
        self.assertEqual(issue.resolution_note, 'Sorted on site.')
        self.assertTrue(sender.call_args_list,
                        'resolving a live issue stopped notifying anyone')

    def test_a_live_challan_still_takes_a_grn(self):
        challan = self._make_dc(self.project)
        line = challan.line_items.first()
        response = _client_for(self.se).post(
            reverse('confirm_grn', args=[self.project.project_id, challan.pk]),
            {f'received_qty_{line.pk}': '10', f'damaged_qty_{line.pk}': '0'},
        )
        self.assertEqual(response.status_code, 302)
        line.refresh_from_db()
        self.assertEqual(line.received_quantity, Decimal('10.00'))

    def test_a_live_project_still_reads(self):
        task = self._task(self.project, 'AC Cable Work')
        challan = self._make_dc(self.project)
        client = _client_for(self.pm)

        for url_name, args in [
            ('project_overview', [self.project.project_id]),
            ('project_timeline', [self.project.project_id]),
            ('task_detail',      [self.project.project_id, task.pk]),
        ]:
            with self.subTest(endpoint=url_name):
                self.assertEqual(client.get(reverse(url_name, args=args)).status_code,
                                 200)

        # The DC page is SE/SCM/PM/Admin — driven by the SE who holds tasks here.
        self.assertEqual(
            _client_for(self.se).get(
                reverse('delivery_challan_detail',
                        args=[self.project.project_id, challan.pk])).status_code,
            200,
        )

    def test_my_documents_still_lists_a_live_projects_files(self):
        with patch('projects.supabase_storage.get_supabase_client',
                   return_value=MagicMock()):
            _client_for(self.pm).post(
                reverse('upload_project_document', args=[self.project.project_id]),
                {'files': [_pdf('handover.pdf')]},
            )
        response = _client_for(self.pm).get(reverse('my_documents'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['project_docs']), 1)


# ---------------------------------------------------------------------------
# 6 — Task 6: the three endpoints prompt 0.2's list missed
# ---------------------------------------------------------------------------

class ScopeGateCompletionTests(SoftDeleteBase):
    """0.2 worked from an endpoint list that omitted three views carrying the identical
    PM-only stanza. The delete pair is the sharp one: after 0.2 an unrelated user could
    not upload a document to a project but could still reach the delete endpoint for it.

    The uploader-or-Admin authority check is unchanged and is asserted alongside, so a
    later session cannot satisfy these by widening the wrong rule.
    """

    def setUp(self):
        super().setUp()
        # An SE with no task anywhere on this project — the "unrelated user" the 0.2
        # scope gate exists to shut out. A second project is not needed: holding no
        # task IS the absence of the relationship user_can_view_project() tests.
        self.outsider = _profile('outsider_sd', 'Site Engineer')

    def _upload_doc(self):
        with patch('projects.supabase_storage.get_supabase_client',
                   return_value=MagicMock()):
            _client_for(self.pm).post(
                reverse('upload_project_document', args=[self.project.project_id]),
                {'files': [_pdf('handover.pdf')]},
            )
        return ProjectDocument.objects.get(project=self.project)

    def _upload_attachment(self, task):
        with patch('projects.supabase_storage.get_supabase_client',
                   return_value=MagicMock()):
            _client_for(self.se).post(
                reverse('upload_task_attachment',
                        args=[self.project.project_id, task.pk]),
                {'files': [_photo('roof.jpg')]},
            )
        return TaskAttachment.objects.get(task=task)

    def test_an_unrelated_user_cannot_delete_a_project_document(self):
        doc = self._upload_doc()
        response = _client_for(self.outsider).post(
            reverse('delete_project_document',
                    args=[self.project.project_id, doc.pk]))
        self.assertEqual(response.status_code, 404)
        doc.refresh_from_db()
        self.assertFalse(doc.is_deleted)

    def test_an_unrelated_user_cannot_delete_a_task_attachment(self):
        task = self._task(self.project, 'AC Cable Work')
        attach = self._upload_attachment(task)
        response = _client_for(self.outsider).post(
            reverse('delete_task_attachment',
                    args=[self.project.project_id, task.pk, attach.pk]))
        self.assertEqual(response.status_code, 404)
        attach.refresh_from_db()
        self.assertFalse(attach.is_deleted)

    def test_an_unrelated_user_cannot_read_a_delivery_challan(self):
        challan = self._make_dc(self.project)
        response = _client_for(self.outsider).get(
            reverse('delivery_challan_detail',
                    args=[self.project.project_id, challan.pk]))
        self.assertEqual(response.status_code, 404)

    def test_the_uploader_or_admin_check_still_applies_alongside_the_scope_gate(self):
        """Scope is not a substitute for authority. The SE holds tasks here, so the new
        gate lets them through — and the pre-existing uploader-or-Admin rule must still
        refuse them the PM's document. Both gates, not one replacing the other."""
        doc = self._upload_doc()
        response = _client_for(self.se).post(
            reverse('delete_project_document',
                    args=[self.project.project_id, doc.pk]))
        self.assertEqual(response.status_code, 403)
        doc.refresh_from_db()
        self.assertFalse(doc.is_deleted)

    def test_the_uploader_still_deletes_their_own_document(self):
        """The positive control for the pair above."""
        doc = self._upload_doc()
        response = _client_for(self.pm).post(
            reverse('delete_project_document',
                    args=[self.project.project_id, doc.pk]))
        self.assertEqual(response.status_code, 302)
        doc.refresh_from_db()
        self.assertTrue(doc.is_deleted)
        self.assertEqual(doc.deleted_by, self.pm)

    def test_the_uploader_still_deletes_their_own_task_attachment(self):
        task = self._task(self.project, 'AC Cable Work')
        attach = self._upload_attachment(task)
        response = _client_for(self.se).post(
            reverse('delete_task_attachment',
                    args=[self.project.project_id, task.pk, attach.pk]))
        self.assertEqual(response.status_code, 302)
        attach.refresh_from_db()
        self.assertTrue(attach.is_deleted)
