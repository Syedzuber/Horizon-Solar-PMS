"""Approvals S2a — the material pre-order screens (approval_views.py). Approvals 2a-2 adds
the revision screens: editable resubmit, kept approvals, round snapshots and the change
list, proxy evidence files, and a History that names who did every action (the last four
classes).

What this file pins, and why each matters:

  * WHO MAY SEE AND WHO MAY ACT. Every view: an allowed role gets 200/302, a disallowed
    one a 403 WITH A BODY (decorators._forbidden), and an anonymous caller the login
    redirect. The list shows exactly what the detail page admits.
  * NOT YOURS IS 403; NO LONGER OPEN IS A MESSAGE. A PM who is not the assignee, and the
    raiser, are refused at the door. A stale decision, or anything else the chokepoint
    refuses, comes back as a message on the request page — never a 500.
  * DOUBLE-SUBMIT CREATES ONE REQUEST (client_uuid).
  * A PROXY SAYS SO, and TURNAROUND COMES FROM THE STEP ROW — pinned against a step whose
    ledger actor (the decider) differs from its recorded_by (SCM).
  * FILES: uploaded before the chokepoint, removed again when it refuses.

Run with:
    python manage.py test projects.tests_approval_views --settings=solarpms.test_settings
and under the real settings (Postgres, migrations applied):
    python manage.py test projects.tests_approval_views
"""
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .approval_views import step_turnaround
from .approvals import (
    ProxyDecision, apply_approval_decision, create_approval_request,
    resubmit_approval_request, round_snapshot, withdraw_approval_request,
)
from .utils import record_transition
from .models import (
    ApprovalAttachment, ApprovalRequest, ApprovalRoundSnapshot, ApprovalStep, Program,
    Project, SiteGroup, StatusTransition, Vendor, VendorOrder, VendorOrderSite,
    APPROVAL_APPROVED, APPROVAL_CHANGES_REQUESTED, APPROVAL_KIND_MATERIAL_PRE_DISPATCH,
    APPROVAL_KIND_MATERIAL_PRE_ORDER,
    APPROVAL_OPEN, APPROVAL_WITHDRAWN,
    APPROVAL_PARTY_DESIGN, APPROVAL_PARTY_PM,
    APPROVAL_STEP_APPROVED, APPROVAL_STEP_CHANGES_REQUESTED, APPROVAL_STEP_PENDING,
    APPROVAL_STEP_SUPERSEDED, GROUP_TYPE_PROCUREMENT, SUBJECT_APPROVAL_REQUEST,
)
from .permissions import user_can_view_vendor_order


def _profile(username, role, **flags):
    """A post_save signal creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.replace('_', ' ').title())
    profile = user.profile
    profile.role = role
    profile.is_active = True
    for field, value in flags.items():
        setattr(profile, field, value)
    profile.save()
    return profile


class ApprovalViewFixture(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.scm      = _profile('av_scm', 'SCM')
        cls.scm_b    = _profile('av_scm_b', 'SCM')
        cls.pm       = _profile('av_pm', 'PM')
        cls.pm_b     = _profile('av_pm_b', 'PM')
        cls.head     = _profile('av_head', 'Design', is_design_head=True)
        cls.deputy   = _profile('av_deputy', 'Design')
        cls.head.design_head_deputy = cls.deputy
        cls.head.save(update_fields=['design_head_deputy'])
        cls.designer = _profile('av_designer', 'Design')          # no Head authority
        cls.ceo      = _profile('av_ceo', 'CEO')
        cls.admin    = _profile('av_admin', 'Admin')
        cls.sysadmin = _profile('av_sysadmin', 'System Admin')
        cls.finance  = _profile('av_finance', 'Finance')
        cls.se       = _profile('av_se', 'Site Engineer')
        cls.vendor = Vendor.objects.create(name='AV Vendor', contact_person='R',
                                           phone='9000000002')

    # ── helpers ─────────────────────────────────────────────────────────────

    def raise_material(self, design=True, raised_by=None, **overrides):
        kwargs = dict(
            kind=APPROVAL_KIND_MATERIAL_PRE_ORDER, raised_by=raised_by or self.scm,
            title='Module make', description='Propose Waaree 545 Wp.',
            pm_assignee=self.pm, design_signoff_required=design,
            design_assignee=self.head if design else None,
            material={'proposed_make': 'Waaree', 'specification': '545 Wp mono PERC'},
        )
        kwargs.update(overrides)
        return create_approval_request(**kwargs)

    def step(self, approval, party):
        approval.refresh_from_db()
        return ApprovalStep.objects.exclude(verdict=APPROVAL_STEP_SUPERSEDED).get(
            request=approval, party=party, round=approval.current_round)

    def client_for(self, profile):
        client = Client(SERVER_NAME='localhost')
        client.force_login(profile.user)
        return client

    def messages_of(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def assert_forbidden(self, response):
        self.assertEqual(response.status_code, 403)
        self.assertIn(b'Access denied', response.content)   # _forbidden's body, not empty

    def assert_to_detail(self, response, approval):
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'],
                         reverse('approval_detail', args=[approval.pk]))


# ===========================================================================
# Access: every view, allowed / disallowed / anonymous
# ===========================================================================

class AccessTests(ApprovalViewFixture):

    def setUp(self):
        self.approval = self.raise_material()
        self.pm_step = self.step(self.approval, APPROVAL_PARTY_PM)

    def test_anonymous_is_sent_to_login_everywhere(self):
        anon = Client(SERVER_NAME='localhost')
        gets = [reverse('approval_list'), reverse('approval_create'),
                reverse('approval_detail', args=[self.approval.pk]),
                reverse('approval_resubmit', args=[self.approval.pk])]
        posts = [reverse('approval_decide', args=[self.pm_step.pk]),
                 reverse('approval_record_proxy', args=[self.pm_step.pk]),
                 reverse('approval_reassign', args=[self.pm_step.pk]),
                 reverse('approval_withdraw', args=[self.approval.pk])]
        for url in gets:
            response = anon.get(url)
            self.assertEqual((response.status_code, response['Location']), (302, '/login/'), url)
        for url in posts:
            response = anon.post(url, {'note': 'x'})
            self.assertEqual((response.status_code, response['Location']), (302, '/login/'), url)
        self.assertEqual(self.step(self.approval, APPROVAL_PARTY_PM).verdict,
                         APPROVAL_STEP_PENDING)

    def test_list(self):
        url = reverse('approval_list')
        for profile in (self.scm, self.pm, self.head, self.deputy, self.ceo, self.admin,
                        self.sysadmin):
            self.assertEqual(self.client_for(profile).get(url).status_code, 200,
                             profile.user.username)
        for profile in (self.finance, self.se, self.designer):
            self.assert_forbidden(self.client_for(profile).get(url))

    def test_create(self):
        url = reverse('approval_create')
        self.assertEqual(self.client_for(self.scm).get(url).status_code, 302)   # to ?key=
        self.assertEqual(self.client_for(self.scm).get(url, follow=True).status_code, 200)
        for profile in (self.pm, self.head, self.ceo, self.admin, self.finance):
            self.assert_forbidden(self.client_for(profile).get(url))
            self.assert_forbidden(self.client_for(profile).post(url, {'title': 'x'}))
        self.assertEqual(ApprovalRequest.objects.count(), 1)

    def test_detail(self):
        url = reverse('approval_detail', args=[self.approval.pk])
        for profile in (self.scm, self.scm_b, self.ceo, self.admin, self.sysadmin,
                        self.pm, self.head, self.deputy):
            self.assertEqual(self.client_for(profile).get(url).status_code, 200,
                             profile.user.username)
        for profile in (self.pm_b, self.finance, self.designer, self.se):
            self.assert_forbidden(self.client_for(profile).get(url))

    def test_resubmit(self):
        apply_approval_decision(self.pm_step, APPROVAL_STEP_CHANGES_REQUESTED, self.pm,
                                note='Use 550 Wp.')
        url = reverse('approval_resubmit', args=[self.approval.pk])
        self.assertEqual(self.client_for(self.scm).get(url).status_code, 200)
        for profile in (self.pm, self.head, self.ceo, self.pm_b):
            self.assert_forbidden(self.client_for(profile).get(url))
            self.assert_forbidden(self.client_for(profile).post(url, {'note': 'x'}))
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.current_round, 1)

    def test_decide(self):
        url = reverse('approval_decide', args=[self.pm_step.pk])
        for profile in (self.pm_b, self.scm, self.ceo, self.head, self.finance):
            self.assert_forbidden(self.client_for(profile).post(
                url, {'verdict': APPROVAL_STEP_APPROVED}))
        self.assertEqual(self.step(self.approval, APPROVAL_PARTY_PM).verdict,
                         APPROVAL_STEP_PENDING)
        response = self.client_for(self.pm).post(url, {'verdict': APPROVAL_STEP_APPROVED})
        self.assert_to_detail(response, self.approval)
        self.assertEqual(self.step(self.approval, APPROVAL_PARTY_PM).verdict,
                         APPROVAL_STEP_APPROVED)

    def test_proxy(self):
        url = reverse('approval_record_proxy', args=[self.pm_step.pk])
        data = {'verdict': APPROVAL_STEP_APPROVED, 'decided_by': self.pm.pk,
                'channel': 'whatsapp', 'evidence': 'WhatsApp 10:02 "ok"'}
        for profile in (self.pm, self.ceo, self.admin, self.head):
            self.assert_forbidden(self.client_for(profile).post(url, data))
        self.assert_to_detail(self.client_for(self.scm).post(url, data), self.approval)
        self.assertTrue(self.step(self.approval, APPROVAL_PARTY_PM).is_proxy)

    def test_withdraw(self):
        url = reverse('approval_withdraw', args=[self.approval.pk])
        for profile in (self.pm, self.ceo, self.admin, self.head):
            self.assert_forbidden(self.client_for(profile).post(url, {'note': 'x'}))
        self.assert_to_detail(self.client_for(self.scm_b).post(url, {'note': 'Not needed.'}),
                              self.approval)
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, APPROVAL_WITHDRAWN)

    def test_reassign(self):
        url = reverse('approval_reassign', args=[self.pm_step.pk])
        data = {'new_assignee': self.pm_b.pk, 'note': 'On leave.'}
        for profile in (self.pm, self.ceo, self.head):
            self.assert_forbidden(self.client_for(profile).post(url, data))
        self.assert_to_detail(self.client_for(self.scm).post(url, data), self.approval)
        self.assertEqual(self.step(self.approval, APPROVAL_PARTY_PM).assignee, self.pm_b)

    def test_get_on_a_post_only_view_changes_nothing(self):
        response = self.client_for(self.pm).get(
            reverse('approval_decide', args=[self.pm_step.pk]))
        self.assert_to_detail(response, self.approval)
        self.assertEqual(self.step(self.approval, APPROVAL_PARTY_PM).verdict,
                         APPROVAL_STEP_PENDING)


# ===========================================================================
# Deciding: not yours is 403, no longer open is a message
# ===========================================================================

class DecideTests(ApprovalViewFixture):

    def test_a_pm_who_is_not_the_assignee_cannot_decide(self):
        approval = self.raise_material(design=False)
        pm_step = self.step(approval, APPROVAL_PARTY_PM)
        response = self.client_for(self.pm_b).post(
            reverse('approval_decide', args=[pm_step.pk]),
            {'verdict': APPROVAL_STEP_APPROVED})
        self.assert_forbidden(response)
        self.assertEqual(self.step(approval, APPROVAL_PARTY_PM).verdict,
                         APPROVAL_STEP_PENDING)

    def test_the_raiser_cannot_decide_the_pm_step(self):
        approval = self.raise_material(design=False)
        pm_step = self.step(approval, APPROVAL_PARTY_PM)
        self.assert_forbidden(self.client_for(self.scm).post(
            reverse('approval_decide', args=[pm_step.pk]),
            {'verdict': APPROVAL_STEP_APPROVED}))
        self.assertEqual(self.step(approval, APPROVAL_PARTY_PM).verdict,
                         APPROVAL_STEP_PENDING)

    def test_a_raiser_with_head_authority_is_refused_by_the_chokepoint_as_a_message(self):
        """The door admits Design Head authority on a design step; the chokepoint's
        same-person rule then refuses the raiser. That refusal is a message, not a 500."""
        scm_head = _profile('av_scm_head', 'SCM', is_design_head=True)
        approval = self.raise_material(raised_by=scm_head)
        design_step = self.step(approval, APPROVAL_PARTY_DESIGN)
        response = self.client_for(scm_head).post(
            reverse('approval_decide', args=[design_step.pk]),
            {'verdict': APPROVAL_STEP_APPROVED})
        self.assert_to_detail(response, approval)
        self.assertTrue(any('raised this request and cannot also approve it' in m
                            for m in self.messages_of(response)))
        self.assertEqual(self.step(approval, APPROVAL_PARTY_DESIGN).verdict,
                         APPROVAL_STEP_PENDING)

    def test_a_refusal_surfaces_as_a_message_not_a_500(self):
        approval = self.raise_material(design=False)
        pm_step = self.step(approval, APPROVAL_PARTY_PM)
        response = self.client_for(self.pm).post(
            reverse('approval_decide', args=[pm_step.pk]),
            {'verdict': APPROVAL_STEP_CHANGES_REQUESTED, 'note': '   '}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Say what has to change, or why it is rejected.')
        self.assertEqual(self.step(approval, APPROVAL_PARTY_PM).verdict,
                         APPROVAL_STEP_PENDING)

    def test_a_stale_decision_on_a_closed_step_is_refused_cleanly(self):
        approval = self.raise_material(design=False)
        pm_step = self.step(approval, APPROVAL_PARTY_PM)
        page = self.client_for(self.pm).get(reverse('approval_detail', args=[approval.pk]))
        self.assertContains(page, reverse('approval_decide', args=[pm_step.pk]))
        # Meanwhile SCM withdraws it; the PM's open page still posts.
        withdraw_approval_request(approval, self.scm, 'Vendor dropped out.')
        response = self.client_for(self.pm).post(
            reverse('approval_decide', args=[pm_step.pk]),
            {'verdict': APPROVAL_STEP_APPROVED})
        self.assert_to_detail(response, approval)
        self.assertIn('This request is withdrawn; nothing more can be decided on it.',
                      self.messages_of(response))
        self.assertEqual(ApprovalStep.objects.get(pk=pm_step.pk).verdict,
                         APPROVAL_STEP_SUPERSEDED)

    def test_a_second_decision_on_the_same_step_is_refused_cleanly(self):
        approval = self.raise_material(design=True)
        pm_step = self.step(approval, APPROVAL_PARTY_PM)
        url = reverse('approval_decide', args=[pm_step.pk])
        self.client_for(self.pm).post(url, {'verdict': APPROVAL_STEP_APPROVED})
        response = self.client_for(self.pm).post(url, {'verdict': APPROVAL_STEP_APPROVED})
        self.assert_to_detail(response, approval)
        self.assertIn('The PM step is already approved; nothing was recorded.',
                      self.messages_of(response))

    def test_a_closed_step_is_read_only_not_forbidden(self):
        approval = self.raise_material(design=True)
        pm_step = self.step(approval, APPROVAL_PARTY_PM)
        apply_approval_decision(pm_step, APPROVAL_STEP_APPROVED, self.pm)
        response = self.client_for(self.pm).get(reverse('approval_detail', args=[approval.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, reverse('approval_decide', args=[pm_step.pk]))
        self.assertContains(response, 'Approved')

    def test_the_deputy_decides_the_heads_step(self):
        approval = self.raise_material(design=True)
        design_step = self.step(approval, APPROVAL_PARTY_DESIGN)
        response = self.client_for(self.deputy).post(
            reverse('approval_decide', args=[design_step.pk]),
            {'verdict': APPROVAL_STEP_APPROVED})
        self.assert_to_detail(response, approval)
        design_step.refresh_from_db()
        self.assertEqual((design_step.verdict, design_step.decided_by),
                         (APPROVAL_STEP_APPROVED, self.deputy))


# ===========================================================================
# Raise
# ===========================================================================

class CreateTests(ApprovalViewFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.program = Program.objects.create(
            name='AV Tender', program_type='OPEX', client_name='Client', status='Active',
            short_tender_code='AVT')
        cls.gone_program = Program.objects.create(
            name='AV Gone', program_type='OPEX', client_name='Client', status='Active',
            short_tender_code='AVG', is_deleted=True)
        cls.site = Project.objects.create(
            customer_name='AV Site', status='Active', customer_phone='9876543210',
            site_address='1 Sun Road', city='Lucknow', state='Uttar Pradesh',
            project_type='Residential', dc_capacity_kw=Decimal('5.00'), assigned_pm=cls.pm)
        cls.group = SiteGroup.objects.create(program=cls.program, name='AV Batch',
                                             group_type=GROUP_TYPE_PROCUREMENT,
                                             created_by=cls.pm)

    def form(self, **overrides):
        data = {'client_uuid': str(uuid.uuid4()), 'title': 'Inverter make',
                'description': 'Propose Sungrow for the tender.',
                'proposed_make': 'Sungrow', 'specification': '50 kW',
                'quantity_note': '12 units', 'pm_assignee': self.pm.pk,
                'design_signoff_required': 'on', 'design_assignee': self.head.pk,
                'program': [self.program.pk], 'project': [self.site.pk],
                'site_group': [self.group.pk], 'vendor': self.vendor.pk}
        data.update(overrides)
        return data

    def test_raise_writes_one_request_through_the_chokepoint(self):
        response = self.client_for(self.scm).post(reverse('approval_create'), self.form())
        approval = ApprovalRequest.objects.get()
        self.assert_to_detail(response, approval)
        self.assertEqual((approval.kind, approval.raised_by, approval.vendor,
                          approval.design_signoff_required),
                         (APPROVAL_KIND_MATERIAL_PRE_ORDER, self.scm, self.vendor, True))
        self.assertEqual(approval.material_detail.proposed_make, 'Sungrow')
        self.assertEqual((list(approval.programs.all()), list(approval.projects.all()),
                          list(approval.site_groups.all())),
                         ([self.program], [self.site], [self.group]))
        self.assertEqual(set(approval.steps.values_list('party', 'assignee')),
                         {(APPROVAL_PARTY_PM, self.pm.pk),
                          (APPROVAL_PARTY_DESIGN, self.head.pk)})

    def test_double_submit_with_the_same_client_uuid_creates_one_request(self):
        data = self.form()
        client = self.client_for(self.scm)
        first = client.post(reverse('approval_create'), data)
        second = client.post(reverse('approval_create'), data)
        approval = ApprovalRequest.objects.get()
        self.assert_to_detail(first, approval)
        self.assert_to_detail(second, approval)
        self.assertEqual(ApprovalRequest.objects.count(), 1)
        self.assertEqual(ApprovalStep.objects.count(), 2)

    def test_back_button_then_submit_again_raises_nothing_new(self):
        """The walkthrough failure (27 Sep): Raise, press Back, Raise again. The browser
        re-fetched the form, the old page minted a new key per GET, and a second request
        was written. The key now lives in the URL, so the re-fetch carries it."""
        client = self.client_for(self.scm)
        opened = client.get(reverse('approval_create'))
        self.assertEqual(opened.status_code, 302)
        form_url = opened['Location']
        self.assertIn('?key=', form_url)
        key = form_url.split('?key=', 1)[1]
        self.assertContains(client.get(form_url), f'value="{key}"')

        data = self.form()
        del data['client_uuid']          # rely on the URL's key alone
        first = client.post(form_url, data)
        approval = ApprovalRequest.objects.get()
        self.assert_to_detail(first, approval)

        back = client.get(form_url)      # Back: the browser fetches the page again
        self.assertContains(back, f'This form has already raised')
        self.assertContains(back, f'request #{approval.pk}')
        self.assertNotContains(back, '>Raise approval</button>')

        again = client.post(form_url, data)
        self.assert_to_detail(again, approval)
        self.assertEqual(ApprovalRequest.objects.count(), 1)

    def test_a_fresh_open_gets_a_fresh_key(self):
        client = self.client_for(self.scm)
        first = client.get(reverse('approval_create'))['Location']
        second = client.get(reverse('approval_create'))['Location']
        self.assertNotEqual(first, second)
        malformed = client.get(reverse('approval_create') + '?key=nonsense')
        self.assertEqual(malformed.status_code, 302)
        self.assertIn('?key=', malformed['Location'])

    def test_design_head_is_ignored_unless_signoff_is_ticked(self):
        data = self.form()
        del data['design_signoff_required']
        self.client_for(self.scm).post(reverse('approval_create'), data)
        approval = ApprovalRequest.objects.get()
        self.assertFalse(approval.design_signoff_required)
        self.assertEqual(list(approval.steps.values_list('party', flat=True)),
                         [APPROVAL_PARTY_PM])

    def test_a_chokepoint_refusal_redraws_the_form_with_its_message(self):
        response = self.client_for(self.scm).post(
            reverse('approval_create'), self.form(design_assignee=''))
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'Choose the Design Head who will sign off.',
                            status_code=400)
        self.assertContains(response, 'Inverter make', status_code=400)   # kept
        self.assertFalse(ApprovalRequest.objects.exists())

    def test_a_deleted_tender_is_refused_before_anything_is_written(self):
        response = self.client_for(self.scm).post(
            reverse('approval_create'), self.form(program=[self.gone_program.pk]))
        self.assertContains(response, 'A tender you chose no longer exists.',
                            status_code=400)
        self.assertFalse(ApprovalRequest.objects.exists())

    def test_the_form_offers_live_scope_only_and_preselects_the_only_head(self):
        response = self.client_for(self.scm).get(reverse('approval_create'), follow=True)
        self.assertContains(response, 'AV Tender')
        self.assertNotContains(response, 'AV Gone')
        self.assertContains(response, f'<option value="{self.head.pk}" selected>')

    @mock.patch('projects.approval_views.get_supabase_client')
    def test_files_are_stored_then_attached(self, get_client):
        upload = SimpleUploadedFile('datasheet.pdf', b'%PDF-1.4 x',
                                    content_type='application/pdf')
        response = self.client_for(self.scm).post(
            reverse('approval_create'), dict(self.form(), attachments=[upload]))
        approval = ApprovalRequest.objects.get()
        self.assert_to_detail(response, approval)
        attachment = ApprovalAttachment.objects.get(request=approval)
        self.assertEqual((attachment.round, attachment.file_name, attachment.uploaded_by),
                         (1, 'datasheet.pdf', self.scm))
        self.assertTrue(attachment.path.startswith('approvals/'))
        get_client.return_value.storage.from_.return_value.upload.assert_called_once()
        get_client.return_value.storage.from_.return_value.remove.assert_not_called()

    @mock.patch('projects.approval_views.get_supabase_client')
    def test_files_are_removed_again_when_the_chokepoint_refuses(self, get_client):
        upload = SimpleUploadedFile('datasheet.pdf', b'%PDF-1.4 x',
                                    content_type='application/pdf')
        response = self.client_for(self.scm).post(
            reverse('approval_create'),
            dict(self.form(pm_assignee=''), attachments=[upload]))
        self.assertContains(response, 'Choose the PM who will approve this.',
                            status_code=400)
        bucket = get_client.return_value.storage.from_.return_value
        bucket.upload.assert_called_once()
        stored_path = bucket.upload.call_args.kwargs['path']
        bucket.remove.assert_called_once_with([stored_path])
        self.assertFalse(ApprovalAttachment.objects.exists())

    @mock.patch('projects.approval_views.get_supabase_client')
    def test_a_bad_file_is_refused_before_anything_is_stored(self, get_client):
        upload = SimpleUploadedFile('run.exe', b'MZ', content_type='application/octet-stream')
        response = self.client_for(self.scm).post(
            reverse('approval_create'), dict(self.form(), attachments=[upload]))
        self.assertContains(response, 'run.exe: unsupported type (.exe).', status_code=400)
        get_client.assert_not_called()
        self.assertFalse(ApprovalRequest.objects.exists())


# ===========================================================================
# Detail: proxy steps, turnaround, history
# ===========================================================================

class DetailTests(ApprovalViewFixture):

    def test_a_proxy_step_is_shown_as_a_proxy(self):
        approval = self.raise_material(design=True)
        pm_step = self.step(approval, APPROVAL_PARTY_PM)
        self.client_for(self.scm).post(
            reverse('approval_record_proxy', args=[pm_step.pk]),
            {'verdict': APPROVAL_STEP_APPROVED, 'decided_by': self.pm.pk,
             'channel': 'whatsapp', 'evidence': 'WhatsApp 26 Sep 10:02: "go ahead"'})
        response = self.client_for(self.head).get(
            reverse('approval_detail', args=[approval.pk]))
        self.assertContains(response, 'Recorded by Av Scm from WhatsApp')
        self.assertContains(response, 'WhatsApp 26 Sep 10:02: &quot;go ahead&quot;')
        self.assertContains(response, '>Proxy<')

    def test_a_step_its_decider_typed_is_not_shown_as_a_proxy(self):
        approval = self.raise_material(design=False)
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_APPROVED, self.pm)
        response = self.client_for(self.scm).get(
            reverse('approval_detail', args=[approval.pk]))
        self.assertNotContains(response, 'Recorded by')

    def test_turnaround_is_computed_from_the_step_row_not_the_ledger(self):
        """A proxy approval that closes the request: the ledger row's actor is the
        DECIDER (the PM) and its time is when the status moved, while the step row's
        recorded_by is SCM. The page must show decided_at − activated_at from the step."""
        asked = datetime(2026, 9, 1, 9, 0, tzinfo=dt_timezone.utc)
        answered = asked + timedelta(hours=2, minutes=15)
        with mock.patch.object(timezone, 'now', return_value=asked):
            approval = self.raise_material(design=False)
        pm_step = self.step(approval, APPROVAL_PARTY_PM)
        with mock.patch.object(timezone, 'now', return_value=answered):
            apply_approval_decision(
                pm_step, APPROVAL_STEP_APPROVED, self.scm,
                proxy=ProxyDecision(self.pm, 'phone', 'Call at 11:15.'))

        pm_step.refresh_from_db()
        ledger = StatusTransition.objects.get(subject_type=SUBJECT_APPROVAL_REQUEST,
                                              subject_id=approval.pk,
                                              to_status=APPROVAL_APPROVED)
        self.assertEqual(ledger.actor, self.pm)              # the decider
        self.assertEqual(pm_step.recorded_by, self.scm)       # who typed it
        self.assertNotEqual(ledger.actor, pm_step.recorded_by)
        self.assertEqual(step_turnaround(pm_step), timedelta(hours=2, minutes=15))

        response = self.client_for(self.ceo).get(
            reverse('approval_detail', args=[approval.pk]))
        self.assertContains(response, '2 h 15 min')
        self.assertContains(response, 'Recorded by Av Scm from a phone call')

    def test_each_parallel_step_has_its_own_turnaround(self):
        """The PM's proxy approval moves no status, so it has NO ledger row; the only
        'approved' row is the Head's, 5 h in, with the Head as actor. Each step still
        shows its own decided_at − activated_at."""
        asked = datetime(2026, 9, 1, 9, 0, tzinfo=dt_timezone.utc)
        with mock.patch.object(timezone, 'now', return_value=asked):
            approval = self.raise_material(design=True)
        pm_step = self.step(approval, APPROVAL_PARTY_PM)
        design_step = self.step(approval, APPROVAL_PARTY_DESIGN)
        with mock.patch.object(timezone, 'now', return_value=asked + timedelta(hours=2, minutes=15)):
            apply_approval_decision(pm_step, APPROVAL_STEP_APPROVED, self.scm,
                                    proxy=ProxyDecision(self.pm, 'whatsapp', 'ok at 11:15'))
        with mock.patch.object(timezone, 'now', return_value=asked + timedelta(hours=5)):
            apply_approval_decision(design_step, APPROVAL_STEP_APPROVED, self.head)

        ledger = list(StatusTransition.objects.filter(
            subject_type=SUBJECT_APPROVAL_REQUEST, subject_id=approval.pk,
            to_status=APPROVAL_APPROVED))
        self.assertEqual([row.actor for row in ledger], [self.head])
        pm_step.refresh_from_db()
        self.assertEqual(pm_step.recorded_by, self.scm)
        self.assertEqual(ledger[0].occurred_at - pm_step.activated_at, timedelta(hours=5))
        self.assertEqual(step_turnaround(pm_step), timedelta(hours=2, minutes=15))

        response = self.client_for(self.scm).get(
            reverse('approval_detail', args=[approval.pk]))
        self.assertContains(response, '2 h 15 min')
        self.assertContains(response, '<dd class="col-7">5 h</dd>', html=False)

    def test_every_round_is_drawn_and_the_resubmit_note_is_in_the_history(self):
        approval = self.raise_material(design=False)
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.pm, note='Use 550 Wp.')
        self.client_for(self.scm).post(reverse('approval_resubmit', args=[approval.pk]),
                                       {'note': 'Changed to 550 Wp.'})
        response = self.client_for(self.pm).get(reverse('approval_detail', args=[approval.pk]))
        self.assertContains(response, 'Round 2')
        self.assertContains(response, 'Round 1')
        self.assertContains(response, 'Use 550 Wp.')
        self.assertContains(response, 'Changed to 550 Wp.')

    def test_labels_are_plain_english(self):
        approval = self.raise_material(design=False)
        response = self.client_for(self.pm).get(reverse('approval_detail', args=[approval.pk]))
        self.assertContains(response, 'Request changes')
        self.assertNotContains(response, '>changes_requested<')
        self.assertNotContains(response, 'material_pre_order<')


# ===========================================================================
# Resubmit, withdraw, reassign
# ===========================================================================

class ResubmitTests(ApprovalViewFixture):

    def setUp(self):
        self.approval = self.raise_material(design=False)
        apply_approval_decision(self.step(self.approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.pm, note='Use 550 Wp.')
        self.url = reverse('approval_resubmit', args=[self.approval.pk])

    def test_the_form_says_the_details_are_not_edited(self):
        response = self.client_for(self.scm).get(self.url)
        self.assertContains(response, 'Describe what you changed')

    def test_resubmit_opens_the_next_round_with_the_same_approver(self):
        response = self.client_for(self.scm).post(
            self.url, {'note': 'Now 550 Wp.', f'assignee_{APPROVAL_PARTY_PM}': self.pm.pk})
        self.assert_to_detail(response, self.approval)
        self.approval.refresh_from_db()
        self.assertEqual((self.approval.status, self.approval.current_round), (APPROVAL_OPEN, 2))
        self.assertEqual(self.step(self.approval, APPROVAL_PARTY_PM).assignee, self.pm)
        remark = StatusTransition.objects.filter(
            subject_type=SUBJECT_APPROVAL_REQUEST, subject_id=self.approval.pk,
        ).order_by('-occurred_at', '-pk').first().remark
        self.assertEqual(remark, 'Now 550 Wp.')

    def test_an_approver_override_is_recorded_before_the_note(self):
        self.client_for(self.scm).post(
            self.url, {'note': 'PM changed.', f'assignee_{APPROVAL_PARTY_PM}': self.pm_b.pk})
        self.assertEqual(self.step(self.approval, APPROVAL_PARTY_PM).assignee, self.pm_b)
        remark = StatusTransition.objects.filter(
            subject_type=SUBJECT_APPROVAL_REQUEST, subject_id=self.approval.pk,
        ).order_by('-occurred_at', '-pk').first().remark
        self.assertTrue(remark.startswith('Approver changed — pm:'), remark)
        self.assertTrue(remark.endswith('PM changed.'), remark)

    def test_an_empty_note_is_the_chokepoints_refusal_as_a_message(self):
        response = self.client_for(self.scm).post(self.url, {'note': ''})
        self.assertContains(response, 'Say what changed in this revision.', status_code=400)
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, APPROVAL_CHANGES_REQUESTED)

    def test_a_refusal_after_the_request_moved_on_goes_back_to_the_request(self):
        """The same refusal once the request is no longer awaiting changes (another SCM
        user withdrew it while this form was open): the message goes to the request —
        there is no form left to fill."""
        page = self.client_for(self.scm)
        page.get(self.url)
        with mock.patch('projects.approval_views.user_can_resubmit_approval_request',
                        return_value=True):
            withdraw_approval_request(self.approval, self.scm_b, 'Dropped.')
            response = page.post(self.url, {'note': 'Now 550 Wp.'})
        self.assert_to_detail(response, self.approval)
        self.assertIn('This request is withdrawn, which is final. Raise a new request '
                      'instead.', self.messages_of(response))

    def test_a_request_not_waiting_for_changes_sends_scm_back(self):
        fresh = self.raise_material(design=False)
        response = self.client_for(self.scm).get(
            reverse('approval_resubmit', args=[fresh.pk]))
        self.assert_to_detail(response, fresh)


class WithdrawReassignTests(ApprovalViewFixture):

    def test_withdraw_needs_a_note(self):
        approval = self.raise_material()
        response = self.client_for(self.scm).post(
            reverse('approval_withdraw', args=[approval.pk]), {'note': ''})
        self.assertIn('Say why this request is being withdrawn.', self.messages_of(response))
        approval.refresh_from_db()
        self.assertEqual(approval.status, APPROVAL_OPEN)

    def test_withdrawing_twice_is_a_message(self):
        approval = self.raise_material()
        url = reverse('approval_withdraw', args=[approval.pk])
        self.client_for(self.scm).post(url, {'note': 'Dropped.'})
        response = self.client_for(self.scm).post(url, {'note': 'Again.'})
        self.assertIn('This request is already withdrawn.', self.messages_of(response))

    def test_reassign_to_an_ineligible_person_is_a_message(self):
        approval = self.raise_material(design=False)
        pm_step = self.step(approval, APPROVAL_PARTY_PM)
        response = self.client_for(self.scm).post(
            reverse('approval_reassign', args=[pm_step.pk]),
            {'new_assignee': self.finance.pk, 'note': 'x'})
        self.assert_to_detail(response, approval)
        self.assertTrue(any('cannot be named as the PM approver' in m
                            for m in self.messages_of(response)))
        self.assertEqual(self.step(approval, APPROVAL_PARTY_PM).assignee, self.pm)

    def test_a_pm_reassigned_away_can_still_read_the_request(self):
        approval = self.raise_material(design=False)
        pm_step = self.step(approval, APPROVAL_PARTY_PM)
        self.client_for(self.scm).post(reverse('approval_reassign', args=[pm_step.pk]),
                                       {'new_assignee': self.pm_b.pk, 'note': 'On leave.'})
        response = self.client_for(self.pm).get(reverse('approval_detail', args=[approval.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="verdict"')


# ===========================================================================
# List and nav
# ===========================================================================

class ListTests(ApprovalViewFixture):

    def setUp(self):
        self.with_design = self.raise_material(design=True, title='With design')
        self.pm_only = self.raise_material(design=False, title='PM only',
                                           pm_assignee=self.pm_b)

    def test_portfolio_roles_see_everything(self):
        for profile in (self.scm, self.ceo, self.admin, self.sysadmin):
            response = self.client_for(profile).get(reverse('approval_list'))
            self.assertContains(response, 'With design')
            self.assertContains(response, 'PM only')

    def test_a_pm_sees_only_what_they_are_named_on(self):
        response = self.client_for(self.pm).get(reverse('approval_list'))
        self.assertContains(response, 'With design')
        self.assertNotContains(response, 'PM only')

    def test_design_head_authority_sees_requests_with_a_design_step(self):
        for profile in (self.head, self.deputy):
            response = self.client_for(profile).get(reverse('approval_list'))
            self.assertContains(response, 'With design')
            self.assertNotContains(response, 'PM only')

    def test_the_list_never_shows_what_the_detail_refuses(self):
        client = self.client_for(self.deputy)
        self.assert_forbidden(client.get(reverse('approval_detail', args=[self.pm_only.pk])))
        self.assertEqual(client.get(reverse('approval_detail',
                                            args=[self.with_design.pk])).status_code, 200)

    def test_filters(self):
        withdraw_approval_request(self.pm_only, self.scm, 'Dropped.')
        client = self.client_for(self.scm)
        response = client.get(reverse('approval_list'), {'status': APPROVAL_WITHDRAWN})
        self.assertContains(response, 'PM only')
        self.assertNotContains(response, 'With design')
        response = client.get(reverse('approval_list'),
                              {'kind': 'material_pre_dispatch'})
        self.assertContains(response, 'No approval requests match this filter.')
        response = client.get(reverse('approval_list'), {'status': 'nonsense'})
        self.assertContains(response, 'With design')

    def test_nav_entry_follows_the_list_predicate(self):
        link = f'href="{reverse("approval_list")}"'
        for profile in (self.scm, self.pm, self.deputy, self.ceo):
            response = self.client_for(profile).get(reverse('notifications'))
            self.assertContains(response, link, msg_prefix=profile.user.username)
        for profile in (self.finance, self.se, self.designer):
            response = self.client_for(profile).get(reverse('notifications'))
            self.assertNotContains(response, link, msg_prefix=profile.user.username)


# ===========================================================================
# Approvals 2a-2 — resubmit revision and kept approvals
# ===========================================================================

KEEP_REASON = 'The make change does not touch the layout.'


class RevisionFixture(ApprovalViewFixture):
    """A design-signoff request whose Design Head APPROVED and whose PM then asked for
    changes: the Head's approval is the one that may be kept."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.tender_a = Program.objects.create(
            name='AV Tender A', program_type='OPEX', client_name='Client', status='Active',
            short_tender_code='AVA')
        cls.tender_b = Program.objects.create(
            name='AV Tender B', program_type='OPEX', client_name='Client', status='Active',
            short_tender_code='AVB')
        cls.site = Project.objects.create(
            customer_name='AV Site', status='Active', customer_phone='9876543210',
            site_address='1 Sun Road', city='Lucknow', state='Uttar Pradesh',
            project_type='Residential', dc_capacity_kw=Decimal('5.00'), assigned_pm=cls.pm)

    def setUp(self):
        self.approval = self.raise_material(design=True, programs=[self.tender_a],
                                            projects=[self.site])
        apply_approval_decision(self.step(self.approval, APPROVAL_PARTY_DESIGN),
                                APPROVAL_STEP_APPROVED, self.head)
        apply_approval_decision(self.step(self.approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.pm, note='Use Adani.')
        self.url = reverse('approval_resubmit', args=[self.approval.pk])

    def form(self, **overrides):
        """The resubmit page's POST with every drawn field as it was pre-filled."""
        data = {'revise': '1', 'title': 'Module make', 'description': 'Propose Waaree 545 Wp.',
                'proposed_make': 'Waaree', 'specification': '545 Wp mono PERC',
                'quantity_note': '', 'vendor': '', 'program': [self.tender_a.pk],
                'project': [self.site.pk], 'note': 'Changed the make.',
                f'assignee_{APPROVAL_PARTY_PM}': self.pm.pk,
                f'assignee_{APPROVAL_PARTY_DESIGN}': self.head.pk}
        data.update(overrides)
        return data

    def detail(self, profile=None):
        return self.client_for(profile or self.scm).get(
            reverse('approval_detail', args=[self.approval.pk]))


class ResubmitRevisionTests(RevisionFixture):

    def test_a_revision_changes_the_fields_and_round_2_shows_them(self):
        response = self.client_for(self.scm).post(
            self.url, self.form(title='Module make v2', proposed_make='Adani'))
        self.assert_to_detail(response, self.approval)
        self.approval.refresh_from_db()
        self.assertEqual((self.approval.current_round, self.approval.title),
                         (2, 'Module make v2'))
        self.assertEqual(self.approval.material_detail.proposed_make, 'Adani')
        self.assertEqual(round_snapshot(self.approval, 2)['material']['proposed_make'],
                         'Adani')
        page = self.detail()
        self.assertContains(page, 'Details approvers saw in round 2')
        self.assertContains(page, 'Changed since round 1')
        self.assertContains(page, 'Module make v2')

    def test_only_the_fields_that_changed_are_sent_as_the_revision(self):
        with mock.patch('projects.approval_views.resubmit_approval_request',
                        wraps=resubmit_approval_request) as resubmit:
            self.client_for(self.scm).post(self.url, self.form(proposed_make='Adani'))
        self.assertEqual(resubmit.call_args.kwargs['revision'], {'proposed_make': 'Adani'})

    def test_an_untouched_form_sends_no_revision(self):
        with mock.patch('projects.approval_views.resubmit_approval_request',
                        wraps=resubmit_approval_request) as resubmit:
            self.client_for(self.scm).post(self.url, self.form())
        self.assertIsNone(resubmit.call_args.kwargs['revision'])

    def test_the_form_is_prefilled_with_the_request_as_it_stands(self):
        response = self.client_for(self.scm).get(self.url)
        self.assertContains(response, 'name="revise" value="1"')
        self.assertContains(response, 'value="Module make"')
        self.assertContains(response, f'<option value="{self.tender_a.pk}" selected>AV Tender A')
        self.assertContains(response, 'BOQ items and design sign-off cannot be changed')
        self.assertNotContains(response, 'NOT editable here')

    def test_an_inactive_vendor_stays_offered_as_before(self):
        ApprovalRequest.objects.filter(pk=self.approval.pk).update(vendor=self.vendor)
        Vendor.objects.filter(pk=self.vendor.pk).update(is_active=False)
        response = self.client_for(self.scm).get(self.url)
        self.assertContains(response, 'AV Vendor (as before — inactive)')
        with mock.patch('projects.approval_views.resubmit_approval_request',
                        wraps=resubmit_approval_request) as resubmit:
            self.client_for(self.scm).post(self.url, self.form(vendor=self.vendor.pk))
        self.assertIsNone(resubmit.call_args.kwargs['revision'])


class KeepApprovalTests(RevisionFixture):

    def test_keep_is_offered_only_for_an_approved_previous_round_step(self):
        response = self.client_for(self.scm).get(self.url)
        self.assertContains(response, "Keep Av Head's approval from round 1")
        self.assertContains(response, f'name="keep_{APPROVAL_PARTY_DESIGN}"')
        self.assertNotContains(response, f'name="keep_{APPROVAL_PARTY_PM}"')
        self.assertContains(response, 'Av Head will not be asked to review this revision. '
                                      'Keep it only if your changes do not affect what they '
                                      'approved.')

    def test_a_superseded_step_is_offered_no_keep(self):
        other = self.raise_material(design=True)
        apply_approval_decision(self.step(other, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.pm, note='No.')
        response = self.client_for(self.scm).get(
            reverse('approval_resubmit', args=[other.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="keep_')

    def test_keep_without_a_reason_is_refused_by_the_form_with_values_kept(self):
        for reason in ('', 'short reason x', 'a b c d e f g h i j k l m n'):   # 0, 12, 14
            response = self.client_for(self.scm).post(self.url, self.form(
                title='Typed title survives', **{f'keep_{APPROVAL_PARTY_DESIGN}': 'on',
                                                  f'keep_reason_{APPROVAL_PARTY_DESIGN}': reason}))
            self.assertContains(response, 'at least 15 characters, not counting spaces',
                                status_code=400, msg_prefix=repr(reason))
            self.assertContains(response, 'value="Typed title survives"', status_code=400)
            self.assertContains(response, f'id="apKeep{APPROVAL_PARTY_DESIGN}" checked',
                                status_code=400)
            if reason:
                self.assertContains(response, reason, status_code=400)
        self.approval.refresh_from_db()
        self.assertEqual((self.approval.status, self.approval.current_round),
                         (APPROVAL_CHANGES_REQUESTED, 1))

    def test_keep_plus_an_approver_change_for_the_same_party_is_refused(self):
        head_b = _profile('av_head_b', 'Design', is_design_head=True)
        response = self.client_for(self.scm).post(self.url, self.form(**{
            f'keep_{APPROVAL_PARTY_DESIGN}': 'on',
            f'keep_reason_{APPROVAL_PARTY_DESIGN}': KEEP_REASON,
            f'assignee_{APPROVAL_PARTY_DESIGN}': head_b.pk}))
        self.assertContains(response, 'Av Head&#x27;s approval cannot be kept while you '
                                      'change the Design Head approver.', status_code=400)
        self.assertContains(response, f'<option value="{head_b.pk}" selected>',
                            status_code=400)
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.current_round, 1)

    def test_a_kept_step_renders_as_kept_and_is_not_timed(self):
        self.client_for(self.scm).post(self.url, self.form(**{
            f'keep_{APPROVAL_PARTY_DESIGN}': 'on',
            f'keep_reason_{APPROVAL_PARTY_DESIGN}': KEEP_REASON}))
        kept = self.step(self.approval, APPROVAL_PARTY_DESIGN)
        self.assertIsNotNone(kept.carried_from_id)
        page = self.detail()
        self.assertContains(page, 'Kept from round 1 · decided by Av Head on ')
        self.assertContains(page, ' · kept by Av Scm on ')
        self.assertContains(page, f'IST · Reason: {KEEP_REASON}')
        self.assertContains(page, '<dd class="col-7">Not timed (kept)</dd>', html=False)
        self.assertNotContains(page, 'originally recorded by')

    def test_a_kept_proxy_step_says_who_originally_recorded_it(self):
        other = self.raise_material(design=True)
        apply_approval_decision(self.step(other, APPROVAL_PARTY_DESIGN),
                                APPROVAL_STEP_APPROVED, self.scm_b,
                                proxy=ProxyDecision(self.head, 'whatsapp', 'WA 10:02 "ok"'))
        apply_approval_decision(self.step(other, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.pm, note='No.')
        self.client_for(self.scm).post(
            reverse('approval_resubmit', args=[other.pk]),
            {'note': 'Changed.', f'keep_{APPROVAL_PARTY_DESIGN}': 'on',
             f'keep_reason_{APPROVAL_PARTY_DESIGN}': KEEP_REASON})
        page = self.client_for(self.scm).get(reverse('approval_detail', args=[other.pk]))
        self.assertContains(page, 'Kept from round 1 · decided by Av Head on ')
        self.assertContains(page, '(originally recorded by Av Scm B from WhatsApp)')


# ===========================================================================
# Approvals 2a-2 — round snapshots and the change list
# ===========================================================================

class RoundChangeTests(RevisionFixture):

    def test_the_change_list_shows_old_and_new_values_and_scope_added_and_removed(self):
        self.client_for(self.scm).post(self.url, self.form(
            proposed_make='Adani', program=[self.tender_b.pk]))
        page = self.detail()
        self.assertContains(page, 'Changed since round 1')
        self.assertContains(page, '<span class="text-muted">Waaree</span> → Adani', html=False)
        self.assertContains(page, '+ Added: AV Tender B')
        self.assertContains(page, '− Removed: AV Tender A')
        self.assertNotContains(page, 'Change list unavailable')

    def test_a_round_with_nothing_changed_says_so(self):
        self.client_for(self.scm).post(self.url, self.form())
        self.assertContains(self.detail(), 'No details changed in this round.')

    def test_a_round_without_a_snapshot_shows_the_fallback_not_an_error(self):
        self.client_for(self.scm).post(self.url, self.form(title='Module make v2'))
        real = round_snapshot
        with mock.patch('projects.approval_views.round_snapshot',
                        side_effect=lambda approval, n: None if n == 1 else real(approval, n)):
            page = self.detail()
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Details as currently recorded (no snapshot for this round)')
        self.assertContains(page, 'Change list unavailable for this round')


# ===========================================================================
# Approvals 2a-2 — proxy evidence files
# ===========================================================================

class ProxyEvidenceTests(ApprovalViewFixture):

    def setUp(self):
        self.approval = self.raise_material(design=False)
        self.pm_step = self.step(self.approval, APPROVAL_PARTY_PM)
        self.url = reverse('approval_record_proxy', args=[self.pm_step.pk])

    def proxy(self, **overrides):
        data = {'verdict': APPROVAL_STEP_APPROVED, 'decided_by': self.pm.pk,
                'channel': 'whatsapp', 'evidence': 'WhatsApp 26 Sep 10:02: "go ahead"',
                'evidence_files': [SimpleUploadedFile('whatsapp.png', b'\x89PNG x',
                                                      content_type='image/png')]}
        data.update(overrides)
        return data

    @mock.patch('projects.approval_views.get_supabase_client')
    def test_an_evidence_file_is_listed_under_its_step(self, get_client):
        response = self.client_for(self.scm).post(self.url, self.proxy())
        self.assert_to_detail(response, self.approval)
        attachment = ApprovalAttachment.objects.get(request=self.approval)
        self.assertEqual((attachment.step_id, attachment.file_name, attachment.uploaded_by),
                         (self.pm_step.pk, 'whatsapp.png', self.scm))
        page = self.client_for(self.pm).get(reverse('approval_detail', args=[self.approval.pk]))
        # Once — under the step, not again in the round's own file list.
        self.assertEqual(page.content.count(b'>whatsapp.png</span>'), 1)

    @mock.patch('projects.approval_views.get_supabase_client')
    def test_a_refused_proxy_leaves_no_attachment_and_removes_the_file(self, get_client):
        response = self.client_for(self.scm).post(self.url, self.proxy(decided_by=self.finance.pk))
        self.assert_to_detail(response, self.approval)
        self.assertTrue(any('cannot decide the PM step' in m for m in self.messages_of(response)))
        self.assertFalse(ApprovalAttachment.objects.exists())
        bucket = get_client.return_value.storage.from_.return_value
        bucket.remove.assert_called_once_with([bucket.upload.call_args.kwargs['path']])

    @mock.patch('projects.approval_views.get_supabase_client')
    def test_evidence_takes_pdf_and_photos_only(self, get_client):
        heic = SimpleUploadedFile('photo.heic', b'x', content_type='image/heic')
        sheet = SimpleUploadedFile('notes.xlsx', b'x', content_type='application/vnd.'
                                   'openxmlformats-officedocument.spreadsheetml.sheet')
        response = self.client_for(self.scm).post(
            self.url, self.proxy(evidence_files=[heic, sheet]))
        messages_ = self.messages_of(response)
        self.assertIn('photo.heic: unsupported type (.heic).', messages_)
        self.assertIn('notes.xlsx: unsupported type (.xlsx).', messages_)
        get_client.assert_not_called()
        self.assertEqual(self.step(self.approval, APPROVAL_PARTY_PM).verdict,
                         APPROVAL_STEP_PENDING)


# ===========================================================================
# Approvals 2a-2 — History: every action, who and when (D-A22)
# ===========================================================================

class HistoryTests(ApprovalViewFixture):

    def history(self, approval, profile=None):
        response = self.client_for(profile or self.scm).get(
            reverse('approval_detail', args=[approval.pk]))
        return response, response.context['history']

    def test_a_reassignment_appears_with_the_scm_users_name_and_time(self):
        approval = self.raise_material(design=False)
        at = datetime(2026, 9, 1, 4, 30, tzinfo=dt_timezone.utc)          # 10:00 IST
        with mock.patch.object(timezone, 'now', return_value=at):
            self.client_for(self.scm_b).post(
                reverse('approval_reassign', args=[self.step(approval, APPROVAL_PARTY_PM).pk]),
                {'new_assignee': self.pm_b.pk, 'note': 'On leave.'})
        response, history = self.history(approval)
        entry = next(h for h in history if h['what'] == 'Reassigned the PM step (round 1)')
        self.assertEqual((entry['actor'], entry['at'], entry['lines'], entry['remark']),
                         ('Av Scm B', at, ['Av Pm → Av Pm B'], 'On leave.'))
        self.assertContains(response, '<strong>Reassigned the PM step (round 1)</strong> · '
                                      'Av Scm B · <span class="text-muted">01 Sep 2026, '
                                      '10:00 IST</span>', html=False)

    def test_every_scm_action_names_its_actor(self):
        """Raised by one SCM user; a proxy, the resubmit (keeping the Head's approval)
        by a SECOND SCM user on the first one's request; a reassignment and the
        withdrawal by the first again."""
        approval = self.raise_material(design=True)
        self.client_for(self.scm_b).post(
            reverse('approval_record_proxy',
                    args=[self.step(approval, APPROVAL_PARTY_DESIGN).pk]),
            {'verdict': APPROVAL_STEP_APPROVED, 'decided_by': self.head.pk,
             'channel': 'whatsapp', 'evidence': 'WA "fine"'})
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.pm, note='Use Adani.')
        self.client_for(self.scm_b).post(
            reverse('approval_resubmit', args=[approval.pk]),
            {'note': 'Now Adani.', f'keep_{APPROVAL_PARTY_DESIGN}': 'on',
             f'keep_reason_{APPROVAL_PARTY_DESIGN}': KEEP_REASON})
        self.client_for(self.scm).post(
            reverse('approval_reassign', args=[self.step(approval, APPROVAL_PARTY_PM).pk]),
            {'new_assignee': self.pm_b.pk, 'note': 'On leave.'})
        self.client_for(self.scm).post(reverse('approval_withdraw', args=[approval.pk]),
                                       {'note': 'Dropped.'})

        _, history = self.history(approval)
        self.assertEqual([(h['what'], h['actor']) for h in history], [
            ('Raised', 'Av Scm'),
            ('Design Head approved (round 1)', 'Av Head'),
            ('PM requested changes (round 1)', 'Av Pm'),
            ('Resubmitted as round 2', 'Av Scm B'),
            ('Reassigned the PM step (round 2)', 'Av Scm'),
            ('Withdrawn', 'Av Scm'),
        ])
        by_what = {h['what']: h for h in history}
        self.assertEqual(by_what['Design Head approved (round 1)']['lines'],
                         ['Recorded by Av Scm B from WhatsApp'])
        self.assertIn('Approval kept — design: Av Head (round 1).',
                      by_what['Resubmitted as round 2']['remark'])
        self.assertTrue(all(h['at'] is not None for h in history))

    def test_a_decision_that_moved_the_request_is_one_line_not_two(self):
        approval = self.raise_material(design=False)
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_APPROVED, self.pm)
        _, history = self.history(approval)
        self.assertEqual([h['what'] for h in history], ['Raised', 'PM approved (round 1)'])
        self.assertEqual(history[1]['outcome'], 'Request now approved')

    def test_an_unmatched_ledger_row_is_never_dropped(self):
        approval = self.raise_material(design=False)
        record_transition(approval, to_status=APPROVAL_APPROVED, from_status=APPROVAL_OPEN,
                          actor=self.ceo, remark='Set by hand.')
        response, history = self.history(approval)
        entry = history[-1]
        self.assertEqual((entry['what'], entry['actor'], entry['remark']),
                         ('Approved', 'Av Ceo', 'Set by hand.'))
        self.assertContains(response, '<strong>Approved</strong> · Av Ceo · ', html=False)

    def test_a_withdrawal_from_an_open_round_appears_once(self):
        approval = self.raise_material(design=True)       # two pending steps
        withdraw_approval_request(approval, self.scm, 'Dropped.')
        self.assertEqual(ApprovalStep.objects.filter(
            request=approval, verdict=APPROVAL_STEP_SUPERSEDED).count(), 2)
        _, history = self.history(approval)
        self.assertEqual([(h['what'], h['actor']) for h in history],
                         [('Raised', 'Av Scm'), ('Withdrawn', 'Av Scm')])


# ===========================================================================
# Approvals 3a — material pre-dispatch requests
# ===========================================================================

class PreDispatchFixture(ApprovalViewFixture):
    """Two PO/PI records of AV Vendor — one sized against a site the PM runs (the PM may
    open it), one naming no site (only the portfolio roles may) — one of another vendor,
    and an approved pre-order approval for each vendor."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.other_vendor = Vendor.objects.create(name='AV Other', contact_person='O',
                                                 phone='9000000008')
        cls.site = Project.objects.create(
            customer_name='AV Dispatch Site', status='Active', customer_phone='9876543210',
            site_address='2 Sun Road', city='Lucknow', state='Uttar Pradesh',
            project_type='Residential', dc_capacity_kw=Decimal('5.00'), assigned_pm=cls.pm)
        cls.order = VendorOrder.objects.create(
            vendor=cls.vendor, project_type='Residential', total_amount=Decimal('1000'),
            created_by=cls.scm, po_number='PO-SITE', pi_number='PI-SITE')
        VendorOrderSite.objects.create(order=cls.order, project=cls.site)
        cls.central = VendorOrder.objects.create(
            vendor=cls.vendor, project_type='Residential', total_amount=Decimal('2000'),
            created_by=cls.scm, po_number='PO-CENTRAL', pi_number='')
        cls.other_order = VendorOrder.objects.create(
            vendor=cls.other_vendor, project_type='Residential',
            total_amount=Decimal('3000'), created_by=cls.scm, po_number='PO-OTHER')

    def setUp(self):
        self.pre_order = self.approved_pre_order('Modules pre-order', self.vendor)
        self.other_pre_order = self.approved_pre_order('Other pre-order', self.other_vendor)

    def approved_pre_order(self, title, vendor, pm=None):
        approval = self.raise_material(title=title, vendor=vendor, pm_assignee=pm or self.pm)
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_APPROVED, pm or self.pm)
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_DESIGN),
                                APPROVAL_STEP_APPROVED, self.head)
        approval.refresh_from_db()
        return approval

    def raise_dispatch(self, order=None, pre_order=None, **overrides):
        order = order or self.order
        kwargs = dict(
            kind=APPROVAL_KIND_MATERIAL_PRE_DISPATCH, title='Modules, lot 1 of 2',
            vendor=order.vendor,
            material={'proposed_make': 'Waaree', 'vendor_order': order,
                      'pre_order_request': pre_order})
        kwargs.update(overrides)
        return self.raise_material(**kwargs)

    def create_url(self, client):
        opened = client.get(reverse('approval_create'),
                            {'kind': APPROVAL_KIND_MATERIAL_PRE_DISPATCH})
        self.assertEqual(opened.status_code, 302)
        return opened['Location']

    def form(self, **overrides):
        data = {'client_uuid': str(uuid.uuid4()), 'title': 'Modules, lot 2 of 3',
                'description': 'First truck of modules.', 'proposed_make': 'Waaree',
                'specification': '545 Wp', 'quantity_note': '120 modules',
                'pm_assignee': self.pm.pk, 'vendor_order': self.order.pk}
        data.update(overrides)
        return data

    def detail(self, approval, profile=None):
        return self.client_for(profile or self.scm).get(
            reverse('approval_detail', args=[approval.pk]))


class PreDispatchRaiseTests(PreDispatchFixture):

    def test_the_list_offers_both_raise_actions(self):
        response = self.client_for(self.scm).get(reverse('approval_list'))
        create = reverse('approval_create')
        self.assertContains(response, f'href="{create}?kind=material_pre_order"')
        self.assertContains(response, f'href="{create}?kind=material_pre_dispatch"')
        self.assertContains(response, 'Raise — before order')
        self.assertContains(response, 'Raise — before dispatch')
        self.assertNotContains(self.client_for(self.pm).get(reverse('approval_list')),
                               'Raise — before dispatch')

    def test_the_kind_is_kept_through_the_key_redirect(self):
        client = self.client_for(self.scm)
        url = self.create_url(client)
        self.assertRegex(url,
                         r'^/approvals/new/\?kind=material_pre_dispatch&key=[0-9a-f-]{36}$')
        page = client.get(url)
        self.assertContains(page, 'Material — before dispatch')
        self.assertContains(page, 'name="vendor_order"')
        self.assertContains(page, 'name="pre_order_request"')
        self.assertNotContains(page, 'name="vendor"')

    def test_no_kind_keeps_the_pre_order_redirect_exactly(self):
        client = self.client_for(self.scm)
        location = client.get(reverse('approval_create'))['Location']
        self.assertRegex(location, r'^/approvals/new/\?key=[0-9a-f-]{36}$')
        explicit = client.get(reverse('approval_create'),
                              {'kind': APPROVAL_KIND_MATERIAL_PRE_ORDER})
        self.assertRegex(explicit['Location'],
                         r'^/approvals/new/\?kind=material_pre_order&key=[0-9a-f-]{36}$')
        page = client.get(location)
        self.assertContains(page, 'Material — before order')
        self.assertContains(page, 'name="vendor"')
        self.assertNotContains(page, 'name="vendor_order"')

    def test_any_other_kind_goes_back_to_the_list(self):
        for kind in ('contractor_bill', 'nonsense', ''):
            with self.subTest(kind=kind):
                response = self.client_for(self.scm).get(reverse('approval_create'),
                                                         {'kind': kind})
                self.assertEqual(response['Location'], reverse('approval_list'))
                response = self.client_for(self.scm).post(
                    reverse('approval_create') + f'?kind={kind}', self.form())
                self.assertEqual(response['Location'], reverse('approval_list'))
        self.assertFalse(ApprovalRequest.objects.filter(
            kind=APPROVAL_KIND_MATERIAL_PRE_DISPATCH).exists())

    def test_the_pickers_group_by_vendor_newest_first_and_offer_approved_pre_orders(self):
        vendorless = self.raise_material(title='Vendorless pre-order', design=False)
        apply_approval_decision(self.step(vendorless, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_APPROVED, self.pm)
        self.raise_material(title='Still open pre-order', vendor=self.vendor)
        client = self.client_for(self.scm)
        content = client.get(self.create_url(client)).content.decode()
        recorded = timezone.localtime(self.order.created_at).strftime('%d %b %Y')
        # AV Other sorts before AV Vendor; within AV Vendor the newer record comes first.
        self.assertLess(content.index('<optgroup label="AV Other">'),
                        content.index('<optgroup label="AV Vendor">'))
        self.assertLess(content.index('PO PO-CENTRAL'), content.index('PO PO-SITE'))
        self.assertIn('AV Vendor · PO PO-CENTRAL · PI — · recorded', content)
        self.assertIn(f'PI PI-SITE · recorded {recorded} · {self.site.project_id}', content)
        self.assertIn(f'PO/PI record #{self.central.pk}', content)   # no site, no tender
        self.assertIn(f'data-vendor="{self.vendor.pk}"', content)
        self.assertIn('Modules pre-order · AV Vendor · approved', content)
        self.assertIn('Other pre-order', content)
        self.assertNotIn('Vendorless pre-order', content)
        self.assertNotIn('Still open pre-order', content)

    def test_pre_dispatch_without_a_pre_order_link(self):
        client = self.client_for(self.scm)
        response = client.post(self.create_url(client), self.form())
        approval = ApprovalRequest.objects.get(kind=APPROVAL_KIND_MATERIAL_PRE_DISPATCH)
        self.assert_to_detail(response, approval)
        self.assertEqual((approval.vendor, approval.material_detail.vendor_order,
                          approval.material_detail.pre_order_request),
                         (self.vendor, self.order, None))

    def test_pre_dispatch_with_a_pre_order_link(self):
        client = self.client_for(self.scm)
        client.post(self.create_url(client),
                    self.form(pre_order_request=self.pre_order.pk))
        approval = ApprovalRequest.objects.get(kind=APPROVAL_KIND_MATERIAL_PRE_DISPATCH)
        self.assertEqual(approval.material_detail.pre_order_request, self.pre_order)

    def test_the_vendor_is_the_records_whatever_is_posted(self):
        client = self.client_for(self.scm)
        client.post(self.create_url(client), self.form(vendor=self.other_vendor.pk))
        approval = ApprovalRequest.objects.get(kind=APPROVAL_KIND_MATERIAL_PRE_DISPATCH)
        self.assertEqual(approval.vendor, self.vendor)

    def test_a_missing_record_is_refused_before_anything_is_written(self):
        client = self.client_for(self.scm)
        url = self.create_url(client)
        for missing in ('999999', 'x'):
            response = client.post(url, self.form(vendor_order=missing))
            self.assertContains(response, 'The PO/PI record you chose no longer exists.',
                                status_code=400)
        response = client.post(url, self.form(vendor_order=''))
        self.assertContains(response, 'Choose the PO/PI record this dispatch is against.',
                            status_code=400)
        self.assertContains(response, 'First truck of modules.', status_code=400)  # kept
        self.assertFalse(ApprovalRequest.objects.filter(
            kind=APPROVAL_KIND_MATERIAL_PRE_DISPATCH).exists())

    def test_a_pre_order_of_another_vendor_is_refused_before_anything_is_written(self):
        client = self.client_for(self.scm)
        response = client.post(self.create_url(client),
                               self.form(pre_order_request=self.other_pre_order.pk))
        self.assertContains(response, 'is for a different vendor from the PO/PI record',
                            status_code=400)
        self.assertFalse(ApprovalRequest.objects.filter(
            kind=APPROVAL_KIND_MATERIAL_PRE_DISPATCH).exists())

    def test_an_unapproved_pre_order_is_the_chokepoints_refusal(self):
        still_open = self.raise_material(title='Open one', vendor=self.vendor)
        client = self.client_for(self.scm)
        response = client.post(self.create_url(client),
                               self.form(pre_order_request=still_open.pk))
        self.assertContains(response,
                            'The linked pre-order approval has not been approved.',
                            status_code=400)
        self.assertFalse(ApprovalRequest.objects.filter(
            kind=APPROVAL_KIND_MATERIAL_PRE_DISPATCH).exists())

    def test_a_pre_order_raise_ignores_the_dispatch_fields(self):
        before = ApprovalRequest.objects.count()
        response = self.client_for(self.scm).post(
            reverse('approval_create'),
            self.form(vendor=self.vendor.pk, pre_order_request=self.pre_order.pk,
                      design_signoff_required='on', design_assignee=self.head.pk))
        approval = ApprovalRequest.objects.order_by('-pk').first()
        self.assertEqual(ApprovalRequest.objects.count(), before + 1)
        self.assert_to_detail(response, approval)
        self.assertEqual(approval.kind, APPROVAL_KIND_MATERIAL_PRE_ORDER)
        self.assertEqual((approval.material_detail.vendor_order,
                          approval.material_detail.pre_order_request), (None, None))


class PreDispatchDetailTests(PreDispatchFixture):

    def recorded(self, order):
        return timezone.localtime(order.created_at).strftime('%d %b %Y')

    def test_the_block_shows_the_record_and_the_pre_order(self):
        approval = self.raise_dispatch(pre_order=self.pre_order)
        response = self.detail(approval)
        content = response.content.decode()
        self.assertEqual(content.count('Dispatch against'), 2)   # request card + round 1
        self.assertContains(response, 'PO/PI record · PO PO-SITE · PI PI-SITE')
        self.assertContains(response, f'AV Vendor · recorded {self.recorded(self.order)}')
        order_link = f'href="{reverse("vendor_order_detail", args=[self.order.pk])}"'
        pre_link = f'href="{reverse("approval_detail", args=[self.pre_order.pk])}"'
        self.assertEqual(content.count(order_link), 2)
        self.assertEqual(content.count(pre_link), 2)
        self.assertContains(response, 'Modules pre-order')

    def test_no_pre_order_line_when_none_is_linked(self):
        response = self.detail(self.raise_dispatch())
        self.assertContains(response, 'Dispatch against')
        self.assertNotContains(response, 'Pre-order approval:')

    def test_a_pre_order_request_has_no_block(self):
        self.assertNotContains(self.detail(self.pre_order), 'Dispatch against')

    def test_the_record_link_follows_user_can_view_vendor_order(self):
        on_site = self.raise_dispatch()
        central = self.raise_dispatch(order=self.central)
        self.assertTrue(user_can_view_vendor_order(self.pm.user, self.order))
        self.assertFalse(user_can_view_vendor_order(self.pm.user, self.central))

        seen = self.detail(on_site, self.pm)
        self.assertContains(seen, reverse('vendor_order_detail', args=[self.order.pk]))
        hidden = self.detail(central, self.pm)
        self.assertEqual(hidden.status_code, 200)
        self.assertContains(hidden, 'PO/PI record · PO PO-CENTRAL · PI —')   # still shown
        self.assertContains(hidden, f'recorded {self.recorded(self.central)}')
        self.assertNotContains(hidden, reverse('vendor_order_detail', args=[self.central.pk]))
        self.assertNotContains(hidden, 'Open the PO/PI record')
        # SCM, a portfolio role, gets the link on the same request.
        self.assertContains(self.detail(central),
                            reverse('vendor_order_detail', args=[self.central.pk]))

    def test_the_pre_order_link_follows_who_may_read_the_pre_order(self):
        theirs = self.approved_pre_order('Pm B pre-order', self.vendor, pm=self.pm_b)
        approval = self.raise_dispatch(pre_order=theirs)
        response = self.detail(approval, self.pm)
        self.assertContains(response, 'Pre-order approval: Pm B pre-order')
        self.assertNotContains(response, reverse('approval_detail', args=[theirs.pk]))
        # The Design Head reads both requests (each has a design step): the link is drawn.
        self.assertContains(self.detail(approval, self.head),
                            reverse('approval_detail', args=[theirs.pk]))

    def test_a_round_is_drawn_from_its_snapshot(self):
        approval = self.raise_dispatch()
        snapshot = round_snapshot(approval, 1)
        snapshot['material']['vendor_order']['po_number'] = 'PO-AS-SEEN'
        # Imitate a round whose approvers saw a number that differs from today's row.
        # QuerySet.update() goes around the append-only save() (SECONDARY_FINDINGS, S1.1).
        ApprovalRoundSnapshot.objects.filter(request=approval, round=1).update(
            snapshot=snapshot)
        content = self.detail(approval).content.decode()
        self.assertIn('PO PO-SITE', content)        # the request card: the live record
        self.assertIn('PO PO-AS-SEEN', content)     # round 1: its snapshot
        self.assertEqual(content.count(f'recorded {self.recorded(self.order)}'), 2)

    def test_a_round_without_a_snapshot_shows_the_current_block(self):
        approval = self.raise_dispatch()
        ApprovalRoundSnapshot.objects.filter(request=approval).delete()
        response = self.detail(approval)
        self.assertEqual(response.content.decode().count('PO/PI record · PO PO-SITE'), 2)


class PreDispatchResubmitTests(PreDispatchFixture):

    def setUp(self):
        super().setUp()
        self.approval = self.raise_dispatch(pre_order=self.pre_order)
        apply_approval_decision(self.step(self.approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.pm, note='Redo.')
        self.url = reverse('approval_resubmit', args=[self.approval.pk])

    def test_the_vendor_is_read_only(self):
        response = self.client_for(self.scm).get(self.url)
        self.assertContains(response, 'Taken from the PO/PI record')
        self.assertContains(response, 'AV Vendor')
        self.assertNotContains(response, 'name="vendor"')

    def test_a_posted_vendor_does_not_change_it(self):
        response = self.client_for(self.scm).post(self.url, {
            'revise': '1', 'note': 'Changed the make.', 'title': self.approval.title,
            'description': self.approval.description, 'vendor': self.other_vendor.pk,
            'proposed_make': 'Adani'})
        self.assert_to_detail(response, self.approval)
        self.approval.refresh_from_db()
        self.assertEqual((self.approval.current_round, self.approval.vendor),
                         (2, self.vendor))
        snapshot = round_snapshot(self.approval, 2)
        self.assertEqual((snapshot['vendor']['id'], snapshot['material']['proposed_make']),
                         (self.vendor.pk, 'Adani'))

    def test_a_pre_order_request_still_edits_its_vendor(self):
        pre = self.raise_material(title='Editable', vendor=self.vendor, design=False)
        apply_approval_decision(self.step(pre, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.pm, note='Redo.')
        url = reverse('approval_resubmit', args=[pre.pk])
        self.assertContains(self.client_for(self.scm).get(url), 'name="vendor"')
        self.client_for(self.scm).post(url, {
            'revise': '1', 'note': 'Other vendor.', 'title': pre.title,
            'description': pre.description, 'vendor': self.other_vendor.pk})
        pre.refresh_from_db()
        self.assertEqual(pre.vendor, self.other_vendor)


class PreDispatchCardAndAgingTests(PreDispatchFixture):

    def test_a_pre_dispatch_request_is_on_the_pm_card_and_the_aging_page(self):
        approval = self.raise_dispatch(title='Lot 1 dispatch', design_signoff_required=False,
                                       design_assignee=None)
        response = self.client_for(self.pm).get(reverse('dashboard_pm'))
        rows = response.context['approvals_waiting']['rows']
        self.assertEqual([(r['approval'].pk, r['kind_label'], r['vendor_name'])
                          for r in rows],
                         [(approval.pk, 'Material — before dispatch', 'AV Vendor')])
        self.assertContains(response, 'Lot 1 dispatch')

        aging = self.client_for(self.scm).get(reverse('approval_aging'))
        self.assertContains(aging, 'Lot 1 dispatch')
        self.assertContains(aging, 'Material — before dispatch · AV Vendor')
