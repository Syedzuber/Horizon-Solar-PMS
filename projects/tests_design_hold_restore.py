"""§D18 — lifting a Design Hold taken from an Arka review status restores that status.

WHY THIS FILE EXISTS
--------------------
A Design Hold is stored as `survey_returned`. The Head lifts it by supplying a survey (a file
through design_survey_upload, or a folder link through design_survey_link_set), and both call
_status_after_unblock(). Until this change that function always DERIVED the status, so a hold
taken at `arka_submitted` or `awaiting_head_arka` came back at `in_design` on the same
attempt. The pending Arka verdict could then never be recorded: both verdict targets test the
status, the Arka left design_qc_queue, and the designer's only way on was a resubmission that
stood the old version down with its verdict pending forever (EXECUTION_MODULE_DEFERRED.md §D18).

THE RULES UNDER TEST (D18 Part B sign-off, 17 Sep 2026)
  R1  a lift restores the from-status of the hold's own StatusTransition row, but only when that
      status is `arka_submitted` or `awaiting_head_arka`; every other from-status is derived
  R2  ...and only when the current Arka is in the state that status implies; otherwise
      derived, with a warning
  R3  a hold with no ledger row of its own is derived, with a warning, and never refused
  (b) a ledgered hold from a status where a hold is now refused is derived, with a warning
      naming it for repair
  (d) a restore to `arka_submitted` evaluates the progression rule in the same transaction

FIXTURES CARRY WHAT THEIR STATUS IMPLIES. Every site is allocated, designed and reviewed
through the real endpoints, so the attempt, the ArkaSubmission and every StatusTransition row
are the ones the product writes. The exceptions are named where they occur, each with the
reason no product path can produce the state.
"""
import unittest
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .design_views import apply_design_status
from .models import (
    ActivityLog, ArkaSubmission, BOQ, BOQItem, BOQItemMaster, DesignAssignment,
    DesignChangeRequest, Program, Project, StatusTransition,
    ARKA_APPROVED, ARKA_PENDING, CHANGE_REQUEST_PENDING, DESIGN_ARKA_REJECTED,
    DESIGN_ARKA_SUBMITTED, DESIGN_ARTIFACTS_UPLOADED, DESIGN_AWAITING_ALLOCATION,
    DESIGN_AWAITING_HEAD_ARKA, DESIGN_AWAITING_HEAD_QC, DESIGN_FILE_CAD_ZIP, DESIGN_IN_DESIGN,
    DESIGN_IN_QC, DESIGN_SURVEY_RETURNED, ERR_LAYOUT, SUBJECT_DESIGN_ASSIGNMENT,
)

LOGGER = 'projects.design_views'

#: The three statuses §D13 closed to a hold, and the only ones a ledgered pre-§D13 hold can
#: have been taken from.
D13_DOORS = (DESIGN_ARTIFACTS_UPLOADED, DESIGN_IN_QC, DESIGN_AWAITING_HEAD_QC)


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


class HoldRestoreBase(TestCase):

    def setUp(self):
        self.designer = _profile('hrs_des',  'Design')
        self.qc       = _profile('hrs_qc',   'Design', is_design_qc=True)
        self.head     = _profile('hrs_head', 'Design', is_design_head=True)
        self.pm       = _profile('hrs_pm',   'PM')
        # Nothing mandatory, so BOQ completion turns on the stamp and not on catalogue flags.
        self.master = BOQItemMaster.objects.create(
            code='OPX-HRS-1', description='Module 550Wp', unit='Nos',
            category='Solar Modules', project_type='OPEX',
            is_active=True, is_mandatory=False, sort_order=1)
        self.program = Program.objects.create(
            name='Test-HoldRestore', program_type='OPEX', client_name='HRSClient',
            status='Active', short_tender_code='HRS')

    # ── sites, driven through the real endpoints ───────────────────────────

    def _new_site(self, code):
        site = Project(
            project_id=code, customer_name='HRSClient', customer_phone='9876543210',
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
        return ArkaSubmission.objects.get(attempt__assignment__project=site, is_current=True)

    def _post(self, profile, name, site, data=None):
        self.client.force_login(profile.user)
        return self.client.post(reverse(name, kwargs={'project_id': site.project_id}),
                                data or {})

    def _messages(self, response):
        return ' '.join(str(m) for m in get_messages(response.wsgi_request))

    def _step(self, profile, name, site, expected, data=None):
        response = self._post(profile, name, site, data)
        self.assertEqual(self._a(site).status, expected,
                         f'{name} did not reach {expected}: {self._messages(response)}')
        return response

    def _ledger(self, site):
        return list(StatusTransition.objects
                    .filter(subject_type=SUBJECT_DESIGN_ASSIGNMENT, subject_id=self._a(site).pk)
                    .order_by('occurred_at', 'pk'))

    def _in_design(self, code):
        site = self._new_site(code)
        self._step(self.head, 'design_allocate', site, DESIGN_IN_DESIGN,
                   {'designer_id': self.designer.pk})
        return site

    def _submit_arka(self, site):
        self._step(self.designer, 'design_arka_submit', site, DESIGN_ARKA_SUBMITTED,
                   {'capacity_kw': '100.00', 'arka_link': 'https://example.com/arka',
                    'remarks': ''})

    def _arka_submitted(self, code):
        """`arka_submitted` with v1 pending at both gates: Design QC owes the verdict."""
        site = self._in_design(code)
        self._submit_arka(site)
        return site

    def _awaiting_head_arka(self, code):
        site = self._arka_submitted(code)
        self._step(self.qc, 'design_arka_approve', site, DESIGN_AWAITING_HEAD_ARKA)
        return site

    def _arka_approved(self, code):
        """`arka_submitted` with v1 approved at both gates: the artifacts are outstanding."""
        site = self._awaiting_head_arka(code)
        self._step(self.head, 'design_arka_head_approve', site, DESIGN_ARKA_SUBMITTED)
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

    def _at_review_status(self, code, status):
        """A site at one of §D13's three doors, reached through the real endpoints."""
        site = self._arka_approved(code)
        self._upload_cad(site)
        self._complete_boq(site)
        self.assertEqual(self._a(site).status, DESIGN_ARTIFACTS_UPLOADED)
        if status in (DESIGN_IN_QC, DESIGN_AWAITING_HEAD_QC):
            self._step(self.qc, 'design_qc_start', site, DESIGN_IN_QC)
        if status == DESIGN_AWAITING_HEAD_QC:
            self._step(self.qc, 'design_qc_pass', site, DESIGN_AWAITING_HEAD_QC)
        return site

    # ── the hold and its lift ──────────────────────────────────────────────

    def _hold(self, site):
        return self._post(self.designer, 'design_mark_blocked', site,
                          {'reason': 'the survey has no roof dimensions'})

    def _held(self, site):
        self._hold(site)
        self.assertEqual(self._a(site).status, DESIGN_SURVEY_RETURNED)

    def _lift_by_link(self, site, profile=None):
        return self._post(profile or self.head, 'design_survey_link_set', site,
                          {'survey_folder_url': 'https://drive.google.com/drive/folders/new'})

    def _lift_by_upload(self, site, profile=None):
        upload = SimpleUploadedFile('survey.pdf', b'%PDF-1.4 stub',
                                    content_type='application/pdf')
        with patch('projects.design_views.upload_design_file',
                   return_value=('Horizon-PMS-Design', f'{site.project_id}/survey/new.pdf')):
            return self._post(profile or self.head, 'design_survey_upload', site,
                              {'survey_file': upload})

    def _queue_html(self, profile):
        self.client.force_login(profile.user)
        response = self.client.get(reverse('design_qc_queue'))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _review_link(self, site):
        """design_qc_queue's Arka rows link here; its package rows link to design_qc_review."""
        return reverse('design_head_review', kwargs={'project_id': site.project_id})


# ===========================================================================
# R1 — a hold from `arka_submitted` restores it, by either lift
# ===========================================================================

class RestoreFromArkaSubmittedTests(HoldRestoreBase):

    def test_01_lift_by_link_restores_and_design_qc_can_rule(self):
        site = self._arka_submitted('HRS-R1-LINK')
        self._held(site)
        self.assertNotIn(self._review_link(site), self._queue_html(self.qc))

        self._lift_by_link(site)

        self.assertEqual(self._a(site).status, DESIGN_ARKA_SUBMITTED)
        arka = self._arka(site)
        self.assertEqual((arka.version, arka.verdict, arka.head_verdict),
                         (1, ARKA_PENDING, ARKA_PENDING))
        self.assertEqual([(t.from_status, t.to_status) for t in self._ledger(site)[-2:]],
                         [(DESIGN_ARKA_SUBMITTED, DESIGN_SURVEY_RETURNED),
                          (DESIGN_SURVEY_RETURNED, DESIGN_ARKA_SUBMITTED)])
        # Back in the reviewer's queue, and the verdict can actually be recorded.
        self.assertIn(self._review_link(site), self._queue_html(self.qc))
        self._step(self.qc, 'design_arka_approve', site, DESIGN_AWAITING_HEAD_ARKA)

    def test_02_lift_by_upload_restores(self):
        site = self._arka_submitted('HRS-R1-FILE')
        self._held(site)
        self._lift_by_upload(site)
        self.assertEqual(self._a(site).status, DESIGN_ARKA_SUBMITTED)
        self.assertEqual(ActivityLog.objects.filter(project=site).latest('id').action_code,
                         'design_survey_unblocked')

    def test_03_approved_at_both_gates_restores_and_resubmission_stays_closed(self):
        """The "artifacts outstanding" state. Derived, it came back at `in_design`, where
        ARKA_SUBMITTABLE_STATUSES lets the designer stand an approved Arka down."""
        site = self._arka_approved('HRS-R1-APPR')
        self._held(site)
        self._lift_by_link(site)
        self.assertEqual(self._a(site).status, DESIGN_ARKA_SUBMITTED)

        self._post(self.designer, 'design_arka_submit', site,
                   {'capacity_kw': '100.00', 'arka_link': 'https://example.com/v2',
                    'remarks': ''})
        self.assertEqual(ArkaSubmission.objects.filter(attempt__assignment__project=site).count(), 1)
        arka = self._arka(site)
        self.assertEqual((arka.version, arka.verdict, arka.head_verdict),
                         (1, ARKA_APPROVED, ARKA_APPROVED))


# ===========================================================================
# R1 — a hold from `awaiting_head_arka` restores it
# ===========================================================================

class RestoreFromAwaitingHeadArkaTests(HoldRestoreBase):

    def test_01_lift_restores_and_the_head_can_rule(self):
        site = self._awaiting_head_arka('HRS-R1-HEAD')
        self._held(site)
        self._lift_by_link(site)

        self.assertEqual(self._a(site).status, DESIGN_AWAITING_HEAD_ARKA)
        arka = self._arka(site)
        self.assertEqual((arka.verdict, arka.head_verdict), (ARKA_APPROVED, ARKA_PENDING))
        self.assertIn(self._review_link(site), self._queue_html(self.head))
        self._step(self.head, 'design_arka_head_approve', site, DESIGN_ARKA_SUBMITTED)


# ===========================================================================
# (d) — a package completed during the hold advances on the way back
# ===========================================================================

class AdvanceOnLiftTests(HoldRestoreBase):

    def test_01_cad_and_boq_during_the_hold_then_lift_reaches_artifacts_uploaded(self):
        site = self._arka_approved('HRS-D-ADV')
        self._held(site)
        before = len(self._ledger(site))

        # Neither endpoint tests the status, so both land while the site is on hold; the
        # progression rule fires only from `arka_submitted`, so neither advances it.
        self._upload_cad(site)
        self._complete_boq(site)
        self.assertEqual(self._a(site).status, DESIGN_SURVEY_RETURNED)
        self.assertEqual(len(self._ledger(site)), before)

        self._lift_by_link(site)

        self.assertEqual(self._a(site).status, DESIGN_ARTIFACTS_UPLOADED)
        self.assertEqual([(t.from_status, t.to_status) for t in self._ledger(site)[before:]],
                         [(DESIGN_SURVEY_RETURNED, DESIGN_ARKA_SUBMITTED),
                          (DESIGN_ARKA_SUBMITTED, DESIGN_ARTIFACTS_UPLOADED)])

    def test_02_an_incomplete_package_stays_at_arka_submitted(self):
        site = self._arka_approved('HRS-D-INC')
        self._held(site)
        self._upload_cad(site)
        self._lift_by_link(site)
        self.assertEqual(self._a(site).status, DESIGN_ARKA_SUBMITTED)


# ===========================================================================
# R2 — the Arka no longer matches the status the hold was taken from
# ===========================================================================

class ArkaMismatchFallbackTests(HoldRestoreBase):

    def test_01_a_verdict_edited_out_of_band_during_the_hold_derives(self):
        """No product path records a verdict during a hold (VerdictDuringHoldTests). The Arka
        admin can: `verdict` is editable there, and this save() is that edit."""
        site = self._arka_submitted('HRS-R2-EDIT')
        self._held(site)
        arka = self._arka(site)
        arka.verdict = ARKA_APPROVED
        arka.save(update_fields=['verdict'])

        with self.assertLogs(LOGGER, 'WARNING') as logs:
            self._lift_by_link(site)

        self.assertEqual(self._a(site).status, DESIGN_IN_DESIGN)
        self.assertTrue(any('HRS-R2-EDIT' in line and 'arka_submitted' in line
                            and 'qc=approved head=pending' in line and '§D18 R2' in line
                            for line in logs.output), logs.output)

    def test_02_no_current_arka_derives(self):
        site = self._awaiting_head_arka('HRS-R2-GONE')
        self._held(site)
        ArkaSubmission.objects.filter(attempt__assignment__project=site).update(is_current=False)

        with self.assertLogs(LOGGER, 'WARNING') as logs:
            self._lift_by_link(site)

        self.assertEqual(self._a(site).status, DESIGN_IN_DESIGN)
        self.assertTrue(any('HRS-R2-GONE' in line and 'is missing' in line
                            for line in logs.output), logs.output)


# ===========================================================================
# R3 — the hold being lifted has no ledger row of its own
# ===========================================================================

class NoLedgerRowTests(HoldRestoreBase):

    def _hold_without_a_row(self, site):
        """A hold placed before fc24728 (5 Sep 2026) has the hold columns and no
        StatusTransition. A queryset update writes exactly that and nothing else, which is
        why it is used here instead of design_mark_blocked."""
        DesignAssignment.objects.filter(project=site).update(
            status=DESIGN_SURVEY_RETURNED, survey_returned_at=timezone.now(),
            survey_returned_by=self.designer, survey_return_reason='pre-ledger hold')

    def test_01_a_pre_ledger_hold_is_derived_logged_and_not_refused(self):
        site = self._arka_submitted('HRS-R3-PRE')
        self._hold_without_a_row(site)

        with self.assertLogs(LOGGER, 'WARNING') as logs:
            response = self._lift_by_link(site)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._a(site).status, DESIGN_IN_DESIGN)
        self.assertTrue(any('HRS-R3-PRE' in line and 'pre-ledger hold' in line
                            for line in logs.output), logs.output)

    def test_02_an_older_ledgered_hold_is_not_mistaken_for_this_one(self):
        """THE LAST-ROW GUARD. The site's first hold, from `arka_submitted`, is on the ledger
        and was lifted. The second has no row. Reading "the latest row INTO survey_returned"
        alone would find the first hold and restore a status the second never interrupted."""
        site = self._arka_submitted('HRS-R3-OLD')
        self._held(site)
        self._lift_by_link(site)
        self.assertEqual(self._a(site).status, DESIGN_ARKA_SUBMITTED)
        self._hold_without_a_row(site)

        with self.assertLogs(LOGGER, 'WARNING') as logs:
            self._lift_by_link(site)

        self.assertEqual(self._a(site).status, DESIGN_IN_DESIGN)
        self.assertTrue(any('HRS-R3-OLD' in line and 'pre-ledger hold' in line
                            for line in logs.output), logs.output)


# ===========================================================================
# Two holds on one site — the most recent is the one lifted
# ===========================================================================

class TwoHoldsTests(HoldRestoreBase):

    def test_01_first_from_in_design_then_from_arka_submitted(self):
        site = self._in_design('HRS-2H-A')
        self._held(site)
        self._lift_by_link(site)
        self.assertEqual(self._a(site).status, DESIGN_IN_DESIGN)

        self._submit_arka(site)
        self._held(site)
        self._lift_by_link(site)
        self.assertEqual(self._a(site).status, DESIGN_ARKA_SUBMITTED)

    def test_02_first_from_arka_submitted_then_from_arka_rejected(self):
        site = self._arka_submitted('HRS-2H-B')
        self._held(site)
        self._lift_by_link(site)
        self.assertEqual(self._a(site).status, DESIGN_ARKA_SUBMITTED)

        self._step(self.qc, 'design_arka_reject', site, DESIGN_ARKA_REJECTED,
                   {'rejection_reason': 'string sizing is wrong',
                    'error_category': ERR_LAYOUT})
        self._held(site)
        self._lift_by_link(site)
        # `arka_rejected` is not restorable: derived, as before.
        self.assertEqual(self._a(site).status, DESIGN_IN_DESIGN)


# ===========================================================================
# §D13's three doors — refused exactly as before
# ===========================================================================

class D13DoorsUnchangedTests(HoldRestoreBase):
    """The hold is refused at each door; nothing is written; a Head's link on the site moves
    no status. These assertions hold on the commit before §D18 as well."""

    def test_01_each_door_refuses_the_hold_and_writes_nothing(self):
        for i, status in enumerate(D13_DOORS):
            with self.subTest(status=status):
                site = self._at_review_status(f'HRS-D13-{i}', status)
                ledger_before = len(self._ledger(site))

                response = self._hold(site)

                self.assertEqual(response.status_code, 302)
                self.assertIn('so it cannot go on Design Hold', self._messages(response))
                assignment = self._a(site)
                self.assertEqual(assignment.status, status)
                self.assertIsNone(assignment.survey_returned_at)
                self.assertEqual(len(self._ledger(site)), ledger_before)
                self.assertFalse(ActivityLog.objects.filter(
                    project=site, action_code='design_blocked').exists())

                self._lift_by_link(site)
                self.assertEqual(self._a(site).status, status)
                self.assertEqual(len(self._ledger(site)), ledger_before)


# ===========================================================================
# (b) — a ledgered hold from one of §D13's doors, placed before §D13
# ===========================================================================

class PreD13HoldTests(HoldRestoreBase):
    """The result status is NOT asserted. Derived, such a site returns to `in_design` on the
    same attempt, which is the §D13 defect; a test does not pin a defect. It asserts only that
    the lift goes through and that the warning names the site for repair."""

    def test_01_each_door_lifts_and_logs_for_repair(self):
        for i, status in enumerate(D13_DOORS):
            with self.subTest(status=status):
                site = self._at_review_status(f'HRS-B-{i}', status)
                # design_mark_blocked has refused these statuses since 969eed4, so the real
                # endpoint cannot place this hold any more. apply_design_status() is the call
                # that endpoint makes, and it writes the same StatusTransition row a pre-§D13
                # hold left.
                apply_design_status(
                    self._a(site), DESIGN_SURVEY_RETURNED, self.designer,
                    'Site placed on Design Hold — survey inadequate: pre-§D13', 'design_blocked',
                    extra_fields={'survey_returned_at': timezone.now(),
                                  'survey_returned_by': self.designer,
                                  'survey_return_reason': 'pre-§D13'})

                with self.assertLogs(LOGGER, 'WARNING') as logs:
                    response = self._lift_by_link(site)

                self.assertEqual(response.status_code, 302)
                self.assertNotEqual(self._a(site).status, DESIGN_SURVEY_RETURNED)
                self.assertEqual(
                    ActivityLog.objects.filter(project=site).latest('id').action_code,
                    'design_survey_unblocked')
                self.assertTrue(any(f'HRS-B-{i}' in line and status in line
                                    and 'pre-§D13 hold; needs repair' in line
                                    for line in logs.output), logs.output)


# ===========================================================================
# A verdict during the hold — refused, and still recordable after the lift
# ===========================================================================

class VerdictDuringHoldTests(HoldRestoreBase):

    def test_01_design_qc_is_refused_during_the_hold(self):
        site = self._arka_submitted('HRS-V-QC')
        self._held(site)
        self._post(self.qc, 'design_arka_approve', site)
        self.assertEqual(self._a(site).status, DESIGN_SURVEY_RETURNED)
        self.assertEqual(self._arka(site).verdict, ARKA_PENDING)

        self._lift_by_link(site)
        self._step(self.qc, 'design_arka_approve', site, DESIGN_AWAITING_HEAD_ARKA)

    def test_02_the_head_is_refused_during_the_hold(self):
        site = self._awaiting_head_arka('HRS-V-HEAD')
        self._held(site)
        self._post(self.head, 'design_arka_head_approve', site)
        self.assertEqual(self._a(site).status, DESIGN_SURVEY_RETURNED)
        self.assertEqual(self._arka(site).head_verdict, ARKA_PENDING)

        self._lift_by_link(site)
        self._step(self.head, 'design_arka_head_approve', site, DESIGN_ARKA_SUBMITTED)


# ===========================================================================
# Only Design Head authority lifts a hold
# ===========================================================================

class LiftAuthorityTests(HoldRestoreBase):

    def test_01_the_designer_and_design_qc_are_refused_by_both_lifts(self):
        site = self._arka_submitted('HRS-AUTH')
        self._held(site)
        ledger_before = len(self._ledger(site))

        for profile in (self.designer, self.qc):
            for lift in (self._lift_by_link, self._lift_by_upload):
                with self.subTest(user=profile.user.username, lift=lift.__name__):
                    self.assertEqual(lift(site, profile).status_code, 403)
                    self.assertEqual(self._a(site).status, DESIGN_SURVEY_RETURNED)
                    self.assertEqual(len(self._ledger(site)), ledger_before)


# ===========================================================================
# KNOWN GAP — a hold is accepted over a pending change request
# ===========================================================================

class HoldOverPendingChangeRequestTests(HoldRestoreBase):
    """EXECUTION_MODULE_DEFERRED.md: "A Design Hold is accepted over a pending change request".

    design_mark_blocked reads no change request. The state is unreachable today: a pre-release
    request needs `qc_started_at` on the current attempt, and the only writer of that field is
    design_qc_start(), which moves the site to `in_qc`. So qc_started_at is set directly below;
    no product path produces it at `in_design` or `arka_submitted`.
    """

    def _pending_request_at_arka_submitted(self, code):
        site = self._arka_submitted(code)
        attempt = self._arka(site).attempt
        attempt.qc_started_at = timezone.now()   # see the class docstring
        attempt.save(update_fields=['qc_started_at'])
        self._post(self.pm, 'design_change_request', site, {'reason': 'the brief moved'})
        return site

    def test_01_the_pending_request_state_is_built(self):
        """Pins the fixture, so the expected failure below cannot pass for a broken setUp."""
        site = self._pending_request_at_arka_submitted('HRS-CR-FIX')
        self.assertTrue(DesignChangeRequest.objects.filter(
            attempt__assignment__project=site, verdict=CHANGE_REQUEST_PENDING).exists())
        self.assertEqual(self._a(site).status, DESIGN_ARKA_SUBMITTED)

    # REMOVING THIS DECORATOR IS PART OF THE FIX (the DEFERRED entry named in the docstring).
    @unittest.expectedFailure
    def test_02_the_hold_is_refused_while_a_change_request_is_pending(self):
        site = self._pending_request_at_arka_submitted('HRS-CR-GAP')
        self._hold(site)
        self.assertEqual(self._a(site).status, DESIGN_ARKA_SUBMITTED)
