"""
W-4 — the designer may upload design files only while the package is still with them.

WHAT IS PINNED HERE
-------------------
  * Each status in DESIGN_UPLOAD_OPEN_STATUSES accepts the allocated designer's upload, and
    the new file becomes current.
  * `artifacts_uploaded` (package complete, QC not started): the upload supersedes the old
    file and the status does not move.
  * Each refused status — from `in_qc` onward, plus the pre-design statuses — is refused with
    the workspace message + redirect, BEFORE the file is touched: no storage call, no new
    DesignFile, the previous file stays current, no activity entry, no PDF extraction.
  * The closed-attempt backstop refuses even inside the window.
  * A QC failure (new attempt) reopens uploads.
  * The workspace's `can_upload_artifacts` flag follows the same rule, and the closed note
    shows when it is off.

Storage is mocked throughout — no test here reaches a bucket.
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .design_views import DESIGN_UPLOAD_OPEN_STATUSES
from .models import (
    ActivityLog, ArkaSubmission, DesignAssignment, DesignAttempt, DesignFile, Program,
    Project, StatusTransition, SUBJECT_DESIGN_ASSIGNMENT,
    ARKA_APPROVED, DESIGN_ALLOCATED, DESIGN_ARKA_REJECTED, DESIGN_ARKA_SUBMITTED,
    DESIGN_ARTIFACTS_UPLOADED, DESIGN_AWAITING_ALLOCATION, DESIGN_AWAITING_HEAD_ARKA,
    DESIGN_AWAITING_HEAD_QC, DESIGN_AWAITING_PM_APPROVAL, DESIGN_FILE_CAD_ZIP, DESIGN_IN_DESIGN,
    DESIGN_IN_QC, DESIGN_PM_REJECTED, DESIGN_RELEASED, DESIGN_SURVEY_RETURNED,
    ERR_DRAWING_INCOMPLETE,
)

VALIDATE = 'projects.design_views.validate_cad_zip'
STORE = 'projects.design_views.upload_design_file'
EXTRACT = 'projects.design_views.extract_cad_pdfs'

# W-1 (21 Nov 2026) took `awaiting_head_arka` out of the window: nothing writes it any more,
# and a legacy site parked there is moved on by Design QC first. It is pinned as REFUSED.
OPEN = (DESIGN_IN_DESIGN, DESIGN_ARKA_SUBMITTED, DESIGN_ARKA_REJECTED,
        DESIGN_SURVEY_RETURNED, DESIGN_ARTIFACTS_UPLOADED)
REFUSED = (DESIGN_IN_QC, DESIGN_AWAITING_HEAD_QC, DESIGN_AWAITING_PM_APPROVAL,
           DESIGN_RELEASED, DESIGN_PM_REJECTED, DESIGN_AWAITING_ALLOCATION, DESIGN_ALLOCATED,
           DESIGN_AWAITING_HEAD_ARKA)

CLOSED_NOTE = 'Uploads are closed while the design is in review.'


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


class UploadWindowBase(TestCase):
    """One OPEX site with attempt 1: an Arka approved at both gates, a current CAD zip v1
    and the BOQ marked complete — a complete package, so any status can be set over it."""

    def setUp(self):
        self.designer = _profile('w4_des', 'Design')
        self.program = Program.objects.create(
            name='Test-W4', program_type='OPEX', client_name='W4Client',
            status='Active', short_tender_code='W4')
        self.site = Project(
            project_id='W4-A', customer_name='W4Client', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code='W4-A',
            dc_capacity_kw=Decimal('100.00'), status='Draft',
            assigned_design=self.designer)
        self.site.save()
        self.assignment = DesignAssignment.objects.create(
            project=self.site, status=DESIGN_ARKA_SUBMITTED, assigned_to=self.designer,
            current_attempt_number=1,
            survey_file_bucket='b', survey_file_path='W4-A/survey/x.pdf')
        self.attempt = DesignAttempt.objects.create(
            assignment=self.assignment, attempt_number=1,
            boq_submitted_at=timezone.now(), boq_submitted_by=self.designer)
        self.arka = ArkaSubmission.objects.create(
            attempt=self.attempt, version=1, capacity_kw=Decimal('100'),
            arka_link='https://example.com/arka', submitted_by=self.designer,
            verdict=ARKA_APPROVED, head_verdict=ARKA_APPROVED)
        self.old_zip = DesignFile.objects.create(
            attempt=self.attempt, kind=DESIGN_FILE_CAD_ZIP, version=1,
            bucket='Horizon-PMS-Design', path='W4-A/cad_zip/old.zip',
            derived_from_arka=self.arka, uploaded_by=self.designer, is_current=True)

    # ── helpers ────────────────────────────────────────────────────────────────

    def _set_status(self, status):
        # A queryset update, not save(): the fixture places the site at a status without
        # writing a ledger row, so the ledger count below measures the upload alone.
        DesignAssignment.objects.filter(pk=self.assignment.pk).update(status=status)
        self.assignment.refresh_from_db()

    def _upload(self):
        """POST a CAD zip as the designer. Returns (response, store_mock, extract_mock)."""
        self.client.force_login(self.designer.user)
        upload = SimpleUploadedFile('new.zip', b'PK\x03\x04stub',
                                    content_type='application/zip')
        with patch(VALIDATE, return_value=[]), \
             patch(STORE, return_value=('Horizon-PMS-Design', 'W4-A/cad_zip/new.zip')) as store, \
             patch(EXTRACT) as extract:
            response = self.client.post(
                reverse('design_artifact_upload', kwargs={'project_id': 'W4-A'}),
                {'kind': DESIGN_FILE_CAD_ZIP, 'artifact_file': upload, 'remarks': ''})
        return response, store, extract

    def _messages(self, response):
        return ' '.join(str(m) for m in get_messages(response.wsgi_request))

    def _site_activity(self):
        # Every activity row except the login the test client's force_login() writes — that
        # row is auth noise (project=None), not something the upload did.
        return ActivityLog.objects.exclude(action_code='user_login').count()

    def _ledger_count(self):
        return StatusTransition.objects.filter(
            subject_type=SUBJECT_DESIGN_ASSIGNMENT, subject_id=self.assignment.pk).count()

    def _current_zip(self):
        return DesignFile.objects.get(
            attempt__assignment=self.assignment,
            attempt__attempt_number=self.assignment.current_attempt_number,
            kind=DESIGN_FILE_CAD_ZIP, is_current=True)


class WindowConstantTests(TestCase):

    def test_the_window_is_exactly_the_ruled_five(self):
        """A status added to or dropped from the window is a ruling, not a refactor."""
        self.assertEqual(set(DESIGN_UPLOAD_OPEN_STATUSES), set(OPEN))
        self.assertEqual(len(DESIGN_UPLOAD_OPEN_STATUSES), len(OPEN))


class AllowedStatusTests(UploadWindowBase):

    def test_each_open_status_stores_the_upload_and_makes_it_current(self):
        for status in OPEN:
            with self.subTest(status=status):
                DesignFile.objects.filter(pk__gt=self.old_zip.pk).delete()
                DesignFile.objects.filter(pk=self.old_zip.pk).update(
                    is_current=True, superseded_by=None)
                self._set_status(status)

                response, store, extract = self._upload()

                self.assertEqual(response.status_code, 302)
                self.assertIn('CAD (zip) v2 uploaded', self._messages(response))
                new = self._current_zip()
                self.assertEqual(new.version, 2)
                self.assertEqual(new.path, 'W4-A/cad_zip/new.zip')
                self.old_zip.refresh_from_db()
                self.assertFalse(self.old_zip.is_current)
                store.assert_called_once()
                extract.assert_called_once()

    def test_artifacts_uploaded_supersedes_and_leaves_the_status_alone(self):
        """Package complete, QC not started: the designer fixes their own mistake. The
        progression rule fires only from arka_submitted, so nothing moves."""
        self._set_status(DESIGN_ARTIFACTS_UPLOADED)
        ledger_before = self._ledger_count()

        response, _store, _extract = self._upload()

        self.assertEqual(response.status_code, 302)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, DESIGN_ARTIFACTS_UPLOADED)
        self.assertEqual(self._ledger_count(), ledger_before, 'no status write')
        new = self._current_zip()
        self.assertEqual(new.version, 2)
        self.old_zip.refresh_from_db()
        self.assertFalse(self.old_zip.is_current)
        self.assertEqual(self.old_zip.superseded_by_id, new.pk)
        self.assertEqual(new.derived_from_arka_id, self.arka.pk)
        self.assertEqual(DesignFile.objects.filter(
            attempt=self.attempt, kind=DESIGN_FILE_CAD_ZIP, is_current=True).count(), 1)


class RefusedStatusTests(UploadWindowBase):

    def _assert_refused(self, response, store, extract, files_before, logs_before):
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'],
                         reverse('design_site_workspace', kwargs={'project_id': 'W4-A'}))
        self.assertIn('uploads are closed while the design is in review',
                      self._messages(response))
        self.assertEqual(DesignFile.objects.count(), files_before, 'no new DesignFile')
        self.old_zip.refresh_from_db()
        self.assertTrue(self.old_zip.is_current, 'the previous file stays current')
        self.assertIsNone(self.old_zip.superseded_by_id)
        self.assertEqual(self._site_activity(), logs_before, 'no activity entry')
        store.assert_not_called()
        extract.assert_not_called()

    def test_each_refused_status_stores_nothing(self):
        for status in REFUSED:
            with self.subTest(status=status):
                self._set_status(status)
                files_before = DesignFile.objects.count()
                logs_before = self._site_activity()

                response, store, extract = self._upload()

                self._assert_refused(response, store, extract, files_before, logs_before)

    def test_in_qc_is_refused(self):
        """The window closes exactly at QC start — the boundary the ruling names."""
        self._set_status(DESIGN_IN_QC)
        files_before = DesignFile.objects.count()
        logs_before = self._site_activity()

        response, store, extract = self._upload()

        self._assert_refused(response, store, extract, files_before, logs_before)
        self.assertIn('(this site is "In QC")', self._messages(response))

    def test_a_closed_attempt_is_refused_inside_the_window(self):
        """The backstop: a window status over a closed current attempt still refuses."""
        self._set_status(DESIGN_ARKA_SUBMITTED)
        DesignAttempt.objects.filter(pk=self.attempt.pk).update(closed_at=timezone.now())
        files_before = DesignFile.objects.count()
        logs_before = self._site_activity()

        response, store, extract = self._upload()

        self._assert_refused(response, store, extract, files_before, logs_before)


class ReworkReopensTests(UploadWindowBase):

    def test_a_qc_failure_reopens_uploads_on_the_new_attempt(self):
        self._set_status(DESIGN_IN_QC)
        refused, _s, _e = self._upload()
        self.assertIn('uploads are closed', self._messages(refused))

        qc = _profile('w4_qc', 'Design', is_design_qc=True)
        self.client.force_login(qc.user)
        self.client.post(reverse('design_qc_fail', kwargs={'project_id': 'W4-A'}), {
            'qc_remarks': 'Sheet 3 is missing the string layout.',
            'error_category': ERR_DRAWING_INCOMPLETE,
            'redo_scope_submitted': '1', 'redo': ['cad'],
        })
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.current_attempt_number, 2)
        self.assertEqual(self.assignment.status, DESIGN_ARKA_SUBMITTED,
                         'the approved Arka carried forward, so the attempt opens there')

        response, store, _extract = self._upload()

        self.assertIn('CAD (zip) v1 uploaded', self._messages(response))
        store.assert_called_once()
        new = self._current_zip()
        self.assertEqual(new.attempt.attempt_number, 2)
        self.assertIsNone(new.carried_forward_from_id)


class WorkspaceFlagTests(UploadWindowBase):

    def _workspace(self, status):
        self._set_status(status)
        self.client.force_login(self.designer.user)
        return self.client.get(
            reverse('design_site_workspace', kwargs={'project_id': 'W4-A'}))

    def test_flag_true_in_an_open_status_and_the_form_shows(self):
        response = self._workspace(DESIGN_ARKA_SUBMITTED)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['can_upload_artifacts'])
        self.assertContains(response, reverse('design_artifact_upload',
                                              kwargs={'project_id': 'W4-A'}))
        self.assertNotContains(response, CLOSED_NOTE)

    def test_flag_false_in_review_and_after_release_with_the_note(self):
        for status in (DESIGN_IN_QC, DESIGN_RELEASED):
            with self.subTest(status=status):
                response = self._workspace(status)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.context['can_upload_artifacts'])
                self.assertNotContains(response, reverse('design_artifact_upload',
                                                         kwargs={'project_id': 'W4-A'}))
                self.assertContains(response, CLOSED_NOTE)
