"""Contractor bills 4a-3 — the Site Engineer confirms the WORK, never the bill (D-A53,
D-A54); the raise form never fails silently.

What this file pins, and why each matters:

  * THE CHOKEPOINT HOLDS THE SITE ENGINEER RULES, not only the screen: a confirmation
    with no site photo is refused, a reject is refused, an SCM proxy confirmation needs a
    photo among its evidence, and photos go on no other party's step. A refusal leaves
    the step pending and no attachment row.
  * A VIEWER WHO IS ONLY THE SITE ENGINEER never sees the amount, bill number, bill date
    or PDF — on the Request card, in any round, in History — and the PDF is not even
    signed for them. The PM, SCM and the raiser see everything, including the Site
    Engineer's photos beside the Site Engineer step.
  * NOTICES TO A SITE ENGINEER carry the work (contractor, site, tasks), never the bill;
    the PM and SCM keep the bill summary.
  * THE RAISE FORM: every required field says so, the task tick-list is checked in the
    browser, and every re-render says to attach the PDF again.

Run with:
    python manage.py test projects.tests_contractor_bill_se --settings=solarpms.test_settings
"""
import re
from types import SimpleNamespace
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from django.urls import reverse

from . import approval_notices
from .approval_views import SITE_ENGINEER_WORK_NOTE, sees_bill
from .approvals import (
    ApprovalRefused, ProxyDecision, apply_approval_decision, resubmit_approval_request,
    withdraw_approval_request,
)
from .models import (
    ApprovalAttachment, ApprovalRequest, ApprovalStep,
    APPROVAL_CHANGES_REQUESTED, APPROVAL_PARTY_PM, APPROVAL_PARTY_SITE_ENGINEER,
    APPROVAL_PROXY_WHATSAPP, APPROVAL_STEP_APPROVED, APPROVAL_STEP_CHANGES_REQUESTED,
    APPROVAL_STEP_PENDING, APPROVAL_STEP_REJECTED,
)
from .tests_approval_notices import NoticeBase
from .tests_approvals import SE_PHOTO
from .tests_contractor_bill_screens import SIGNED, ScreenFixture, _pdf
from .tests_contractor_bills import BUCKET, PDF

PDF_EVIDENCE = {'file_name': 'chat.pdf', 'bucket': 'fixture-photos',
                'path': 'approvals/fixture/chat.pdf'}
BILL_B16 = 'Nobody has checked this amount against a rate or work order.'


def _patch(test, target, **kwargs):
    """Start a mock.patch for one test and stop it when the test ends."""
    patcher = mock.patch(target, **kwargs)
    started = patcher.start()
    test.addCleanup(patcher.stop)
    return started


def _jpg(name='site.jpg'):
    return SimpleUploadedFile(name, b'\xff\xd8\xff\xe0 site', content_type='image/jpeg')


# ===========================================================================
# The chokepoint
# ===========================================================================

class ChokepointTests(ScreenFixture):

    def setUp(self):
        super().setUp()
        self.approval = self.raise_bill()
        self.se_step = self.step(self.approval, APPROVAL_PARTY_SITE_ENGINEER)

    def assert_refused(self, text, *args, **kwargs):
        with self.assertRaises(ApprovalRefused) as caught:
            apply_approval_decision(*args, **kwargs)
        self.assertIn(text, str(caught.exception))
        self.se_step.refresh_from_db()
        self.assertEqual(self.se_step.verdict, APPROVAL_STEP_PENDING)
        self.assertFalse(ApprovalAttachment.objects.filter(request=self.approval).exists())

    def test_a_confirmation_without_a_photo_is_refused(self):
        self.assert_refused('Attach at least one photo', self.se_step,
                            APPROVAL_STEP_APPROVED, self.se)
        self.assert_refused('Attach at least one photo', self.se_step,
                            APPROVAL_STEP_APPROVED, self.se, files=[dict(PDF_EVIDENCE)])

    def test_a_site_engineer_reject_is_refused(self):
        self.assert_refused('never rejected at this step', self.se_step,
                            APPROVAL_STEP_REJECTED, self.se, note='Bad bill.',
                            files=[dict(SE_PHOTO)])

    def test_a_confirmation_with_a_photo_links_it_to_the_step_and_asks_the_pm(self):
        apply_approval_decision(self.se_step, APPROVAL_STEP_APPROVED, self.se,
                                files=[dict(SE_PHOTO)])
        photo = ApprovalAttachment.objects.get(request=self.approval)
        self.assertEqual((photo.step_id, photo.round, photo.file_name),
                         (self.se_step.pk, 1, 'work.jpg'))
        self.assertIsNotNone(self.step(self.approval, APPROVAL_PARTY_PM).activated_at)

    def test_work_not_done_needs_a_note_and_may_carry_photos(self):
        self.assert_refused('Say what has to change', self.se_step,
                            APPROVAL_STEP_CHANGES_REQUESTED, self.se)
        apply_approval_decision(self.se_step, APPROVAL_STEP_CHANGES_REQUESTED, self.se,
                                note='Block B not finished.', files=[dict(SE_PHOTO)])
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, APPROVAL_CHANGES_REQUESTED)
        self.assertEqual(ApprovalAttachment.objects.get().step_id, self.se_step.pk)

    def test_an_scm_proxy_confirmation_needs_a_photo_among_its_evidence(self):
        def proxy(files):
            return ProxyDecision(self.se, APPROVAL_PROXY_WHATSAPP, 'Sent photos.', files)
        self.assert_refused('Attach at least one photo', self.se_step,
                            APPROVAL_STEP_APPROVED, self.scm, proxy=proxy([]))
        self.assert_refused('Attach at least one photo', self.se_step,
                            APPROVAL_STEP_APPROVED, self.scm,
                            proxy=proxy([dict(PDF_EVIDENCE)]))
        apply_approval_decision(self.se_step, APPROVAL_STEP_APPROVED, self.scm,
                                proxy=proxy([dict(PDF_EVIDENCE), dict(SE_PHOTO)]))
        self.assertEqual(ApprovalAttachment.objects.filter(step=self.se_step).count(), 2)

    def test_photos_go_on_no_other_step_and_never_beside_a_proxy(self):
        apply_approval_decision(self.se_step, APPROVAL_STEP_APPROVED, self.se,
                                files=[dict(SE_PHOTO)])
        pm_step = self.step(self.approval, APPROVAL_PARTY_PM)
        with self.assertRaises(ApprovalRefused) as caught:
            apply_approval_decision(pm_step, APPROVAL_STEP_APPROVED, self.pm,
                                    files=[dict(SE_PHOTO)])
        self.assertIn("only to a Site Engineer's decision", str(caught.exception))
        with self.assertRaises(ApprovalRefused):
            apply_approval_decision(
                pm_step, APPROVAL_STEP_APPROVED, self.scm, files=[dict(SE_PHOTO)],
                proxy=ProxyDecision(self.pm, APPROVAL_PROXY_WHATSAPP, 'OK.'))
        pm_step.refresh_from_db()
        self.assertEqual(pm_step.verdict, APPROVAL_STEP_PENDING)

    def test_a_material_decision_is_unchanged(self):
        material = self.raise_material()
        apply_approval_decision(self.step(material, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_REJECTED, self.pm, note='No.')
        self.assertEqual(ApprovalRequest.objects.get(pk=material.pk).status, 'rejected')


# ===========================================================================
# The decide view
# ===========================================================================

class DecideViewTests(ScreenFixture):

    def setUp(self):
        super().setUp()
        self.approval = self.raise_bill()
        self.se_step = self.step(self.approval, APPROVAL_PARTY_SITE_ENGINEER)
        self.url = reverse('approval_decide', args=[self.se_step.pk])
        self.client_mock = _patch(self, 'projects.approval_views.get_supabase_client')
        self.upload = _patch(self, 'projects.approval_views._validate_and_upload')

    def post(self, **data):
        return self.client_for(self.se).post(self.url, data, follow=True)

    def messages_of(self, response):
        return [str(m) for m in response.context['messages']]

    def test_confirm_work_done_with_a_photo(self):
        response = self.post(verdict=APPROVAL_STEP_APPROVED, site_photos=[_jpg()])
        self.assertIn('Recorded: work confirmed for the Site Engineer step.',
                      self.messages_of(response))
        self.se_step.refresh_from_db()
        self.assertEqual(self.se_step.verdict, APPROVAL_STEP_APPROVED)
        photo = ApprovalAttachment.objects.get(step=self.se_step)
        self.assertEqual(photo.file_name, 'site.jpg')
        self.assertIn(f'approvals/{self.approval.pk}/round-1/site-{self.se_step.pk}/',
                      photo.path)

    def test_confirm_without_a_photo_is_refused_and_nothing_is_stored(self):
        response = self.post(verdict=APPROVAL_STEP_APPROVED)
        self.assertTrue(any('Attach at least one photo' in m
                            for m in self.messages_of(response)))
        self.upload.assert_not_called()
        self.se_step.refresh_from_db()
        self.assertEqual(self.se_step.verdict, APPROVAL_STEP_PENDING)

    def test_a_pdf_is_not_a_site_photo(self):
        response = self.post(verdict=APPROVAL_STEP_APPROVED, site_photos=[_pdf()])
        self.assertTrue(any(m.startswith('CB-9.pdf:') for m in self.messages_of(response)))
        self.upload.assert_not_called()

    def test_a_reject_is_refused_and_its_photo_removed(self):
        response = self.post(verdict=APPROVAL_STEP_REJECTED, note='Bad.',
                             site_photos=[_jpg()])
        self.assertTrue(any('never rejected' in m for m in self.messages_of(response)))
        path = self.upload.call_args.args[3]
        self.client_mock.return_value.storage.from_.return_value.remove.assert_called_once_with(
            [path])
        self.assertFalse(ApprovalAttachment.objects.exists())

    def test_work_not_done_sends_the_bill_back_to_scm(self):
        response = self.post(verdict=APPROVAL_STEP_CHANGES_REQUESTED,
                             note='Block B is not finished.')
        self.assertIn('Recorded: work not done for the Site Engineer step.',
                      self.messages_of(response))
        self.assertEqual(ApprovalRequest.objects.get(pk=self.approval.pk).status,
                         APPROVAL_CHANGES_REQUESTED)


# ===========================================================================
# What each viewer sees
# ===========================================================================

class VisibilityTests(ScreenFixture):
    """The bill raised with the site's PM as a DIFFERENT PM (pm_b), so a PM warning exists
    for the full view and must not reach the Site Engineer."""

    def setUp(self):
        super().setUp()
        self.approval = self.raise_bill(pm_assignee=self.pm_b)
        self.url = reverse('approval_detail', args=[self.approval.pk])
        self.signer = _patch(self, 'projects.bill_storage.get_design_file_url',
                             return_value=SIGNED)

    MONEY = ('₹12,500.00', 'CB-7', 'Bill number', 'Bill date', SIGNED, 'CB-7.pdf')

    def test_the_site_engineer_sees_the_work_only(self):
        page = self.client_for(self.se).get(self.url)
        content = page.content.decode()
        for text in self.MONEY + (BILL_B16, 'is not the assigned PM'):
            with self.subTest(hidden=text):
                self.assertNotIn(text, content)
        for text in ('Civil Co', f'{self.site.project_id} — Bill Site', 'Foundation',
                     'Wiring — Block A', 'Foundation, block A.', SITE_ENGINEER_WORK_NOTE,
                     'is not complete'):
            with self.subTest(shown=text):
                self.assertIn(text, content.replace('&#x27;', "'"))
        self.signer.assert_not_called()
        # Their step: confirm (photos required) and work not done; never a reject.
        self.assertIn('Confirm work done', content)
        self.assertIn('Work not done', content)
        self.assertRegex(content, r'name="site_photos"[^>]*required')
        self.assertNotIn('value="rejected"', content)

    def test_the_pm_and_scm_see_the_bill_and_the_site_engineers_photos(self):
        apply_approval_decision(self.step(self.approval, APPROVAL_PARTY_SITE_ENGINEER),
                                APPROVAL_STEP_APPROVED, self.se, files=[dict(SE_PHOTO)])
        for profile in (self.pm_b, self.scm):
            with self.subTest(viewer=profile.user.username):
                content = self.client_for(profile).get(self.url).content.decode()
                for text in self.MONEY + (BILL_B16, 'Site photos', 'work.jpg',
                                          'Work confirmed',
                                          'Site Engineer confirmed the work done (round 1)'):
                    self.assertIn(text, content)
                self.assertNotIn(SITE_ENGINEER_WORK_NOTE, content)

    def test_history_and_rounds_hide_the_bill_from_the_site_engineer_after_a_resubmit(self):
        apply_approval_decision(self.step(self.approval, APPROVAL_PARTY_SITE_ENGINEER),
                                APPROVAL_STEP_APPROVED, self.se, files=[dict(SE_PHOTO)])
        apply_approval_decision(self.step(self.approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.pm_b, note='Rate?')
        resubmit_approval_request(self.approval, self.scm, 'Rate explained.',
                                  carry={APPROVAL_PARTY_SITE_ENGINEER: 'Work unchanged.'})
        content = self.client_for(self.se).get(self.url).content.decode()
        for text in self.MONEY:
            self.assertNotIn(text, content)
        # The kept Site Engineer step shows the photos of the step it keeps (P6).
        self.assertIn('Photos from round 1', content)
        self.assertIn('Site Engineer confirmed the work done (round 1)', content)

    def test_scms_proxy_form_on_the_site_engineer_step_has_two_outcomes(self):
        se_step = self.step(self.approval, APPROVAL_PARTY_SITE_ENGINEER)
        content = self.client_for(self.scm).get(self.url).content.decode()
        action = reverse('approval_record_proxy', args=[se_step.pk])
        form = content[content.index(f'action="{action}"'):].split('</form>')[0]
        options = re.findall(r'<option value="(approved|changes_requested|rejected)">', form)
        self.assertEqual(options, ['approved', 'changes_requested'])


class SeesBillTests(SimpleTestCase):
    """The predicate on its own: only a viewer whose sole standing is the Site Engineer
    step is refused the bill."""

    def viewer(self, pk, role='Site Engineer'):
        return SimpleNamespace(profile=SimpleNamespace(pk=pk, role=role))

    def test_the_rule(self):
        approval = SimpleNamespace(raised_by_id=1)
        steps = [SimpleNamespace(party=APPROVAL_PARTY_SITE_ENGINEER, assignee_id=2,
                                 decided_by_id=None),
                 SimpleNamespace(party=APPROVAL_PARTY_PM, assignee_id=3, decided_by_id=None)]
        self.assertFalse(sees_bill(self.viewer(2), approval, steps))
        self.assertTrue(sees_bill(self.viewer(3, 'PM'), approval, steps))
        self.assertTrue(sees_bill(self.viewer(1, 'SCM'), approval, steps))
        self.assertTrue(sees_bill(self.viewer(9, 'CEO'), approval, steps))
        # A past Site Engineer now named on the PM step sees it all.
        steps.append(SimpleNamespace(party=APPROVAL_PARTY_PM, assignee_id=2,
                                     decided_by_id=None))
        self.assertTrue(sees_bill(self.viewer(2), approval, steps))


# ===========================================================================
# Notices
# ===========================================================================

class SiteEngineerNoticeTests(NoticeBase):

    def setUp(self):
        super().setUp()
        self.signer = _patch(self, 'projects.bill_storage.get_design_file_url')
        self.addCleanup(lambda: self.signer.assert_not_called())

    def mail_to(self, username):
        return [m for m in self.mail if m['to'] == f'{username}@example.com']

    def assert_no_bill(self, mail):
        for part in ('text', 'html'):
            for secret in ('₹', '12,500', '12500', 'CB-001', 'bill.pdf', 'fixture-bills'):
                self.assertNotIn(secret, mail[part], f'{secret} in the {part} part')

    def test_e1_to_the_site_engineer_carries_the_work_not_the_bill(self):
        approval, _ = self.act(self.raise_bill)
        mail = self.mail_to('ap_se')
        self.assertEqual(len(mail), 1)
        site = approval.bill_detail.project.project_id
        self.assertEqual(mail[0]['subject'], 'Work to confirm: Civil works bill')
        self.assertTrue(mail[0]['text'].endswith(
            f'\n\nWork: Approval Contractor · {site} · Foundation'), mail[0]['text'])
        self.assertIn(f'Work:</span> Approval Contractor · {site} · Foundation',
                      mail[0]['html'])
        self.assert_no_bill(mail[0])
        self.assertIn('asks you to confirm the work on "Civil works bill"',
                      self.note_for(self.se))

    def test_the_pm_and_scm_keep_the_bill_summary(self):
        approval, _ = self.act(self.raise_bill)
        self.decide(approval, APPROVAL_PARTY_SITE_ENGINEER, APPROVAL_STEP_APPROVED, self.se,
                    files=[dict(SE_PHOTO)])
        pm = self.mail_to('ap_pm')[-1]
        self.assertIn('\n\nBill: Approval Contractor', pm['text'])
        self.assertIn('₹12,500.00', pm['text'])
        self.assertIn('Ap_Se confirmed the work done as the Site Engineer',
                      self.note_for(self.pm))
        self.decide(approval, APPROVAL_PARTY_PM, APPROVAL_STEP_APPROVED, self.pm)
        self.assertIn('\n\nBill: ', self.mail_to('ap_scm')[-1]['text'])

    def test_e3_to_the_site_engineer_carries_the_work(self):
        approval, _ = self.act(self.raise_bill)
        self.act(withdraw_approval_request, approval, self.scm, 'Raised twice.')
        mail = self.mail_to('ap_se')[-1]
        self.assertIn('\n\nWork: ', mail['text'])
        self.assert_no_bill(mail)

    def test_work_not_done_tells_scm_in_those_words(self):
        approval, _ = self.act(self.raise_bill)
        self.decide(approval, APPROVAL_PARTY_SITE_ENGINEER, APPROVAL_STEP_CHANGES_REQUESTED,
                    self.se, note='Block B not finished.')
        self.assertIn('Ap_Se said the work on "Civil works bill" (Contractor bill) is not '
                      'done, as the Site Engineer.', self.note_for(self.scm))

    def test_the_work_summary_names_the_first_task_and_counts_the_rest(self):
        from .models import ProjectPhase, Task
        approval = self.raise_bill()
        detail = approval.bill_detail
        phase = ProjectPhase.objects.get(project=detail.project)
        extra = Task.objects.create(phase=phase, task_name='Wiring', task_order=2)
        detail.task_links.create(task=extra)
        self.assertEqual(approval_notices.work_summary(approval),
                         f'Approval Contractor · {detail.project.project_id} · '
                         f'Foundation, +1 more')


# ===========================================================================
# The raise form never fails silently
# ===========================================================================

class RaiseFormTests(ScreenFixture):

    REATTACH = ('Attach the bill PDF again — the browser does not keep a chosen file '
                'after the page reloads.')

    def test_every_required_field_says_so_and_the_task_list_is_checked_in_the_browser(self):
        client = self.client_for(self.scm)
        content = client.get(self.step2_url(client)).content.decode()
        for name in ('vendor', 'bill_number', 'bill_date', 'amount', 'bill_pdf',
                     'site_engineer_assignee', 'pm_assignee', 'title', 'description'):
            with self.subTest(field=name):
                tag = re.search(rf'<(input|select|textarea)[^>]*name="{name}"[^>]*>', content)
                self.assertIn('required', tag.group(0))
        self.assertIn("setCustomValidity(none ? 'Tick at least one task", content)
        self.assertIn('The Site Engineer sees the title and description. Do not put the '
                      'amount in them.', content)
        self.assertNotIn(self.REATTACH, content)

    def test_every_re_render_says_to_attach_the_pdf_again(self):
        self.uploads()
        client = self.client_for(self.scm)
        url = self.step2_url(client)
        warned = client.post(url, self.form())                          # warnings, 200
        refused = client.post(url, self.form(bill_number=''))           # refusal, 400
        for response in (warned, refused):
            with self.subTest(status=response.status_code):
                content = response.content.decode()
                self.assertEqual(content.count(self.REATTACH), 2)       # banner + beside
                self.assertIn("scrollIntoView({ block: 'start' })", content)
        self.assertEqual((warned.status_code, refused.status_code), (200, 400))
