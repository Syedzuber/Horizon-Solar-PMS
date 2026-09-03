"""
Checklist versioning and completion snapshots — prompt 0.5.

WHY THIS FILE EXISTS
--------------------
`ChecklistItemCompletion` foreign-keyed `ChecklistItem` and stored no copy of the text it
was answering. Two defects came out of that, and this file pins both by reproducing them:

  1. REWRITING. Rewording a checklist label changed the wording displayed against every
     completion already recorded against it. Forty sites completed in March showed April's
     question, and a "Yes" recorded against the old one appeared to answer the new one.
     Pinned by `test_rewording_the_item_leaves_an_existing_snapshot_alone`.

  2. DELETION. The FK was CASCADE, so deleting a checklist item deleted every completion
     of it — the tick, the photo, who checked it and when. Not rewritten; gone. Pinned by
     `test_deleting_the_item_keeps_the_completion_and_its_snapshot`.

For CEIG paperwork, completion certificates and warranty claims that is a compromised
audit trail rather than a display bug.

The fix has two halves and both are tested here: the completion snapshots the text (R-8,
`BOQRevision.snapshot` is the existing precedent), and the checklist itself is versioned
and immutable once active (R-7, exactly as `TaskTemplate` is since 0.4).

WHAT IS NOT TESTED HERE, BECAUSE 0.5 DOES NOT DO IT
    ChecklistTaskLink still matches on (task_name, project_type); prompt 2.4 replaces the
    string match with a TaskTemplateTask reference. No response types, no acceptance rules,
    no checklist gate on task completion (B-6, prompt 2.3), no authoring UI.

Run with:
    python manage.py test projects --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse

from .models import (
    Checklist, ChecklistItem, ChecklistItemCompletion, ChecklistTaskLink, Project, Task,
    TemplateVersionLocked, derive_checklist_code,
)
from .utils import RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL
from .views import _checklist_for_task


def _profile(username, role, email=''):
    """Create a User and set its auto-created UserProfile's role. Same helper shape as
    tests_residential_baseline.py — a post_save signal makes the profile, so fetch and
    mutate rather than creating a second one."""
    user = User.objects.create_user(username=username, password='pw12345', email=email)
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.save()
    return profile


def _client_for(profile):
    client = Client()
    client.force_login(profile.user)
    return client


def _photo(name='site.jpg'):
    """A JPEG upload that passes the real _validate_and_upload() checks."""
    return SimpleUploadedFile(name, b'\xff\xd8\xff\xe0fake-jpeg-bytes',
                              content_type='image/jpeg')


class ChecklistSnapshotBase(TestCase):
    """One activated Residential project, and a published checklist on one of its tasks.

    The fixture authors in the ORDER THE NEW MODEL REQUIRES — create the draft, add the
    items, publish — because a published version's content is frozen (R-7). That order is
    the visible cost of the fix and it belongs in the fixture, stated, rather than hidden
    inside a helper.
    """

    def setUp(self):
        # Required data: activation raises and rolls back without the Finance account.
        self.finance = _profile('fin_cs', 'Finance',
                                email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)
        self.pm      = _profile('pm_cs',     'PM')
        self.design  = _profile('design_cs', 'Design')

        self.project = Project.objects.create(
            customer_name='Alpha Residence',
            customer_phone='9876543210',
            site_address='1 Sun Road',
            city='Lucknow',
            project_type='Residential',
            capacity_kw=Decimal('5.00'),
            status='Draft',
            assigned_pm=self.pm,
            target_commissioning_date=date.today() + timedelta(days=90),
        )
        response = _client_for(self.pm).post(
            reverse('project_activate', args=[self.project.project_id]),
            {'assigned_design_id': self.design.pk},
        )
        self.assertEqual(response.status_code, 302, 'activation did not redirect')
        self.project.refresh_from_db()

        self.task = Task.objects.get(phase__project=self.project,
                                     task_name='Pre Commissioning Check List')

        self.checklist, items = self._publish_checklist(
            'Pre-commissioning', ['Earth resistance < 5 ohm'], task=self.task)
        self.item = items[0]

    # -- fixture helpers -----------------------------------------------------

    def _publish_checklist(self, name, labels, task=None, code=None, version_no=1):
        """Author a draft, fill it, publish it, and optionally link it to a task."""
        checklist = Checklist.objects.create(name=name, code=code or '',
                                             version_no=version_no)
        items = [
            ChecklistItem.objects.create(checklist=checklist, label=label, order=n)
            for n, label in enumerate(labels, start=1)
        ]
        checklist.activate()
        if task is not None:
            ChecklistTaskLink.objects.create(
                checklist=checklist, task_name=task.task_name,
                project_type=self.project.project_type)
        return checklist, items

    def _complete(self, item, task=None, actor=None):
        """Tick an item THROUGH THE REAL VIEW, so the fixture itself asserts the photo
        rule and the snapshot write still happen where they are supposed to."""
        task = task or self.task
        with patch('projects.supabase_storage.get_supabase_client',
                   return_value=MagicMock()):
            return _client_for(actor or self.pm).post(
                reverse('checklist_item_complete',
                        args=[self.project.project_id, task.pk, item.pk]),
                {'photo': _photo()},
            )


# ---------------------------------------------------------------------------
# 1 — The snapshot: the completion stores the text it was answering (R-8)
# ---------------------------------------------------------------------------

class SnapshotWriteTests(ChecklistSnapshotBase):

    def test_completing_an_item_writes_the_label_into_the_snapshot(self):
        response = self._complete(self.item)
        self.assertEqual(response.status_code, 302)

        completion = ChecklistItemCompletion.objects.get(item=self.item, task=self.task)
        self.assertTrue(completion.is_checked)
        self.assertEqual(completion.item_text_snapshot, 'Earth resistance < 5 ohm')

    def test_the_snapshot_is_written_in_the_same_save_as_the_tick_and_the_photo(self):
        """A checked item can no more lack the question it answered than lack its photo."""
        self._complete(self.item)
        completion = ChecklistItemCompletion.objects.get(item=self.item, task=self.task)
        self.assertTrue(completion.is_checked)
        self.assertTrue(completion.item_text_snapshot)
        self.assertEqual(completion.photo_file_name, 'site.jpg')
        self.assertTrue(completion.photo_url)
        self.assertTrue(completion.photo_supabase_path)

    def test_an_unchecked_row_has_no_snapshot(self):
        completion = ChecklistItemCompletion.objects.create(item=self.item, task=self.task)
        self.assertFalse(completion.is_checked)
        self.assertEqual(completion.item_text_snapshot, '')

    def test_the_task_page_renders_the_snapshot_not_the_current_label(self):
        """THE READ PATH. A snapshot nothing reads is decoration."""
        self._complete(self.item)

        # .update() bypasses the R-7 guard deliberately: the subject here is what an
        # already-completed row RENDERS, not who is allowed to reword.
        ChecklistItem.objects.filter(pk=self.item.pk).update(
            label='Earth resistance below 5 ohm at all three electrodes')

        rendered = _client_for(self.pm).get(
            reverse('task_detail', args=[self.project.project_id, self.task.pk]))
        self.assertContains(rendered, 'Earth resistance &lt; 5 ohm')
        self.assertNotContains(rendered, 'at all three electrodes')

    def test_an_open_row_renders_the_live_label(self):
        """The snapshot is what WAS answered; an unanswered row shows what is asked now."""
        rendered = _client_for(self.pm).get(
            reverse('task_detail', args=[self.project.project_id, self.task.pk]))
        self.assertContains(rendered, 'Earth resistance &lt; 5 ohm')


# ---------------------------------------------------------------------------
# 2 — The two defects, reproduced directly
# ---------------------------------------------------------------------------

class HistoryIsNoLongerRewritableTests(ChecklistSnapshotBase):

    def test_rewording_the_item_leaves_an_existing_snapshot_alone(self):
        """DEFECT 1. Before 0.5 this test could not be written: there was nothing on the
        completion to be left alone, and the reworded label WAS the displayed history."""
        self._complete(self.item)
        completion = ChecklistItemCompletion.objects.get(item=self.item, task=self.task)
        self.assertEqual(completion.item_text_snapshot, 'Earth resistance < 5 ohm')

        ChecklistItem.objects.filter(pk=self.item.pk).update(label='Something else entirely')

        completion.refresh_from_db()
        self.assertEqual(completion.item_text_snapshot, 'Earth resistance < 5 ohm')
        self.item.refresh_from_db()
        self.assertEqual(self.item.label, 'Something else entirely')

    def test_deleting_the_item_keeps_the_completion_and_its_snapshot(self):
        """DEFECT 2, and the worse of the two. The FK was CASCADE: one admin tidying up a
        checklist erased the inspection record of every site that had ever answered it."""
        self._complete(self.item)
        completion = ChecklistItemCompletion.objects.get(item=self.item, task=self.task)
        checked_by, checked_at = completion.checked_by, completion.checked_at
        photo_path = completion.photo_supabase_path

        ChecklistItem.objects.filter(pk=self.item.pk).delete()

        completion.refresh_from_db()
        self.assertIsNone(completion.item_id)                      # provenance gone
        self.assertEqual(completion.item_text_snapshot, 'Earth resistance < 5 ohm')
        self.assertTrue(completion.is_checked)                     # the record survives
        self.assertEqual(completion.checked_by, checked_by)
        self.assertEqual(completion.checked_at, checked_at)
        self.assertEqual(completion.photo_supabase_path, photo_path)

    def test_deleting_the_whole_checklist_keeps_the_completions(self):
        """The cascade ran two levels deep: Checklist to items to completions."""
        self._complete(self.item)
        self.assertEqual(ChecklistItemCompletion.objects.count(), 1)

        Checklist.objects.filter(pk=self.checklist.pk).delete()

        self.assertEqual(ChecklistItem.objects.count(), 0)
        self.assertEqual(ChecklistItemCompletion.objects.count(), 1)
        completion = ChecklistItemCompletion.objects.get()
        self.assertIsNone(completion.item_id)
        self.assertEqual(completion.item_text_snapshot, 'Earth resistance < 5 ohm')

    def test_two_orphaned_completions_on_one_task_both_survive(self):
        """The uniqueness rule is PARTIAL now (condition item__isnull=False). Two
        completions orphaned on the same task answered two genuinely different questions,
        and neither may be squeezed out by the other."""
        # The published v1 is frozen, so the second line arrives the only legitimate way:
        # a new draft version carrying both.
        draft = Checklist.objects.create(name=self.checklist.name,
                                         code=self.checklist.code, version_no=2)
        line_a = ChecklistItem.objects.create(checklist=draft,
                                              label='Earth resistance < 5 ohm', order=1)
        line_b = ChecklistItem.objects.create(checklist=draft,
                                              label='Torque check', order=2)
        draft.activate()

        self._complete(line_a)
        self._complete(line_b)
        self.assertEqual(ChecklistItemCompletion.objects.count(), 2)

        ChecklistItem.objects.filter(pk__in=[line_a.pk, line_b.pk]).delete()

        self.assertEqual(ChecklistItemCompletion.objects.count(), 2)
        self.assertEqual(
            sorted(c.item_text_snapshot for c in ChecklistItemCompletion.objects.all()),
            ['Earth resistance < 5 ohm', 'Torque check'],
        )


# ---------------------------------------------------------------------------
# 3 — R-7: content is immutable once the version is published
# ---------------------------------------------------------------------------

class ChecklistImmutabilityTests(ChecklistSnapshotBase):

    def test_editing_an_item_on_an_active_checklist_raises(self):
        self.item.label = 'Reworded in place'
        with self.assertRaises(TemplateVersionLocked):
            self.item.save()

    def test_deleting_an_item_on_an_active_checklist_raises(self):
        with self.assertRaises(TemplateVersionLocked):
            self.item.delete()

    def test_adding_an_item_to_an_active_checklist_raises(self):
        """Adding is content too: a question added to a live checklist retroactively
        makes every site that already completed it incomplete."""
        with self.assertRaises(TemplateVersionLocked):
            ChecklistItem.objects.create(checklist=self.checklist, label='New line', order=9)

    def test_editing_an_item_on_a_draft_checklist_succeeds(self):
        draft = Checklist.objects.create(name='Mounting structure')
        item = ChecklistItem.objects.create(checklist=draft, label='Purlin spacing', order=1)
        item.label = 'Purlin spacing 1.4 m'
        item.save()
        item.refresh_from_db()
        self.assertEqual(item.label, 'Purlin spacing 1.4 m')

    def test_deleting_an_item_on_a_draft_checklist_succeeds(self):
        draft = Checklist.objects.create(name='Mounting structure')
        item = ChecklistItem.objects.create(checklist=draft, label='Purlin spacing', order=1)
        item.delete()
        self.assertEqual(draft.items.count(), 0)

    def test_an_archived_version_is_frozen_as_hard_as_an_active_one(self):
        """An archived version is the record of what last month's sites answered;
        rewriting it would make that record a lie."""
        Checklist.objects.filter(pk=self.checklist.pk).update(status=Checklist.ARCHIVED)
        item = ChecklistItem.objects.get(pk=self.item.pk)     # fresh, uncached parent
        item.label = 'Reworded after archiving'
        with self.assertRaises(TemplateVersionLocked):
            item.save()

    def test_the_portal_admin_item_edit_refuses_a_published_version(self):
        """The view refuses with a message on the screen rather than letting the model
        turn into a 500 — the same choice _DraftOnlyContentAdmin makes."""
        admin = _profile('admin_cs', 'Admin')
        response = _client_for(admin).post(
            reverse('admin_checklist_item_edit', args=[self.checklist.pk, self.item.pk]),
            {'label': 'Reworded through the screen'},
        )
        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.label, 'Earth resistance < 5 ohm')

    def test_the_portal_admin_item_add_refuses_a_published_version(self):
        admin = _profile('admin_cs2', 'Admin')
        response = _client_for(admin).post(
            reverse('admin_checklist_item_add', args=[self.checklist.pk]),
            {'label': 'Snuck in'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.checklist.items.count(), 1)

    def test_the_portal_admin_item_delete_refuses_a_published_version(self):
        admin = _profile('admin_cs3', 'Admin')
        response = _client_for(admin).post(
            reverse('admin_checklist_item_delete', args=[self.checklist.pk, self.item.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.checklist.items.count(), 1)


# ---------------------------------------------------------------------------
# 4 — Versioning: one active version per code, and activation archives its predecessor
# ---------------------------------------------------------------------------

class ChecklistVersioningTests(ChecklistSnapshotBase):

    def test_activating_version_two_archives_version_one(self):
        draft = Checklist.objects.create(name=self.checklist.name,
                                         code=self.checklist.code, version_no=2)
        ChecklistItem.objects.create(checklist=draft, label='Reworded line', order=1)
        self.assertEqual(draft.status, Checklist.DRAFT)

        draft.activate()

        self.checklist.refresh_from_db()
        self.assertEqual(self.checklist.status, Checklist.ARCHIVED)
        self.assertEqual(draft.status, Checklist.ACTIVE)
        self.assertIsNotNone(draft.effective_from)

    def test_two_active_versions_of_one_code_cannot_exist(self):
        """The partial unique constraint is what makes 'at most one active version per
        code' true at the DATABASE rather than by convention."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Checklist.objects.create(name=self.checklist.name,
                                         code=self.checklist.code, version_no=2,
                                         status=Checklist.ACTIVE)

    def test_two_versions_of_one_code_cannot_share_a_version_number(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Checklist.objects.create(name=self.checklist.name,
                                         code=self.checklist.code, version_no=1)

    def test_any_number_of_drafts_and_archives_coexist_beside_the_active_one(self):
        Checklist.objects.create(name=self.checklist.name, code=self.checklist.code,
                                 version_no=2, status=Checklist.DRAFT)
        Checklist.objects.create(name=self.checklist.name, code=self.checklist.code,
                                 version_no=3, status=Checklist.DRAFT)
        Checklist.objects.create(name=self.checklist.name, code=self.checklist.code,
                                 version_no=4, status=Checklist.ARCHIVED)
        self.assertEqual(Checklist.objects.filter(code=self.checklist.code).count(), 4)
        self.assertEqual(
            Checklist.objects.filter(code=self.checklist.code,
                                     status=Checklist.ACTIVE).count(), 1)

    def test_only_a_draft_can_be_activated(self):
        with self.assertRaises(TemplateVersionLocked):
            self.checklist.activate()

    def test_activation_archives_and_promotes_in_one_transaction(self):
        """There is no instant at which a code has two active versions or none. The
        constraint would reject the former outright; this asserts the latter."""
        draft = Checklist.objects.create(name=self.checklist.name,
                                         code=self.checklist.code, version_no=2)
        ChecklistItem.objects.create(checklist=draft, label='Reworded line', order=1)
        draft.activate()
        self.assertEqual(
            Checklist.objects.filter(code=self.checklist.code,
                                     status=Checklist.ACTIVE).count(), 1)

    def test_the_code_is_derived_from_the_name_and_disambiguated(self):
        first = Checklist.objects.create(name='MMS Installation')
        self.assertEqual(first.code, 'MMS-INSTALLATION')
        second = Checklist.objects.create(name='MMS Installation')
        self.assertEqual(second.code, 'MMS-INSTALLATION-2')

    def test_a_supplied_code_is_never_overwritten(self):
        """This is the whole of what keeps version 2 in version 1's family."""
        v2 = Checklist.objects.create(name='Anything at all',
                                      code=self.checklist.code, version_no=2)
        self.assertEqual(v2.code, self.checklist.code)

    def test_derive_checklist_code_falls_back_when_the_name_slugs_to_nothing(self):
        self.assertEqual(derive_checklist_code('///', Checklist), 'CHECKLIST')


# ---------------------------------------------------------------------------
# 5 — Resolution: _checklist_for_task behaves exactly as is_active did
# ---------------------------------------------------------------------------

class ChecklistResolutionTests(ChecklistSnapshotBase):

    def test_it_resolves_the_same_checklist_for_the_same_task(self):
        self.assertEqual(_checklist_for_task(self.task, self.project), self.checklist)

    def test_an_archived_checklist_is_not_offered(self):
        """Exactly as is_active=False behaved: treated as unassigned, link and all."""
        Checklist.objects.filter(pk=self.checklist.pk).update(status=Checklist.ARCHIVED)
        self.assertIsNone(_checklist_for_task(self.task, self.project))

    def test_a_draft_checklist_is_not_offered(self):
        Checklist.objects.filter(pk=self.checklist.pk).update(status=Checklist.DRAFT)
        self.assertIsNone(_checklist_for_task(self.task, self.project))

    def test_a_task_with_no_link_resolves_to_nothing(self):
        other = (Task.objects.filter(phase__project=self.project)
                 .exclude(pk=self.task.pk).first())
        self.assertIsNone(_checklist_for_task(other, self.project))

    def test_publishing_version_two_moves_the_task_onto_it(self):
        """The link says which FAMILY the task uses; status says which version is live.
        Without family resolution the link would still point at the archived v1 and the
        checklist would vanish off the task the moment v2 went out."""
        draft = Checklist.objects.create(name=self.checklist.name,
                                         code=self.checklist.code, version_no=2)
        ChecklistItem.objects.create(checklist=draft, label='Reworded line', order=1)
        draft.activate()

        self.assertEqual(_checklist_for_task(self.task, self.project), draft)

    def test_a_different_project_type_does_not_match_the_link(self):
        self.project.project_type = 'CAPEX'
        self.assertIsNone(_checklist_for_task(self.task, self.project))


# ---------------------------------------------------------------------------
# 6 — Everything about checklist_item_complete that must NOT have changed
# ---------------------------------------------------------------------------

class CompletionBehaviourUnchangedTests(ChecklistSnapshotBase):

    def test_a_photo_is_still_mandatory(self):
        _client_for(self.pm).post(
            reverse('checklist_item_complete',
                    args=[self.project.project_id, self.task.pk, self.item.pk]), {})
        self.assertEqual(ChecklistItemCompletion.objects.count(), 0)

    def test_a_checked_item_can_still_never_lack_a_photo(self):
        self._complete(self.item)
        for completion in ChecklistItemCompletion.objects.filter(is_checked=True):
            self.assertTrue(completion.photo_file_name)
            self.assertTrue(completion.photo_url)
            self.assertTrue(completion.photo_supabase_path)

    def test_rechecking_is_a_no_op_and_does_not_write_a_second_row(self):
        self._complete(self.item)
        first = ChecklistItemCompletion.objects.get(item=self.item, task=self.task)

        self._complete(self.item)

        self.assertEqual(ChecklistItemCompletion.objects.count(), 1)
        second = ChecklistItemCompletion.objects.get(item=self.item, task=self.task)
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(second.checked_at, first.checked_at)

    def test_rechecking_does_not_overwrite_the_original_snapshot(self):
        """The idempotent early return is what protects the snapshot from a second,
        later-worded write."""
        self._complete(self.item)
        ChecklistItem.objects.filter(pk=self.item.pk).update(label='Reworded afterwards')

        self._complete(self.item)

        completion = ChecklistItemCompletion.objects.get(task=self.task)
        self.assertEqual(completion.item_text_snapshot, 'Earth resistance < 5 ohm')

    def test_an_item_from_another_checklist_is_never_trusted(self):
        """A raw item_id belonging to a checklist not linked to this task is refused."""
        _other, foreign = self._publish_checklist('Unrelated', ['Foreign line'])
        response = self._complete(foreign[0])
        self.assertEqual(response.status_code, 404)
        self.assertEqual(ChecklistItemCompletion.objects.count(), 0)

    def test_the_same_item_on_two_tasks_completes_independently(self):
        """The (item, task) key, still enforced for live rows by the partial constraint."""
        second_task = (Task.objects.filter(phase__project=self.project)
                       .exclude(pk=self.task.pk).first())
        ChecklistTaskLink.objects.create(
            checklist=self.checklist, task_name=second_task.task_name,
            project_type=self.project.project_type)

        self._complete(self.item)
        self._complete(self.item, task=second_task)

        self.assertEqual(ChecklistItemCompletion.objects.count(), 2)
        self.assertEqual(ChecklistItemCompletion.objects.filter(item=self.item).count(), 2)

    def test_one_live_completion_per_item_and_task(self):
        self._complete(self.item)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ChecklistItemCompletion.objects.create(item=self.item, task=self.task)


# ---------------------------------------------------------------------------
# 7 — is_active is a shim over status, not a second source of truth
# ---------------------------------------------------------------------------

class IsActiveShimTests(TestCase):
    """The column is gone (migration 0070). What remains is a property, so there is one
    stored answer to "is this live" and existing callers keep working against it."""

    def test_is_active_is_not_a_field(self):
        self.assertNotIn('is_active', [f.name for f in Checklist._meta.get_fields()])

    def test_reading_it_answers_from_status(self):
        checklist = Checklist.objects.create(name='Shim read')
        self.assertFalse(checklist.is_active)          # a draft is not live
        ChecklistItem.objects.create(checklist=checklist, label='A line', order=1)
        checklist.activate()
        self.assertTrue(checklist.is_active)

    def test_writing_true_publishes_and_writing_false_archives(self):
        checklist = Checklist.objects.create(name='Shim write', is_active=True)
        self.assertEqual(checklist.status, Checklist.ACTIVE)
        checklist.is_active = False
        self.assertEqual(checklist.status, Checklist.ARCHIVED)
