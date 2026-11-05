"""
TD-B2 — tender documents: manage page, upload, supersede, void.

WHAT IS PINNED HERE
-------------------
  gate        PM and coordinator of the site, and Admin -> 200; an SE on the site, SCM,
              Finance, CEO and System Admin -> 403; a PM of another tender -> 404; on
              GET and on both POSTs. Residential, CAPEX and Cancelled sites -> 404.
  upload      Management only: row saved, no-prices fields empty (even if ticked);
              Site team without the tick: form error, storage never called; with it:
              confirmed by the uploader, time set; a fake PDF: refused before storage
  supersede   the old row points at the new one; another tender's, a voided and an
              already-replaced document are refused before storage; a document voided
              while the file uploads is caught under the lock and the file discarded
  failure     the row failing after storage discards the file and saves nothing
  duplicate   same user, name and size in 60 s refused; after 61 s allowed
  void        a 10-character reason works; "   short   " refused; a second void
              refused; another tender's document 404; the file is never removed
  silence     no ActivityLog row (R5), no notification (B2-c)
  history     shown to management, never to an SE (helper result asserted too)

Storage is mocked at tender_documents' _client — no test reaches a bucket.

Run with:
    python manage.py test projects.tests_tender_documents_b2 --settings=solarpms.test_settings
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    TENDER_DOC_MANAGEMENT, TENDER_DOC_SITE_TEAM, ActivityLog, Notification, Program,
    Project, ProjectPhase, Task, TenderDocument,
)
from .permissions import user_can_view_tender_document_history
from .tender_documents import discard_tender_document_file, store_tender_document_file

CLIENT = 'projects.tender_documents._client'
STORE = 'projects.views_tender_documents.store_tender_document_file'
DISCARD = 'projects.views_tender_documents.discard_tender_document_file'
BUCKET = 'test-delivery-bucket'
PDF_BYTES = b'%PDF-1.4 tender document stub'
PNG_BYTES = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32


def _profile(username, role):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.save()
    return profile


def _pdf(name='spec.pdf', body=PDF_BYTES):
    return SimpleUploadedFile(name, body, content_type='application/pdf')


def _messages(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


@override_settings(SUPABASE_DELIVERY_BUCKET=BUCKET)
class TenderDocsB2Base(TestCase):
    """P1 (OPEX): S1 (pm1 its PM, coord its coordinator, se1 holds a task), S1B (live),
    S_cancel (Cancelled), S_deleted (soft-deleted) — so the banner counts 2.
    P2 (OPEX): S2 (pm2). PC (CAPEX): C1. R1: Residential, no Program.
    P1 has current_doc (Management only); P2 has other_doc."""

    def setUp(self):
        self.pm1      = _profile('b2_pm1', 'PM')
        self.pm2      = _profile('b2_pm2', 'PM')
        self.coord    = _profile('b2_coord', 'Project Coordinator')
        self.se1      = _profile('b2_se1', 'Site Engineer')
        self.scm      = _profile('b2_scm', 'SCM')
        self.finance  = _profile('b2_fin', 'Finance')
        self.ceo      = _profile('b2_ceo', 'CEO')
        self.admin    = _profile('b2_admin', 'Admin')
        self.sysadmin = _profile('b2_sysadmin', 'System Admin')

        self.p1 = self._program('B2-P1', 'OPEX', 'BTP1')
        self.p2 = self._program('B2-P2', 'OPEX', 'BTP2')
        self.pc = self._program('B2-PC', 'CAPEX', '')

        self.s1 = self._site('BTP1-S1', 'OPEX', self.p1, self.pm1)
        self.s1.coordinators.add(self.coord)
        self._task(self.s1, self.se1)
        self.s1b = self._site('BTP1-S1B', 'OPEX', self.p1, self.pm1)
        self.s_cancel = self._site('BTP1-SX', 'OPEX', self.p1, self.pm1, status='Cancelled')
        self.s_deleted = self._site('BTP1-SD', 'OPEX', self.p1, self.pm1, is_deleted=True)
        self.s2 = self._site('BTP2-S2', 'OPEX', self.p2, self.pm2)
        self.c1 = self._site('BTPC-C1', 'CAPEX', self.pc, self.pm1)
        self.r1 = self._site('BTR-R1', 'Residential', None, self.pm1)

        self.current_doc = self._doc(self.p1, 'Approved makes', TENDER_DOC_MANAGEMENT)
        self.other_doc = self._doc(self.p2, 'Other tender spec', TENDER_DOC_MANAGEMENT)

        patcher = patch(CLIENT)
        self.storage = patcher.start()
        self.addCleanup(patcher.stop)

    # -- fixtures -----------------------------------------------------------------

    def _program(self, name, program_type, code):
        return Program.objects.create(name=name, program_type=program_type,
                                      client_name='B2 Client', status='Active',
                                      short_tender_code=code)

    def _site(self, code, project_type, program, pm, status='Active', is_deleted=False):
        site = Project(
            project_id=code, customer_name='B2 Client', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type=project_type,
            program=program, site_code=code if program else None,
            dc_capacity_kw=Decimal('100.00'), status=status, assigned_pm=pm,
            is_deleted=is_deleted)
        site.save()
        return site

    def _task(self, site, profile):
        phase = ProjectPhase.objects.create(project=site, phase_name='Install',
                                            phase_order=1)
        Task.objects.create(phase=phase, task_name='Mount structure', task_order=1,
                            assigned_role=Task.SITE_ENGINEER, assigned_to=profile)

    def _doc(self, program, title, visibility, **extra):
        fields = dict(
            program=program, title=title, category='technical_spec',
            visibility=visibility, storage_bucket=BUCKET,
            storage_path=f'program/{program.pk}/{title}.pdf',
            original_filename=f'{title}.pdf', content_type='application/pdf',
            size_bytes=1024, uploaded_by=self.pm1)
        if visibility == TENDER_DOC_SITE_TEAM:
            fields.update(no_prices_confirmed_by=self.pm1,
                          no_prices_confirmed_at=timezone.now())
        fields.update(extra)
        return TenderDocument.objects.create(**fields)

    # -- requests -----------------------------------------------------------------

    def _manage(self, site):
        return reverse('tender_documents_manage', args=[site.project_id])

    def _get(self, profile, site=None):
        self.client.force_login(profile.user)
        return self.client.get(self._manage(site or self.s1))

    def _upload(self, profile, site=None, **overrides):
        data = {'title': 'Technical spec', 'category': 'technical_spec',
                'visibility': TENDER_DOC_MANAGEMENT, 'file': _pdf()}
        data.update(overrides)
        self.client.force_login(profile.user)
        return self.client.post(
            reverse('tender_documents_upload', args=[(site or self.s1).project_id]), data)

    def _void(self, profile, doc, reason='Uploaded the wrong revision', site=None):
        self.client.force_login(profile.user)
        return self.client.post(
            reverse('tender_documents_void', args=[(site or self.s1).project_id, doc.pk]),
            {'void_reason': reason})

    @property
    def _uploads(self):
        return self.storage.return_value.storage.from_.return_value.upload

    @property
    def _removes(self):
        return self.storage.return_value.storage.from_.return_value.remove


class GateTests(TenderDocsB2Base):
    """R1, R2, R8 and the a/b/c order: 404 / 404 / 403."""

    ALLOWED = ('pm1', 'coord', 'admin')
    FORBIDDEN = ('se1', 'scm', 'finance', 'ceo', 'sysadmin')

    def test_managers_and_admin_get_the_page(self):
        for name in self.ALLOWED:
            with self.subTest(user=name):
                self.assertEqual(self._get(getattr(self, name)).status_code, 200)

    def test_viewers_who_do_not_manage_get_403_on_every_route(self):
        for name in self.FORBIDDEN:
            profile = getattr(self, name)
            with self.subTest(user=name):
                self.assertEqual(self._get(profile).status_code, 403)
                self.assertEqual(self._upload(profile).status_code, 403)
                self.assertEqual(self._void(profile, self.current_doc).status_code, 403)
        self._uploads.assert_not_called()
        self.assertEqual(TenderDocument.objects.filter(program=self.p1).count(), 1)
        self.current_doc.refresh_from_db()
        self.assertIsNone(self.current_doc.voided_at)

    def test_pm_of_another_tender_gets_404_on_every_route(self):
        self.assertEqual(self._get(self.pm2).status_code, 404)
        self.assertEqual(self._upload(self.pm2).status_code, 404)
        self.assertEqual(self._void(self.pm2, self.current_doc).status_code, 404)
        self._uploads.assert_not_called()
        self.current_doc.refresh_from_db()
        self.assertIsNone(self.current_doc.voided_at)

    def test_residential_capex_and_cancelled_sites_404_even_for_admin(self):
        for site in (self.r1, self.c1, self.s_cancel):
            for profile in (self.pm1, self.admin):
                with self.subTest(site=site.project_id, user=profile.user.username):
                    self.assertEqual(self._get(profile, site).status_code, 404)
                    self.assertEqual(self._upload(profile, site).status_code, 404)
        self._uploads.assert_not_called()

    def test_soft_deleted_site_404(self):
        self.assertEqual(self._get(self.admin, self.s_deleted).status_code, 404)

    def test_upload_and_void_refuse_get(self):
        self.client.force_login(self.pm1.user)
        url = reverse('tender_documents_upload', args=[self.s1.project_id])
        self.assertEqual(self.client.get(url).status_code, 405)

    def test_banner_counts_live_sites_and_names_the_tender(self):
        response = self._get(self.pm1)
        # S1 and S1B; Cancelled and soft-deleted sites are not counted.
        self.assertContains(response, 'Applies to all 2 sites in <strong>B2-P1</strong>')

    def test_no_template_comment_leaks_and_no_open_link(self):
        content = self._get(self.pm1).content.decode()
        self.assertNotIn('TD-B2 manage page', content)
        self.assertNotIn('{#', content)
        # B2-d: no open or download link to a stored file on this page.
        self.assertNotIn('storage_path', content)
        self.assertNotIn(self.current_doc.storage_path, content)


class UploadTests(TenderDocsB2Base):
    """R3, R9, B2-b and the storage-then-row order."""

    def test_management_upload_saves_a_row_without_no_prices_fields(self):
        response = self._upload(self.pm1, no_prices_confirmed='on')
        self.assertRedirects(response, self._manage(self.s1), fetch_redirect_response=False)
        doc = TenderDocument.objects.get(program=self.p1, title='Technical spec')
        self.assertEqual(doc.visibility, TENDER_DOC_MANAGEMENT)
        self.assertIsNone(doc.no_prices_confirmed_by)
        self.assertIsNone(doc.no_prices_confirmed_at)
        self.assertEqual(doc.storage_bucket, BUCKET)
        self.assertTrue(doc.storage_path.startswith(f'program/{self.p1.pk}/'))
        self.assertEqual(doc.original_filename, 'spec.pdf')
        self.assertEqual(doc.size_bytes, len(PDF_BYTES))
        self.assertEqual(doc.uploaded_by, self.pm1)
        self._uploads.assert_called_once()

    def test_site_team_without_tick_is_a_form_error_and_storage_is_not_called(self):
        with patch(STORE, wraps=store_tender_document_file) as store:
            response = self._upload(self.pm1, visibility=TENDER_DOC_SITE_TEAM)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Site Engineers will see this file. Confirm it '
                                      'contains no prices.')
        store.assert_not_called()
        self._uploads.assert_not_called()
        self.assertFalse(TenderDocument.objects.filter(title='Technical spec').exists())

    def test_site_team_with_tick_records_who_and_when(self):
        before = timezone.now()
        self._upload(self.coord, visibility=TENDER_DOC_SITE_TEAM, no_prices_confirmed='on',
                     file=SimpleUploadedFile('layout.png', PNG_BYTES,
                                             content_type='image/png'))
        doc = TenderDocument.objects.get(title='Technical spec')
        self.assertEqual(doc.visibility, TENDER_DOC_SITE_TEAM)
        self.assertEqual(doc.no_prices_confirmed_by, self.coord)
        self.assertGreaterEqual(doc.no_prices_confirmed_at, before)
        self.assertEqual(doc.content_type, 'image/png')

    def test_fake_pdf_is_refused_before_storage(self):
        with patch(STORE, wraps=store_tender_document_file) as store:
            response = self._upload(self.pm1, file=_pdf('fake.pdf', b'not a pdf at all'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'fake.pdf')
        store.assert_not_called()
        self._uploads.assert_not_called()
        self.assertFalse(TenderDocument.objects.filter(title='Technical spec').exists())

    def test_admin_may_upload(self):
        self._upload(self.admin)
        self.assertTrue(TenderDocument.objects.filter(program=self.p1,
                                                      uploaded_by=self.admin).exists())

    def test_row_failure_after_storage_discards_the_file_and_saves_nothing(self):
        with patch.object(TenderDocument.objects, 'create',
                          side_effect=DatabaseError('boom')), \
                self.assertLogs('projects.views_tender_documents', level='ERROR'):
            response = self._upload(self.pm1)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Nothing was saved')
        self._uploads.assert_called_once()
        stored_path = self._uploads.call_args.kwargs['path']
        self._removes.assert_called_once_with([stored_path])
        self.assertFalse(TenderDocument.objects.filter(title='Technical spec').exists())

    def test_storage_failure_is_a_form_error_and_no_row(self):
        self._uploads.side_effect = RuntimeError('network down')
        with self.assertLogs('projects.tender_documents', level='ERROR'):
            response = self._upload(self.pm1)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Upload to storage failed')
        self.assertFalse(TenderDocument.objects.filter(title='Technical spec').exists())


class SupersedeTests(TenderDocsB2Base):
    """R7: one level; refused choices never reach storage."""

    def test_replaces_marks_the_old_row_superseded(self):
        self._upload(self.pm1, title='Approved makes v2', replaces=self.current_doc.pk)
        new = TenderDocument.objects.get(title='Approved makes v2')
        self.current_doc.refresh_from_db()
        self.assertEqual(self.current_doc.superseded_by, new)
        self.assertIsNotNone(self.current_doc.superseded_at)
        self.assertIsNone(new.superseded_by)

    def _refused_choice(self, doc):
        with patch(STORE, wraps=store_tender_document_file) as store:
            response = self._upload(self.pm1, title='Replacement', replaces=doc.pk)
        self.assertEqual(response.status_code, 200)
        store.assert_not_called()
        self.assertFalse(TenderDocument.objects.filter(title='Replacement').exists())

    def test_another_tenders_document_is_refused(self):
        self._refused_choice(self.other_doc)
        self.other_doc.refresh_from_db()
        self.assertIsNone(self.other_doc.superseded_by)

    def test_voided_document_is_refused(self):
        TenderDocument.objects.filter(pk=self.current_doc.pk).update(
            voided_at=timezone.now(), voided_by=self.admin, void_reason='Wrong tender file')
        self._refused_choice(self.current_doc)

    def test_already_replaced_document_is_refused(self):
        newer = self._doc(self.p1, 'Approved makes v2', TENDER_DOC_MANAGEMENT)
        TenderDocument.objects.filter(pk=self.current_doc.pk).update(
            superseded_by=newer, superseded_at=timezone.now())
        self._refused_choice(self.current_doc)
        self.current_doc.refresh_from_db()
        self.assertEqual(self.current_doc.superseded_by, newer)

    def test_document_voided_during_the_upload_is_caught_under_the_lock(self):
        doc = self.current_doc

        def store_and_void(program_id, uploaded):
            # Another manager voids the chosen document after the form validated it.
            TenderDocument.objects.filter(pk=doc.pk).update(
                voided_at=timezone.now(), voided_by=self.admin,
                void_reason='Voided by someone else')
            return BUCKET, f'program/{program_id}/race.pdf', 'application/pdf', 10

        with patch(STORE, side_effect=store_and_void):
            response = self._upload(self.pm1, title='Replacement', replaces=doc.pk)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'was voided or replaced while you were uploading')
        self._removes.assert_called_once_with([f'program/{self.p1.pk}/race.pdf'])
        self.assertFalse(TenderDocument.objects.filter(title='Replacement').exists())
        doc.refresh_from_db()
        self.assertIsNone(doc.superseded_by)


class DuplicateTests(TenderDocsB2Base):
    """The 60-second guard: same user, file name and size, same Program."""

    def test_second_upload_within_60_seconds_is_refused(self):
        self._upload(self.pm1)
        response = self._upload(self.pm1)
        self.assertRedirects(response, self._manage(self.s1), fetch_redirect_response=False)
        self.assertTrue(any('less than a minute ago' in m for m in _messages(response)))
        self.assertEqual(TenderDocument.objects.filter(title='Technical spec').count(), 1)
        self._uploads.assert_called_once()

    def test_same_file_after_61_seconds_is_allowed(self):
        self._upload(self.pm1)
        TenderDocument.objects.filter(title='Technical spec').update(
            uploaded_at=timezone.now() - timedelta(seconds=61))
        self._upload(self.pm1)
        self.assertEqual(TenderDocument.objects.filter(title='Technical spec').count(), 2)

    def test_another_user_with_the_same_file_is_not_a_duplicate(self):
        self._upload(self.pm1)
        self._upload(self.coord)
        self.assertEqual(TenderDocument.objects.filter(title='Technical spec').count(), 2)


class VoidTests(TenderDocsB2Base):
    """Conditional update, stripped reason >= 10, file kept."""

    def test_void_with_a_ten_character_reason(self):
        response = self._void(self.pm1, self.current_doc, reason='  0123456789  ')
        self.assertRedirects(response, self._manage(self.s1), fetch_redirect_response=False)
        self.current_doc.refresh_from_db()
        self.assertIsNotNone(self.current_doc.voided_at)
        self.assertEqual(self.current_doc.voided_by, self.pm1)
        self.assertEqual(self.current_doc.void_reason, '0123456789')
        self._removes.assert_not_called()

    def test_short_reason_after_stripping_is_refused(self):
        response = self._void(self.pm1, self.current_doc, reason='   short   ')
        self.assertTrue(any('at least 10 characters' in m for m in _messages(response)))
        self.current_doc.refresh_from_db()
        self.assertIsNone(self.current_doc.voided_at)

    def test_second_void_is_refused_and_keeps_the_first(self):
        self._void(self.pm1, self.current_doc, reason='First reason given')
        response = self._void(self.admin, self.current_doc, reason='Second reason given')
        self.assertTrue(any('already voided or replaced' in m for m in _messages(response)))
        self.current_doc.refresh_from_db()
        self.assertEqual(self.current_doc.voided_by, self.pm1)
        self.assertEqual(self.current_doc.void_reason, 'First reason given')

    def test_superseded_document_cannot_be_voided(self):
        newer = self._doc(self.p1, 'Approved makes v2', TENDER_DOC_MANAGEMENT)
        TenderDocument.objects.filter(pk=self.current_doc.pk).update(
            superseded_by=newer, superseded_at=timezone.now())
        response = self._void(self.pm1, self.current_doc)
        self.assertTrue(any('already voided or replaced' in m for m in _messages(response)))
        self.current_doc.refresh_from_db()
        self.assertIsNone(self.current_doc.voided_at)

    def test_another_tenders_document_is_404(self):
        self.assertEqual(self._void(self.pm1, self.other_doc).status_code, 404)
        self.other_doc.refresh_from_db()
        self.assertIsNone(self.other_doc.voided_at)

    def test_discard_refuses_a_recorded_path(self):
        self.assertFalse(discard_tender_document_file(BUCKET, self.current_doc.storage_path))
        self.assertFalse(discard_tender_document_file('other-bucket', 'program/1/x.pdf'))
        self._removes.assert_not_called()
        self.assertTrue(discard_tender_document_file(BUCKET, 'program/1/unrecorded.pdf'))
        self._removes.assert_called_once_with(['program/1/unrecorded.pdf'])

    def test_discard_never_raises(self):
        self._removes.side_effect = RuntimeError('network down')
        with self.assertLogs('projects.tender_documents', level='ERROR'):
            self.assertFalse(discard_tender_document_file(BUCKET, 'program/1/orphan.pdf'))


class SilenceAndHistoryTests(TenderDocsB2Base):
    """R5 (no ActivityLog), B2-c (no notification), R7 history audience."""

    def test_no_activity_log_and_no_notification(self):
        self.client.force_login(self.pm1.user)   # its user_login row is written here
        logs_before = ActivityLog.objects.count()
        notes_before = Notification.objects.count()
        with patch('projects.notifications.send_notification') as send, \
                patch('projects.notify.notify') as notify, \
                patch('projects.notify.notify_now') as notify_now:
            self.client.post(reverse('tender_documents_upload', args=[self.s1.project_id]),
                             {'title': 'Spec v2', 'category': 'technical_spec',
                              'visibility': TENDER_DOC_SITE_TEAM, 'no_prices_confirmed': 'on',
                              'replaces': self.current_doc.pk, 'file': _pdf()})
            new = TenderDocument.objects.get(title='Spec v2')
            self.client.post(reverse('tender_documents_void',
                                     args=[self.s1.project_id, new.pk]),
                             {'void_reason': 'Wrong revision uploaded'})
            self.client.get(self._manage(self.s1))
        new.refresh_from_db()
        self.assertIsNotNone(new.voided_at)
        self.assertEqual(ActivityLog.objects.count(), logs_before)
        self.assertEqual(Notification.objects.count(), notes_before)
        send.assert_not_called()
        notify.assert_not_called()
        notify_now.assert_not_called()

    def test_history_shown_to_management(self):
        self._void(self.pm1, self.current_doc, reason='Wrong tender file')
        response = self._get(self.pm1)
        self.assertContains(response, 'id="td-history"')
        self.assertContains(response, 'Wrong tender file')
        self.assertContains(self._get(self.admin), 'id="td-history"')

    def test_history_helper_refuses_a_site_engineer(self):
        self.assertFalse(user_can_view_tender_document_history(self.se1.user, self.s1))
        self.assertTrue(user_can_view_tender_document_history(self.pm1.user, self.s1))
        # The SE cannot reach the page at all.
        self.assertEqual(self._get(self.se1).status_code, 403)

    def test_current_list_excludes_voided_and_superseded(self):
        newer = self._doc(self.p1, 'Approved makes v2', TENDER_DOC_MANAGEMENT)
        TenderDocument.objects.filter(pk=self.current_doc.pk).update(
            superseded_by=newer, superseded_at=timezone.now())
        content = self._get(self.pm1).content.decode()
        current = content.split('id="td-current"')[1].split('id="td-upload"')[0]
        self.assertIn('Approved makes v2', current)
        self.assertNotIn(f'id="td-doc-{self.current_doc.pk}"', current)
        self.assertIn(f'id="td-hist-{self.current_doc.pk}"', content)
