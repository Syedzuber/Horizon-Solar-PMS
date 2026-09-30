"""Contractor bills 4a-2 — the screens: raise, warnings, detail with the private PDF,
history, resubmit, emails.

What this file pins, and why each matters:

  * NO FILE IS STORED BEFORE THE WARNING CHECK. A warning, a refusal of the form, a
    refusal by approvals._clean_bill (asked with a placeholder PDF) and storage being
    off all re-render with nothing uploaded — the upload mocks are never called.
  * WHATEVER IS STORED FOR A BILL THAT WAS NOT WRITTEN IS REMOVED: a chokepoint refusal
    after the upload, a photo that failed, a racing twin. And a PDF a bill records is
    never removed.
  * THE PDF IS ONLY EVER A SIGNED, EXPIRING LINK on the page, minted per render (once per
    distinct file), "File unavailable" when none can be minted — and never in an email or
    an in-app notice, which carry a one-line summary instead.
  * A BILL'S RESUBMIT NEVER MOVES THE BILL: a posted scope or vendor change is refused
    (4b-1; the rest of bill revision is tests_contractor_bill_revision.py).
  * WHO SEES A BILL is the approvals rule: SCM, the named Site Engineer and PM; not
    another PM or Site Engineer, not Finance.

Run with:
    python manage.py test projects.tests_contractor_bill_screens --settings=solarpms.test_settings
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from . import approval_notices
from .approval_queries import pending_approvals_card
from .approvals import (
    apply_approval_decision, resubmit_approval_request, withdraw_approval_request,
)
from .bill_rules import format_bill_amount, task_status_label
from .bill_storage import BILL_LINK_SECONDS, BILL_STORAGE_OFF, BillStorageError
from .models import (
    ApprovalAttachment, ApprovalRequest, ApprovalRoundSnapshot, ApprovalStep,
    ContractorBillDetail, PaymentRequest, Project, Task, Vendor,
    APPROVAL_KIND_CONTRACTOR_BILL, APPROVAL_OPEN, APPROVAL_PARTY_PM,
    APPROVAL_PARTY_SITE_ENGINEER, APPROVAL_STEP_APPROVED, APPROVAL_STEP_CHANGES_REQUESTED,
    VENDOR_KIND_BOTH, VENDOR_KIND_CONTRACTOR,
)
from .tests_approval_notices import NoticeBase
from .tests_approvals import SE_PHOTO
from .tests_contractor_bills import BUCKET, PDF, BillFixture, _profile, _site, _task

SIGNED = 'https://storage.example/signed/bill.pdf?token=abc'
STORED = {'file_name': 'CB-9.pdf', 'bucket': BUCKET, 'path': 'site/bill/new.pdf',
          'file_size_kb': 12}


def _pdf(name='CB-9.pdf', body=b'%PDF-1.4 a contractor bill'):
    return SimpleUploadedFile(name, body, content_type='application/pdf')


def _photo(name='work.jpg'):
    return SimpleUploadedFile(name, b'\xff\xd8\xff\xe0 photo', content_type='image/jpeg')


class ScreenFixture(BillFixture):
    """BillFixture's people, contractor, supplier and sites, plus the raise page's URLs
    and a form that passes every refusal."""

    def setUp(self):
        super().setUp()
        self.bucket = self.settings(SUPABASE_BILLS_BUCKET=BUCKET)
        self.bucket.enable()
        self.addCleanup(self.bucket.disable)
        # Every page that draws a bill signs its PDF: never over the network in a test.
        signing = mock.patch('projects.bill_storage.get_design_file_url', return_value=SIGNED)
        signing.start()
        self.addCleanup(signing.stop)

    def step2_url(self, client, project=None):
        """Step 2's URL for `project`, with the key the page issues."""
        response = client.get(reverse('approval_create'),
                              {'kind': APPROVAL_KIND_CONTRACTOR_BILL,
                               'project': (project or self.site).pk})
        self.assertEqual(response.status_code, 302)
        return response['Location']

    def form(self, **overrides):
        values = {
            'title': 'Civil works bill', 'description': 'Foundation, block A.',
            'vendor': str(self.contractor.pk), 'task': [str(self.foundation.pk)],
            'site_engineer_assignee': str(self.se.pk), 'pm_assignee': str(self.pm.pk),
            'amount': '12500.00', 'bill_number': 'CB-9',
            'bill_date': (timezone.localdate() - timedelta(days=2)).isoformat(),
            'bill_pdf': _pdf(),
        }
        values.update(overrides)
        return {key: value for key, value in values.items() if value is not None}

    def finish_foundation(self):
        """The fixture's Foundation task Done, so a bill naming only it warns of nothing."""
        Task.objects.filter(pk=self.foundation.pk).update(status=Task.DONE)

    def uploads(self):
        """Patch both uploads: the PDF (its stored dict) and the photos' storage client.
        Returns (upload_bill_pdf mock, photo client mock)."""
        pdf = mock.patch('projects.approval_views.upload_bill_pdf', return_value=dict(STORED))
        client = mock.patch('projects.approval_views.get_supabase_client')
        photo_upload = mock.patch('projects.approval_views._validate_and_upload')
        pdf_mock, client_mock = pdf.start(), client.start()
        self.photo_upload = photo_upload.start()
        for patcher in (pdf, client, photo_upload):
            self.addCleanup(patcher.stop)
        return pdf_mock, client_mock

    def bill_storage_client(self):
        """bill_storage's own client (discard's remove), recorded."""
        patcher = mock.patch('projects.bill_storage._client')
        client = patcher.start()
        self.addCleanup(patcher.stop)
        return client

    def messages_of(self, response):
        return [str(m) for m in response.context['messages']]


# ===========================================================================
# The raise page
# ===========================================================================

class RaisePageTests(ScreenFixture):

    def test_the_list_offers_the_bill_raise_to_scm_only(self):
        self.assertContains(self.client_for(self.scm).get(reverse('approval_list')),
                            'Raise a contractor bill')
        self.assertNotContains(self.client_for(self.pm).get(reverse('approval_list')),
                               'Raise a contractor bill')

    def test_nobody_but_scm_reaches_the_raise_page(self):
        finance = _profile('cs_finance', 'Finance')
        ceo = _profile('cs_ceo', 'CEO')
        url = (reverse('approval_create') + f'?kind=contractor_bill&project={self.site.pk}'
               f'&key=11111111-1111-1111-1111-111111111111')
        for profile in (self.se, self.pm, finance, ceo):
            with self.subTest(role=profile.role):
                client = self.client_for(profile)
                self.assertEqual(client.get(url).status_code, 403)
                self.assertEqual(client.post(url, self.form()).status_code, 403)
        self.assertFalse(ApprovalRequest.objects.exists())

    def test_step_1_lists_live_billable_sites_only(self):
        draft = _site('Draft Site', status='Draft')
        deleted = _site('Deleted Site', is_deleted=True)
        test_data = _site('Test Site', is_test=True)
        client = self.client_for(self.scm)
        location = client.get(reverse('approval_create'), {'kind': 'contractor_bill'})['Location']
        self.assertRegex(location,
                         r'^/approvals/new/\?kind=contractor_bill&key=[0-9a-f-]{36}$')
        page = client.get(location)
        self.assertContains(page, f'value="{self.site.pk}"')
        self.assertContains(page, f'value="{self.other_site.pk}"')
        for site in (draft, deleted, test_data):
            self.assertNotContains(page, f'value="{site.pk}"')
        self.assertNotContains(page, 'name="bill_pdf"')

    def test_the_key_redirect_keeps_the_site_and_the_key_survives_step_1(self):
        client = self.client_for(self.scm)
        url = self.step2_url(client)
        self.assertIn(f'project={self.site.pk}', url)
        key = url.split('key=')[1].split('&')[0]
        page = client.get(url)
        self.assertContains(page, f'name="client_uuid" value="{key}"')
        # "Change site" returns to step 1 under the same key.
        self.assertContains(page, f'kind=contractor_bill&amp;key={key}"')

    def test_a_site_that_cannot_take_a_bill_goes_back_to_step_1(self):
        draft = _site('Draft Site', status='Draft')
        client = self.client_for(self.scm)
        for raw in (str(draft.pk), '999999', 'junk'):
            with self.subTest(project=raw):
                response = client.get(reverse('approval_create'), {
                    'kind': 'contractor_bill', 'project': raw,
                    'key': '22222222-2222-2222-2222-222222222222'})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response['Location'],
                                 '/approvals/new/?kind=contractor_bill'
                                 '&key=22222222-2222-2222-2222-222222222222')

    def test_step_2_offers_the_sites_tasks_engineers_pm_and_contractors(self):
        mirror = _task(self.site, 'Material delivered', is_mirror=True)
        both = Vendor.objects.create(name='Both Co', contact_person='R', phone='9000000013',
                                     kind=VENDOR_KIND_BOTH)
        idle = Vendor.objects.create(name='Idle Contractor', contact_person='R',
                                     phone='9000000014', kind=VENDOR_KIND_CONTRACTOR,
                                     is_active=False)
        client = self.client_for(self.scm)
        page = client.get(self.step2_url(client))
        content = page.content.decode()
        self.assertContains(page, f'value="{self.foundation.pk}"')
        self.assertContains(page, 'Wiring — Block A')
        self.assertContains(page, 'Not Started')
        self.assertNotContains(page, f'value="{mirror.pk}" id="cbTask')
        self.assertNotContains(page, f'id="cbTask{self.elsewhere.pk}"')
        # Contractors and "both" only, active only.
        self.assertIn('>Civil Co<', content)
        self.assertIn('>Both Co<', content)
        self.assertNotIn('Module Co', content)
        self.assertNotIn('Idle Contractor', content)
        # The Site Engineer holding a task here first, and preselected (the only one);
        # the site's assigned PM preselected.
        self.assertLess(content.index('Cb_Se ·'), content.index('>Cb_Se_B<'))
        self.assertIn(f'<option value="{self.se.pk}" selected>', content)
        self.assertIn(f'<option value="{self.pm.pk}" selected>', content)
        self.assertContains(page, 'Raise bill')

    def test_no_engineer_is_preselected_when_two_hold_tasks_here(self):
        Task.objects.filter(pk=self.wiring.pk).update(assigned_to=self.se_b)
        client = self.client_for(self.scm)
        content = client.get(self.step2_url(client)).content.decode()
        self.assertNotIn(f'<option value="{self.se.pk}" selected>', content)
        self.assertNotIn(f'<option value="{self.se_b.pk}" selected>', content)

    def test_with_storage_off_the_page_says_so_and_a_post_is_refused_untouched(self):
        pdf, photos = self.uploads()
        client = self.client_for(self.scm)
        url = self.step2_url(client)
        with self.settings(SUPABASE_BILLS_BUCKET=''):
            page = client.get(url)
            self.assertContains(page, 'Contractor bills cannot be raised yet')
            self.assertNotContains(page, 'Raise bill</button>')
            response = client.post(url, self.form(attachments=[_photo()]))
        self.assertEqual(response.status_code, 400)
        self.assertIn(BILL_STORAGE_OFF, self.messages_of(response))
        pdf.assert_not_called()
        photos.assert_not_called()
        self.assertFalse(ApprovalRequest.objects.exists())


class WarningTests(ScreenFixture):
    """D-A50: warnings come back BEFORE anything is uploaded; "Raise anyway" goes ahead."""

    def test_a_warning_re_renders_with_the_same_key_and_uploads_nothing(self):
        pdf, photos = self.uploads()
        client = self.client_for(self.scm)
        url = self.step2_url(client)
        key = url.split('key=')[1].split('&')[0]
        response = client.post(url, self.form(attachments=[_photo()]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, escape("'Foundation' is not complete — it is Not Started."))
        self.assertContains(response, 'name="confirm_warnings"')
        self.assertContains(response, 'Raise anyway')
        self.assertContains(response, f'name="client_uuid" value="{key}"')
        self.assertContains(response, 'value="CB-9"')   # what was typed stays
        self.assertContains(response, f'value="{self.foundation.pk}" id="cbTask'
                                      f'{self.foundation.pk}" checked')
        pdf.assert_not_called()
        photos.assert_not_called()
        self.assertFalse(ApprovalRequest.objects.exists())

    def test_every_warning_kind_is_shown(self):
        self.finish_foundation()
        self.raise_bill(bill=self.bill(tasks=[self.foundation], bill_number='CB-9'))
        na = _task(self.site, 'Earthing', is_not_applicable=True)
        pdf, _ = self.uploads()
        client = self.client_for(self.scm)
        response = client.post(self.step2_url(client), self.form(
            task=[str(self.foundation.pk), str(na.pk)],
            site_engineer_assignee=str(self.se_b.pk), pm_assignee=str(self.pm_b.pk)))
        self.assertEqual(response.status_code, 200)
        for text in ("'Earthing' is marked Not Applicable.",
                     "'Foundation' is also on bill CB-9",
                     'Civil Co already has bill number CB-9',
                     f'Cb_Se_B holds no task on {self.site.project_id}.',
                     f'Cb_Pm_B is not the assigned PM on {self.site.project_id}.'):
            with self.subTest(text=text):
                self.assertContains(response, escape(text))
        pdf.assert_not_called()
        self.assertEqual(ApprovalRequest.objects.count(), 1)

    def test_raise_anyway_raises_the_bill(self):
        pdf, _ = self.uploads()
        client = self.client_for(self.scm)
        url = self.step2_url(client)
        response = client.post(url, self.form(confirm_warnings='1',
                                              attachments=[_photo()]))
        approval = ApprovalRequest.objects.get()
        self.assertRedirects(response, reverse('approval_detail', args=[approval.pk]),
                             fetch_redirect_response=False)
        pdf.assert_called_once()
        self.assertEqual(pdf.call_args.args[1], self.site)
        detail = approval.bill_detail
        self.assertEqual((detail.pdf_bucket, detail.pdf_path), (BUCKET, STORED['path']))
        self.assertEqual(detail.bill_number, 'CB-9')
        self.assertEqual(detail.amount, Decimal('12500.00'))
        self.assertEqual(list(detail.tasks.all()), [self.foundation])
        self.assertEqual(self.photo_upload.call_count, 1)
        photo = approval.attachments.get()
        self.assertEqual(photo.file_name, 'work.jpg')
        self.assertNotEqual(photo.bucket, BUCKET)   # photos are public, never the bills bucket

    def test_a_bill_with_nothing_to_warn_about_raises_at_once(self):
        self.finish_foundation()
        pdf, _ = self.uploads()
        client = self.client_for(self.scm)
        response = client.post(self.step2_url(client), self.form())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ApprovalRequest.objects.get().kind, APPROVAL_KIND_CONTRACTOR_BILL)
        pdf.assert_called_once()


class RefusalBeforeUploadTests(ScreenFixture):
    """The form's refusals and approvals._clean_bill's, asked with a placeholder PDF:
    each a 400 with nothing uploaded and nothing written."""

    def test_each_refusal_uploads_nothing(self):
        mirror = _task(self.site, 'Material delivered', is_mirror=True)
        future = (timezone.localdate() + timedelta(days=1)).isoformat()
        cases = {
            'no PDF':          (dict(bill_pdf=None), "Attach the contractor's bill as a PDF."),
            'not a PDF':       (dict(bill_pdf=_pdf(body=b'hello')),
                                'That file is not a PDF, although its name ends in .pdf.'),
            'a PDF as photo':  (dict(attachments=[_pdf('scan.pdf')]), 'scan.pdf:'),
            'supplier':        (dict(vendor=str(self.supplier.pk)),
                                'Module Co is recorded as a supplier, not a contractor.'),
            'no contractor':   (dict(vendor=''), 'Choose the contractor this bill is from.'),
            'another site':    (dict(task=[str(self.elsewhere.pk)]),
                                f"'Elsewhere' is not a task on {self.site.project_id}."),
            'mirror task':     (dict(task=[str(mirror.pk)]), 'is a mirror task'),
            'no task':         (dict(task=None), 'Choose at least one task this bill covers.'),
            'future date':     (dict(bill_date=future), 'The bill date cannot be in the future.'),
            'no bill number':  (dict(bill_number=''), "Enter the contractor's bill number."),
            'zero amount':     (dict(amount='0'), 'The bill amount must be more than zero.'),
        }
        pdf, photos = self.uploads()
        client = self.client_for(self.scm)
        url = self.step2_url(client)
        for name, (overrides, message) in cases.items():
            with self.subTest(name):
                response = client.post(url, self.form(confirm_warnings='1', **overrides))
                self.assertEqual(response.status_code, 400)
                self.assertTrue(any(message in m for m in self.messages_of(response)),
                                self.messages_of(response))
        pdf.assert_not_called()
        photos.assert_not_called()
        self.assertFalse(ApprovalRequest.objects.exists())

    def test_a_post_without_its_site_goes_back_to_step_1(self):
        pdf, _ = self.uploads()
        response = self.client_for(self.scm).post(
            reverse('approval_create') + '?kind=contractor_bill'
            '&key=33333333-3333-3333-3333-333333333333', self.form())
        self.assertEqual(response['Location'], '/approvals/new/?kind=contractor_bill'
                                               '&key=33333333-3333-3333-3333-333333333333')
        pdf.assert_not_called()


class AfterUploadTests(ScreenFixture):
    """What happens to the stored files when something fails after the upload."""

    def test_a_chokepoint_refusal_after_the_upload_removes_the_pdf_and_photos(self):
        _, photo_client = self.uploads()
        bills = self.bill_storage_client()
        client = self.client_for(self.scm)
        # A Site Engineer named as the PM: the chokepoint's assignee rule, asked only
        # after the upload.
        response = client.post(self.step2_url(client), self.form(
            confirm_warnings='1', pm_assignee=str(self.se_b.pk), attachments=[_photo()]))
        self.assertEqual(response.status_code, 400)
        self.assertTrue(any('cannot be named as the PM approver' in m
                            for m in self.messages_of(response)))
        bills.return_value.storage.from_.assert_called_with(BUCKET)
        bills.return_value.storage.from_.return_value.remove.assert_called_once_with(
            [STORED['path']])
        photo_path = self.photo_upload.call_args.args[3]
        photo_client.return_value.storage.from_.return_value.remove.assert_called_once_with(
            [photo_path])
        self.assertFalse(ApprovalRequest.objects.exists())

    def test_a_photo_that_fails_to_upload_removes_the_pdf(self):
        self.uploads()
        self.photo_upload.side_effect = RuntimeError('storage down')
        bills = self.bill_storage_client()
        client = self.client_for(self.scm)
        response = client.post(self.step2_url(client),
                               self.form(confirm_warnings='1', attachments=[_photo()]))
        self.assertEqual(response.status_code, 400)
        self.assertIn('An attachment could not be uploaded. Nothing was saved; try again.',
                      self.messages_of(response))
        bills.return_value.storage.from_.return_value.remove.assert_called_once_with(
            [STORED['path']])
        self.assertFalse(ApprovalRequest.objects.exists())

    def test_a_pdf_that_fails_to_upload_stores_no_photo(self):
        pdf, photos = self.uploads()
        pdf.side_effect = BillStorageError('Upload to storage failed: Timeout. Nothing was saved.')
        client = self.client_for(self.scm)
        response = client.post(self.step2_url(client),
                               self.form(confirm_warnings='1', attachments=[_photo()]))
        self.assertEqual(response.status_code, 400)
        self.assertIn('Upload to storage failed: Timeout. Nothing was saved.',
                      self.messages_of(response))
        photos.assert_not_called()
        self.assertFalse(ApprovalRequest.objects.exists())

    def test_the_pdf_a_bill_records_is_never_removed(self):
        self.uploads()
        bills = self.bill_storage_client()
        client = self.client_for(self.scm)
        client.post(self.step2_url(client), self.form(confirm_warnings='1'))
        self.assertTrue(ContractorBillDetail.objects.filter(pdf_path=STORED['path']).exists())
        bills.return_value.storage.from_.return_value.remove.assert_not_called()

    def test_a_used_key_returns_its_bill_and_uploads_nothing(self):
        pdf, _ = self.uploads()
        client = self.client_for(self.scm)
        url = self.step2_url(client)
        client.post(url, self.form(confirm_warnings='1'))
        approval = ApprovalRequest.objects.get()
        pdf.reset_mock()
        again = client.post(url, self.form(confirm_warnings='1'))
        self.assertRedirects(again, reverse('approval_detail', args=[approval.pk]),
                             fetch_redirect_response=False)
        pdf.assert_not_called()
        self.assertContains(client.get(url), f'already raised <strong>request #{approval.pk}')
        self.assertEqual(ApprovalRequest.objects.count(), 1)

    def test_a_racing_twin_keeps_its_bill_and_ours_is_removed(self):
        twin = self.raise_bill()
        self.uploads()
        bills = self.bill_storage_client()
        client = self.client_for(self.scm)
        with mock.patch('projects.approval_views.create_approval_request',
                        return_value=twin):
            response = client.post(self.step2_url(client), self.form(confirm_warnings='1'))
        self.assertRedirects(response, reverse('approval_detail', args=[twin.pk]),
                             fetch_redirect_response=False)
        bills.return_value.storage.from_.return_value.remove.assert_called_once_with(
            [STORED['path']])


# ===========================================================================
# The detail page
# ===========================================================================

class DetailTests(ScreenFixture):

    def setUp(self):
        super().setUp()
        self.approval = self.raise_bill()
        self.url = reverse('approval_detail', args=[self.approval.pk])
        patcher = mock.patch('projects.bill_storage.get_design_file_url', return_value=SIGNED)
        self.signer = patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_bill_block_shows_the_amount_beside_the_signed_pdf(self):
        page = self.client_for(self.scm).get(self.url)
        content = page.content.decode()
        for text in ('Civil Co', f'{self.site.project_id} — Bill Site', 'CB-7',
                     'Foundation', 'Wiring — Block A', 'Not Started',
                     'Approving confirms the work was done. Nobody has checked this amount '
                     'against a rate or work order.'):
            self.assertIn(text, content)
        # The amount and the link in one row.
        amount_at = content.index('₹12,500.00')
        self.assertLess(content.index(SIGNED, amount_at) - amount_at, 200)
        self.signer.assert_called_once_with(BUCKET, PDF['path'], BILL_LINK_SECONDS)
        # The PDF is never an attachment row, and no public attachment link names it.
        self.assertFalse(ApprovalAttachment.objects.filter(path=PDF['path']).exists())
        # Material-only rows are not drawn for a bill.
        self.assertNotIn('Design sign-off', content)

    def test_a_tasks_status_is_read_now(self):
        Task.objects.filter(pk=self.foundation.pk).update(status=Task.IN_PROGRESS)
        self.assertContains(self.client_for(self.scm).get(self.url), 'In Progress')

    def test_warnings_show_only_while_open_or_waiting_for_changes(self):
        warning = escape("'Foundation' is not complete")
        self.assertContains(self.client_for(self.scm).get(self.url), warning)
        apply_approval_decision(self.step(self.approval, APPROVAL_PARTY_SITE_ENGINEER),
                                APPROVAL_STEP_APPROVED, self.se, files=[dict(SE_PHOTO)])
        apply_approval_decision(self.step(self.approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.pm, note='Fix it.')
        self.assertContains(self.client_for(self.scm).get(self.url), warning)
        with self.settings(SUPABASE_BILLS_BUCKET=BUCKET):
            withdraw_approval_request(self.approval, self.scm, 'Duplicate.')
        self.assertNotContains(self.client_for(self.scm).get(self.url), warning)

    def test_the_file_is_unavailable_when_storage_is_off(self):
        with self.settings(SUPABASE_BILLS_BUCKET=''):
            page = self.client_for(self.scm).get(self.url)
        self.assertContains(page, 'File unavailable')
        self.assertNotContains(page, SIGNED)
        self.signer.assert_not_called()

    def test_each_round_draws_its_own_snapshot_and_the_pdf_is_signed_once(self):
        snapshot = ApprovalRoundSnapshot.objects.get(request=self.approval, round=1)
        payload = snapshot.snapshot
        payload['bill']['amount'] = '99999.50'
        ApprovalRoundSnapshot.objects.filter(pk=snapshot.pk).update(snapshot=payload)
        apply_approval_decision(self.step(self.approval, APPROVAL_PARTY_SITE_ENGINEER),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.se, note='Photos.')
        resubmit_approval_request(self.approval, self.scm, 'Photos added.')
        page = self.client_for(self.scm).get(self.url)
        self.assertContains(page, '₹99,999.50')        # round 1, as its snapshot says
        self.assertContains(page, '₹12,500.00')        # the bill now, and round 2
        # 4b-1: the change list compares the two snapshots' bill blocks.
        self.assertContains(page, '<span class="text-muted">₹99,999.50</span> → ₹12,500.00')
        self.signer.assert_called_once()

    def test_a_material_page_reads_nothing_of_bills(self):
        material = self.raise_material()
        with CaptureQueriesContext(connection) as queries:
            page = self.client_for(self.scm).get(
                reverse('approval_detail', args=[material.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertFalse([q for q in queries.captured_queries
                          if 'contractorbill' in q['sql']])
        self.assertNotContains(page, 'Approving confirms the work was done')
        self.assertContains(page, 'Design sign-off')
        self.signer.assert_not_called()

    def test_who_may_open_a_bill(self):
        finance = _profile('cs_fin', 'Finance')
        expected = {self.scm: 200, self.se: 200, self.pm: 200,
                    self.pm_b: 403, self.se_b: 403, finance: 403}
        for profile, status in expected.items():
            with self.subTest(user=profile.user.username):
                self.assertEqual(self.client_for(profile).get(self.url).status_code, status)
        # 5b (D-A58): once a payment request points at the bill, Finance may open it.
        # Written directly: the admission reads only that a payment exists.
        PaymentRequest.objects.create(
            contractor_bill=self.approval.bill_detail, project=self.site,
            vendor=self.contractor, amount=Decimal('1000'), requested_by=self.scm.user)
        with self.subTest(user=finance.user.username, forwarded=True):
            self.assertEqual(self.client_for(finance).get(self.url).status_code, 200)


# ===========================================================================
# Resubmit
# ===========================================================================

class ResubmitTests(ScreenFixture):

    def setUp(self):
        super().setUp()
        self.approval = self.raise_bill()
        apply_approval_decision(self.step(self.approval, APPROVAL_PARTY_SITE_ENGINEER),
                                APPROVAL_STEP_APPROVED, self.se, files=[dict(SE_PHOTO)])
        apply_approval_decision(self.step(self.approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.pm, note='Photos.')
        self.url = reverse('approval_resubmit', args=[self.approval.pk])

    def test_the_page_draws_the_bill_read_only_and_nothing_material(self):
        page = self.client_for(self.scm).get(self.url)
        self.assertEqual(page.status_code, 200)
        # 4b-1: the bill's own fields are editable; the contractor and site are text.
        for absent in ('name="project"', 'name="vendor"', 'The material',
                       'line-0-description'):
            self.assertNotContains(page, absent)
        for present in ('name="revise"', 'name="amount"', 'name="bill_number"',
                        'name="bill_date"', 'name="bill_pdf"', 'name="task"'):
            self.assertContains(page, present)
        self.assertContains(page, 'CB-7')
        self.assertContains(page, 'name="keep_site_engineer"')
        self.assertContains(page, 'accept="image/jpeg,image/png,.jpg,.jpeg,.png"')

    def test_only_scm_opens_it(self):
        for profile in (self.se, self.pm):
            self.assertEqual(self.client_for(profile).get(self.url).status_code, 403)

    def bill_post(self, **overrides):
        """The resubmit page's POST as drawn for the fixture bill, unchanged, with the
        warnings already accepted."""
        values = {
            'revise': '1', 'title': 'Civil works bill', 'description': 'Foundation, block A.',
            'amount': '12500.00', 'bill_number': 'CB-7',
            'bill_date': (timezone.localdate() - timedelta(days=3)).isoformat(),
            'task': [str(self.foundation.pk), str(self.wiring.pk)],
            'assignee_site_engineer': str(self.se.pk), 'assignee_pm': str(self.pm.pk),
            'confirm_warnings': '1', 'note': 'Photos added.'}
        values.update(overrides)
        return values

    def test_a_posted_contractor_or_site_change_is_refused(self):
        for change, message in (({'vendor': str(self.supplier.pk)}, 'contractor on a bill'),
                                ({'project': [str(self.other_site.pk)]}, 'site on a bill')):
            with self.subTest(change=list(change)):
                response = self.client_for(self.scm).post(self.url, self.bill_post(**change))
                self.assertEqual(response.status_code, 400)
                self.assertTrue(any(message in m for m in self.messages_of(response)))
                approval = ApprovalRequest.objects.get(pk=self.approval.pk)
                self.assertEqual(approval.current_round, 1)
                self.assertEqual(approval.vendor, self.contractor)
                self.assertEqual(list(approval.projects.all()), [self.site])

    def test_a_title_change_with_the_site_engineer_kept_goes_to_the_pm(self):
        response = self.client_for(self.scm).post(self.url, self.bill_post(
            title='Changed title', keep_site_engineer='on',
            keep_reason_site_engineer='Only the title of the bill changed.'))
        self.assertEqual(response.status_code, 302)
        approval = ApprovalRequest.objects.get(pk=self.approval.pk)
        self.assertEqual((approval.status, approval.current_round), (APPROVAL_OPEN, 2))
        self.assertEqual(approval.title, 'Changed title')
        self.assertEqual(approval.vendor, self.contractor)
        self.assertEqual(list(approval.projects.all()), [self.site])
        pm_step = ApprovalStep.objects.get(request=approval, round=2, party=APPROVAL_PARTY_PM)
        self.assertIsNotNone(pm_step.activated_at)

    def test_a_pdf_is_not_a_photo_on_resubmit(self):
        response = self.client_for(self.scm).post(self.url, {
            'note': 'Scan attached.', 'attachments': [_pdf('scan.pdf')]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(ApprovalRequest.objects.get(pk=self.approval.pk).current_round, 1)


# ===========================================================================
# Emails and notices
# ===========================================================================

class BillEmailTests(NoticeBase):
    """Every bill email carries the one-line summary (ruling 4); no email or notice ever
    carries the PDF, its link or where it is stored."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch('projects.bill_storage.get_design_file_url')
        self.signer = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: self.signer.assert_not_called())

    def summary(self, approval):
        site = approval.bill_detail.project.project_id
        return f'Approval Contractor · {site} · bill CB-001 · ₹12,500.00'

    def assert_carries_summary(self, mail, approval):
        summary = self.summary(approval)
        self.assertTrue(mail['text'].endswith(f'\n\nBill: {summary}'), mail['text'])
        self.assertIn(f'Bill:</span> {summary}', mail['html'])
        for secret in ('fixture-bills', 'bill/fixture.pdf', 'bill.pdf', 'token'):
            self.assertNotIn(secret, mail['text'])
            self.assertNotIn(secret, mail['html'])

    def assert_carries_work(self, mail, approval):
        """4a-3 (D-A53): a Site Engineer's email carries the work — contractor, site,
        tasks — and never the bill number, the amount or the PDF."""
        site = approval.bill_detail.project.project_id
        self.assertTrue(mail['text'].endswith(
            f'\n\nWork: Approval Contractor · {site} · Foundation'), mail['text'])
        for secret in ('CB-001', '₹', 'fixture-bills', 'bill/fixture.pdf', 'bill.pdf',
                       'token'):
            self.assertNotIn(secret, mail['text'])
            self.assertNotIn(secret, mail['html'])

    def test_e1_to_the_site_engineer(self):
        approval, _ = self.act(self.raise_bill)
        mail = [m for m in self.mail if m['to'] == 'ap_se@example.com']
        self.assertEqual(len(mail), 1)
        self.assert_carries_work(mail[0], approval)
        self.assertNotIn('Work:', self.note_for(self.se))
        self.assertTrue(mail[0]['text'].startswith(self.note_for(self.se)))

    def test_e1_to_the_pm_and_e2_to_the_raiser(self):
        approval, _ = self.act(self.raise_bill)
        self.decide(approval, APPROVAL_PARTY_SITE_ENGINEER, APPROVAL_STEP_APPROVED, self.se,
                    files=[dict(SE_PHOTO)])
        pm_mail = [m for m in self.mail if m['to'] == 'ap_pm@example.com']
        self.assert_carries_summary(pm_mail[-1], approval)
        self.decide(approval, APPROVAL_PARTY_PM, APPROVAL_STEP_APPROVED, self.pm)
        scm_mail = [m for m in self.mail if m['to'] == 'ap_scm@example.com']
        self.assertTrue(scm_mail[-1]['subject'].startswith('Approved'))
        self.assert_carries_summary(scm_mail[-1], approval)

    def test_e3_withdrawal(self):
        approval, _ = self.act(self.raise_bill)
        self.act(withdraw_approval_request, approval, self.scm, 'Raised twice.')
        mail = [m for m in self.mail if m['to'] == 'ap_se@example.com']
        self.assertTrue(mail[-1]['subject'].startswith('Withdrawn'))
        self.assert_carries_work(mail[-1], approval)

    def test_a_material_email_is_unchanged(self):
        self.create_material(design=False)
        mail = [m for m in self.mail if m['to'] == 'ap_pm@example.com']
        self.assertIn('\n\nMaterial: ', mail[0]['text'])
        self.assertIn('Material:</span>', mail[0]['html'])
        self.assertNotIn('Bill:', mail[0]['html'])

    def test_the_summary_function_names_no_file(self):
        approval = self.raise_bill()
        self.assertEqual(approval_notices.bill_summary(approval), self.summary(approval))
        self.assertEqual(approval_notices.email_summary(approval),
                         (self.summary(approval), 'Bill'))


# ===========================================================================
# Labels and helpers
# ===========================================================================

class KindLabelTests(ScreenFixture):

    def test_lists_cards_and_aging_read_contractor_bill(self):
        approval = self.raise_bill()
        self.assertContains(self.client_for(self.scm).get(reverse('approval_list')),
                            'Contractor bill')
        self.assertContains(self.client_for(self.scm).get(reverse('approval_aging')),
                            'Contractor bill')
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_SITE_ENGINEER),
                                APPROVAL_STEP_APPROVED, self.se, files=[dict(SE_PHOTO)])
        card = pending_approvals_card(self.pm.user)
        self.assertEqual([row['kind_label'] for row in card['rows']], ['Contractor bill'])


class HelperTests(TestCase):

    def test_format_bill_amount_keeps_paise_in_indian_grouping(self):
        for value, text in ((Decimal('12500'), '₹12,500.00'), ('12500.00', '₹12,500.00'),
                            (Decimal('12345678.5'), '₹1,23,45,678.50'),
                            (Decimal('999.99'), '₹999.99'), (100000, '₹1,00,000.00'),
                            ('junk', '₹junk')):
            with self.subTest(value=value):
                self.assertEqual(format_bill_amount(value), text)

    def test_task_status_label(self):
        task = Task(status=Task.DONE, approved_at=None, is_not_applicable=False)
        self.assertEqual(task_status_label(task, 'OPEX'), 'Done — not yet approved')
        self.assertEqual(task_status_label(task, 'Residential'), 'Done')
        task.approved_at = timezone.now()
        self.assertEqual(task_status_label(task, 'OPEX'), 'Done — approved')
        task.is_not_applicable = True
        self.assertEqual(task_status_label(task, 'OPEX'), 'Not Applicable')
