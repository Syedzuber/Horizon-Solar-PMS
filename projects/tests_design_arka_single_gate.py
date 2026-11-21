"""W-1 — the Design Head's separate Arka approval is removed (ruling 1, Praveen, 21 Nov 2026).

WHAT IS PINNED HERE
-------------------
  * Design QC's Arka approval moves the site on. With the CAD and BOQ already in, the same
    request reaches `artifacts_uploaded`: two activity rows (the approval and the package
    advance) and one StatusTransition, because the approval re-states `arka_submitted` and
    the ledger records no x -> x row (R-3). Without them, the site stays `arka_submitted`
    and is pending at the Designer.
  * QC's rejection still writes `arka_rejected`.
  * The Head's Arka URLs no longer resolve, and the views are gone.
  * THE TWO-PEOPLE RULE MOVED TO THE PACKAGE GATE: the Head's final pass AND fail refuse the
    person who gave the Arka its Design QC verdict, and admit another Head-authority user.
    The review screen explains the refusal before anyone clicks.
  * A QC failure that carries a QC-approved Arka opens the next attempt at `arka_submitted`.
  * A hold placed after QC's Arka approval lifts back to `arka_submitted`.
  * THE STRANDED-SITE FALLBACK: a legacy site at `awaiting_head_arka` is listed in the QC
    queue and moved on by Design QC without a second verdict; the designer is refused it,
    and QC's reject cannot turn the approval into a rejection.
  * `awaiting_head_arka` is not in the upload window or the change-request statuses.
  * Capacity counts a QC-approved Arka (design_metrics and design_analytics).

Every step goes through the real endpoints, so the attempt, the Arka and every ledger row are
the ones the product writes. The one exception — placing a legacy site at
`awaiting_head_arka`, which nothing writes any more — goes through apply_design_status().
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import NoReverseMatch, Resolver404, resolve, reverse

from . import design_views
from .design_analytics import analytics_dataset
from .design_metrics import tender_metrics
from .design_views import (
    CHANGE_REQUEST_STATUSES, DESIGN_UPLOAD_OPEN_STATUSES, apply_design_status,
)
from .models import (
    ActivityLog, ArkaSubmission, BOQ, BOQItem, BOQItemMaster, DesignAssignment, Program,
    Project, StatusTransition, SUBJECT_DESIGN_ASSIGNMENT,
    ARKA_APPROVED, ARKA_PENDING, ARKA_REJECTED,
    DESIGN_ARKA_REJECTED, DESIGN_ARKA_SUBMITTED, DESIGN_ARTIFACTS_UPLOADED,
    DESIGN_AWAITING_ALLOCATION, DESIGN_AWAITING_HEAD_ARKA, DESIGN_AWAITING_HEAD_QC,
    DESIGN_AWAITING_PM_APPROVAL, DESIGN_FILE_CAD_ZIP, DESIGN_IN_DESIGN, DESIGN_IN_QC,
    DESIGN_SURVEY_RETURNED, ERR_BOQ_QUANTITY, ERR_LAYOUT,
    PENDING_AT_DESIGNER, PENDING_AT_QC_POOL, QC_FAILED, QC_PASSED, QC_PENDING,
    design_pending_at,
)


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


class SingleGateBase(TestCase):

    def setUp(self):
        self.designer = _profile('sg_des',  'Design')
        self.qc       = _profile('sg_qc',   'Design', is_design_qc=True)
        self.qc2      = _profile('sg_qc2',  'Design', is_design_qc=True)
        self.head     = _profile('sg_head', 'Design', is_design_head=True)
        # Holds both flags: may QC an Arka, and has Head authority at the package gate.
        self.both     = _profile('sg_both', 'Design', is_design_head=True, is_design_qc=True)
        self.pm       = _profile('sg_pm',   'PM')
        # Nothing mandatory, so BOQ completion turns on the stamp and not on catalogue flags.
        self.master = BOQItemMaster.objects.create(
            code='OPX-SG-1', description='Module 550Wp', unit='Nos',
            category='Solar Modules', project_type='OPEX',
            is_active=True, is_mandatory=False, sort_order=1)
        self.program = Program.objects.create(
            name='Test-SingleGate', program_type='OPEX', client_name='SGClient',
            status='Active', short_tender_code='SG')

    # ── sites, driven through the real endpoints ───────────────────────────

    def _new_site(self, code):
        site = Project(
            project_id=code, customer_name='SGClient', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code=code, assigned_pm=self.pm,
            dc_capacity_kw=Decimal('100.00'), status='Draft')
        site.save()
        DesignAssignment.objects.create(
            project=site, status=DESIGN_AWAITING_ALLOCATION,
            survey_file_bucket='b', survey_file_path=f'{code}/survey/x.pdf')
        return site

    def _a(self, site):
        return DesignAssignment.objects.get(project=site)

    def _arka(self, site):
        """The current Arka of the CURRENT attempt — an earlier attempt keeps its own."""
        a = self._a(site)
        return ArkaSubmission.objects.get(attempt__assignment=a, is_current=True,
                                          attempt__attempt_number=a.current_attempt_number)

    def _post(self, profile, name, site, data=None):
        self.client.force_login(profile.user)
        return self.client.post(reverse(name, kwargs={'project_id': site.project_id}),
                                data or {})

    def _get(self, profile, name, site=None):
        self.client.force_login(profile.user)
        kwargs = {'project_id': site.project_id} if site is not None else {}
        return self.client.get(reverse(name, kwargs=kwargs))

    def _messages(self, response):
        return ' '.join(str(m) for m in get_messages(response.wsgi_request))

    def _step(self, profile, name, site, expected, data=None):
        response = self._post(profile, name, site, data)
        self.assertEqual(self._a(site).status, expected,
                         f'{name} did not reach {expected}: {self._messages(response)}')
        return response

    def _transitions(self, site):
        return list(StatusTransition.objects
                    .filter(subject_type=SUBJECT_DESIGN_ASSIGNMENT, subject_id=self._a(site).pk)
                    .order_by('occurred_at', 'pk'))

    def _arka_submitted(self, code):
        site = self._new_site(code)
        self._step(self.head, 'design_allocate', site, DESIGN_IN_DESIGN,
                   {'designer_id': self.designer.pk})
        self._step(self.designer, 'design_arka_submit', site, DESIGN_ARKA_SUBMITTED,
                   {'capacity_kw': '120.00', 'arka_link': 'https://example.com/arka',
                    'remarks': ''})
        return site

    def _upload_cad(self, site):
        upload = SimpleUploadedFile('cad.zip', b'PK\x03\x04stub', content_type='application/zip')
        with patch('projects.design_views.validate_cad_zip', return_value=[]), \
             patch('projects.design_views.upload_design_file',
                   return_value=('Horizon-PMS-Design', f'{site.project_id}/cad_zip/cad.zip')):
            return self._post(self.designer, 'design_artifact_upload', site, {
                'kind': DESIGN_FILE_CAD_ZIP, 'artifact_file': upload, 'remarks': ''})

    def _complete_boq(self, site):
        boq = BOQ.objects.create(project=site, status='Draft')
        BOQItem.objects.create(
            boq=boq, serial_no=1, category='Solar Modules', description='Module 550Wp',
            item_master=self.master, uom='Nos', boq_quantity=Decimal('40'))
        return self._post(self.designer, 'design_boq_complete', site, {'boq_remarks': ''})

    def _with_artifacts(self, code):
        """Arka submitted (pending), CAD and BOQ already in — the package waits on QC."""
        site = self._arka_submitted(code)
        self._upload_cad(site)
        self._complete_boq(site)
        self.assertEqual(self._a(site).status, DESIGN_ARKA_SUBMITTED)
        return site

    def _to_awaiting_head_qc(self, code, arka_qc=None, package_qc=None):
        """Through the Arka gate (by `arka_qc`) and package QC (by `package_qc`)."""
        site = self._with_artifacts(code)
        self._step(arka_qc or self.qc, 'design_arka_approve', site, DESIGN_ARTIFACTS_UPLOADED)
        self._step(package_qc or self.qc, 'design_qc_start', site, DESIGN_IN_QC)
        self._step(package_qc or self.qc, 'design_qc_pass', site, DESIGN_AWAITING_HEAD_QC)
        return site

    def _make_legacy(self, site):
        """A site QC approved before W-1 shipped, still waiting for the removed Head step.
        Nothing writes `awaiting_head_arka` any more; apply_design_status() is the call the
        pre-W-1 QC approval made."""
        arka = self._arka(site)
        arka.verdict = ARKA_APPROVED
        arka.reviewed_by = self.qc
        arka.save(update_fields=['verdict', 'reviewed_by'])
        apply_design_status(self._a(site), DESIGN_AWAITING_HEAD_ARKA, self.qc,
                            'legacy: passed Design QC, awaiting the Head (pre-W-1)',
                            'design_arka_qc_approved')
        self.assertEqual(self._a(site).status, DESIGN_AWAITING_HEAD_ARKA)


# ===========================================================================
# T1 — QC's approval moves the site on
# ===========================================================================

class QcApprovalMovesOnTests(SingleGateBase):

    def test_a_complete_package_reaches_artifacts_uploaded_in_one_request(self):
        site = self._with_artifacts('SG-T1-FULL')
        transitions_before = len(self._transitions(site))
        last_activity = ActivityLog.objects.filter(project=site).latest('id').pk

        response = self._post(self.qc, 'design_arka_approve', site)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._a(site).status, DESIGN_ARTIFACTS_UPLOADED)
        arka = self._arka(site)
        self.assertEqual((arka.verdict, arka.head_verdict), (ARKA_APPROVED, ARKA_PENDING))
        self.assertEqual(arka.reviewed_by_id, self.qc.pk)
        # Both writes went through apply_design_status(): two activity rows from this one
        # request, in order...
        codes = list(ActivityLog.objects.filter(project=site, pk__gt=last_activity)
                     .order_by('pk').values_list('action_code', flat=True))
        self.assertEqual(codes, ['design_arka_qc_approved', 'design_artifacts_uploaded'])
        # ...and one StatusTransition: the approval re-states `arka_submitted`, which the
        # ledger does not record (R-3), and the advance is the one real move.
        new = [(t.from_status, t.to_status) for t in self._transitions(site)[transitions_before:]]
        self.assertEqual(new, [(DESIGN_ARKA_SUBMITTED, DESIGN_ARTIFACTS_UPLOADED)])
        self.assertIn('package is complete', self._messages(response))
        # Pending at Design QC (the open pool — nobody is named on this site).
        self.assertEqual(design_pending_at(self._a(site), arka), PENDING_AT_QC_POOL)

    def test_without_cad_or_boq_it_stays_arka_submitted_and_is_the_designers(self):
        site = self._arka_submitted('SG-T1-PART')
        self._upload_cad(site)    # CAD in, BOQ not

        self._step(self.qc, 'design_arka_approve', site, DESIGN_ARKA_SUBMITTED)

        arka = self._arka(site)
        self.assertEqual(arka.verdict, ARKA_APPROVED)
        self.assertEqual(design_pending_at(self._a(site), arka), PENDING_AT_DESIGNER)
        # And completing the BOQ now completes the package — no Head step in between.
        self._complete_boq(site)
        self.assertEqual(self._a(site).status, DESIGN_ARTIFACTS_UPLOADED)

    def test_qc_rejection_still_goes_to_arka_rejected(self):
        site = self._arka_submitted('SG-T1-REJ')
        self._step(self.qc, 'design_arka_reject', site, DESIGN_ARKA_REJECTED,
                   {'rejection_reason': 'string sizing is wrong', 'error_category': ERR_LAYOUT})
        arka = self._arka(site)
        self.assertEqual((arka.verdict, arka.head_verdict), (ARKA_REJECTED, ARKA_PENDING))


# ===========================================================================
# T2 — the Head's Arka gate is gone
# ===========================================================================

class HeadArkaGateRemovedTests(SingleGateBase):

    def test_the_head_arka_urls_no_longer_resolve(self):
        for name in ('design_arka_head_approve', 'design_arka_head_reject'):
            with self.subTest(name=name):
                with self.assertRaises(NoReverseMatch):
                    reverse(name, kwargs={'project_id': 'SG-X'})
        for path in ('/design/SG-X/arka/head/approve/', '/design/SG-X/arka/head/reject/'):
            with self.subTest(path=path):
                with self.assertRaises(Resolver404):
                    resolve(path)

    def test_the_views_and_the_target_resolver_are_gone(self):
        for name in ('design_arka_head_approve', 'design_arka_head_reject',
                     '_head_verdict_target'):
            self.assertFalse(hasattr(design_views, name), name)

    def test_a_post_to_the_old_path_changes_nothing(self):
        site = self._arka_submitted('SG-T2-POST')
        self._step(self.qc, 'design_arka_approve', site, DESIGN_ARKA_SUBMITTED)
        self.client.force_login(self.head.user)
        response = self.client.post(f'/design/{site.project_id}/arka/head/approve/')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._arka(site).head_verdict, ARKA_PENDING)

    def test_the_head_sees_no_arka_verdict_form(self):
        site = self._arka_submitted('SG-T2-FORM')
        self._step(self.qc, 'design_arka_approve', site, DESIGN_ARKA_SUBMITTED)
        response = self._get(self.head, 'design_head_review', site)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('can_head_verdict', response.context)
        self.assertFalse(response.context['can_qc_verdict'])
        self.assertNotContains(response, 'Reject as Design Head')
        self.assertNotContains(response, 'Awaiting the Design Head')

    def test_awaiting_head_arka_is_out_of_the_upload_window_and_change_request_statuses(self):
        self.assertNotIn(DESIGN_AWAITING_HEAD_ARKA, DESIGN_UPLOAD_OPEN_STATUSES)
        self.assertNotIn(DESIGN_AWAITING_HEAD_ARKA, CHANGE_REQUEST_STATUSES)


# ===========================================================================
# T3 — the two-people protection, at the package gate
# ===========================================================================

class ArkaQcGiverRefusedAtFinalVerdictTests(SingleGateBase):

    def test_the_arka_qc_giver_is_refused_the_final_pass_and_another_head_is_allowed(self):
        # `both` QCs the Arka; somebody else QCs the package, so the Arka is the only clash.
        site = self._to_awaiting_head_qc('SG-T3-PASS', arka_qc=self.both, package_qc=self.qc)

        response = self._post(self.both, 'design_head_qc_pass', site)
        self.assertEqual(self._a(site).status, DESIGN_AWAITING_HEAD_QC)
        attempt = self._arka(site).attempt
        attempt.refresh_from_db()
        self.assertEqual(attempt.head_verdict, QC_PENDING)
        self.assertIn('Design QC verdict on this attempt', self._messages(response))

        self._step(self.head, 'design_head_qc_pass', site, DESIGN_AWAITING_PM_APPROVAL)
        attempt.refresh_from_db()
        self.assertEqual((attempt.head_verdict, attempt.head_reviewed_by_id),
                         (QC_PASSED, self.head.pk))

    def test_the_arka_qc_giver_is_refused_the_final_fail_too(self):
        site = self._to_awaiting_head_qc('SG-T3-FAIL', arka_qc=self.both, package_qc=self.qc)

        self._post(self.both, 'design_head_qc_fail', site,
                   {'head_remarks': 'wrong', 'error_category': ERR_LAYOUT})
        self.assertEqual(self._a(site).status, DESIGN_AWAITING_HEAD_QC)
        self.assertEqual(self._a(site).attempts.count(), 1, 'an attempt was opened anyway')

        self._post(self.head, 'design_head_qc_fail', site,
                   {'head_remarks': 'wrong', 'error_category': ERR_LAYOUT})
        attempt = self._a(site).attempts.get(attempt_number=1)
        self.assertEqual(attempt.head_verdict, QC_FAILED)

    def test_the_review_screen_explains_the_refusal_before_the_click(self):
        site = self._to_awaiting_head_qc('SG-T3-UI', arka_qc=self.both, package_qc=self.qc)

        response = self._get(self.both, 'design_qc_review', site)
        self.assertFalse(response.context['can_head_verdict'])
        self.assertTrue(response.context['blocked_by_own_qc_verdict'])
        self.assertTrue(response.context['own_arka_qc_verdict'])
        self.assertContains(response, 'Design QC verdict on this attempt')

        response = self._get(self.head, 'design_qc_review', site)
        self.assertTrue(response.context['can_head_verdict'])
        self.assertFalse(response.context['own_arka_qc_verdict'])


# ===========================================================================
# T1 — the next attempt opens on QC's approval of the carried Arka
# ===========================================================================

class CarriedArkaOpeningTests(SingleGateBase):

    def test_a_qc_fail_carrying_a_qc_approved_arka_opens_at_arka_submitted(self):
        site = self._with_artifacts('SG-CARRY')
        self._step(self.qc, 'design_arka_approve', site, DESIGN_ARTIFACTS_UPLOADED)
        self._step(self.qc, 'design_qc_start', site, DESIGN_IN_QC)

        self._step(self.qc, 'design_qc_fail', site, DESIGN_ARKA_SUBMITTED,
                   {'qc_remarks': 'quantities are off', 'error_category': ERR_BOQ_QUANTITY,
                    'redo_scope_submitted': '1', 'redo': ['boq']})

        a = self._a(site)
        self.assertEqual(a.current_attempt_number, 2)
        carried = self._arka(site)
        self.assertEqual(carried.attempt.attempt_number, 2)
        self.assertEqual((carried.verdict, carried.head_verdict), (ARKA_APPROVED, ARKA_PENDING))
        self.assertIsNotNone(carried.carried_forward_from_id)
        self.assertEqual(design_pending_at(a, carried), PENDING_AT_DESIGNER)


# ===========================================================================
# T2 — hold restore after QC's Arka approval
# ===========================================================================

class HoldAfterQcApprovalTests(SingleGateBase):

    def test_a_hold_after_qc_approval_lifts_back_to_arka_submitted(self):
        site = self._arka_submitted('SG-HOLD')
        self._step(self.qc, 'design_arka_approve', site, DESIGN_ARKA_SUBMITTED)

        self._post(self.designer, 'design_mark_blocked', site,
                   {'reason': 'the survey has no roof dimensions'})
        self.assertEqual(self._a(site).status, DESIGN_SURVEY_RETURNED)
        self._post(self.head, 'design_survey_link_set', site,
                   {'survey_folder_url': 'https://drive.google.com/drive/folders/new'})

        self.assertEqual(self._a(site).status, DESIGN_ARKA_SUBMITTED)
        self.assertEqual(self._arka(site).verdict, ARKA_APPROVED)
        # And the package still completes from there.
        self._upload_cad(site)
        self._complete_boq(site)
        self.assertEqual(self._a(site).status, DESIGN_ARTIFACTS_UPLOADED)


# ===========================================================================
# T4 — the stranded-site fallback
# ===========================================================================

class StrandedSiteFallbackTests(SingleGateBase):

    def test_the_qc_queue_lists_a_legacy_site_for_design_qc_to_move_on(self):
        site = self._arka_submitted('SG-LEG-Q')
        self._make_legacy(site)

        response = self._get(self.qc2, 'design_qc_queue')
        rows = [r for r in response.context['arka_rows'] if r['site'] == site]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['legacy'])
        self.assertTrue(rows[0]['can_qc'])

        response = self._get(self.qc2, 'design_head_review', site)
        self.assertTrue(response.context['can_move_on_legacy'])
        self.assertFalse(response.context['can_qc_verdict'])
        self.assertContains(response, 'Move on — Arka v1')

    def test_moving_a_complete_legacy_site_on_reaches_artifacts_uploaded(self):
        site = self._with_artifacts('SG-LEG-FULL')
        self._make_legacy(site)
        transitions_before = len(self._transitions(site))

        self._step(self.qc2, 'design_arka_approve', site, DESIGN_ARTIFACTS_UPLOADED)

        # No second verdict: the original QC reviewer stays on the row.
        arka = self._arka(site)
        self.assertEqual((arka.verdict, arka.reviewed_by_id), (ARKA_APPROVED, self.qc.pk))
        self.assertEqual(
            [(t.from_status, t.to_status) for t in self._transitions(site)[transitions_before:]],
            [(DESIGN_AWAITING_HEAD_ARKA, DESIGN_ARKA_SUBMITTED),
             (DESIGN_ARKA_SUBMITTED, DESIGN_ARTIFACTS_UPLOADED)])

    def test_moving_an_incomplete_legacy_site_on_returns_it_to_the_designer(self):
        site = self._arka_submitted('SG-LEG-PART')
        self._make_legacy(site)
        self._step(self.qc, 'design_arka_approve', site, DESIGN_ARKA_SUBMITTED)
        self.assertEqual(design_pending_at(self._a(site), self._arka(site)),
                         PENDING_AT_DESIGNER)

    def test_the_designer_is_refused_and_qc_reject_cannot_overturn_the_approval(self):
        site = self._arka_submitted('SG-LEG-REF')
        self._make_legacy(site)

        self.assertEqual(self._post(self.designer, 'design_arka_approve', site).status_code, 403)
        self._post(self.qc, 'design_arka_reject', site,
                   {'rejection_reason': 'changed my mind', 'error_category': ERR_LAYOUT})

        self.assertEqual(self._a(site).status, DESIGN_AWAITING_HEAD_ARKA)
        self.assertEqual(self._arka(site).verdict, ARKA_APPROVED)

    def test_a_legacy_hold_from_awaiting_head_arka_lifts_to_arka_submitted(self):
        site = self._arka_submitted('SG-LEG-HOLD')
        self._make_legacy(site)
        self._post(self.designer, 'design_mark_blocked', site,
                   {'reason': 'the survey has no roof dimensions'})
        self.assertEqual(self._a(site).status, DESIGN_SURVEY_RETURNED)

        self._post(self.head, 'design_survey_link_set', site,
                   {'survey_folder_url': 'https://drive.google.com/drive/folders/new'})

        self.assertEqual(self._a(site).status, DESIGN_ARKA_SUBMITTED)


# ===========================================================================
# Capacity counts a QC-approved Arka
# ===========================================================================

class CapacityCountsQcApprovedArkaTests(SingleGateBase):

    def test_a_qc_approved_arka_is_counted_in_both_capacity_figures(self):
        site = self._arka_submitted('SG-CAP')
        # Before the verdict: nothing counted.
        self.assertEqual(tender_metrics(self.program)['capacity']['contributing'], 0)

        self._step(self.qc, 'design_arka_approve', site, DESIGN_ARKA_SUBMITTED)

        capacity = tender_metrics(self.program)['capacity']
        self.assertEqual(capacity['contributing'], 1)
        self.assertEqual(capacity['designed_kw'], Decimal('120.00'))
        rows = [s for s in analytics_dataset([self.program])['sites']
                if s['project'].pk == site.pk]
        self.assertEqual(rows[0]['capacity_kw'], Decimal('120.00'))
