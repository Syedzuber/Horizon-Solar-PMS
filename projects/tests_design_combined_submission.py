"""
Combined Arka+CAD submission, and BOQ decoupled from Arka approval.

WHAT CHANGED, AND WHY IT NEEDS ITS OWN FILE
-------------------------------------------
Part 3 held one rule: no CAD file and no BOQ completion until the CURRENT Arka is
approved at both gates. This session split that rule in half and dropped both halves,
for two DIFFERENT reasons — which is the thing these tests exist to keep straight:

  CAD  keeps its pairing and loses its gate. `DesignFile.derived_from_arka` is still
       NOT NULL, so an Arka must EXIST; it no longer has to be approved. A designer who
       finished the drawing may upload it in the same sitting as the Arka.

  BOQ  loses the gate outright, and has no pairing to keep. There is no FK from BOQ or
       BOQItem to ArkaSubmission — the bill is assembled from the shared BOQItemMaster
       catalogue and what this module records is a per-attempt completion STAMP. It was
       being gated on the staleness of a layout it does not derive from.

THE GAP THIS OPENS IS TESTED, NOT HIDDEN. Within one attempt an Arka rejection is
answered by a NEW ARKA VERSION, not a new attempt, so the QC rework loop's
`REDO_ARKA => REDO_CAD` rule never sees it. A CAD uploaded before approval therefore
survives the rejection of the very Arka it names, still `is_current`, still pointing at
a superseded version. Tests 6 and 7 assert that this is what happens — they PIN the gap
rather than claiming it is closed. Closing it is a separate decision.

Numbered VERIFICATION comments map to the session brief's verification list.
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from .models import (
    Program, Project, UserProfile, BOQ, BOQItem, BOQItemMaster,
    DesignAssignment, DesignAttempt, ArkaSubmission, DesignFile,
    DESIGN_IN_DESIGN, DESIGN_ARKA_SUBMITTED, DESIGN_ARKA_REJECTED,
    DESIGN_ARTIFACTS_UPLOADED, DESIGN_AWAITING_HEAD_ARKA,
    ARKA_PENDING, ARKA_APPROVED, ARKA_REJECTED,
    ATTEMPT_REASON_INITIAL, ERR_LAYOUT,
    DESIGN_FILE_CAD_ZIP,
)
from .design_views import _current_attempt, _current_arka, _approved_arka


def _profile(username, role, is_design_head=False, is_design_qc=False):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x',
                                    email=f'{username}@example.com')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.is_design_head = is_design_head
    profile.is_design_qc = is_design_qc
    profile.save()
    return profile


class CombinedSubmissionBase(TestCase):
    """One OPEX site with a designer, a Design QC reviewer and a Design Head.

    The site starts at `in_design` with NO attempt and NO Arka — the state the old rule
    made a dead end for every artifact, and the state most of these tests act from.
    """

    def setUp(self):
        self.designer = _profile('cs_des',  'Design')
        self.qc       = _profile('cs_qc',   'Design', is_design_qc=True)
        self.head     = _profile('cs_head', 'Design', is_design_head=True)
        self.pm       = _profile('cs_pm',   'PM')

        # A tiny OPEX catalogue with NOTHING flagged mandatory, so
        # get_opex_mandatory_items() is empty and the completion guard turns on the one
        # thing these tests are about rather than on a fixture's flags.
        self.master = BOQItemMaster.objects.create(
            code='OPX-CS-1', description='Module 550Wp', unit='Nos',
            category='Solar Modules', project_type='OPEX',
            is_active=True, is_mandatory=False, sort_order=1)

        self.program = Program.objects.create(
            name='Test-CombinedSub', program_type='OPEX', client_name='CSClient',
            status='Active', short_tender_code='CS')

        self.site = Project(
            project_id='CS-A', customer_name='CSClient', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code='CS-A',
            dc_capacity_kw=Decimal('100.00'), status='Draft',
            assigned_design=self.designer, assigned_pm=self.pm)
        self.site.save()
        self.assignment = DesignAssignment.objects.create(
            project=self.site, status=DESIGN_IN_DESIGN, assigned_to=self.designer,
            survey_file_bucket='b', survey_file_path='CS-A/survey/x.pdf')

    # ── helpers ─────────────────────────────────────────────────────────────

    def _login(self, profile):
        self.assertTrue(self.client.login(username=profile.user.username, password='x'))

    def _attempt(self):
        """The current attempt, re-read. `current_attempt_number` is the pointer
        _current_attempt() follows and the views move it, so a stale in-memory
        assignment reports None for an attempt that exists."""
        self.assignment.refresh_from_db()
        return _current_attempt(self.assignment)

    def _messages(self, response):
        return ' '.join(str(m) for m in get_messages(response.wsgi_request))

    def _url(self, name, **extra):
        return reverse(name, kwargs={'project_id': self.site.project_id, **extra})

    def _submit_arka(self, capacity='120.00'):
        """Post a real Arka submission through the view, as the designer would."""
        return self.client.post(self._url('design_arka_submit'), {
            'capacity_kw': capacity,
            'arka_link': 'https://example.com/arka',
            'remarks': '',
        })

    def _upload_cad(self):
        """Post a CAD zip through the view with STORAGE STUBBED.

        `validate_cad_zip` and `upload_design_file` are patched at their point of use in
        design_views — the archive rules and the bucket are Part 8's tests, and this file
        is about the verdict gate that used to sit in front of both.
        """
        upload = SimpleUploadedFile('cad.zip', b'PK\x03\x04stub',
                                    content_type='application/zip')
        with patch('projects.design_views.validate_cad_zip', return_value=[]), \
             patch('projects.design_views.upload_design_file',
                   return_value=('Horizon-PMS-Design', 'CS-A/cad_zip/cad.zip')):
            return self.client.post(self._url('design_artifact_upload'), {
                'kind': DESIGN_FILE_CAD_ZIP,
                'artifact_file': upload,
                'remarks': '',
            })

    def _seed_boq(self, quantity='40'):
        boq = BOQ.objects.create(project=self.site, status='Draft')
        BOQItem.objects.create(
            boq=boq, serial_no=1, category='Solar Modules',
            description='Module 550Wp', item_master=self.master, uom='Nos',
            boq_quantity=Decimal(quantity))
        return boq

    def _complete_boq(self):
        return self.client.post(self._url('design_boq_complete'), {'boq_remarks': ''})

    def _qc_approve(self):
        self._login(self.qc)
        return self.client.post(self._url('design_arka_approve'))

    def _qc_reject(self, reason='Row spacing is wrong.'):
        self._login(self.qc)
        return self.client.post(self._url('design_arka_reject'), {
            'rejection_reason': reason, 'error_category': ERR_LAYOUT})

    def _head_approve(self):
        self._login(self.head)
        return self.client.post(self._url('design_arka_head_approve'))


# ===========================================================================
# VERIFICATION 2 — Arka and CAD submitted together
# ===========================================================================

class CombinedArkaAndCadTests(CombinedSubmissionBase):

    def test_02_arka_and_cad_land_in_one_sitting(self):
        """VERIFICATION 2 — both land, and the CAD names the Arka it arrived with."""
        self._login(self.designer)
        self._submit_arka()
        response = self._upload_cad()

        self.assignment.refresh_from_db()
        attempt = _current_attempt(self.assignment)
        arka    = _current_arka(attempt)

        self.assertIsNotNone(arka, 'the Arka landed')
        self.assertEqual(arka.version, 1)
        self.assertEqual(arka.verdict, ARKA_PENDING,
                         'nobody has reviewed it — that is the point')
        self.assertEqual(arka.head_verdict, ARKA_PENDING)

        cad = attempt.design_files.get(kind=DESIGN_FILE_CAD_ZIP, is_current=True)
        self.assertEqual(cad.version, 1, 'the CAD landed too')
        self.assertEqual(cad.derived_from_arka_id, arka.pk,
                         'the pairing survives the gate removal')
        self.assertIn('uploaded', self._messages(response).lower())

    def test_02b_the_upload_is_still_refused_with_no_arka_at_all(self):
        """The EXISTENCE requirement is not dropped with the approval requirement.

        derived_from_arka is NOT NULL: with no Arka there is nothing to pair to, so this
        is the one refusal design_artifact_upload still owes the designer.
        """
        self._login(self.designer)
        response = self._upload_cad()

        self.assertIsNone(_current_attempt(self.assignment))
        self.assertEqual(DesignFile.objects.count(), 0)
        self.assertEqual(response.status_code, 302)
        self.assertIn('no design attempt', self._messages(response).lower())

    def test_02c_a_rejected_arka_still_accepts_a_cad_upload(self):
        """The widest case: not merely "unapproved" but actively REJECTED.

        Asserted because it is the one that looks like a bug from the outside, and
        because the designer sitting at `arka_rejected` with a corrected drawing in hand
        is exactly who the change is for.
        """
        self._login(self.designer)
        self._submit_arka()
        self._qc_reject()

        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, DESIGN_ARKA_REJECTED)

        self._login(self.designer)
        self._upload_cad()
        attempt = _current_attempt(self.assignment)
        self.assertEqual(attempt.design_files.filter(is_current=True).count(), 1)
        self.assertEqual(_current_arka(attempt).verdict, ARKA_REJECTED,
                         'paired to a rejected version, which is now permitted')

    def test_02d_a_cad_uploaded_before_approval_completes_the_package_after_it(self):
        """Order-insensitivity, end to end: CAD first, approvals second.

        _maybe_advance_to_artifacts_uploaded() is evaluated after every Part 3 write, and
        the Head's approval is a Part 3 write. The package must complete on THAT event
        rather than needing the designer to touch the CAD again.
        """
        self._login(self.designer)
        self._submit_arka()
        self._upload_cad()
        self._seed_boq()
        self._login(self.designer)
        self._complete_boq()

        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, DESIGN_ARKA_SUBMITTED,
                         'artifacts are in, but the Arka is not approved yet')

        self._qc_approve()
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, DESIGN_AWAITING_HEAD_ARKA)

        self._head_approve()
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, DESIGN_ARTIFACTS_UPLOADED,
                         'the Head\'s approval is what completed it, with no re-upload')


# ===========================================================================
# VERIFICATION 3 — BOQ completed with no Arka at all
# ===========================================================================

class BoqIndependentOfArkaTests(CombinedSubmissionBase):

    def test_03_boq_completes_on_a_site_with_no_arka_submitted(self):
        """VERIFICATION 3 — allowed now, refused before.

        This is the case the old gate refused with "no design attempt has been opened
        yet — submit an Arka first". The stamp lives on DesignAttempt, so the view opens
        attempt 1 to hold it.
        """
        self._seed_boq()
        self._login(self.designer)

        self.assertIsNone(_current_attempt(self.assignment),
                          'no attempt exists before this POST')

        response = self._complete_boq()

        self.assignment.refresh_from_db()
        attempt = _current_attempt(self.assignment)
        self.assertIsNotNone(attempt, 'attempt 1 was opened to carry the stamp')
        self.assertEqual(attempt.attempt_number, 1)
        self.assertEqual(attempt.opened_reason, ATTEMPT_REASON_INITIAL)
        self.assertIsNotNone(attempt.boq_submitted_at)
        self.assertEqual(attempt.boq_submitted_by_id, self.designer.pk)
        self.assertEqual(attempt.arka_submissions.count(), 0,
                         'and no Arka was invented to satisfy a gate that is gone')
        self.assertIn('boq marked complete', self._messages(response).lower())

    def test_03b_a_refused_completion_leaves_no_stray_attempt_behind(self):
        """The lazy open runs LAST. An empty BOQ is still refused, and refusing it must
        not leave a half-started design attempt on a site nobody has started designing."""
        self._seed_boq(quantity='0')
        self._login(self.designer)
        response = self._complete_boq()

        self.assignment.refresh_from_db()
        self.assertIsNone(_current_attempt(self.assignment))
        self.assertEqual(DesignAttempt.objects.count(), 0)
        self.assertIn('at least one boq item', self._messages(response).lower())

    def test_03c_an_arka_can_still_be_submitted_after_the_boq_opened_the_attempt(self):
        """The attempt this view opens must be a NORMAL attempt 1, not a special one.

        design_arka_submit() reuses whatever _current_attempt() returns, so an Arka
        submitted afterwards has to land on it as version 1 and leave the BOQ stamp alone.
        """
        self._seed_boq()
        self._login(self.designer)
        self._complete_boq()
        self._submit_arka()

        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.attempts.count(), 1, 'one attempt, not two')
        attempt = _current_attempt(self.assignment)
        arka = _current_arka(attempt)
        self.assertEqual(arka.version, 1)
        self.assertIsNotNone(attempt.boq_submitted_at, 'the stamp survived')
        self.assertEqual(self.assignment.status, DESIGN_ARKA_SUBMITTED)

    def test_03d_boq_alone_does_not_carry_the_site_past_the_gate(self):
        """The gate that remains. BOQ may outrun the Arka; it may not replace it.

        _maybe_advance_to_artifacts_uploaded() still demands an approved Arka AND a CAD,
        so a completed BOQ on its own leaves the site exactly where it was.
        """
        self._seed_boq()
        self._login(self.designer)
        self._complete_boq()

        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, DESIGN_IN_DESIGN,
                         'still in design — the BOQ advanced nothing')
        self.assertIsNone(_approved_arka(_current_attempt(self.assignment)))

    def test_03e_double_completion_is_still_refused(self):
        """The "already stamped" guard had to be rewritten to tolerate attempt=None.
        Assert it still refuses the case it was written for."""
        self._seed_boq()
        self._login(self.designer)
        self._complete_boq()
        first_stamp = self._attempt().boq_submitted_at

        response = self._complete_boq()
        self.assertIn('already marked complete', self._messages(response).lower())
        self.assertEqual(self._attempt().boq_submitted_at, first_stamp)

    def test_03f_only_the_allocated_designer_may_complete_a_boq(self):
        """Removing the Arka gate must not have removed the authority gate with it."""
        other = _profile('cs_other', 'Design')
        self._seed_boq()
        self._login(other)
        response = self._complete_boq()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(DesignAttempt.objects.count(), 0,
                         'a forbidden POST opens no attempt')


# ===========================================================================
# VERIFICATIONS 4 & 5 — rejection after a combined submission
# ===========================================================================

class RejectionAfterCombinedSubmissionTests(CombinedSubmissionBase):

    def test_04_rejecting_the_arka_asks_only_for_a_new_arka(self):
        """VERIFICATION 4 — CAD was fine, so only the Arka comes back.

        No new attempt opens (an in-attempt rejection is answered by a new Arka VERSION),
        the BOQ stamp is untouched, and the designer is put back at `arka_rejected` with
        one thing to do.
        """
        self._login(self.designer)
        self._submit_arka()
        self._upload_cad()
        self._seed_boq()
        self._login(self.designer)
        self._complete_boq()

        self._qc_reject()
        self.assignment.refresh_from_db()

        self.assertEqual(self.assignment.status, DESIGN_ARKA_REJECTED,
                         'the designer owes a new Arka')
        self.assertEqual(self.assignment.attempts.count(), 1,
                         'an Arka rejection is not a rework loop — no new attempt')
        attempt = _current_attempt(self.assignment)
        self.assertIsNotNone(attempt.boq_submitted_at,
                             'the BOQ was not in question and is not asked for again')
        self.assertEqual(attempt.design_files.filter(is_current=True).count(), 1,
                         'nor is the CAD re-requested')

    def test_05_the_cad_survives_the_rejection_of_the_arka_it_names(self):
        """VERIFICATION 5 / PRE-FLIGHT 3 — THE GAP, PINNED.

        This asserts a shortcoming, not a feature. Because the CAD could be uploaded
        before approval, it can now outlive the Arka version it was drawn against: after
        a rejection and a replacement Arka v2, the CAD is STILL is_current and STILL
        points at the superseded v1. `REDO_ARKA => REDO_CAD` cannot help — that rule
        governs the QC rework loop, and no new attempt was opened here for it to act on.

        Nothing in this session closes that. If a later session does, this test is the
        one that should fail first.
        """
        self._login(self.designer)
        self._submit_arka()
        self._upload_cad()

        attempt = self._attempt()
        v1 = _current_arka(attempt)
        cad = attempt.design_files.get(is_current=True)
        self.assertEqual(cad.derived_from_arka_id, v1.pk)

        self._qc_reject()
        self._login(self.designer)
        self._submit_arka(capacity='140.00')          # v2, a different layout

        attempt.refresh_from_db()
        v2 = _current_arka(attempt)
        v1.refresh_from_db()
        cad.refresh_from_db()

        self.assertEqual(v2.version, 2)
        self.assertFalse(v1.is_current, 'v1 has been superseded')
        self.assertEqual(v1.verdict, ARKA_REJECTED)

        self.assertTrue(cad.is_current,
                        'THE GAP: the CAD is still the live drawing for this attempt')
        self.assertEqual(cad.derived_from_arka_id, v1.pk,
                         'THE GAP: and it still names the rejected, superseded version')

    def test_05b_and_the_stale_cad_completes_the_package_once_v2_is_approved(self):
        """The consequence of test 05, stated as the thing an operator would see.

        Approving v2 advances the site to `artifacts_uploaded` on the strength of a CAD
        drawn against v1. Design QC receives a package whose drawing does not match its
        layout, with nothing on the row saying so except derived_from_arka itself.
        """
        self._login(self.designer)
        self._submit_arka()
        self._upload_cad()
        self._seed_boq()
        self._login(self.designer)
        self._complete_boq()

        self._qc_reject()
        self._login(self.designer)
        self._submit_arka(capacity='140.00')
        self._qc_approve()
        self._head_approve()

        self.assignment.refresh_from_db()
        attempt = _current_attempt(self.assignment)
        cad = attempt.design_files.get(is_current=True)
        v2  = _current_arka(attempt)

        self.assertEqual(self.assignment.status, DESIGN_ARTIFACTS_UPLOADED)
        self.assertEqual(v2.head_verdict, ARKA_APPROVED)
        self.assertNotEqual(cad.derived_from_arka_id, v2.pk,
                            'THE GAP: the package is complete on a mismatched drawing')

    def test_05c_the_workspace_names_the_version_the_cad_actually_derives_from(self):
        """The one thing that DOES surface the gap, so it is asserted rather than assumed.

        The artifacts panel renders each file's derived_from_arka. It is the only screen
        that shows a reader the mismatch, which is why it must keep doing so.
        """
        self._login(self.designer)
        self._submit_arka()
        self._upload_cad()
        self._qc_reject()
        self._login(self.designer)
        self._submit_arka(capacity='140.00')

        response = self.client.get(self._url('design_site_workspace'))
        self.assertEqual(response.status_code, 200)
        attempt = self._attempt()
        cad = attempt.design_files.get(is_current=True)
        self.assertEqual(cad.derived_from_arka.version, 1)
        self.assertContains(response, 'Arka v1')


# ===========================================================================
# TASK 3 — the REDO_ARKA => REDO_CAD rule
# ===========================================================================

class RedoScopeCouplingTests(CombinedSubmissionBase):
    """The rule is unchanged by this session and is re-asserted at the unit level.

    IT IS ALSO NOT NEW. The brief recorded it as "never reachable until this session";
    it has been reachable through the QC-failure form since Part 9.1, and
    tests_design_part9.test_redoing_the_arka_forces_redoing_the_cad already exercises it
    end to end. What this session actually changes is that the rule now has a BLIND SPOT
    beside it — the in-attempt Arka rejection of test 05 — which the rule cannot see
    because no attempt opens there.
    """

    def test_redo_arka_without_redo_cad_is_still_refused(self):
        from .design_views import _posted_redo_scope

        class _Req:
            POST = {'redo_scope_submitted': '1'}

        request = _Req()
        request.POST = type('P', (), {
            '__contains__': lambda self, k: k == 'redo_scope_submitted',
            'get': lambda self, k, d=None: d,
            'getlist': lambda self, k: ['arka', 'boq'],
        })()

        redo, error = _posted_redo_scope(request, None)
        self.assertEqual(redo, set())
        self.assertIn('new Arka means new CAD', error)

    def test_redo_arka_with_redo_cad_is_accepted(self):
        from .design_views import _posted_redo_scope

        request = type('R', (), {})()
        request.POST = type('P', (), {
            '__contains__': lambda self, k: k == 'redo_scope_submitted',
            'get': lambda self, k, d=None: d,
            'getlist': lambda self, k: ['arka', 'cad'],
        })()

        redo, error = _posted_redo_scope(request, None)
        self.assertEqual(redo, {'arka', 'cad'})
        self.assertEqual(error, '')

    def test_the_rule_does_not_fire_on_an_in_attempt_arka_rejection(self):
        """Why the rule cannot protect the combined-submission case: it is never consulted.

        An Arka rejection opens no attempt, so `_posted_redo_scope` and
        `_carry_forward_artifacts` are both bypassed entirely and the CAD is never
        considered. This is the mechanism behind test 05.
        """
        self._login(self.designer)
        self._submit_arka()
        self._upload_cad()
        before = self.assignment.attempts.count()

        self._qc_reject()

        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.attempts.count(), before,
                         'no attempt opened, so no redo scope was ever asked for')
        self.assertEqual(_current_attempt(self.assignment).qc_verdict, 'pending',
                         'and no QC verdict was recorded against the attempt')


# ===========================================================================
# VERIFICATION 6 — the Residential characterisation net
# ===========================================================================

class ResidentialUntouchedTests(CombinedSubmissionBase):

    def test_06_nothing_here_reaches_a_residential_project(self):
        """A DesignAssignment only ever exists on an OPEX site — design_views._opex_site
        404s everything else — so both changed endpoints are structurally unreachable for
        Residential. Asserted from the URL rather than argued from the helper."""
        residential = Project(
            project_id='CS-RES', customer_name='House', customer_phone='9876543211',
            site_address='2 Sun Rd', city='Delhi', project_type='Residential',
            dc_capacity_kw=Decimal('10.00'), status='Draft',
            assigned_design=self.designer, assigned_pm=self.pm)
        residential.save()

        self._login(self.designer)
        for name in ('design_boq_complete', 'design_artifact_upload'):
            response = self.client.post(
                reverse(name, kwargs={'project_id': 'CS-RES'}), {})
            self.assertEqual(response.status_code, 404, name)

        self.assertFalse(BOQ.objects.filter(project=residential).exists(),
                         'and no BOQ was brought into existence on the way past')
