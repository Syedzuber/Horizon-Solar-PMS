"""Contractor bills 4b-1 — revising a bill on resubmit; the Site Engineer's card.

What this file pins, and why each matters:

  * A BILL'S CONTRACTOR AND SITE ARE LOCKED in the chokepoint: another vendor or any other
    scope is refused and writes nothing; repeating what the bill holds is dropped (Q4).
  * THE REVISED VALUES MEET CREATE'S REFUSALS (_clean_bill's parts) — but the contractor
    is not asked again, and the PDF only when a new one is sent (Q1); nor is the site's
    deleted/Draft check — the site is locked and was checked at raise (4b-2).
  * A REPLACED PDF IS NEVER LOST: the previous round's snapshot keeps the old file, its
    round still signs it, and discard_unrecorded_bill_pdf refuses any file a snapshot
    records (Q2).
  * THE SITE ENGINEER'S CONFIRMATION IS KEPT ONLY WHILE THE TASKS ARE UNCHANGED (B), in
    the chokepoint and on the form.
  * THE RESUBMIT POST'S ORDER: nothing is uploaded before the form, storage, the
    chokepoint's own refusals and the warnings have passed (Q3: warnings on every bill
    resubmit); a late failure removes the new PDF.
  * "WHAT CHANGED" names the bill's changes to SCM and the PM; the Site Engineer reads
    only the task changes, and "No change to the work in this round." (Q5).
  * THE SITE ENGINEER'S "Work to confirm" CARD: their own live bill steps, one query, and
    nothing of the bill's money — not even fetched.
  * EVERY PHOTO INPUT'S accept NAMES MIME TYPES AND EXTENSIONS, no capture (Q6).

Run with:
    python manage.py test projects.tests_contractor_bill_revision --settings=solarpms.test_settings
"""
import re
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .approval_forms import file_accept
from .approval_queries import work_to_confirm_card
from .approvals import (
    BILL_CONTRACTOR_LOCKED, BILL_KEEP_TASKS_CHANGED, BILL_SITE_LOCKED, ApprovalRefused,
    apply_approval_decision, resubmit_approval_request, round_snapshot,
)
from .approval_views import round_changes
from .bill_storage import BILL_STORAGE_OFF, discard_unrecorded_bill_pdf
from .models import (
    ApprovalRequest, ApprovalRoundSnapshot, ApprovalStep, ContractorBillDetail,
    ContractorBillTask, Program, Task,
    APPROVAL_OPEN, APPROVAL_PARTY_PM, APPROVAL_PARTY_SITE_ENGINEER, APPROVAL_STEP_APPROVED,
    APPROVAL_STEP_CHANGES_REQUESTED, APPROVAL_STEP_PENDING,
)
from .tests_approvals import SE_PHOTO
from .tests_contractor_bill_screens import (
    SIGNED, STORED, ScreenFixture, _pdf, _photo,
)
from .tests_contractor_bills import BUCKET, PDF, _profile, _task

NEW_PDF = {'file_name': 'CB-7-rev.pdf', 'bucket': BUCKET, 'path': 'site/bill/rev.pdf',
           'file_size_kb': 30}


class RevisionFixture(ScreenFixture):
    """A raised bill; `sent_back_by_pm()` has the Site Engineer confirm and the PM ask
    for changes (so the confirmation may be kept), `sent_back_by_se()` has the Site
    Engineer say the work is not done."""

    def setUp(self):
        super().setUp()
        self.approval = self.raise_bill()

    def sent_back_by_pm(self):
        apply_approval_decision(self.step(self.approval, APPROVAL_PARTY_SITE_ENGINEER),
                                APPROVAL_STEP_APPROVED, self.se, files=[dict(SE_PHOTO)])
        apply_approval_decision(self.step(self.approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.pm, note='Rate?')

    def sent_back_by_se(self):
        apply_approval_decision(self.step(self.approval, APPROVAL_PARTY_SITE_ENGINEER),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.se, note='Not done.')

    def resubmit(self, **kwargs):
        kwargs.setdefault('note', 'Revised.')
        return resubmit_approval_request(self.approval, self.scm, **kwargs)

    def detail(self):
        return ContractorBillDetail.objects.get(request=self.approval)

    def task_pks(self):
        return list(ContractorBillTask.objects.filter(detail__request=self.approval)
                    .order_by('pk').values_list('task_id', flat=True))

    def untouched(self):
        """Everything a refused resubmit must leave as it was."""
        detail = self.detail()
        approval = ApprovalRequest.objects.get(pk=self.approval.pk)
        return (approval.status, approval.current_round, approval.title,
                approval.vendor_id, list(approval.projects.values_list('pk', flat=True)),
                detail.amount, detail.bill_number, detail.bill_date, detail.pdf_path,
                self.task_pks(), ApprovalRoundSnapshot.objects.count(),
                ApprovalStep.objects.count())


# ===========================================================================
# The chokepoint
# ===========================================================================

class ChokepointRevisionTests(RevisionFixture):

    def test_the_bill_is_revised_and_each_round_keeps_its_own_block(self):
        self.sent_back_by_se()
        new_date = timezone.localdate() - timedelta(days=1)
        self.resubmit(revision={'amount': '13000.50', 'bill_number': 'CB-7A',
                                'bill_date': new_date.isoformat(),
                                'tasks': [self.foundation], 'pdf': dict(NEW_PDF)})
        detail = self.detail()
        self.assertEqual((detail.amount, detail.bill_number, detail.bill_date),
                         (Decimal('13000.50'), 'CB-7A', new_date))
        self.assertEqual((detail.pdf_file_name, detail.pdf_bucket, detail.pdf_path,
                          detail.pdf_size_kb), ('CB-7-rev.pdf', BUCKET, NEW_PDF['path'], 30))
        self.assertEqual(self.task_pks(), [self.foundation.pk])
        one, two = round_snapshot(self.approval, 1), round_snapshot(self.approval, 2)
        self.assertEqual((one['schema'], two['schema']), (3, 3))
        self.assertEqual(one['bill']['amount'], '12500.00')
        self.assertEqual(one['bill']['pdf']['path'], PDF['path'])
        self.assertEqual(len(one['bill']['tasks']), 2)
        self.assertEqual(two['bill']['amount'], '13000.50')
        self.assertEqual(two['bill']['bill_number'], 'CB-7A')
        self.assertEqual(two['bill']['bill_date'], new_date.isoformat())
        self.assertEqual(two['bill']['pdf']['path'], NEW_PDF['path'])
        self.assertEqual([t['id'] for t in two['bill']['tasks']], [self.foundation.pk])

    def test_unchanged_tasks_keep_their_links_and_added_ones_follow(self):
        self.sent_back_by_se()
        links = list(ContractorBillTask.objects.filter(detail__request=self.approval)
                     .order_by('pk').values_list('pk', flat=True))
        third = _task(self.site, 'Earthing')
        self.resubmit(revision={'tasks': [third, self.wiring, self.foundation]})
        rows = list(ContractorBillTask.objects.filter(detail__request=self.approval)
                    .order_by('pk').values_list('pk', 'task_id'))
        self.assertEqual([pk for pk, _ in rows[:2]], links)
        self.assertEqual([task for _, task in rows],
                         [self.foundation.pk, self.wiring.pk, third.pk])

    def test_another_contractor_or_site_is_refused_and_writes_nothing(self):
        self.sent_back_by_se()
        program = Program.objects.create(name='Tender X', short_tender_code='TX')
        cases = [
            ({'vendor': self.supplier}, BILL_CONTRACTOR_LOCKED),
            ({'vendor': self.supplier, 'amount': '1.00'}, BILL_CONTRACTOR_LOCKED),
            ({'projects': [self.other_site]}, BILL_SITE_LOCKED),
            ({'projects': [self.site, self.other_site]}, BILL_SITE_LOCKED),
            ({'projects': []}, BILL_SITE_LOCKED),
            ({'programs': [program]}, BILL_SITE_LOCKED),
        ]
        for revision, message in cases:
            with self.subTest(revision=list(revision)):
                before = self.untouched()
                with self.assertRaises(ApprovalRefused) as caught:
                    self.resubmit(revision=revision)
                self.assertEqual(str(caught.exception), message)
                self.assertEqual(self.untouched(), before)

    def test_repeating_the_bills_own_contractor_and_site_is_dropped(self):
        self.sent_back_by_se()
        self.resubmit(revision={'vendor': self.contractor, 'projects': [self.site],
                                'programs': [], 'site_groups': []})
        approval = ApprovalRequest.objects.get(pk=self.approval.pk)
        self.assertEqual((approval.current_round, approval.vendor_id),
                         (2, self.contractor.pk))
        self.assertEqual(list(approval.projects.all()), [self.site])

    def test_a_vendor_of_none_is_still_answered_by_choose_the_vendor(self):
        self.sent_back_by_se()
        with self.assertRaisesMessage(ApprovalRefused, 'Choose the vendor'):
            self.resubmit(revision={'vendor': None})

    def test_revised_values_meet_creates_refusals(self):
        self.sent_back_by_se()
        mirror = _task(self.site, 'Material delivered', is_mirror=True)
        cases = [
            ({'tasks': []}, 'Choose at least one task'),
            ({'tasks': [self.elsewhere]}, 'is not a task on'),
            ({'tasks': [mirror]}, 'mirror task'),
            ({'amount': '0'}, 'more than zero'),
            ({'amount': ''}, 'Enter the bill amount'),
            ({'bill_number': '  '}, "contractor's bill number"),
            ({'bill_number': 'x' * 101}, '100 characters'),
            ({'bill_date': (timezone.localdate() + timedelta(days=1)).isoformat()},
             'cannot be in the future'),
            ({'bill_date': 'junk'}, 'Bill date:'),
            ({'pdf': {'file_name': 'x.pdf', 'bucket': 'approvals', 'path': 'p.pdf'}},
             'stored privately'),
            ({'pdf': {'file_name': 'x.png', 'bucket': BUCKET, 'path': 'p.png'}},
             'must be a PDF'),
        ]
        for revision, message in cases:
            with self.subTest(revision=revision):
                before = self.untouched()
                with self.assertRaises(ApprovalRefused) as caught:
                    self.resubmit(revision=revision)
                self.assertIn(message, str(caught.exception))
                self.assertEqual(self.untouched(), before)

    def test_the_contractor_is_not_asked_again_nor_an_unchanged_pdf(self):
        """Q1: an inactive (or reclassified) contractor does not block a revision, and
        with storage off an unchanged PDF is not refused for its bucket."""
        self.sent_back_by_se()
        type(self.contractor).objects.filter(pk=self.contractor.pk).update(
            is_active=False, kind='supplier')
        with self.settings(SUPABASE_BILLS_BUCKET=''):
            self.resubmit(revision={'amount': '12600.00'})
        self.assertEqual(self.detail().amount, Decimal('12600.00'))
        self.assertEqual(self.detail().pdf_path, PDF['path'])

    def test_a_site_deleted_or_put_back_to_draft_after_raise_does_not_refuse_a_revision(self):
        """4b-2 (D-A55 extension): the site is locked and was checked at raise, so a
        resubmit does not ask again. A raise on such a site is still refused."""
        Site = type(self.site)
        status = self.site.status
        for label, change in (('deleted', {'is_deleted': True}), ('Draft', {'status': 'Draft'})):
            with self.subTest(label):
                self.approval = self.raise_bill()
                self.sent_back_by_se()
                Site.objects.filter(pk=self.site.pk).update(**change)
                try:
                    self.resubmit(revision={'amount': '12600.00'})
                finally:
                    Site.objects.filter(pk=self.site.pk).update(is_deleted=False, status=status)
                approval = ApprovalRequest.objects.get(pk=self.approval.pk)
                self.assertEqual((approval.status, approval.current_round), (APPROVAL_OPEN, 2))
                self.assertEqual(self.detail().amount, Decimal('12600.00'))
        for change, message in (({'is_deleted': True}, 'That site has been deleted.'),
                                ({'status': 'Draft'}, 'is Draft; a bill cannot be raised')):
            with self.subTest(raise_on=change):
                Site.objects.filter(pk=self.site.pk).update(**change)
                try:
                    with self.assertRaisesMessage(ApprovalRefused, message):
                        self.raise_bill(self.bill(project=Site.objects.get(pk=self.site.pk)))
                finally:
                    Site.objects.filter(pk=self.site.pk).update(is_deleted=False, status=status)

    def test_a_bill_key_on_a_material_request_is_refused(self):
        material = self.raise_material()
        apply_approval_decision(self.step(material, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.pm, note='Make?')
        with self.assertRaisesMessage(ApprovalRefused, '"amount" belongs to a contractor bill'):
            resubmit_approval_request(material, self.scm, 'Revised.',
                                      revision={'amount': '10.00'})

    def test_the_site_engineers_confirmation_is_kept_only_while_the_tasks_are_unchanged(self):
        self.sent_back_by_pm()
        keep = {APPROVAL_PARTY_SITE_ENGINEER: 'The work itself is unchanged.'}
        for revision in ({'tasks': [self.foundation]},
                         {'tasks': [self.foundation, self.wiring, _task(self.site, 'Extra')]}):
            with self.subTest(tasks=len(revision['tasks'])):
                before = self.untouched()
                with self.assertRaises(ApprovalRefused) as caught:
                    self.resubmit(revision=revision, carry=keep)
                self.assertEqual(str(caught.exception), BILL_KEEP_TASKS_CHANGED)
                self.assertEqual(self.untouched(), before)
        # The same tasks, in another order, with the amount changed: kept.
        self.resubmit(revision={'tasks': [self.wiring, self.foundation],
                                'amount': '12000.00'}, carry=keep)
        se = self.step(self.approval, APPROVAL_PARTY_SITE_ENGINEER)
        self.assertIsNotNone(se.carried_from_id)

    def test_changed_tasks_without_a_keep_ask_the_site_engineer_again(self):
        self.sent_back_by_pm()
        self.resubmit(revision={'tasks': [self.foundation]})
        se = self.step(self.approval, APPROVAL_PARTY_SITE_ENGINEER)
        self.assertEqual(se.verdict, APPROVAL_STEP_PENDING)
        self.assertIsNotNone(se.activated_at)


class DiscardTests(RevisionFixture):

    def test_a_pdf_only_a_snapshot_records_is_never_removed(self):
        self.sent_back_by_se()
        self.resubmit(revision={'pdf': dict(NEW_PDF)})
        client = self.bill_storage_client()
        # The round-1 PDF is on no bill any more, only in round 1's snapshot.
        self.assertFalse(ContractorBillDetail.objects.filter(pdf_path=PDF['path']).exists())
        self.assertFalse(discard_unrecorded_bill_pdf(dict(PDF)))
        self.assertFalse(discard_unrecorded_bill_pdf(dict(NEW_PDF)))
        client.assert_not_called()
        # A file nothing records is still removed.
        self.assertTrue(discard_unrecorded_bill_pdf(dict(STORED)))
        client.return_value.storage.from_.return_value.remove.assert_called_once_with(
            [STORED['path']])


# ===========================================================================
# The resubmit page
# ===========================================================================

class ResubmitPageTests(RevisionFixture):

    def setUp(self):
        super().setUp()
        self.url = reverse('approval_resubmit', args=[self.approval.pk])

    def post_values(self, **overrides):
        values = {
            'revise': '1', 'title': 'Civil works bill', 'description': 'Foundation, block A.',
            'amount': '12500.00', 'bill_number': 'CB-7',
            'bill_date': (timezone.localdate() - timedelta(days=3)).isoformat(),
            'task': [str(self.foundation.pk), str(self.wiring.pk)],
            'assignee_site_engineer': str(self.se.pk), 'assignee_pm': str(self.pm.pk),
            'confirm_warnings': '1', 'note': 'Revised as asked.'}
        values.update(overrides)
        return {key: value for key, value in values.items() if value is not None}

    def post(self, **overrides):
        return self.client_for(self.scm).post(self.url, self.post_values(**overrides))

    def test_the_form_is_prefilled_from_the_bill(self):
        self.sent_back_by_se()
        content = self.client_for(self.scm).get(self.url).content.decode()
        for text in ('value="12500.00"', 'value="CB-7"',
                     f'value="{(timezone.localdate() - timedelta(days=3)).isoformat()}"',
                     'Civil Co', f'{self.site.project_id} — Bill Site', SIGNED, 'CB-7.pdf',
                     f'data-bill-tasks="{self.foundation.pk},{self.wiring.pk}"'):
            self.assertIn(text, content)
        for task in (self.foundation, self.wiring):
            self.assertRegex(content, rf'value="{task.pk}" id="apTask{task.pk}" checked')
        self.assertNotIn(f'id="apTask{self.elsewhere.pk}"', content)   # another site's task
        self.assertNotIn('Attach the new bill PDF again', content)

    def test_a_revision_with_a_new_pdf_is_stored_then_written(self):
        self.sent_back_by_se()
        pdf_upload, _ = self.uploads()
        response = self.post(amount='13000.00', bill_number='CB-7A',
                             task=[str(self.foundation.pk)], bill_pdf=_pdf('CB-7-rev.pdf'))
        self.assertEqual(response.status_code, 302)
        pdf_upload.assert_called_once()
        detail = self.detail()
        self.assertEqual((detail.amount, detail.bill_number, detail.pdf_path),
                         (Decimal('13000.00'), 'CB-7A', STORED['path']))
        self.assertEqual(round_snapshot(self.approval, 1)['bill']['pdf']['path'], PDF['path'])
        self.assertEqual(self.task_pks(), [self.foundation.pk])

    def test_a_site_deleted_after_raise_does_not_stop_the_resubmit_post(self):
        """4b-2: the view's read-only pre-check (_revision_writes) no longer asks the
        site's deleted/Draft question either."""
        self.sent_back_by_se()
        type(self.site).objects.filter(pk=self.site.pk).update(is_deleted=True)
        response = self.post(amount='12600.00')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.detail().amount, Decimal('12600.00'))
        self.assertEqual(ApprovalRequest.objects.get(pk=self.approval.pk).current_round, 2)

    def test_nothing_is_uploaded_before_form_storage_refusals_and_warnings(self):
        self.sent_back_by_se()
        pdf_upload, photo_client = self.uploads()
        cases = [
            ('form', {'bill_pdf': _pdf('bad.pdf', b'not a pdf')}, 400,
             'not a PDF'),
            ('refusal', {'amount': '0', 'bill_pdf': _pdf()}, 400, 'more than zero'),
            ('locked', {'project': [str(self.other_site.pk)], 'bill_pdf': _pdf()}, 400,
             'site on a bill'),
            ('warnings', {'confirm_warnings': None, 'bill_pdf': _pdf()}, 200, None),
        ]
        for label, overrides, status, message in cases:
            with self.subTest(label):
                before = self.untouched()
                response = self.post(attachments=[_photo()], **overrides)
                self.assertEqual(response.status_code, status)
                if message:
                    self.assertTrue(any(message in m for m in self.messages_of(response)),
                                    self.messages_of(response))
                self.assertEqual(self.untouched(), before)
        pdf_upload.assert_not_called()
        self.photo_upload.assert_not_called()
        with self.settings(SUPABASE_BILLS_BUCKET=''):
            response = self.post(bill_pdf=_pdf())
        self.assertEqual(response.status_code, 400)
        self.assertIn(BILL_STORAGE_OFF, self.messages_of(response))
        pdf_upload.assert_not_called()

    def test_warnings_run_on_every_resubmit_and_leave_this_bill_out(self):
        self.sent_back_by_se()
        response = self.post(confirm_warnings=None)       # nothing changed at all
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('Resubmit anyway', content)
        self.assertIn('&#x27;Foundation&#x27; is not complete', content)
        # The bill's own number and tasks are never "another bill" or "repeated".
        self.assertNotIn('already has bill number', content)
        self.assertNotIn('is also on bill', content)
        self.assertEqual(ApprovalRequest.objects.get(pk=self.approval.pk).current_round, 1)
        self.assertEqual(self.post().status_code, 302)    # ticked: goes ahead

    def test_the_reattach_banner_shows_only_when_a_pdf_was_being_replaced(self):
        self.sent_back_by_se()
        self.uploads()
        with_pdf = self.post(amount='0', bill_pdf=_pdf()).content.decode()
        self.assertIn('id="apReattach"', with_pdf)
        self.assertIn('Attach the new bill PDF again', with_pdf)
        without = self.post(amount='0').content.decode()
        self.assertNotIn('id="apReattach"', without)

    def test_a_late_refusal_removes_the_new_pdf_and_keeps_the_old(self):
        self.sent_back_by_se()
        self.uploads()
        client = self.bill_storage_client()
        with mock.patch('projects.approval_views.resubmit_approval_request',
                        side_effect=ApprovalRefused('Refused late.')):
            response = self.post(bill_pdf=_pdf())
        self.assertEqual(response.status_code, 400)
        client.return_value.storage.from_.return_value.remove.assert_called_once_with(
            [STORED['path']])
        self.assertEqual(self.detail().pdf_path, PDF['path'])

    def test_a_photo_failure_removes_the_new_pdf(self):
        self.sent_back_by_se()
        self.uploads()
        self.photo_upload.side_effect = RuntimeError('storage down')
        client = self.bill_storage_client()
        response = self.post(bill_pdf=_pdf(), attachments=[_photo()])
        self.assertEqual(response.status_code, 400)
        client.return_value.storage.from_.return_value.remove.assert_called_once_with(
            [STORED['path']])
        self.assertEqual(self.approval.bill_detail.pdf_path, PDF['path'])

    def test_keeping_the_confirmation_with_changed_tasks_is_refused_on_the_form(self):
        self.sent_back_by_pm()
        keep = {'keep_site_engineer': 'on',
                'keep_reason_site_engineer': 'The work itself is unchanged.'}
        response = self.post(task=[str(self.foundation.pk)], **keep)
        self.assertEqual(response.status_code, 400)
        self.assertTrue(any('tasks on this bill changed' in m
                            for m in self.messages_of(response)))
        # Redrawn with the changed ticks: the keep box is not offered.
        self.assertNotContains(response, 'name="keep_site_engineer"', status_code=400)
        # Unchanged ticks: kept, and the PM is asked at once.
        self.assertEqual(self.post(**keep).status_code, 302)
        self.assertEqual(self.step(self.approval, APPROVAL_PARTY_PM).verdict,
                         APPROVAL_STEP_PENDING)
        self.assertIsNotNone(
            self.step(self.approval, APPROVAL_PARTY_SITE_ENGINEER).carried_from_id)

    def test_an_unknown_or_blank_contractor_is_refused_with_the_lock_message(self):
        self.sent_back_by_se()
        for raw in ('', '999999'):
            with self.subTest(vendor=raw):
                response = self.post(vendor=raw)
                self.assertEqual(response.status_code, 400)
                self.assertIn(BILL_CONTRACTOR_LOCKED, self.messages_of(response))

    def test_the_page_without_revise_revises_nothing(self):
        """A page opened before 4b-1 posts no revise=1: only the note and photos."""
        self.sent_back_by_se()
        values = self.post_values(revise=None, amount='1.00', title='Other')
        response = self.client_for(self.scm).post(self.url, values)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.detail().amount, Decimal('12500.00'))
        self.assertEqual(ApprovalRequest.objects.get(pk=self.approval.pk).title,
                         'Civil works bill')


# ===========================================================================
# What changed, per viewer
# ===========================================================================

class ChangeListTests(RevisionFixture):

    def setUp(self):
        super().setUp()
        self.sent_back_by_se()
        self.url = reverse('approval_detail', args=[self.approval.pk])
        self.new_date = timezone.localdate() - timedelta(days=1)

    def revise_everything(self):
        self.resubmit(revision={'amount': '13000.00', 'bill_number': 'CB-7A',
                                'bill_date': self.new_date.isoformat(),
                                'tasks': [self.foundation], 'pdf': dict(NEW_PDF)})

    def changes_block(self, profile):
        content = self.client_for(profile).get(self.url).content.decode()
        start = content.index('Changed since round 1')
        return content[start:content.index('</div>\n  </div>', start)]

    def test_scm_and_the_pm_read_every_bill_change(self):
        self.revise_everything()
        for profile in (self.scm, self.pm):
            with self.subTest(viewer=profile.user.username):
                block = self.changes_block(profile)
                for text in ('Amount', '₹12,500.00</span> → ₹13,000.00',
                             'Bill number', 'CB-7</span> → CB-7A', 'Bill date',
                             self.new_date.strftime('%d %b %Y'),
                             'Tasks', '− Removed: Wiring — Block A',
                             'Bill PDF', 'CB-7.pdf</span> → CB-7-rev.pdf'):
                    self.assertIn(text, block)

    def test_the_site_engineer_reads_only_the_task_changes(self):
        self.revise_everything()
        block = self.changes_block(self.se)
        self.assertIn('− Removed: Wiring — Block A', block)
        for text in ('Amount', '₹', 'CB-7', 'Bill number', 'Bill date', 'Bill PDF',
                     'CB-7-rev.pdf'):
            self.assertNotIn(text, block)

    def test_the_site_engineer_reads_no_change_to_the_work(self):
        self.resubmit(revision={'amount': '13000.00', 'title': 'Civil works, revised'})
        self.assertIn('No change to the work in this round.', self.changes_block(self.se))
        scm = self.changes_block(self.scm)
        self.assertIn('Amount', scm)
        self.assertIn('Civil works, revised', scm)
        self.assertNotIn('No change to the work', scm)

    def test_a_same_name_replacement_reads_as_a_new_file(self):
        self.resubmit(revision={'pdf': dict(NEW_PDF, file_name='CB-7.pdf')})
        self.assertIn('CB-7.pdf</span> → CB-7.pdf (new file)', self.changes_block(self.scm))

    def test_each_round_signs_its_own_pdf(self):
        self.revise_everything()
        with mock.patch('projects.bill_storage.get_design_file_url',
                        return_value=SIGNED) as signer:
            self.client_for(self.scm).get(self.url)
        signed = {call.args[1] for call in signer.call_args_list}
        self.assertEqual(signed, {PDF['path'], NEW_PDF['path']})

    def test_round_changes_filters_on_its_own(self):
        self.revise_everything()
        one, two = round_snapshot(self.approval, 1), round_snapshot(self.approval, 2)
        labels = [c['label'] for c in round_changes(one, two)]
        self.assertEqual(labels, ['Amount', 'Bill number', 'Bill date', 'Tasks', 'Bill PDF'])
        self.assertEqual([c['label'] for c in round_changes(one, two, work_only=True)],
                         ['Tasks'])


# ===========================================================================
# The Site Engineer's card
# ===========================================================================

class WorkToConfirmCardTests(RevisionFixture):

    def test_rows_are_the_site_engineers_live_bill_steps_without_money(self):
        other = self.raise_bill(site_engineer_assignee=self.se_b, title='Other SE bill')
        card = work_to_confirm_card(self.se.user)
        self.assertEqual(card['count'], 1)
        row = card['rows'][0]
        self.assertEqual(set(row), {'approval_pk', 'title', 'site', 'contractor', 'days',
                                    'days_text'})
        self.assertEqual((row['approval_pk'], row['title'], row['site'], row['contractor']),
                         (self.approval.pk, 'Civil works bill',
                          f'{self.site.project_id} — Bill Site', 'Civil Co'))
        self.assertEqual(work_to_confirm_card(self.se_b.user)['rows'][0]['approval_pk'],
                         other.pk)

    def test_one_query_that_fetches_no_money_column(self):
        for i in range(3):
            self.raise_bill(title=f'More {i}')
        with CaptureQueriesContext(connection) as ctx:
            card = work_to_confirm_card(self.se.user)
        self.assertEqual(card['count'], 4)
        self.assertEqual(len(ctx.captured_queries), 1)
        sql = ctx.captured_queries[0]['sql']
        for column in ('"amount"', '"bill_number"', '"bill_date"', '"pdf_path"',
                       '"pdf_file_name"', '"pdf_bucket"'):
            self.assertNotIn(column, sql)

    def test_a_decided_step_leaves_the_card_and_the_card_is_for_site_engineers_only(self):
        apply_approval_decision(self.step(self.approval, APPROVAL_PARTY_SITE_ENGINEER),
                                APPROVAL_STEP_APPROVED, self.se, files=[dict(SE_PHOTO)])
        card = work_to_confirm_card(self.se.user)
        self.assertEqual((card['count'], card['rows']), (0, []))
        self.assertIsNone(work_to_confirm_card(self.pm.user))
        self.assertIsNone(work_to_confirm_card(self.scm.user))

    def test_the_dashboard_draws_the_card_without_money(self):
        page = self.client_for(self.se).get(reverse('dashboard_site_engineer'))
        self.assertEqual(page.status_code, 200)
        content = page.content.decode()
        self.assertIn('Work to confirm', content)
        self.assertIn(reverse('approval_detail', args=[self.approval.pk]), content)
        self.assertIn('Civil works bill', content)
        self.assertIn(f'{self.site.project_id} — Bill Site', content)
        for secret in ('₹', '12,500', '12500', 'CB-7', SIGNED, PDF['path']):
            self.assertNotIn(secret, content)
        # The card is a comment-free render: no template comment leaks as text.
        self.assertNotIn('Contractor bills 4b-1', content)

    def test_nothing_to_confirm(self):
        apply_approval_decision(self.step(self.approval, APPROVAL_PARTY_SITE_ENGINEER),
                                APPROVAL_STEP_APPROVED, self.se, files=[dict(SE_PHOTO)])
        page = self.client_for(self.se).get(reverse('dashboard_site_engineer'))
        self.assertContains(page, 'Nothing to confirm')


# ===========================================================================
# Photo inputs on phones (Q6)
# ===========================================================================

class AcceptAttributeTests(RevisionFixture):

    PHOTOS = 'accept="image/jpeg,image/png,.jpg,.jpeg,.png"'

    def test_the_helper(self):
        self.assertEqual(file_accept(['jpg', 'jpeg', 'png']),
                         'image/jpeg,image/png,.jpg,.jpeg,.png')
        self.assertEqual(file_accept(['pdf', 'jpg', 'jpeg', 'png']),
                         'application/pdf,image/jpeg,image/png,.pdf,.jpg,.jpeg,.png')

    def test_every_photo_input_names_mime_types_and_none_uses_capture(self):
        client = self.client_for(self.scm)
        raise_page = client.get(self.step2_url(client)).content.decode()
        se_detail = self.client_for(self.se).get(
            reverse('approval_detail', args=[self.approval.pk])).content.decode()
        scm_detail = client.get(
            reverse('approval_detail', args=[self.approval.pk])).content.decode()
        self.sent_back_by_se()
        resubmit = client.get(
            reverse('approval_resubmit', args=[self.approval.pk])).content.decode()
        inputs = {
            'raise photos': (raise_page, 'attachments'),
            'SE confirm and not-done': (se_detail, 'site_photos'),
            'resubmit photos': (resubmit, 'attachments'),
        }
        for label, (content, name) in inputs.items():
            with self.subTest(label):
                tags = re.findall(rf'<input type="file" name="{name}"[^>]*>', content)
                self.assertTrue(tags)
                for tag in tags:
                    self.assertIn(self.PHOTOS, tag)
                    self.assertNotIn('capture', tag)
        self.assertEqual(len(re.findall(r'<input type="file" name="site_photos"', se_detail)), 2)
        evidence = re.findall(r'<input type="file" name="evidence_files"[^>]*>', scm_detail)
        self.assertTrue(evidence)
        for tag in evidence:
            self.assertIn('accept="application/pdf,image/jpeg,image/png,.pdf,.jpg,.jpeg,.png"',
                          tag)
