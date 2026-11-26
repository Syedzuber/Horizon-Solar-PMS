"""
W-5b — Design QC and the Design Head may revise the CAD during package review.

WHY THIS FILE EXISTS
--------------------
The CAD half of W-5 (W-5a, tests_design_reviewer_revisions, did the Arka). Rulings pinned
here (22 Nov 2026):

  * reviewers replace `cad_zip` only (Q4); the same window and the same people as the
    Arka revision (Q5); 'head' for someone holding both (Q6); a Head revision is final;
  * the reviewer's zip goes through the designer's own helpers — validation, storage,
    versioning, PDF extraction after commit — so it reaches the SE drawings card;
  * order (Q1): window check without the lock -> store -> lock and re-check -> write ->
    extract. A re-check refusal or a transaction error removes the stored object;
  * no status change, no new attempt, no BOQCorrection, no _maybe_advance;
  * carried copies keep the fields; W-6 counts one revision, not one per attempt;
  * the designer's path is unchanged (W-4 still closes it in review).

Every step goes through the real views by POST except the fixtures that put a package in
front of a gate (RevisionBase, shared with W-5a). Storage is always mocked: the view's
upload_design_file returns a (bucket, path) and delete_design_objects is a mock, so no
test touches a bucket.
"""
import io
import zipfile
from unittest import mock

from django.contrib.messages import get_messages
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from . import design_views
from .models import (
    ActivityLog, ArkaSubmission, BOQCorrection, DesignAttempt, DesignFile, DesignFilePdf,
    Project, SiteGroup, SiteGroupMembership,
    CAD_PDF_EXTRACTED, DESIGN_ARKA_SUBMITTED, DESIGN_ARTIFACTS_UPLOADED,
    DESIGN_AWAITING_HEAD_QC, DESIGN_AWAITING_PM_APPROVAL, DESIGN_FILE_CAD_ZIP,
    DESIGN_IN_DESIGN, DESIGN_IN_QC, DESIGN_PM_REJECTED, DESIGN_RELEASED,
    DESIGN_SURVEY_RETURNED, ERR_LAYOUT, REVIEWER_ROLE_HEAD, REVIEWER_ROLE_QC,
    SITE_GROUP_LOCKED,
)
from .permissions import (
    REVIEWER_ACTION_ARKA_REVISE, REVIEWER_ACTION_CAD_REVISE, design_reviewer_turn,
    user_can_correct_boq,
)
from .site_drawings_views import site_drawings_context
from .tests_design_reviewer_revisions import RevisionBase

ACTION_CODE = 'design_cad_reviewer_revised'
EXTRACT = 'projects.design_views.extract_cad_pdfs'
STORE = 'projects.design_views.upload_design_file'
DELETE = 'projects.design_views.delete_design_objects'
BUCKET = 'Horizon-PMS-Design'
STORED_PATH = 'W5-REV/cad_zip/reviewer.zip'
PDF = b'%PDF-1.4\n' + b'drawing ' * 50


def _zip(entries=(('pkg/Layout.pdf', PDF), ('pkg/layout.dwg', b'AC1027'))):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries:
            zf.writestr(name, content)
    return buf.getvalue()


class CadRevisionBase(RevisionBase):
    """RevisionBase's package (attempt 1 in `in_qc`, Arka v1 approved, CAD v1 drawn from
    it, BOQ stamped) plus a POST helper that mocks storage and extraction."""

    def _cad(self, who, reason='Legend and cable schedule corrected', archive=None,
             extract_effect=None, delete_result=None):
        """POST a reviewer CAD. Returns (response, mocks) with mocks store / extract /
        delete, so a test can assert what was (not) called."""
        self._login(who)
        upload = io.BytesIO(archive if archive is not None else _zip())
        upload.name = 'reviewer.zip'
        with mock.patch(STORE, return_value=(BUCKET, STORED_PATH)) as store, \
             mock.patch(EXTRACT, side_effect=extract_effect) as extract, \
             mock.patch(DELETE, return_value=delete_result or [(BUCKET, STORED_PATH, True, '')]) \
                as delete:
            response = self.client.post(
                reverse('design_cad_revise', kwargs={'project_id': self.site.project_id}),
                {'cad_file': upload, 'revision_reason': reason})
        return response, mock.Mock(store=store, extract=extract, delete=delete)

    def _current_cad(self, attempt=None):
        return (attempt or self.attempt).design_files.get(kind=DESIGN_FILE_CAD_ZIP,
                                                          is_current=True)

    def _message(self, response):
        return ' '.join(str(m) for m in get_messages(response.wsgi_request))

    def _assert_no_cad_written(self, mocks=None):
        self.assertEqual(DesignFile.objects.filter(attempt=self.attempt).count(), 1)
        self.assertEqual(self._current_cad().pk, self.cad.pk)
        self.assertFalse(ActivityLog.objects.filter(action_code=ACTION_CODE).exists())
        if mocks is not None:
            mocks.extract.assert_not_called()


# ===========================================================================
# The window
# ===========================================================================

class WindowTests(CadRevisionBase):

    def test_qc_and_the_head_may_revise_in_each_window_status(self):
        # artifacts_uploaded left out: ruling 5 opens revisions at in_qc (see Ruling5Tests).
        # DW-2a (DW-R2): once QC has passed the package only the Head may revise the CAD.
        for status, who in ((DESIGN_IN_QC, self.qc), (DESIGN_IN_QC, self.head),
                            (DESIGN_AWAITING_HEAD_QC, self.head)):
            with self.subTest(status=status, who=who.user.username):
                self._set_status(status)
                before = self._current_cad()
                _response, mocks = self._cad(who)
                after = self._current_cad()
                self.assertEqual(after.version, before.version + 1)
                self.assertEqual(after.uploaded_by_id, who.pk)
                mocks.extract.assert_called_once()
                self.a.refresh_from_db()
                self.assertEqual(self.a.status, status)

    def test_every_other_status_is_refused_and_stores_nothing(self):
        for status in (DESIGN_IN_DESIGN, DESIGN_ARKA_SUBMITTED, DESIGN_AWAITING_PM_APPROVAL,
                       DESIGN_RELEASED, DESIGN_PM_REJECTED, DESIGN_SURVEY_RETURNED):
            for who in (self.qc, self.head):
                with self.subTest(status=status, who=who.user.username):
                    self._set_status(status)
                    response, mocks = self._cad(who)
                    self.assertEqual(response.status_code, 302)
                    self._assert_no_cad_written(mocks)
                    # Refused at the first check: nothing reached storage.
                    mocks.store.assert_not_called()
                    mocks.delete.assert_not_called()
                    self.a.refresh_from_db()
                    self.assertEqual(self.a.status, status)

    def test_the_designer_the_pm_and_an_outsider_are_forbidden(self):
        self.designer.is_design_qc = True   # the flag does not let a designer review their own site
        self.designer.save()
        for who in (self.designer, self.pm, self.other_designer):
            with self.subTest(who=who.user.username):
                response, mocks = self._cad(who)
                self.assertEqual(response.status_code, 403)
                self._assert_no_cad_written(mocks)
                mocks.store.assert_not_called()

    def test_an_open_change_request_refuses_even_the_head(self):
        self._pending_change_request()
        response, mocks = self._cad(self.head)
        self._assert_no_cad_written(mocks)
        mocks.store.assert_not_called()
        # Q3: the shared refusal now says "the design", not "the Arka".
        self.assertIn('The design cannot be revised until it is decided',
                      self._message(response))

    def test_a_closed_attempt_is_refused(self):
        self.attempt.closed_at = timezone.now()
        self.attempt.save()
        _response, mocks = self._cad(self.qc)
        self._assert_no_cad_written(mocks)
        mocks.store.assert_not_called()

    def test_a_locked_procurement_group_refuses(self):
        group = SiteGroup.objects.create(
            program=self.program, name='Batch W5b', status=SITE_GROUP_LOCKED,
            created_by=self.pm, locked_by=self.pm, locked_at=timezone.now())
        SiteGroupMembership.objects.create(group=group, project=self.site, added_by=self.pm)
        _response, mocks = self._cad(self.head)
        self._assert_no_cad_written(mocks)
        mocks.store.assert_not_called()

    def test_the_wording_says_the_design_outside_review(self):
        self._set_status(DESIGN_RELEASED)
        response, _mocks = self._cad(self.qc)
        self.assertIn('this design has moved past review', self._message(response))
        self._set_status(DESIGN_IN_DESIGN)
        response, _mocks = self._cad(self.qc)
        self.assertIn('the design can be revised only while the package is in review',
                      self._message(response))


class LockAndCleanupTests(CadRevisionBase):
    """Ruling Q1: the object is stored before the lock, so a refusal at the re-check or a
    failed transaction must remove it, and neither may leave a row or an activity entry."""

    def test_a_refusal_at_the_recheck_deletes_the_stored_object(self):
        # The first check passes; by the time the lock is held the status has moved.
        with mock.patch.object(design_views, '_revision_window_refusal',
                               side_effect=['', 'this design has moved past review.']):
            response, mocks = self._cad(self.qc)
        self.assertEqual(response.status_code, 302)
        mocks.store.assert_called_once()
        mocks.delete.assert_called_once_with([(BUCKET, STORED_PATH)])
        self._assert_no_cad_written(mocks)
        self.assertIn('this design has moved past review', self._message(response))

    def test_a_transaction_error_leaves_no_row_or_activity(self):
        with mock.patch.object(design_views, 'log_activity',
                               side_effect=RuntimeError('database went away')):
            with self.assertRaises(RuntimeError):
                self._cad(self.qc)
        # The row written before the failure is rolled back with the transaction.
        self.assertEqual(DesignFile.objects.filter(attempt=self.attempt).count(), 1)
        self.assertEqual(self._current_cad().pk, self.cad.pk)
        self.assertFalse(ActivityLog.objects.filter(action_code=ACTION_CODE).exists())

    def test_the_transaction_error_path_calls_delete(self):
        self._login(self.qc)
        upload = io.BytesIO(_zip())
        upload.name = 'reviewer.zip'
        with mock.patch(STORE, return_value=(BUCKET, STORED_PATH)), \
             mock.patch(EXTRACT) as extract, \
             mock.patch(DELETE, return_value=[(BUCKET, STORED_PATH, True, '')]) as delete, \
             mock.patch.object(design_views, 'log_activity', side_effect=RuntimeError('x')):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse('design_cad_revise', kwargs={'project_id': self.site.project_id}),
                    {'cad_file': upload, 'revision_reason': 'Legend'})
        delete.assert_called_once_with([(BUCKET, STORED_PATH)])
        extract.assert_not_called()

    def test_a_failed_delete_is_logged_not_raised(self):
        with mock.patch.object(design_views, '_revision_window_refusal',
                               side_effect=['', 'this design has moved past review.']), \
             self.assertLogs('projects.design_views', level='WARNING') as logs:
            response, _mocks = self._cad(
                self.qc, delete_result=[(BUCKET, STORED_PATH, False, 'ConnectError: down')])
        self.assertEqual(response.status_code, 302)
        self.assertTrue(any('orphaned' in line and STORED_PATH in line
                            for line in logs.output))
        self._assert_no_cad_written()

    def test_the_designer_path_still_leaves_no_cleanup(self):
        """The designer's behaviour is unchanged (Q1): a failed transaction there does not
        call delete_design_objects."""
        self._set_status(DESIGN_ARKA_SUBMITTED)
        self._login(self.designer)
        upload = io.BytesIO(_zip())
        upload.name = 'designer.zip'
        with mock.patch(STORE, return_value=(BUCKET, 'W5-REV/cad_zip/d.zip')), \
             mock.patch(EXTRACT), mock.patch(DELETE) as delete, \
             mock.patch.object(design_views, 'log_activity', side_effect=RuntimeError('x')):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse('design_artifact_upload',
                            kwargs={'project_id': self.site.project_id}),
                    {'kind': DESIGN_FILE_CAD_ZIP, 'artifact_file': upload})
        delete.assert_not_called()


# ===========================================================================
# The stored version
# ===========================================================================

class StoreTests(CadRevisionBase):

    def test_the_new_version_is_current_and_the_previous_superseded(self):
        self._cad(self.qc)
        new = self._current_cad()
        self.cad.refresh_from_db()
        self.assertEqual(new.version, 2)
        self.assertFalse(self.cad.is_current)
        self.assertEqual(self.cad.superseded_by_id, new.pk)
        self.assertEqual((new.bucket, new.path), (BUCKET, STORED_PATH))
        self.assertEqual(new.original_filename, 'reviewer.zip')
        self.assertEqual([e['name'] for e in new.archive_listing],
                         ['pkg/Layout.pdf', 'pkg/layout.dwg'])

    def test_the_record_names_the_reviewer_role_reason_and_designer(self):
        self._cad(self.qc, reason='Legend and cable schedule corrected')
        new = self._current_cad()
        self.assertEqual((new.uploaded_by_id, new.reviewer_role, new.revision_reason,
                          new.designer_at_revision_id, new.remarks),
                         (self.qc.pk, REVIEWER_ROLE_QC, 'Legend and cable schedule corrected',
                          self.designer.pk, ''))
        self.assertEqual(new.derived_from_arka_id, self.arka.pk)
        self.assertEqual(new.reviser_label, 'qc5 (QC)')

    def test_a_head_revision_is_recorded_as_the_head(self):
        self._at_head()
        self._cad(self.head)
        self.assertEqual(self._current_cad().reviewer_role, REVIEWER_ROLE_HEAD)

    def test_derived_from_is_the_current_arka_even_a_reviewer_revised_one(self):
        self._revise(self.head, reason='Tank clearance')
        revised_arka = self._current_arka()
        self.assertEqual(revised_arka.version, 2)
        self._cad(self.qc)
        self.assertEqual(self._current_cad().derived_from_arka_id, revised_arka.pk)

    def test_one_activity_entry_names_the_versions_and_the_reason(self):
        self._cad(self.qc, reason='Legend fixed')
        log = ActivityLog.objects.get(action_code=ACTION_CODE)
        self.assertEqual(log.entity_id, self._current_cad().pk)
        self.assertEqual(log.actor_id, self.qc.pk)
        self.assertIn('qc5 (QC) revised the CAD: v1 → v2; reason: Legend fixed', log.action)

    def test_an_invalid_zip_is_refused_before_storage(self):
        response, mocks = self._cad(self.qc, archive=b'not a zip at all')
        self.assertEqual(response.status_code, 302)
        mocks.store.assert_not_called()
        self._assert_no_cad_written(mocks)

    def test_a_zip_without_a_dwg_is_refused(self):
        _response, mocks = self._cad(self.qc, archive=_zip([('pkg/Layout.pdf', PDF)]))
        mocks.store.assert_not_called()
        self._assert_no_cad_written(mocks)

    def test_a_blank_or_overlong_reason_is_refused(self):
        for reason in ('', '   ', 'x' * (design_views.REVISION_REASON_MAX + 1)):
            with self.subTest(reason=reason[:5]):
                _response, mocks = self._cad(self.qc, reason=reason)
                mocks.store.assert_not_called()
                self._assert_no_cad_written(mocks)

    def test_a_missing_file_is_refused(self):
        self._login(self.qc)
        with mock.patch(STORE) as store:
            self.client.post(
                reverse('design_cad_revise', kwargs={'project_id': self.site.project_id}),
                {'revision_reason': 'Legend'})
        store.assert_not_called()
        self._assert_no_cad_written()

    def test_no_status_change_no_attempt_no_boq_correction_no_advance(self):
        # awaiting_head_qc replaces artifacts_uploaded, now outside the window (ruling 5).
        # DW-2a: at awaiting_head_qc it is the Head's turn, so the Head uploads there.
        for status, who in ((DESIGN_IN_QC, self.qc), (DESIGN_AWAITING_HEAD_QC, self.head)):
            with self.subTest(status=status):
                self._set_status(status)
                with mock.patch.object(design_views, '_maybe_advance_to_artifacts_uploaded') \
                        as advance:
                    _response, mocks = self._cad(who)
                mocks.store.assert_called_once()   # accepted, not refused
                advance.assert_not_called()
                self.a.refresh_from_db()
                self.assertEqual(self.a.status, status)
                self.assertEqual(self.a.current_attempt_number, 1)
        self.assertEqual(DesignAttempt.objects.filter(assignment=self.a).count(), 1)
        self.assertFalse(BOQCorrection.objects.exists())
        # The Arka is untouched by a CAD revision.
        self.assertEqual(ArkaSubmission.objects.filter(attempt=self.attempt).count(), 1)

    def test_a_get_redirects_and_writes_nothing(self):
        self._login(self.qc)
        response = self.client.get(
            reverse('design_cad_revise', kwargs={'project_id': self.site.project_id}))
        self.assertEqual(response.status_code, 302)
        self._assert_no_cad_written()


# ===========================================================================
# PDFs and the SE drawings card
# ===========================================================================

class PdfTests(CadRevisionBase):

    def test_extraction_runs_after_the_transaction_on_the_reviewers_file(self):
        depth_outside = len(connection.savepoint_ids)
        seen = {}

        def check(design_file, upload):
            # The view's atomic block has closed (no savepoint of its own is open), and
            # the row and its activity entry are already written.
            seen['depth'] = len(connection.savepoint_ids)
            seen['pk'] = design_file.pk
            seen['logged'] = ActivityLog.objects.filter(action_code=ACTION_CODE,
                                                        entity_id=design_file.pk).exists()
        self._cad(self.qc, extract_effect=check)
        self.assertEqual(seen['depth'], depth_outside)
        self.assertEqual(seen['pk'], self._current_cad().pk)
        self.assertTrue(seen['logged'])

    def test_an_extraction_failure_is_logged_and_the_revision_stands(self):
        with self.assertLogs('projects.design_views', level='WARNING') as logs:
            response, _mocks = self._cad(self.qc, extract_effect=RuntimeError('boom'))
        self.assertEqual(response.status_code, 302)
        new = self._current_cad()
        self.assertEqual(new.version, 2)
        self.assertIn('CAD v2 is now current', self._message(response))
        self.assertTrue(any(line.startswith('WARNING:projects.design_views:design_cad_revise:')
                            for line in logs.output))
        failed = ActivityLog.objects.get(action_code='cad_pdf_extraction_failed')
        self.assertEqual((failed.entity_id, failed.actor_id), (new.pk, self.qc.pk))

    def test_after_release_the_se_card_shows_the_reviewers_cad_and_pdfs(self):
        """Real extraction (storage mocked), then the real gates: QC pass, the Head's final
        approval, the PM's release. The SE card reads the reviewer's zip unchanged."""
        def fake_put(file_obj, path):
            return BUCKET, path
        self._login(self.qc)
        upload = io.BytesIO(_zip([('pkg/Revised-Layout.pdf', PDF), ('pkg/a.dwg', b'AC')]))
        upload.name = 'reviewer.zip'
        with mock.patch(STORE, return_value=(BUCKET, STORED_PATH)), \
             mock.patch('projects.design_storage.upload_design_file', side_effect=fake_put):
            self.client.post(
                reverse('design_cad_revise', kwargs={'project_id': self.site.project_id}),
                {'cad_file': upload, 'revision_reason': 'Revised layout sheet'})
        reviewer_zip = self._current_cad()
        self.assertEqual(list(reviewer_zip.extracted_pdfs.values_list('entry_name', 'outcome')),
                         [('pkg/Revised-Layout.pdf', CAD_PDF_EXTRACTED)])

        self._post('design_qc_pass')
        self._login(self.head)
        self._post('design_head_qc_pass')
        self._login(self.pm)
        self._post('design_pm_approve')
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_RELEASED)

        ctx = site_drawings_context(self.site)
        self.assertEqual(ctx['state'], 'released')
        self.assertEqual([(d.design_file_id, d.display_name) for d in ctx['drawings']],
                         [(reviewer_zip.pk, 'Revised-Layout.pdf')])
        self.assertFalse(DesignFilePdf.objects.filter(design_file=self.cad).exists())


# ===========================================================================
# Carry-forward and the W-6 count
# ===========================================================================

class CarryTests(CadRevisionBase):

    def _head_fail(self, redo):
        self._login(self.head)
        self._post('design_head_qc_fail', head_remarks='Needs another pass',
                   error_category=ERR_LAYOUT, redo_scope_submitted='1', redo=redo)
        self.a.refresh_from_db()
        return self.a.attempts.get(attempt_number=2)

    def test_a_boq_fail_carries_the_reviewer_cad_with_its_fields(self):
        self._at_head()
        self._cad(self.head, reason='Earthing detail added')
        revision = self._current_cad()
        new_attempt = self._head_fail(['boq'])
        copy = self._current_cad(new_attempt)
        self.assertEqual(copy.carried_forward_from_id, revision.pk)
        self.assertEqual((copy.reviewer_role, copy.revision_reason,
                          copy.designer_at_revision_id, copy.uploaded_by_id),
                         (REVIEWER_ROLE_HEAD, 'Earthing detail added', self.designer.pk,
                          self.head.pk))
        self.assertEqual((copy.bucket, copy.path), (revision.bucket, revision.path))
        # The Arka it was drawn from is carried too, so the copy pairs to the Arka's copy.
        self.assertEqual(copy.derived_from_arka_id, self._current_arka(new_attempt).pk)

    def test_a_cad_fail_does_not_carry_it(self):
        self._at_head()
        self._cad(self.head)
        new_attempt = self._head_fail(['cad'])
        self.assertFalse(new_attempt.design_files.filter(kind=DESIGN_FILE_CAD_ZIP).exists())

    def test_the_w6_count_sees_one_revision_not_one_per_attempt(self):
        self._at_head()
        self._cad(self.head)
        self._head_fail(['boq'])
        self.assertEqual(DesignFile.objects.exclude(reviewer_role='').count(), 2)
        self.assertEqual(DesignFile.objects.exclude(reviewer_role='')
                         .filter(carried_forward_from__isnull=True).count(), 1)


# ===========================================================================
# Arka revision, then CAD revision
# ===========================================================================

class TwoInARowTests(CadRevisionBase):

    PAIRING = 'CAD drawn from Arka v1 · attempt 1;'

    def test_the_pairing_line_goes_and_both_revisions_are_named(self):
        self._revise(self.head, reason='Tank clearance')
        self.assertIn(self.PAIRING, self._page(self.qc))
        self._cad(self.qc, reason='Redrawn on Arka v2')
        new = self._current_cad()
        self.assertEqual(new.derived_from_arka_id, self._current_arka().pk)
        html = self._page(self.qc)
        self.assertNotIn('CAD drawn from Arka', html)
        # The reviewer's CAD row and the reviewer's Arka, each named with the reason.
        self.assertIn('Revised by qc5 (QC): Redrawn on Arka v2', html)
        self.assertIn('revised by head5 (Head): Tank clearance', html)
        # Q5: the superseded designer row keeps its yellow mark as history.
        self.assertIn('drawn from Arka v1; current Arka is v2', html)


# ===========================================================================
# The designer's path, unchanged
# ===========================================================================

class DesignerUnchangedTests(CadRevisionBase):

    def _designer_upload(self):
        self._login(self.designer)
        upload = io.BytesIO(_zip())
        upload.name = 'designer.zip'
        with mock.patch(STORE, return_value=(BUCKET, 'W5-REV/cad_zip/d.zip')) as store, \
             mock.patch(EXTRACT) as extract:
            response = self.client.post(
                reverse('design_artifact_upload', kwargs={'project_id': self.site.project_id}),
                {'kind': DESIGN_FILE_CAD_ZIP, 'artifact_file': upload, 'remarks': 'v2 note'})
        return response, store, extract

    def test_the_designer_still_cannot_upload_in_review(self):
        # W-4 closes the designer's window from QC start; `artifacts_uploaded` is still
        # open to the designer there (DESIGN_UPLOAD_OPEN_STATUSES), so it is not listed.
        for status in (DESIGN_IN_QC, DESIGN_AWAITING_HEAD_QC):
            with self.subTest(status=status):
                self._set_status(status)
                _response, store, extract = self._designer_upload()
                store.assert_not_called()
                extract.assert_not_called()
                self._assert_no_cad_written()

    def test_the_designer_upload_in_an_open_status_goes_through_the_shared_helpers(self):
        self._set_status(DESIGN_ARKA_SUBMITTED)
        with mock.patch.object(design_views, '_put_design_object',
                               wraps=design_views._put_design_object) as put, \
             mock.patch.object(design_views, '_record_design_file_version',
                               wraps=design_views._record_design_file_version) as record, \
             mock.patch.object(design_views, '_extract_cad_pdfs_best_effort',
                               wraps=design_views._extract_cad_pdfs_best_effort) as extract_helper:
            response, store, extract = self._designer_upload()
        self.assertEqual(response.status_code, 302)
        put.assert_called_once()
        record.assert_called_once()
        extract_helper.assert_called_once()
        store.assert_called_once()
        extract.assert_called_once()
        new = self._current_cad()
        self.assertEqual((new.version, new.uploaded_by_id, new.remarks, new.reviewer_role,
                          new.revision_reason, new.designer_at_revision_id),
                         (2, self.designer.pk, 'v2 note', '', '', None))
        self.assertEqual(new.reviser_label, '')
        self.assertTrue(ActivityLog.objects.filter(action_code='design_artifact_uploaded',
                                                   entity_id=new.pk).exists())
        self.assertFalse(ActivityLog.objects.filter(action_code=ACTION_CODE).exists())


# ===========================================================================
# Displays
# ===========================================================================

class DisplayTests(CadRevisionBase):

    def _url(self):
        return reverse('design_cad_revise', kwargs={'project_id': self.site.project_id})

    def test_the_form_shows_only_when_the_flag_is_true(self):
        self.assertIn(self._url(), self._page(self.qc))
        self._at_head()
        self.assertIn(self._url(), self._page(self.head))
        for status in (DESIGN_RELEASED, DESIGN_IN_DESIGN):
            with self.subTest(status=status):
                self._set_status(status)
                self.assertNotIn(self._url(), self._page(self.qc))
                self.assertNotIn(self._url(), self._page(self.head))
        self._set_status(DESIGN_IN_QC)
        self.assertNotIn(self._url(), self._page(self.designer, 'design_site_workspace'))

    def test_each_form_has_its_own_flag(self):
        """DW-2a: the Arka and CAD turns differ (QC's Arka turn starts at Arka review), so
        W-5b's one flag became two; at in_qc both are open for QC."""
        self._login(self.qc)
        response = self.client.get(
            reverse('design_qc_review', kwargs={'project_id': self.site.project_id}))
        self.assertTrue(response.context['can_revise_arka'])
        self.assertTrue(response.context['can_revise_cad'])
        self.assertNotIn('can_revise_design', response.context)
        html = response.content.decode()
        self.assertIn(reverse('design_arka_revise', kwargs={'project_id': self.site.project_id}),
                      html)
        self.assertIn(self._url(), html)

    def test_the_reviewer_row_label_on_the_qc_head_and_pm_screens(self):
        self._at_head()
        self._cad(self.head, reason='Earthing detail added')
        label = 'Revised by head5 (Head): Earthing detail added'
        self.assertIn(label, self._page(self.qc))
        self.assertIn(label, self._page(self.head, 'design_head_review'))
        self._set_status(DESIGN_AWAITING_PM_APPROVAL)
        self.assertIn(label, self._page(self.pm, 'design_site_workspace'))

    def test_a_designer_row_carries_no_label(self):
        self.assertNotIn('Revised by', self._page(self.qc))


# ===========================================================================
# Ruling 5 — revisions open at in_qc, not artifacts_uploaded
# ===========================================================================

class Ruling5Tests(CadRevisionBase):
    """In artifacts_uploaded the designer may still upload (W-4), so reviewer revisions
    (Arka and CAD) wait until QC starts. BOQ correction keeps artifacts_uploaded."""

    BEFORE_QC = 'Revisions open once QC has started reviewing the package.'

    def setUp(self):
        super().setUp()
        self._set_status(DESIGN_ARTIFACTS_UPLOADED)

    def test_arka_and_cad_revisions_are_refused_for_qc_and_the_head(self):
        for who in (self.qc, self.head):
            with self.subTest(who=who.user.username, kind='arka'):
                response = self._revise(who)
                self.assertEqual(response.status_code, 302)
                self._assert_nothing_written()
                self.assertIn(self.BEFORE_QC, self._message(response))
            with self.subTest(who=who.user.username, kind='cad'):
                response, mocks = self._cad(who)
                self.assertEqual(response.status_code, 302)
                self._assert_no_cad_written(mocks)
                mocks.store.assert_not_called()
                self.assertIn(self.BEFORE_QC, self._message(response))
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_ARTIFACTS_UPLOADED)

    def test_the_window_opens_at_in_qc_and_holds_at_the_head(self):
        def may(who, action):
            return design_reviewer_turn(who.user, Project.objects.get(pk=self.site.pk),
                                        action).allowed
        actions = (REVIEWER_ACTION_ARKA_REVISE, REVIEWER_ACTION_CAD_REVISE)
        for who in (self.qc, self.head):
            for action in actions:
                self.assertFalse(may(who, action))
        # DW-2a (DW-R2): it opens for both at in_qc, and holds at the Head for the Head only.
        for status, allowed in ((DESIGN_IN_QC, {self.qc, self.head}),
                                (DESIGN_AWAITING_HEAD_QC, {self.head})):
            self._set_status(status)
            for who in (self.qc, self.head):
                for action in actions:
                    with self.subTest(status=status, who=who.user.username, action=action):
                        self.assertEqual(may(who, action), who in allowed)

    def test_boq_correction_is_still_allowed_in_artifacts_uploaded(self):
        for who in (self.qc, self.head):
            with self.subTest(who=who.user.username):
                self.assertTrue(user_can_correct_boq(who.user, self.site))

    def test_the_form_flag_is_false_and_neither_form_shows(self):
        self._login(self.qc)
        response = self.client.get(
            reverse('design_qc_review', kwargs={'project_id': self.site.project_id}))
        self.assertFalse(response.context['can_revise_arka'])
        self.assertFalse(response.context['can_revise_cad'])
        html = response.content.decode()
        for name in ('design_arka_revise', 'design_cad_revise'):
            self.assertNotIn(reverse(name, kwargs={'project_id': self.site.project_id}), html)

    def test_the_designer_may_still_upload_in_artifacts_uploaded(self):
        self._login(self.designer)
        upload = io.BytesIO(_zip())
        upload.name = 'designer.zip'
        with mock.patch(STORE, return_value=(BUCKET, 'W5-REV/cad_zip/d.zip')) as store,              mock.patch(EXTRACT) as extract:
            self.client.post(
                reverse('design_artifact_upload', kwargs={'project_id': self.site.project_id}),
                {'kind': DESIGN_FILE_CAD_ZIP, 'artifact_file': upload})
        store.assert_called_once()
        extract.assert_called_once()
        new = self._current_cad()
        self.assertEqual((new.version, new.uploaded_by_id, new.reviewer_role),
                         (2, self.designer.pk, ''))
