"""
W-7 — a fail sends back only what failed; a CAD can stay current with a newer Arka.

WHY THIS FILE EXISTS
--------------------
Until W-7 the fail form ticked and locked CAD whenever Arka was ticked, and the view
refused {Arka} and {Arka, BOQ}: "a new Arka means new CAD". Praveen ruled (F1, 22 Nov
2026; ruling 8, 21 Nov) that a fail sends back ONLY the items failed. So:

  * {Arka} and {Arka, BOQ} are accepted at the QC fail, the Head fail and the Head's
    send-back (one shared rule, design_views._posted_redo_scope);
  * the CAD carries into the new attempt whenever CAD is not ticked, still pointing at the
    Arka it was drawn from — on the EARLIER attempt — and nothing re-points it;
  * BOQ Excel / PDF files follow the BOQ box, not the CAD box (Zuber, 22 Nov);
  * the carried CAD counts for package completeness and release, and its PDFs reach the
    SE drawings card through `carried_forward_from` (DR-1's cad_pdf_source);
  * the artifacts panel names the earlier attempt instead of warning.

Every step below goes through the real views by POST, except the fixtures that put a
package in front of a gate (status and verdict columns written directly, as
tests_design_part9 does).
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    Program, Project, DesignAssignment, DesignAttempt, ArkaSubmission, DesignFile,
    DesignFilePdf, BOQ, BOQItem,
    DESIGN_IN_DESIGN, DESIGN_ARKA_SUBMITTED, DESIGN_ARTIFACTS_UPLOADED, DESIGN_IN_QC,
    DESIGN_AWAITING_HEAD_QC, DESIGN_AWAITING_PM_APPROVAL, DESIGN_RELEASED,
    DESIGN_FILE_CAD_ZIP, DESIGN_FILE_BOQ_EXCEL, DESIGN_FILE_BOQ_PDF,
    ARKA_PENDING, ARKA_APPROVED, QC_PASSED, QC_FAILED,
    ATTEMPT_REASON_INITIAL, CAD_PDF_EXTRACTED,
    ERR_LAYOUT, ERR_DRAWING_INCOMPLETE, ERR_BOQ_SPECIFICATION,
)
from .site_drawings_views import site_drawings_context


def _profile(username, role, is_design_head=False, is_design_qc=False):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.is_design_head = is_design_head
    profile.is_design_qc = is_design_qc
    profile.save()
    return profile


class RedoScopeBase(TestCase):
    """A complete attempt-1 package: Arka v1 approved by QC, a CAD zip with one extracted
    PDF, a BOQ Excel and a BOQ PDF file, and the BOQ completion stamp. The QC reviewer and
    the Head are different people, so neither the gate-actor rule nor W-1's own-Arka rule
    refuses anything here."""

    def setUp(self):
        self.qc       = _profile('qc7',   'Design', is_design_qc=True)
        self.head     = _profile('head7', 'Design', is_design_head=True)
        self.designer = _profile('des7',  'Design')
        self.pm       = _profile('pm7',   'PM')

        program = Program.objects.create(
            name='Test-W7', program_type='OPEX', client_name='W7Client',
            status='Active', short_tender_code='W7')
        self.site = Project(
            project_id='W7-REDO', customer_name='W7Client', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX', program=program,
            site_code='W7-REDO', dc_capacity_kw=Decimal('100.00'), status='Draft',
            assigned_pm=self.pm)
        self.site.save()
        self.a = DesignAssignment.objects.create(
            project=self.site, status=DESIGN_IN_QC, assigned_to=self.designer,
            survey_file_bucket='b', survey_file_path='W7-REDO/survey/x.pdf',
            current_attempt_number=1)

        now = timezone.now()
        self.attempt = DesignAttempt.objects.create(
            assignment=self.a, attempt_number=1, opened_reason=ATTEMPT_REASON_INITIAL,
            boq_submitted_at=now, boq_submitted_by=self.designer, qc_started_at=now)
        self.arka = ArkaSubmission.objects.create(
            attempt=self.attempt, version=1, capacity_kw=Decimal('120.00'),
            arka_link='https://example.com/arka1', submitted_by=self.designer,
            verdict=ARKA_APPROVED, reviewed_by=self.qc, reviewed_at=now, is_current=True)
        self.cad = DesignFile.objects.create(
            attempt=self.attempt, kind=DESIGN_FILE_CAD_ZIP, version=1,
            bucket='b', path='W7-REDO/cad_zip/v1.zip', original_filename='W7.zip',
            size_bytes=1000,
            archive_listing=[{'name': 'pkg/layout.pdf', 'size': 10},
                             {'name': 'pkg/layout.dwg', 'size': 20}],
            derived_from_arka=self.arka, uploaded_by=self.designer, is_current=True)
        # DR-1: the extracted PDF is recorded against the ORIGINAL upload; a carried copy
        # reaches it through cad_pdf_source().
        self.pdf = DesignFilePdf.objects.create(
            design_file=self.cad, entry_name='pkg/layout.pdf', display_name='layout.pdf',
            bucket='b', path='W7-REDO/cad_zip/v1/pdf/0.pdf', size_bytes=2048,
            outcome=CAD_PDF_EXTRACTED)
        self.boq_xls = DesignFile.objects.create(
            attempt=self.attempt, kind=DESIGN_FILE_BOQ_EXCEL, version=1,
            bucket='b', path='W7-REDO/boq_excel/v1.xlsx', original_filename='W7.xlsx',
            derived_from_arka=self.arka, uploaded_by=self.designer, is_current=True)
        self.boq_pdf = DesignFile.objects.create(
            attempt=self.attempt, kind=DESIGN_FILE_BOQ_PDF, version=1,
            bucket='b', path='W7-REDO/boq_pdf/v1.pdf', original_filename='W7-boq.pdf',
            derived_from_arka=self.arka, uploaded_by=self.designer, is_current=True)
        # design_boq_complete refuses an empty BOQ; the line items are project-scoped.
        boq = BOQ.objects.create(project=self.site)
        BOQItem.objects.create(boq=boq, serial_no=1, description='Module',
                               uom='Nos', boq_quantity=10)

    # ── putting the package in front of a gate ──────────────────────────────

    def _at_head(self):
        """Design QC passed attempt 1; the package waits for the Head."""
        now = timezone.now()
        self.attempt.qc_verdict = QC_PASSED
        self.attempt.qc_reviewed_by = self.qc
        self.attempt.qc_reviewed_at = now
        self.attempt.head_started_at = now
        self.attempt.save()
        self.a.status = DESIGN_AWAITING_HEAD_QC
        self.a.save()

    # ── real views ──────────────────────────────────────────────────────────

    def _login(self, profile):
        self.client.logout()
        self.assertTrue(self.client.login(username=profile.user.username, password='x'))

    def _post(self, name, **data):
        return self.client.post(
            reverse(name, kwargs={'project_id': self.site.project_id}), data)

    def _head_fail(self, redo, category=ERR_LAYOUT):
        self._at_head()
        self._login(self.head)
        return self._post('design_head_qc_fail', head_remarks='layout is wrong',
                          error_category=category, redo_scope_submitted='1', redo=redo)

    def _qc_fail(self, redo, category=ERR_LAYOUT):
        self._login(self.qc)
        return self._post('design_qc_fail', qc_remarks='layout is wrong',
                          error_category=category, redo_scope_submitted='1', redo=redo)

    def _submit_and_approve_new_arka(self):
        """The designer submits a new Arka on the new attempt and Design QC approves it."""
        self._login(self.designer)
        self._post('design_arka_submit', capacity_kw='118.00',
                   arka_link='https://example.com/arka2')
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_ARKA_SUBMITTED)
        self._login(self.qc)
        self._post('design_arka_approve')
        self.a.refresh_from_db()

    def _new_attempt(self):
        self.a.refresh_from_db()
        return self.a.attempts.order_by('attempt_number').last()

    def _current_files(self, attempt, kind):
        return list(attempt.design_files.filter(kind=kind, is_current=True))


# ===========================================================================
# The Head's fail
# ===========================================================================

class HeadFailArkaOnlyTests(RedoScopeBase):

    def test_head_fail_arka_only_is_accepted_and_carries_cad_and_boq(self):
        self._head_fail(['arka'])

        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.head_verdict, QC_FAILED, 'the fail was refused')
        self.assertEqual(self.attempt.redo_required, ['arka'])

        n2 = self._new_attempt()
        self.assertEqual(n2.attempt_number, 2)
        self.assertEqual(self.a.status, DESIGN_IN_DESIGN,
                         'no Arka is carried, so the designer starts with a new one')
        self.assertEqual(n2.arka_submissions.count(), 0)

        [cad] = self._current_files(n2, DESIGN_FILE_CAD_ZIP)
        self.assertEqual(cad.carried_forward_from_id, self.cad.pk)
        self.assertEqual(cad.path, self.cad.path, 'the stored object is shared')
        self.assertEqual(cad.uploaded_by_id, self.designer.pk)
        self.assertEqual(cad.derived_from_arka_id, self.arka.pk,
                         'the CAD still names the Arka it was drawn from, on attempt 1')

        self.assertEqual(n2.boq_submitted_at, self.attempt.boq_submitted_at,
                         'the BOQ stamp is carried')
        self.assertEqual(len(self._current_files(n2, DESIGN_FILE_BOQ_EXCEL)), 1)
        self.assertEqual(len(self._current_files(n2, DESIGN_FILE_BOQ_PDF)), 1)

    def test_qc_approving_the_new_arka_completes_the_package_in_that_request(self):
        self._head_fail(['arka'])
        self._submit_and_approve_new_arka()

        self.assertEqual(self.a.status, DESIGN_ARTIFACTS_UPLOADED,
                         'carried CAD + carried BOQ + approved new Arka is a package')
        n2 = self._new_attempt()
        new_arka = n2.arka_submissions.get(is_current=True)
        self.assertEqual(new_arka.verdict, ARKA_APPROVED)
        cad = n2.design_files.get(kind=DESIGN_FILE_CAD_ZIP, is_current=True)
        self.assertNotEqual(cad.derived_from_arka_id, new_arka.pk,
                            'nothing re-points the carried CAD at the new Arka')
        self.assertEqual(cad.derived_from_arka_id, self.arka.pk)


class HeadFailArkaAndBoqTests(RedoScopeBase):

    def test_cad_carried_boq_stamp_and_boq_files_not(self):
        self._head_fail(['arka', 'boq'])

        n2 = self._new_attempt()
        self.assertEqual(self.a.status, DESIGN_IN_DESIGN)
        self.assertEqual(len(self._current_files(n2, DESIGN_FILE_CAD_ZIP)), 1,
                         'the CAD was not ticked, so it carries')
        self.assertEqual(self._current_files(n2, DESIGN_FILE_BOQ_EXCEL), [],
                         'BOQ files follow the BOQ box, which was ticked')
        self.assertEqual(self._current_files(n2, DESIGN_FILE_BOQ_PDF), [])
        self.assertIsNone(n2.boq_submitted_at)

    def test_package_completes_only_after_the_arka_approval_and_the_boq(self):
        self._head_fail(['arka', 'boq'])
        self._submit_and_approve_new_arka()
        self.assertEqual(self.a.status, DESIGN_ARKA_SUBMITTED,
                         'approved Arka + carried CAD, but the BOQ is still owed')

        self._login(self.designer)
        self._post('design_boq_complete')
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_ARTIFACTS_UPLOADED)


# ===========================================================================
# The QC fail — the same rule, because the same function decides it
# ===========================================================================

class QcFailArkaOnlyTests(RedoScopeBase):

    def test_qc_fail_arka_only_behaves_as_the_head_fail(self):
        self._qc_fail(['arka'])

        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.qc_verdict, QC_FAILED)
        self.assertEqual(self.attempt.redo_required, ['arka'])
        n2 = self._new_attempt()
        self.assertEqual(self.a.status, DESIGN_IN_DESIGN)
        cad = n2.design_files.get(kind=DESIGN_FILE_CAD_ZIP, is_current=True)
        self.assertEqual(cad.derived_from_arka_id, self.arka.pk)
        self.assertIsNotNone(n2.boq_submitted_at)

        self._submit_and_approve_new_arka()
        self.assertEqual(self.a.status, DESIGN_ARTIFACTS_UPLOADED)


# ===========================================================================
# The combinations that worked before still work
# ===========================================================================

class UnchangedCombinationTests(RedoScopeBase):

    def test_arka_and_cad_still_carries_no_cad(self):
        self._qc_fail(['arka', 'cad'])
        n2 = self._new_attempt()
        self.assertEqual(self.a.status, DESIGN_IN_DESIGN)
        self.assertEqual(n2.arka_submissions.count(), 0)
        self.assertEqual(self._current_files(n2, DESIGN_FILE_CAD_ZIP), [])
        self.assertIsNotNone(n2.boq_submitted_at, 'the BOQ was not ticked')
        # Zuber, 22 Nov: BOQ files follow the BOQ box. Before W-7 they rode on the CAD
        # box and would NOT have carried here.
        self.assertEqual(len(self._current_files(n2, DESIGN_FILE_BOQ_EXCEL)), 1)

    def test_cad_only_carries_the_arka_and_the_boq_files_but_not_the_cad(self):
        self._qc_fail(['cad'], category=ERR_DRAWING_INCOMPLETE)
        n2 = self._new_attempt()
        carried_arka = n2.arka_submissions.get(is_current=True)
        self.assertEqual(carried_arka.carried_forward_from_id, self.arka.pk)
        self.assertEqual(self.a.status, DESIGN_ARKA_SUBMITTED,
                         'an approved Arka is carried, so the designer goes to the CAD')
        self.assertEqual(self._current_files(n2, DESIGN_FILE_CAD_ZIP), [])
        [xls] = self._current_files(n2, DESIGN_FILE_BOQ_EXCEL)
        [pdf] = self._current_files(n2, DESIGN_FILE_BOQ_PDF)
        self.assertEqual(xls.carried_forward_from_id, self.boq_xls.pk)
        self.assertEqual(pdf.carried_forward_from_id, self.boq_pdf.pk)
        # Drawn from the Arka that is itself carried, so they pair to its copy.
        self.assertEqual(xls.derived_from_arka_id, carried_arka.pk)
        self.assertIsNotNone(n2.boq_submitted_at)

    def test_boq_only_re_points_the_cad_to_the_carried_arka_copy(self):
        """The common case keeps its old pairing: the CAD was drawn from the Arka being
        carried, so it pairs to that Arka's copy and the panel shows no mismatch."""
        self._qc_fail(['boq'], category=ERR_BOQ_SPECIFICATION)
        n2 = self._new_attempt()
        carried_arka = n2.arka_submissions.get(is_current=True)
        cad = n2.design_files.get(kind=DESIGN_FILE_CAD_ZIP, is_current=True)
        self.assertEqual(cad.derived_from_arka_id, carried_arka.pk)
        self.assertEqual(self._current_files(n2, DESIGN_FILE_BOQ_EXCEL), [])

    def test_empty_redo_is_still_refused(self):
        self._qc_fail([])
        self.attempt.refresh_from_db()
        self.assertNotEqual(self.attempt.qc_verdict, QC_FAILED)
        self.assertEqual(self.a.attempts.count(), 1, 'an attempt was opened anyway')

    def test_head_send_back_shares_the_rule(self):
        """The third caller of _posted_redo_scope. Asserted at the unit level: the scope
        function is the only place the old refusal lived, and all three views call it."""
        import inspect
        from . import design_views
        for view in (design_views.design_qc_fail, design_views.design_head_qc_fail,
                     design_views.design_head_send_back):
            self.assertIn('_posted_redo_scope(request, attempt)', inspect.getsource(view),
                          view.__name__)


# ===========================================================================
# Chained fails never re-point a CAD at a layout it was not drawn from
# ===========================================================================

class ChainedFailTests(RedoScopeBase):

    def test_arka_fail_then_boq_fail_keeps_the_original_pairing(self):
        self._head_fail(['arka'])
        self._submit_and_approve_new_arka()                    # attempt 2 complete
        self._login(self.qc)
        self._post('design_qc_start')
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_IN_QC)

        self._qc_fail(['boq'], category=ERR_BOQ_SPECIFICATION)
        n3 = self._new_attempt()
        self.assertEqual(n3.attempt_number, 3)
        carried_arka = n3.arka_submissions.get(is_current=True)
        cad = n3.design_files.get(kind=DESIGN_FILE_CAD_ZIP, is_current=True)
        self.assertNotEqual(cad.derived_from_arka_id, carried_arka.pk,
                            'the carried Arka is attempt 2\'s, not the CAD\'s layout')
        self.assertEqual(cad.derived_from_arka_id, self.arka.pk)


# ===========================================================================
# Release and the SE drawings card
# ===========================================================================

class ReleaseTests(RedoScopeBase):

    def test_carried_cad_is_released_and_reaches_the_se_drawings_card(self):
        self._head_fail(['arka'])
        self._submit_and_approve_new_arka()
        self.assertEqual(self.a.status, DESIGN_ARTIFACTS_UPLOADED)

        self._login(self.qc)
        self._post('design_qc_start')
        self._post('design_qc_pass')
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_AWAITING_HEAD_QC)
        self._login(self.head)
        self._post('design_head_qc_pass')
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_AWAITING_PM_APPROVAL)
        self._login(self.pm)
        self._post('design_pm_approve')
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_RELEASED)

        card = site_drawings_context(self.site)
        self.assertEqual(card['state'], 'released')
        self.assertEqual([row.pk for row in card['drawings']], [self.pdf.pk],
                         'the carried copy resolves to the original upload\'s PDFs')
        self.assertFalse(card['unavailable'])

        cad = self._new_attempt().design_files.get(kind=DESIGN_FILE_CAD_ZIP,
                                                   is_current=True)
        self.assertEqual(cad.derived_from_arka_id, self.arka.pk)


# ===========================================================================
# The screens
# ===========================================================================

class FormAndPanelTests(RedoScopeBase):

    def test_fail_form_no_longer_ticks_and_locks_cad(self):
        self._at_head()
        self._login(self.head)
        html = self.client.get(
            reverse('design_qc_review', args=[self.site.project_id])).content.decode()
        self.assertIn('data-redo-group="head_pkg_redo"', html, 'the Head form rendered')
        self.assertIn('redoDefaults', html, 'the category pre-tick stays')
        self.assertNotIn('coupleArkaToCad', html)
        self.assertNotIn('data-redo-mirror', html)
        self.assertNotIn('boxes.cad.disabled', html)
        self.assertIn('Tick only what failed. Items not ticked carry into the next attempt.',
                      html)

    def test_panel_names_the_earlier_attempt_instead_of_warning(self):
        self._head_fail(['arka'])
        self._submit_and_approve_new_arka()

        self._login(self.designer)
        html = self.client.get(reverse('design_site_workspace',
                                       args=[self.site.project_id])).content.decode()
        self.assertIn('Arka v1 · attempt 1', html)
        self.assertIn('kept from attempt 1 — the fail did not ask for a new CAD', html)
        self.assertIn('kept from attempt 1 — the fail did not ask for a new BOQ', html,
                      'the carried BOQ files say BOQ, not CAD')
        self.assertNotIn('not the current Arka', html,
                         'a cross-attempt pairing is the reviewer\'s choice, not staleness')

    def test_same_attempt_mismatch_keeps_the_yellow_warning(self):
        """A second Arka version on the SAME attempt with the CAD still on v1."""
        self.arka.is_current = False
        self.arka.save()
        ArkaSubmission.objects.create(
            attempt=self.attempt, version=2, capacity_kw=Decimal('121.00'),
            arka_link='https://example.com/arka1b', submitted_by=self.designer,
            verdict=ARKA_PENDING, is_current=True)
        self.a.status = DESIGN_ARKA_SUBMITTED
        self.a.save()

        self._login(self.designer)
        html = self.client.get(reverse('design_site_workspace',
                                       args=[self.site.project_id])).content.decode()
        self.assertIn('not the current Arka', html)
        self.assertNotIn('kept from attempt', html)
        self.assertNotIn('Arka v1 · attempt', html)
