"""Approvals S2a — the material pre-order screens (approval_views.py).

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
    withdraw_approval_request,
)
from .models import (
    ApprovalAttachment, ApprovalRequest, ApprovalStep, Program, Project, SiteGroup,
    StatusTransition, Vendor,
    APPROVAL_APPROVED, APPROVAL_CHANGES_REQUESTED, APPROVAL_KIND_MATERIAL_PRE_ORDER,
    APPROVAL_OPEN, APPROVAL_WITHDRAWN,
    APPROVAL_PARTY_DESIGN, APPROVAL_PARTY_PM,
    APPROVAL_STEP_APPROVED, APPROVAL_STEP_CHANGES_REQUESTED, APPROVAL_STEP_PENDING,
    APPROVAL_STEP_SUPERSEDED, GROUP_TYPE_PROCUREMENT, SUBJECT_APPROVAL_REQUEST,
)


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
        self.assertEqual(self.client_for(self.scm).get(url).status_code, 200)
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
        response = self.client_for(self.scm).get(reverse('approval_create'))
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
        self.assertContains(response,
                            'Describe what you changed. The request details above are not edited.')

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
        self.assert_to_detail(response, self.approval)
        self.assertIn('Say what changed in this revision.', self.messages_of(response))
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, APPROVAL_CHANGES_REQUESTED)

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
