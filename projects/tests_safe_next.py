"""
SEC-2 — one safe check, utils.safe_next_url(), for every screen that honours `next`.

Five views.py handlers guarded their `next` with `not urlparse(next).netloc`, which
`/\\evil.com` passes (browsers read `/\\` as `//`), and so does `http:evil.com` — the
one that really left the site (see S below). They now go
through safe_next_url(), as _triage_redirect() does (tests_triage_redirect.py pins that
one, unedited).

WHAT IS PINNED
--------------
  H    The helper, no database: real local paths are followed (stripped), every hostile
       form returns the fallback — None by default, or the URL the caller passes.
  S    The five screens, each through its URL:
         * its REAL `next` (what the live template sends) lands where it did;
         * where that real value equals the screen's own fallback — all five today,
           since project_detail.html is no longer rendered — a second local path
           (/dashboard/se/) proves the follow arm runs, not just the fallback;
         * `next=/\\evil.com` lands on the screen's own fallback AND the action still
           happened (status moved, document stored or deleted, attachment stored or
           deleted);
         * `next=http:evil.com` lands on the fallback too. This is the form that really
           left the site: Django's redirect() percent-encodes the backslash form into a
           local path (/%5Cevil.com), but sends http:evil.com unchanged, and an https
           page resolves that to http://evil.com/.

The live senders (SEC-2 pre-flight P3): the task-status forms send no `next` at all;
project_overview.html's document forms send the overview path; task_detail.html and
_task_attachments.html send the task-detail path.
"""
from datetime import date
from unittest.mock import MagicMock, patch

from django.test import RequestFactory, SimpleTestCase
from django.urls import reverse

from .models import ProjectDocument, Task, TaskAttachment
from .tests_soft_delete import SoftDeleteBase, _client_for as _sd_client, _pdf, _photo
from .tests_task_status_path import TaskStatusPathFixture, _client_for as _ts_client
from .utils import safe_next_url

HOSTILE = '/\\evil.com'
# The form that actually left the site before SEC-2: the old netloc guard followed it and
# Django sends it unencoded, so an https page resolves it to http://evil.com/. (Django's
# redirect() percent-encodes the backslash form to /%5Cevil.com, a local path.)
SCHEME_ONLY = 'http:evil.com'
OTHER_LOCAL = '/dashboard/se/'


# ===========================================================================
# H — the helper itself
# ===========================================================================

class SafeNextUrlHelperTests(SimpleTestCase):
    """No database: the helper reads only the request's host and scheme."""

    def setUp(self):
        self.factory = RequestFactory()
        self.local = reverse('design_tender_dashboard', kwargs={'pk': 7})

    def _safe(self, target, secure=False, fallback=None):
        request = self.factory.post('/any/', secure=secure)
        return safe_next_url(request, target, fallback)

    # -- followed --------------------------------------------------------------

    def test_h1_a_local_path_is_followed(self):
        self.assertEqual(self._safe(self.local), self.local)

    def test_h2_a_local_path_with_query_and_fragment_is_followed(self):
        url = self.local + '?q=abc#row-5'
        self.assertEqual(self._safe(url), url)

    def test_h3_a_local_path_is_followed_on_an_https_request(self):
        self.assertEqual(self._safe(self.local, secure=True), self.local)

    def test_h4_surrounding_spaces_are_stripped(self):
        self.assertEqual(self._safe('  ' + self.local + ' '), self.local)

    # -- refused ---------------------------------------------------------------

    def test_h5_a_backslash_after_the_slash_is_refused(self):
        self.assertIsNone(self._safe('/\\evil.com'))

    def test_h6_a_tab_after_the_slash_is_refused(self):
        self.assertIsNone(self._safe('/\t/evil.com'))

    def test_h7_other_hostile_forms_are_refused(self):
        for value in ('//evil.com', 'https://evil.com/', 'http:evil.com',
                      'javascript:alert(1)'):
            with self.subTest(next=value):
                self.assertIsNone(self._safe(value))

    def test_h8_empty_blank_and_missing_are_refused(self):
        for value in ('', '   ', None):
            with self.subTest(next=value):
                self.assertIsNone(self._safe(value))

    def test_h9_a_refusal_returns_the_callers_fallback_when_given(self):
        self.assertEqual(self._safe(HOSTILE, fallback='/home/'), '/home/')
        self.assertEqual(self._safe(None, fallback='/home/'), '/home/')
        # And a safe target still wins over the fallback.
        self.assertEqual(self._safe(self.local, fallback='/home/'), self.local)


# ===========================================================================
# S1 — task_status_update (fallback: project_overview)
# ===========================================================================

class TaskStatusNextTests(TaskStatusPathFixture):

    def _post(self, task, **extra):
        return _ts_client(self.se).post(
            reverse('task_status_update', args=[self.project.project_id, task.pk]),
            {'status': Task.IN_PROGRESS, **extra})

    def _overview(self):
        return reverse('project_overview', args=[self.project.project_id])

    def test_s1a_the_real_form_sends_no_next_and_lands_on_the_overview(self):
        task = self._se_task(due_date=date.today())
        response = self._post(task)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], self._overview())

    def test_s1b_a_second_local_path_is_followed(self):
        task = self._se_task(due_date=date.today())
        response = self._post(task, next=OTHER_LOCAL)
        self.assertEqual(response['Location'], OTHER_LOCAL)

    def test_s1c_a_backslash_next_lands_on_the_overview_and_the_status_moves(self):
        task = self._se_task(due_date=date.today())
        response = self._post(task, next=HOSTILE)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], self._overview())
        task.refresh_from_db()
        self.assertEqual(task.status, Task.IN_PROGRESS)

    def test_s1d_a_scheme_only_next_lands_on_the_overview(self):
        task = self._se_task(due_date=date.today())
        response = self._post(task, next=SCHEME_ONLY)
        self.assertEqual(response['Location'], self._overview())


# ===========================================================================
# S2–S5 — project documents and task attachments
# ===========================================================================

class FileScreensNextTests(SoftDeleteBase):
    """Uploads go through the real view with the storage client mocked, exactly as
    tests_soft_delete does, so the row the action writes is real."""

    def _overview(self):
        return reverse('project_overview', args=[self.project.project_id])

    def _task_detail(self, task):
        return reverse('task_detail', args=[self.project.project_id, task.pk])

    def _upload_doc(self, **extra):
        with patch('projects.supabase_storage.get_supabase_client',
                   return_value=MagicMock()):
            return _sd_client(self.pm).post(
                reverse('upload_project_document', args=[self.project.project_id]),
                {'files': [_pdf('handover.pdf')], **extra})

    def _delete_doc(self, doc, **extra):
        return _sd_client(self.pm).post(
            reverse('delete_project_document', args=[self.project.project_id, doc.pk]),
            extra)

    def _upload_attachment(self, task, **extra):
        with patch('projects.supabase_storage.get_supabase_client',
                   return_value=MagicMock()):
            return _sd_client(self.se).post(
                reverse('upload_task_attachment', args=[self.project.project_id, task.pk]),
                {'files': [_photo('roof.jpg')], **extra})

    def _delete_attachment(self, task, attach, **extra):
        return _sd_client(self.se).post(
            reverse('delete_task_attachment',
                    args=[self.project.project_id, task.pk, attach.pk]),
            extra)

    def _a_doc(self):
        self._upload_doc()
        return ProjectDocument.objects.get(project=self.project)

    def _an_attachment(self, task):
        self._upload_attachment(task)
        return TaskAttachment.objects.get(task=task)

    # -- S2 upload_project_document (fallback: project_overview) ---------------

    def test_s2a_upload_doc_real_next_lands_on_the_overview(self):
        response = self._upload_doc(next=self._overview())
        self.assertEqual(response['Location'], self._overview())

    def test_s2b_upload_doc_second_local_path_is_followed(self):
        response = self._upload_doc(next=OTHER_LOCAL)
        self.assertEqual(response['Location'], OTHER_LOCAL)

    def test_s2c_upload_doc_backslash_next_falls_back_and_the_file_is_stored(self):
        response = self._upload_doc(next=HOSTILE)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], self._overview())
        self.assertEqual(ProjectDocument.objects.filter(project=self.project).count(), 1)

    def test_s2d_upload_doc_scheme_only_next_falls_back(self):
        response = self._upload_doc(next=SCHEME_ONLY)
        self.assertEqual(response['Location'], self._overview())

    # -- S3 delete_project_document (fallback: project_overview) ---------------

    def test_s3a_delete_doc_real_next_lands_on_the_overview(self):
        response = self._delete_doc(self._a_doc(), next=self._overview())
        self.assertEqual(response['Location'], self._overview())

    def test_s3b_delete_doc_second_local_path_is_followed(self):
        response = self._delete_doc(self._a_doc(), next=OTHER_LOCAL)
        self.assertEqual(response['Location'], OTHER_LOCAL)

    def test_s3c_delete_doc_backslash_next_falls_back_and_the_doc_is_deleted(self):
        doc = self._a_doc()
        response = self._delete_doc(doc, next=HOSTILE)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], self._overview())
        doc.refresh_from_db()
        self.assertTrue(doc.is_deleted)

    def test_s3d_delete_doc_scheme_only_next_falls_back(self):
        response = self._delete_doc(self._a_doc(), next=SCHEME_ONLY)
        self.assertEqual(response['Location'], self._overview())

    # -- S4 upload_task_attachment (fallback: task_detail) ---------------------

    def test_s4a_upload_attachment_real_next_lands_on_task_detail(self):
        task = self._task(self.project, 'AC Cable Work')
        response = self._upload_attachment(task, next=self._task_detail(task))
        self.assertEqual(response['Location'], self._task_detail(task))

    def test_s4b_upload_attachment_second_local_path_is_followed(self):
        task = self._task(self.project, 'AC Cable Work')
        response = self._upload_attachment(task, next=OTHER_LOCAL)
        self.assertEqual(response['Location'], OTHER_LOCAL)

    def test_s4c_upload_attachment_backslash_next_falls_back_and_the_file_is_stored(self):
        task = self._task(self.project, 'AC Cable Work')
        response = self._upload_attachment(task, next=HOSTILE)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], self._task_detail(task))
        self.assertEqual(TaskAttachment.objects.filter(task=task).count(), 1)

    def test_s4d_upload_attachment_scheme_only_next_falls_back(self):
        task = self._task(self.project, 'AC Cable Work')
        response = self._upload_attachment(task, next=SCHEME_ONLY)
        self.assertEqual(response['Location'], self._task_detail(task))

    # -- S5 delete_task_attachment (fallback: task_detail) ---------------------

    def test_s5a_delete_attachment_real_next_lands_on_task_detail(self):
        task = self._task(self.project, 'AC Cable Work')
        response = self._delete_attachment(task, self._an_attachment(task),
                                           next=self._task_detail(task))
        self.assertEqual(response['Location'], self._task_detail(task))

    def test_s5b_delete_attachment_second_local_path_is_followed(self):
        task = self._task(self.project, 'AC Cable Work')
        response = self._delete_attachment(task, self._an_attachment(task),
                                           next=OTHER_LOCAL)
        self.assertEqual(response['Location'], OTHER_LOCAL)

    def test_s5c_delete_attachment_backslash_next_falls_back_and_it_is_deleted(self):
        task = self._task(self.project, 'AC Cable Work')
        attach = self._an_attachment(task)
        response = self._delete_attachment(task, attach, next=HOSTILE)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], self._task_detail(task))
        attach.refresh_from_db()
        self.assertTrue(attach.is_deleted)

    def test_s5d_delete_attachment_scheme_only_next_falls_back(self):
        task = self._task(self.project, 'AC Cable Work')
        response = self._delete_attachment(task, self._an_attachment(task),
                                           next=SCHEME_ONLY)
        self.assertEqual(response['Location'], self._task_detail(task))
