"""
Session 3.1c-i — one design-change window for the PM, coordinators and SCM.

WHAT IS PINNED, AND THE DECISION EACH TEST CITES
------------------------------------------------
  D-a  The window for a RELEASED design is "released, and in no LOCKED procurement group",
       the same for the PM, a coordinator and SCM. A pool site and a draft-group site are
       both open; a locked-group site is closed, with a message rather than a 403.
  D-b  SCM may raise. Finance may not (403).
  Q1   The PM and coordinators keep Part 4's pre-release window; SCM does not have it.
  Q2   A draft-group site stays in its group at raise, leaves only when the Head ACCEPTS,
       and stays if he rejects. site_group_lock() refuses a group holding a pending
       request, naming the site.
  B4   Acceptance re-checks the lock inside its transaction and changes nothing if the BOQ
       was locked after the raise.
  B6   Acceptance clears released_at / released_by in the status write.
  D-c  m_change_request_rate files an accepted request under the SITE's assigned PM; a site
       with no PM files it under "Unassigned".
  Q3   The "Request design change" link on the SCM group screens follows the same
       predicate the POST enforces, is absent on locked rows, and Finance never sees it.

Every test asserts a refusal as well as a pass. Refusals are exercised by direct POST.

FIXTURES. A released site is built with the artifacts release implies — an attempt with
both QC gates passed and closed, an Arka approved at both gates, a current cad_zip, a
submitted BOQ with a quantity on a catalogue item, and an approved due date — parked at
`awaiting_pm_approval` by a fixture writer and then RELEASED THROUGH design_pm_approve(),
the product writer, so released_at / released_by / pm_approved_* and the StatusTransition
row are the product's own. Groups are formed and locked through SCM's own views. The one
deliberate bypass — the lock race in AcceptTests — says so where it happens.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .design_analytics import compute
from .design_views import (
    CHANGE_REQUEST_REMOVAL_REASON, change_request_window_open, pm_change_request_targets,
)
from .models import (
    Program, Project, DesignAssignment, DesignAttempt, ArkaSubmission, DesignFile,
    DesignChangeRequest, DueDateCommitment, SiteGroup, SiteGroupMembership,
    StatusTransition, BOQ, BOQItem, BOQItemMaster,
    DESIGN_IN_DESIGN, DESIGN_IN_QC, DESIGN_RELEASED, DESIGN_AWAITING_PM_APPROVAL,
    ARKA_APPROVED, QC_PASSED, ATTEMPT_REASON_INITIAL, ATTEMPT_REASON_PM_CHANGE_REQUEST,
    CHANGE_REQUEST_PENDING, CHANGE_REQUEST_ACCEPTED, CHANGE_REQUEST_REJECTED,
    DESIGN_FILE_CAD_ZIP, SITE_GROUP_DRAFT, SITE_GROUP_LOCKED, GROUP_TYPE_PROCUREMENT,
    SUBJECT_DESIGN_ASSIGNMENT,
)
from .permissions import design_change_window_open

LOCKED_REFUSAL = 'change request refused — the BOQ is locked'
SCM_PRE_RELEASE_REFUSAL = 'SCM may raise a design change request only once the design is released'
ACCEPT_REFUSAL = 'change request not accepted — the BOQ was locked'


def _profile(username, role, **flags):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    for name, value in flags.items():
        setattr(profile, name, value)
    profile.save()
    return profile


class ChangeWindowBase(TestCase):

    def setUp(self):
        self.head     = _profile('cw_head', 'Design', is_design_head=True)
        self.qc       = _profile('cw_qc', 'Design', is_design_qc=True)
        self.designer = _profile('cw_des', 'Design')
        self.pm       = _profile('cw_pm', 'PM')
        self.coord    = _profile('cw_coord', 'Project Coordinator')
        self.scm      = _profile('cw_scm', 'SCM')
        self.finance  = _profile('cw_fin', 'Finance')
        self.program = Program.objects.create(
            name='Test-CW', program_type='OPEX', client_name='CWClient',
            status='Active', short_tender_code='CW')
        self.item = BOQItemMaster.objects.create(
            code='CW-001', description='Module', unit='Nos', project_type='OPEX')

    # ── fixtures ────────────────────────────────────────────────────────────

    def _site(self, code, pm='default'):
        site = Project(
            project_id=code, customer_name='CWClient', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code=code, dc_capacity_kw=Decimal('100.00'),
            status='Draft', assigned_design=self.designer,
            assigned_pm=self.pm if pm == 'default' else pm)
        site.save()
        site.coordinators.add(self.coord)
        now = timezone.now()
        assignment = DesignAssignment.objects.create(
            project=site, status=DESIGN_IN_DESIGN, assigned_to=self.designer,
            assigned_by=self.head, assigned_at=now - timedelta(days=10),
            survey_file_bucket='b', survey_file_path=f'{code}/survey/x.pdf')
        DueDateCommitment.objects.create(
            assignment=assignment, proposed_date=timezone.localdate() + timedelta(days=5),
            proposed_by=self.designer, approved_by=self.head, approved_at=now,
            is_current=True)
        return site, assignment

    def _package(self, assignment):
        """FIXTURE WRITER — attempt 1 with an Arka approved at both gates, a current
        cad_zip, a submitted BOQ and one BOQ line on a catalogue item, QC started."""
        now = timezone.now()
        attempt = DesignAttempt.objects.create(
            assignment=assignment, attempt_number=1, opened_reason=ATTEMPT_REASON_INITIAL,
            boq_submitted_at=now, boq_submitted_by=self.designer, qc_started_at=now)
        arka = ArkaSubmission.objects.create(
            attempt=attempt, version=1, capacity_kw=Decimal('100.00'),
            arka_link='https://example.com/arka', submitted_by=self.designer,
            verdict=ARKA_APPROVED, reviewed_by=self.qc, reviewed_at=now,
            head_verdict=ARKA_APPROVED, head_reviewed_by=self.head, head_reviewed_at=now,
            is_current=True)
        DesignFile.objects.create(
            attempt=attempt, kind=DESIGN_FILE_CAD_ZIP, version=1, bucket='b',
            path=f'{assignment.project.project_id}/cad/x.zip', original_filename='x.zip',
            size_bytes=10, content_type='application/zip', derived_from_arka=arka,
            uploaded_by=self.designer, is_current=True)
        boq = BOQ.objects.create(project=assignment.project)
        BOQItem.objects.create(
            boq=boq, serial_no=1, category='Solar Modules', description='Module',
            item_master=self.item, uom='Nos', boq_quantity=Decimal('10'))
        DesignAssignment.objects.filter(pk=assignment.pk).update(current_attempt_number=1)
        assignment.refresh_from_db()
        return attempt

    def _pre_release(self, code):
        """A site in Design QC — Part 4's window, before release."""
        site, assignment = self._site(code)
        self._package(assignment)
        DesignAssignment.objects.filter(pk=assignment.pk).update(status=DESIGN_IN_QC)
        assignment.refresh_from_db()
        return site, assignment

    def _released(self, code, pm='default'):
        """Both gates passed and the attempt closed (fixture writer), then released by the
        PM through design_pm_approve() — the product writer of `released`."""
        site, assignment = self._site(code, pm=pm)
        attempt = self._package(assignment)
        now = timezone.now()
        DesignAttempt.objects.filter(pk=attempt.pk).update(
            qc_verdict=QC_PASSED, qc_reviewed_by=self.qc, qc_reviewed_at=now,
            head_started_at=now, head_verdict=QC_PASSED, head_reviewed_by=self.head,
            head_reviewed_at=now, closed_at=now)
        DesignAssignment.objects.filter(pk=assignment.pk).update(
            status=DESIGN_AWAITING_PM_APPROVAL)
        approver = site.assigned_pm or self.coord
        self._login(approver)
        self.client.post(reverse('design_pm_approve', kwargs={'project_id': code}))
        self.client.logout()
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, DESIGN_RELEASED, f'{code} did not release')
        self.assertIsNotNone(assignment.released_at)
        return site, assignment

    def _group(self, name, sites):
        """SCM forms a draft procurement group through site_group_create."""
        self._login(self.scm)
        self.client.post(reverse('site_group_create', kwargs={'pk': self.program.pk}),
                         {'name': name, 'project_ids': [s.pk for s in sites]})
        self.client.logout()
        group = SiteGroup.objects.get(program=self.program, name=name)
        self.assertEqual(group.memberships.filter(removed_at__isnull=True).count(),
                         len(sites))
        return group

    def _lock(self, group):
        self._login(self.scm)
        response = self.client.post(reverse('site_group_lock', kwargs={'pk': group.pk}))
        self.client.logout()
        group.refresh_from_db()
        return response

    # ── helpers ─────────────────────────────────────────────────────────────

    def _login(self, profile):
        self.assertTrue(self.client.login(username=profile.user.username, password='x'))

    def _raise(self, profile, site, reason='Client moved the array to the north shed.'):
        self._login(profile)
        response = self.client.post(
            reverse('design_change_request', kwargs={'project_id': site.project_id}),
            {'reason': reason})
        self.client.logout()
        return response

    def _accept(self, change):
        self._login(self.head)
        response = self.client.post(
            reverse('design_change_request_accept', kwargs={'pk': change.pk}))
        self.client.logout()
        return response

    def _reject(self, change, reason='The current version stands.'):
        self._login(self.head)
        response = self.client.post(
            reverse('design_change_request_reject', kwargs={'pk': change.pk}),
            {'rejection_reason': reason})
        self.client.logout()
        return response

    @staticmethod
    def _messages(response):
        return ' '.join(str(m) for m in get_messages(response.wsgi_request))

    @staticmethod
    def _requests(assignment):
        return DesignChangeRequest.objects.filter(attempt__assignment=assignment)

    @staticmethod
    def _live(site):
        return site.group_memberships.filter(removed_at__isnull=True,
                                             group_type=GROUP_TYPE_PROCUREMENT)


# ===========================================================================
# 1. Raising — who, and on which of the three released states (D-a, D-b)
# ===========================================================================

class RaiseOnReleasedSitesTests(ChangeWindowBase):

    def test_01_pm_coordinator_and_scm_each_raise_on_a_pool_site(self):
        for n, raiser in enumerate((self.pm, self.coord, self.scm)):
            with self.subTest(raiser=raiser.user.username):
                site, assignment = self._released(f'CW-P{n}')
                # Refusal first: Finance is turned away with a 403 and writes nothing.
                self.assertEqual(self._raise(self.finance, site).status_code, 403)
                self.assertFalse(self._requests(assignment).exists())

                response = self._raise(raiser, site)
                self.assertEqual(response.status_code, 302)
                change = self._requests(assignment).get()
                self.assertEqual(change.verdict, CHANGE_REQUEST_PENDING)
                self.assertEqual(change.requested_by, raiser)

    def test_02_pm_coordinator_and_scm_each_raise_on_a_draft_group_site_which_stays(self):
        sites = {raiser: self._released(f'CW-D{n}')
                 for n, raiser in enumerate((self.pm, self.coord, self.scm))}
        group = self._group('Batch D', [s for s, _ in sites.values()])
        for raiser, (site, assignment) in sites.items():
            with self.subTest(raiser=raiser.user.username):
                self._raise(raiser, site)
                self.assertEqual(self._requests(assignment).get().verdict,
                                 CHANGE_REQUEST_PENDING)
                # Q2: the raise does NOT pull the site out of its draft group.
                self.assertEqual(self._live(site).get().group, group)
                # Refusal: a second raise on the same attempt is refused while one is pending.
                response = self._raise(raiser, site, reason='and another thing')
                self.assertIn('is already with the Design Head', self._messages(response))
                self.assertEqual(self._requests(assignment).count(), 1)

    def test_03_all_three_are_refused_on_a_locked_group_site_with_a_message(self):
        locked_site, locked_assignment = self._released('CW-L1')
        pool_site, pool_assignment = self._released('CW-L2')
        self._lock(self._group('Batch L', [locked_site]))
        for raiser in (self.pm, self.coord, self.scm):
            with self.subTest(raiser=raiser.user.username):
                response = self._raise(raiser, locked_site)
                self.assertEqual(response.status_code, 302, 'a business refusal is not a 403')
                self.assertIn(LOCKED_REFUSAL, self._messages(response))
                self.assertIn('Batch L', self._messages(response))
                self.assertFalse(self._requests(locked_assignment).exists())
        # Pass: the same people are not refused everything — the pool site takes a raise.
        self._raise(self.scm, pool_site)
        self.assertTrue(self._requests(pool_assignment).exists())

    def test_04_finance_is_refused_the_form_and_the_post(self):
        site, assignment = self._released('CW-F1')
        self._login(self.finance)
        form_url = reverse('design_change_request_form', kwargs={'project_id': site.project_id})
        self.assertEqual(self.client.get(form_url).status_code, 403)
        self.client.logout()
        self.assertEqual(self._raise(self.finance, site).status_code, 403)
        self.assertFalse(self._requests(assignment).exists())
        # Pass: the site's PM gets the form with the window open.
        self._login(self.pm)
        response = self.client.get(form_url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['window_open'])


# ===========================================================================
# 2. Before release — the PM's window, not SCM's (Q1)
# ===========================================================================

class PreReleaseWindowTests(ChangeWindowBase):

    def test_05_scm_is_refused_before_release_and_the_pm_and_coordinator_are_not(self):
        site, assignment = self._pre_release('CW-Q1')
        response = self._raise(self.scm, site)
        self.assertEqual(response.status_code, 302)
        self.assertIn(SCM_PRE_RELEASE_REFUSAL, self._messages(response))
        self.assertFalse(self._requests(assignment).exists())

        self._raise(self.pm, site)
        self.assertEqual(self._requests(assignment).get().requested_by, self.pm)

        site2, assignment2 = self._pre_release('CW-Q2')
        self._raise(self.coord, site2)
        self.assertEqual(self._requests(assignment2).get().requested_by, self.coord)


# ===========================================================================
# 3. The lock refuses a group holding a pending request (D-d / Q2)
# ===========================================================================

class LockRefusalTests(ChangeWindowBase):

    def test_06_lock_is_refused_naming_the_site_and_allowed_once_rejected(self):
        pending_site, pending_assignment = self._released('CW-K1')
        quiet_site, _ = self._released('CW-K2')
        group = self._group('Batch K', [pending_site, quiet_site])
        self._raise(self.scm, pending_site)

        response = self._lock(group)
        self.assertEqual(group.status, SITE_GROUP_DRAFT)
        message = self._messages(response)
        self.assertIn('cannot be locked', message)
        self.assertIn('CW-K1', message)
        self.assertNotIn('CW-K2', message)

        self._reject(self._requests(pending_assignment).get())
        self._lock(group)
        self.assertEqual(group.status, SITE_GROUP_LOCKED)


# ===========================================================================
# 4. Acceptance and rejection (B4, B6, Q2)
# ===========================================================================

class AcceptTests(ChangeWindowBase):

    def test_07_accept_is_refused_if_the_group_was_locked_after_the_raise(self):
        site, assignment = self._released('CW-A1')
        group = self._group('Batch A', [site])
        self._raise(self.pm, site)
        change = self._requests(assignment).get()
        released_at = assignment.released_at
        attempts_before = assignment.attempts.count()

        # THE RACE, FIXTURED. site_group_lock() refuses this group while the request is
        # pending, so the only way here is a lock that committed between the raise's check
        # and the lock view's — written directly, which is exactly what a race leaves behind.
        SiteGroup.objects.filter(pk=group.pk).update(
            status=SITE_GROUP_LOCKED, locked_by=self.scm, locked_at=timezone.now())

        response = self._accept(change)
        self.assertIn(ACCEPT_REFUSAL, self._messages(response))
        self.assertIn('Batch A', self._messages(response))
        change.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_PENDING)
        self.assertIsNone(change.decided_by_id)
        self.assertEqual(assignment.status, DESIGN_RELEASED)
        self.assertEqual(assignment.released_at, released_at)
        self.assertEqual(assignment.attempts.count(), attempts_before)
        self.assertEqual(self._live(site).get().group, group)

        # Pass: the request is not stranded — the Head can close it by rejecting.
        self._reject(change)
        change.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_REJECTED)

    def test_08_accept_clears_the_release_stamp_and_takes_the_site_out_of_its_group(self):
        site, assignment = self._released('CW-A2')
        group = self._group('Batch A2', [site])
        self._raise(self.scm, site)
        change = self._requests(assignment).get()
        self.assertIsNotNone(assignment.released_by_id)

        response = self._accept(change)
        self.assertIn('It left procurement group "Batch A2"', self._messages(response))
        assignment.refresh_from_db()
        change.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_ACCEPTED)
        self.assertEqual(assignment.status, DESIGN_IN_DESIGN)
        self.assertIsNone(assignment.released_at)
        self.assertIsNone(assignment.released_by_id)
        self.assertEqual(assignment.current_attempt_number, 2)
        self.assertEqual(assignment.attempts.get(attempt_number=2).opened_reason,
                         ATTEMPT_REASON_PM_CHANGE_REQUEST)
        self.assertFalse(self._live(site).exists())
        departed = SiteGroupMembership.objects.get(group=group, project=site)
        self.assertEqual(departed.removal_reason, CHANGE_REQUEST_REMOVAL_REASON)
        # The ledger keeps the release the stamp no longer describes.
        self.assertTrue(StatusTransition.objects.filter(
            subject_type=SUBJECT_DESIGN_ASSIGNMENT, subject_id=assignment.pk,
            from_status=DESIGN_RELEASED, to_status=DESIGN_IN_DESIGN).exists())

        # Refusal: a second acceptance of the same request opens nothing.
        response = self._accept(change)
        self.assertIn('has already been accepted', self._messages(response))
        self.assertEqual(assignment.attempts.count(), 2)

    def test_09_reject_leaves_the_site_in_its_draft_group(self):
        site, assignment = self._released('CW-A3')
        group = self._group('Batch A3', [site])
        self._raise(self.coord, site)
        change = self._requests(assignment).get()

        # Refusal: a rejection with no reason is refused and changes nothing.
        response = self._reject(change, reason='   ')
        self.assertIn('a rejection reason is required', self._messages(response))
        change.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_PENDING)

        self._reject(change)
        change.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_REJECTED)
        self.assertEqual(self._live(site).get().group, group)
        self.assertEqual(assignment.status, DESIGN_RELEASED)
        self.assertIsNotNone(assignment.released_at)


# ===========================================================================
# 5. m_change_request_rate files by the site's PM (D-c)
# ===========================================================================

class ChangeRequestRateAttributionTests(ChangeWindowBase):

    def _panel(self):
        result = compute([self.program], {'change_request_rate'})
        return next(p['data'] for p in result['panels']
                    if p['metric'].key == 'change_request_rate')

    def test_10_an_scm_request_is_filed_under_the_sites_pm_and_a_pmless_one_under_unassigned(self):
        site, assignment = self._released('CW-M1')
        self._released('CW-M2')                       # keeps a finished site for the PM
        orphan, orphan_assignment = self._released('CW-M3', pm=None)
        self._raise(self.scm, site)
        self._accept(self._requests(assignment).get())
        self._raise(self.scm, orphan)
        self._accept(self._requests(orphan_assignment).get())

        rows = {r['label']: r for r in self._panel()['rows']}
        pm_label = self.pm.user.get_full_name() or self.pm.user.username
        scm_label = self.scm.user.get_full_name() or self.scm.user.username
        self.assertEqual(rows[pm_label]['accepted'], 1)
        self.assertEqual(rows[pm_label]['finished'], 1)
        self.assertEqual(rows['Unassigned']['accepted'], 1)
        # Refusal: the raiser gets no row of their own.
        self.assertNotIn(scm_label, rows)


# ===========================================================================
# 6. The control follows the predicate (Q3, B2)
# ===========================================================================

class ControlVisibilityTests(ChangeWindowBase):

    def setUp(self):
        super().setUp()
        self.pool_site, self.pool_a = self._released('CW-V1')
        self.draft_site, self.draft_a = self._released('CW-V2')
        self.locked_site, self.locked_a = self._released('CW-V3')
        self.draft_group = self._group('Batch V draft', [self.draft_site])
        self.locked_group = self._group('Batch V locked', [self.locked_site])
        self._lock(self.locked_group)
        self.assertEqual(self.locked_group.status, SITE_GROUP_LOCKED)

    def _form_url(self, site):
        return reverse('design_change_request_form', kwargs={'project_id': site.project_id})

    def test_11_the_scm_group_screens_offer_the_link_exactly_where_the_predicate_passes(self):
        self._login(self.scm)
        list_page = self.client.get(reverse('site_group_list', kwargs={'pk': self.program.pk}))
        draft_page = self.client.get(reverse('site_group_detail',
                                             kwargs={'pk': self.draft_group.pk}))
        locked_page = self.client.get(reverse('site_group_detail',
                                              kwargs={'pk': self.locked_group.pk}))

        self.assertEqual(list_page.context['change_request_project_ids'], {self.pool_site.pk})
        self.assertContains(list_page, self._form_url(self.pool_site))
        self.assertIn(self.draft_site.pk, draft_page.context['change_request_project_ids'])
        self.assertContains(draft_page, self._form_url(self.draft_site))
        # Refusal: no locked-group row carries the link.
        self.assertEqual(locked_page.context['change_request_project_ids'], set())
        self.assertNotContains(locked_page, self._form_url(self.locked_site))

        for site, assignment, page in ((self.pool_site, self.pool_a, list_page),
                                       (self.draft_site, self.draft_a, draft_page),
                                       (self.locked_site, self.locked_a, locked_page)):
            with self.subTest(site=site.project_id):
                self.assertEqual(site.pk in page.context['change_request_project_ids'],
                                 change_request_window_open(self.scm.user, assignment))

    def test_12_finance_never_sees_the_link_and_the_design_head_gets_none(self):
        self._login(self.finance)
        self.assertEqual(self.client.get(
            reverse('site_group_list', kwargs={'pk': self.program.pk})).status_code, 403)
        self.assertEqual(self.client.get(
            reverse('site_group_detail', kwargs={'pk': self.draft_group.pk})).status_code, 403)
        self.client.logout()

        # The Head reads these screens and may not raise: the set is empty, no link renders.
        self._login(self.head)
        page = self.client.get(reverse('site_group_list', kwargs={'pk': self.program.pk}))
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context['change_request_project_ids'], set())
        self.assertNotContains(page, self._form_url(self.pool_site))
        self.client.logout()

        # Pass: SCM on the same page does get it.
        self._login(self.scm)
        page = self.client.get(reverse('site_group_list', kwargs={'pk': self.program.pk}))
        self.assertContains(page, self._form_url(self.pool_site))

    def test_13_the_form_and_the_pm_dashboard_agree_with_the_predicate_on_all_three_states(self):
        states = ((self.pool_site, self.pool_a, True),
                  (self.draft_site, self.draft_a, True),
                  (self.locked_site, self.locked_a, False))
        targets = pm_change_request_targets(self.pm.user, [s for s, _, _ in states])
        for user_profile in (self.pm, self.scm):
            self._login(user_profile)
            for site, assignment, expected in states:
                with self.subTest(user=user_profile.user.username, site=site.project_id):
                    self.assertEqual(design_change_window_open(assignment), expected)
                    response = self.client.get(self._form_url(site))
                    self.assertEqual(response.context['window_open'], expected)
                    self.assertEqual(response.context['window_open'],
                                     change_request_window_open(user_profile.user, assignment))
                    if user_profile == self.pm:
                        self.assertEqual(site.pk in targets, expected)
                    if not expected:
                        self.assertEqual(response.context['closed_reason'], 'locked')
            self.client.logout()

        # The pre-release site: open for the PM, closed for SCM with its own reason.
        pre_site, _ = self._pre_release('CW-V4')
        self.assertIn(pre_site.pk, pm_change_request_targets(self.pm.user, [pre_site]))
        self._login(self.scm)
        response = self.client.get(self._form_url(pre_site))
        self.assertFalse(response.context['window_open'])
        self.assertEqual(response.context['closed_reason'], 'scm_before_release')
