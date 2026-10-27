"""
DR-1 — the PDFs inside a CAD zip, extracted into their own private objects.

WHAT IS PINNED HERE
-------------------
  * Only `.pdf`-named entries whose bytes start with %PDF- are uploaded; every other
    `.pdf` entry gets an outcome row saying why not, and non-PDF entries are never opened.
  * Nested folders and duplicate basenames: rows are keyed on the FULL entry name.
  * Idempotency: a second run writes nothing and uploads nothing.
  * Carried copies: rows attach to the original DesignFile; a cycle is refused.
  * A storage failure records nothing for that entry, so the next run retries it.
  * DR-9: the upload stands when extraction raises.
  * The command: dry run writes nothing (by captured SQL), --apply writes once.

Storage is mocked throughout — no test here reaches a bucket.
"""
import io
import zipfile
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .design_storage import (
    DESIGN_MIME_TYPE_MAP, DesignStorageError, cad_pdf_source, extract_cad_pdfs,
)
from .models import (
    ActivityLog, ArkaSubmission, DesignAssignment, DesignAttempt, DesignFile,
    DesignFilePdf, Program, Project,
    CAD_PDF_ENCRYPTED, CAD_PDF_EXTRACTED, CAD_PDF_FAILED, CAD_PDF_NOT_A_PDF,
    CAD_PDF_TOO_LARGE, DESIGN_ARKA_SUBMITTED, DESIGN_FILE_CAD_ZIP,
)

PDF = b'%PDF-1.4\n' + b'drawing ' * 50
UPLOAD = 'projects.design_storage.upload_design_file'


def _profile(username, role):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.save()
    return profile


def _zip(entries, compression=zipfile.ZIP_DEFLATED):
    """entries: list of (name, bytes). Returns the archive as bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', compression) as zf:
        for name, content in entries:
            zf.writestr(name, content)
    return buf.getvalue()


def _encrypt_flag(zip_bytes, name):
    """Set bit 0 (encrypted) on `name`'s CENTRAL DIRECTORY record.

    zipfile.writestr() clears any flag a caller sets, so a real encrypted entry cannot be
    written from the standard library; infolist() reads the central directory, which is
    exactly what extract_cad_pdfs() consults before it would open anything.
    """
    data = bytearray(zip_bytes)
    at = data.find(b'PK\x01\x02')   # first central directory record
    while at != -1:
        name_len = int.from_bytes(data[at + 28:at + 30], 'little')
        if data[at + 46:at + 46 + name_len] == name.encode():
            data[at + 8] |= 0x01
            return bytes(data)
        at = data.find(b'PK\x01\x02', at + 4)
    raise AssertionError(f'{name} not in central directory')


def _fake_upload():
    """A stand-in for upload_design_file: records each call, returns a (bucket, path)."""
    calls = []

    def upload(file_obj, path):
        file_obj.seek(0)
        calls.append((file_obj.name, path, file_obj.read()))
        return 'Horizon-PMS-Design', path
    return upload, calls


class ExtractBase(TestCase):
    def setUp(self):
        self.designer = _profile('dr1_des', 'Design')
        self.program = Program.objects.create(
            name='Test-DR1', program_type='OPEX', client_name='DR1Client',
            status='Active', short_tender_code='DR1')
        self.site = Project(
            project_id='DR1-A', customer_name='DR1Client', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code='DR1-A',
            dc_capacity_kw=Decimal('100.00'), status='Draft',
            assigned_design=self.designer)
        self.site.save()
        self.assignment = DesignAssignment.objects.create(
            project=self.site, status=DESIGN_ARKA_SUBMITTED, assigned_to=self.designer,
            current_attempt_number=1,
            survey_file_bucket='b', survey_file_path='DR1-A/survey/x.pdf')
        self.attempt = DesignAttempt.objects.create(assignment=self.assignment,
                                                    attempt_number=1)
        self.arka = ArkaSubmission.objects.create(
            attempt=self.attempt, version=1, capacity_kw=Decimal('100'),
            arka_link='https://example.com/arka', submitted_by=self.designer)

    def _zip_row(self, attempt=None, arka=None, carried_from=None, listing=None):
        return DesignFile.objects.create(
            attempt=attempt or self.attempt, kind=DESIGN_FILE_CAD_ZIP, version=1,
            bucket='Horizon-PMS-Design', path='DR1-A/cad_zip/abc123.zip',
            archive_listing=listing or [], derived_from_arka=arka or self.arka,
            uploaded_by=self.designer, is_current=True, carried_forward_from=carried_from)

    def _extract(self, design_file, zip_bytes):
        upload, calls = _fake_upload()
        with patch(UPLOAD, side_effect=upload):
            rows = extract_cad_pdfs(design_file, io.BytesIO(zip_bytes))
        return rows, calls


class ExtractionTests(ExtractBase):

    def test_nested_folders_and_duplicate_basenames_each_get_their_own_row(self):
        zf = self._zip_row()
        rows, calls = self._extract(zf, _zip([
            ('site/a/plan.pdf', PDF), ('site/b/plan.pdf', PDF), ('site/c/d/SLD.PDF', PDF),
            ('site/plan.dwg', b'AC1027'),
        ]))
        self.assertEqual(sorted(r.entry_name for r in rows),
                         ['site/a/plan.pdf', 'site/b/plan.pdf', 'site/c/d/SLD.PDF'])
        self.assertEqual(sorted(r.display_name for r in rows),
                         ['SLD.PDF', 'plan.pdf', 'plan.pdf'])
        self.assertTrue(all(r.outcome == CAD_PDF_EXTRACTED for r in rows))
        # One object each, under the zip's own path, never named after the entry.
        paths = [path for _name, path, _data in calls]
        self.assertEqual(len(set(paths)), 3)
        for path in paths:
            self.assertRegex(path, r'^DR1-A/cad_zip/abc123/pdf/[0-9a-f]{32}\.pdf$')
        # The bytes uploaded are the entry's bytes, and each carries a .pdf name, which is
        # what makes upload_design_file store it as application/pdf.
        self.assertTrue(all(data == PDF for _n, _p, data in calls))
        self.assertTrue(all(name.lower().endswith('.pdf') for name, _p, _d in calls))
        self.assertEqual(DESIGN_MIME_TYPE_MAP['pdf'], 'application/pdf')
        self.assertEqual({r.path for r in rows}, set(paths))
        self.assertEqual({r.size_bytes for r in rows}, {len(PDF)})

    def test_a_pdf_named_entry_that_is_not_a_pdf_is_recorded_and_not_uploaded(self):
        zf = self._zip_row()
        rows, calls = self._extract(zf, _zip([('fake.pdf', b'GIF89a not a pdf'),
                                              ('real.pdf', PDF)]))
        outcomes = {r.entry_name: r.outcome for r in rows}
        self.assertEqual(outcomes, {'fake.pdf': CAD_PDF_NOT_A_PDF,
                                    'real.pdf': CAD_PDF_EXTRACTED})
        self.assertEqual([name for name, _p, _d in calls], ['real.pdf'])
        fake = DesignFilePdf.objects.get(entry_name='fake.pdf')
        self.assertEqual((fake.bucket, fake.path), ('', ''))

    def test_non_pdf_entries_are_never_opened(self):
        zf = self._zip_row()
        opened = []
        real_open = zipfile.ZipFile.open

        def spy(archive, name, *args, **kwargs):
            opened.append(getattr(name, 'filename', name))
            return real_open(archive, name, *args, **kwargs)

        # Built BEFORE the spy goes on: writestr() opens every entry too, for writing.
        archive = _zip([('a.dwg', b'AC1027'), ('photo.jpg', b'\xff\xd8'), ('folder/', b''),
                        ('boq.xlsx', b'PK'), ('drawing.pdf', PDF)])
        with patch.object(zipfile.ZipFile, 'open', spy):
            rows, _calls = self._extract(zf, archive)
        self.assertEqual(opened, ['drawing.pdf'])
        self.assertEqual([r.entry_name for r in rows], ['drawing.pdf'])

    def test_rerun_is_idempotent(self):
        zf = self._zip_row()
        archive = _zip([('one.pdf', PDF), ('two.pdf', b'nope'), ('x.dwg', b'AC')])
        first, first_calls = self._extract(zf, archive)
        self.assertEqual(len(first), 2)
        self.assertEqual(len(first_calls), 1)
        again, again_calls = self._extract(zf, archive)
        self.assertEqual((again, again_calls), ([], []))
        self.assertEqual(DesignFilePdf.objects.count(), 2)

    def test_encrypted_entry_is_recorded_without_being_opened(self):
        zf = self._zip_row()
        archive = _encrypt_flag(_zip([('locked.pdf', PDF), ('open.pdf', PDF)]),
                                'locked.pdf')
        rows, calls = self._extract(zf, archive)
        self.assertEqual({r.entry_name: r.outcome for r in rows},
                         {'locked.pdf': CAD_PDF_ENCRYPTED, 'open.pdf': CAD_PDF_EXTRACTED})
        self.assertEqual([name for name, _p, _d in calls], ['open.pdf'])

    def test_oversized_pdf_is_too_large_and_not_uploaded(self):
        zf = self._zip_row()
        with patch('projects.design_storage.MAX_DESIGN_FILE_BYTES', 100):
            rows, calls = self._extract(zf, _zip([('big.pdf', PDF)]))
        self.assertEqual([(r.outcome, r.path) for r in rows], [(CAD_PDF_TOO_LARGE, '')])
        self.assertEqual(calls, [])

    def test_corrupt_entry_is_failed(self):
        zf = self._zip_row()
        data = bytearray(_zip([('bad.pdf', b'%PDF-1.4 ' + b'A' * 200)], zipfile.ZIP_STORED))
        data[data.find(b'AAAA')] = ord('B')   # breaks the CRC, keeps the %PDF- header
        rows, calls = self._extract(zf, bytes(data))
        self.assertEqual([r.outcome for r in rows], [CAD_PDF_FAILED])
        self.assertEqual(calls, [])

    def test_storage_failure_records_nothing_for_that_entry_and_is_retried(self):
        zf = self._zip_row()
        archive = _zip([('a.pdf', PDF), ('b.pdf', PDF)])
        upload, calls = _fake_upload()
        outcomes = [upload, DesignStorageError('Upload to storage failed: ConnectError')]

        def flaky(file_obj, path):
            step = outcomes.pop(0)
            if isinstance(step, Exception):
                raise step
            return step(file_obj, path)

        with patch(UPLOAD, side_effect=flaky):
            with self.assertRaises(DesignStorageError):
                extract_cad_pdfs(zf, io.BytesIO(archive))
        self.assertEqual(list(DesignFilePdf.objects.values_list('entry_name', flat=True)),
                         ['a.pdf'])
        rows, retry_calls = self._extract(zf, archive)
        self.assertEqual([r.entry_name for r in rows], ['b.pdf'])
        self.assertEqual(len(retry_calls), 1)

    def test_unreadable_archive_raises_and_writes_nothing(self):
        zf = self._zip_row()
        with self.assertRaises(DesignStorageError):
            self._extract(zf, b'PK\x03\x04stub')
        self.assertFalse(DesignFilePdf.objects.exists())

    def test_check_constraint_ties_the_object_to_the_extracted_outcome(self):
        zf = self._zip_row()
        with self.assertRaises(IntegrityError), transaction.atomic():
            DesignFilePdf.objects.create(design_file=zf, entry_name='a.pdf',
                                         display_name='a.pdf', outcome=CAD_PDF_EXTRACTED)
        with self.assertRaises(IntegrityError), transaction.atomic():
            DesignFilePdf.objects.create(design_file=zf, entry_name='b.pdf',
                                         display_name='b.pdf', outcome=CAD_PDF_NOT_A_PDF,
                                         bucket='Horizon-PMS-Design', path='x/y.pdf')


class CarriedCopyTests(ExtractBase):

    def _carried_copy(self, original):
        attempt2 = DesignAttempt.objects.create(assignment=self.assignment,
                                                attempt_number=2)
        arka2 = ArkaSubmission.objects.create(
            attempt=attempt2, version=1, capacity_kw=Decimal('100'),
            arka_link='https://example.com/arka', submitted_by=self.designer,
            carried_forward_from=self.arka)
        return self._zip_row(attempt=attempt2, arka=arka2, carried_from=original)

    def test_rows_attach_to_the_original_and_the_copy_sees_them(self):
        original = self._zip_row()
        copy = self._carried_copy(original)
        self.assertEqual(cad_pdf_source(copy).pk, original.pk)
        rows, _calls = self._extract(copy, _zip([('p.pdf', PDF)]))
        self.assertEqual([r.design_file_id for r in rows], [original.pk])
        # The original's zip is the same object: extracting it now does nothing.
        self.assertEqual(self._extract(original, _zip([('p.pdf', PDF)])), ([], []))

    def test_a_cycle_is_refused(self):
        original = self._zip_row()
        copy = self._carried_copy(original)
        DesignFile.objects.filter(pk=original.pk).update(carried_forward_from=copy)
        with self.assertRaises(DesignStorageError):
            cad_pdf_source(DesignFile.objects.get(pk=copy.pk))


class UploadViewTests(ExtractBase):
    """design_artifact_upload, driven over HTTP as the allocated designer."""

    def _post(self, archive):
        self.client.force_login(self.designer.user)
        upload = SimpleUploadedFile('cad.zip', archive, content_type='application/zip')
        with patch('projects.design_views.upload_design_file',
                   return_value=('Horizon-PMS-Design', 'DR1-A/cad_zip/abc123.zip')):
            return self.client.post(
                reverse('design_artifact_upload', kwargs={'project_id': 'DR1-A'}),
                {'kind': DESIGN_FILE_CAD_ZIP, 'artifact_file': upload, 'remarks': ''})

    def _message(self, response):
        return ' '.join(str(m) for m in get_messages(response.wsgi_request))

    def test_a_cad_zip_upload_extracts_its_pdfs(self):
        upload, calls = _fake_upload()
        with patch(UPLOAD, side_effect=upload):
            response = self._post(_zip([('d/plan.pdf', PDF), ('d/plan.dwg', b'AC1027')]))
        self.assertEqual(response.status_code, 302)
        design_file = DesignFile.objects.get(kind=DESIGN_FILE_CAD_ZIP)
        self.assertEqual(list(design_file.extracted_pdfs.values_list('entry_name', 'outcome')),
                         [('d/plan.pdf', CAD_PDF_EXTRACTED)])
        self.assertEqual(len(calls), 1)
        self.assertFalse(ActivityLog.objects.filter(
            action_code='cad_pdf_extraction_failed').exists())

    def test_storage_failure_during_upload_extraction_records_an_activity_row(self):
        """The upload path's half of the split the command does not share: a real storage
        failure inside extract_cad_pdfs() (not a patched-out call) leaves the zip uploaded,
        no DesignFilePdf row, and ONE cad_pdf_extraction_failed row naming the designer."""
        with patch(UPLOAD, side_effect=DesignStorageError(
                'Upload to storage failed: ConnectError')):
            response = self._post(_zip([('p.pdf', PDF), ('p.dwg', b'AC1027')]))
        self.assertEqual(response.status_code, 302)
        self.assertIn('CAD (zip) v1 uploaded', self._message(response))
        design_file = DesignFile.objects.get(kind=DESIGN_FILE_CAD_ZIP)
        self.assertFalse(design_file.extracted_pdfs.exists())
        log = ActivityLog.objects.get(action_code='cad_pdf_extraction_failed')
        self.assertEqual((log.entity_id, log.actor_id), (design_file.pk, self.designer.pk))

    def test_dr9_upload_stands_when_extraction_raises(self):
        for error in (RuntimeError('boom'), DesignStorageError('storage down')):
            with self.subTest(error=type(error).__name__):
                DesignFile.objects.all().delete()
                ActivityLog.objects.all().delete()
                with patch('projects.design_views.extract_cad_pdfs', side_effect=error):
                    response = self._post(_zip([('p.pdf', PDF), ('p.dwg', b'AC1027')]))
                self.assertEqual(response.status_code, 302)
                design_file = DesignFile.objects.get(kind=DESIGN_FILE_CAD_ZIP)
                # The designer's message is the ordinary success message, unchanged.
                text = self._message(response)
                self.assertIn('CAD (zip) v1 uploaded', text)
                self.assertNotIn('PDF', text)
                self.assertFalse(design_file.extracted_pdfs.exists())
                log = ActivityLog.objects.get(action_code='cad_pdf_extraction_failed')
                self.assertEqual(log.entity_id, design_file.pk)


class CommandTests(ExtractBase):
    LISTING = [{'name': 'z/a.pdf', 'size': len(PDF)}, {'name': 'z/a.dwg', 'size': 6}]

    def _run(self, *args):
        out, err = io.StringIO(), io.StringIO()
        call_command('extract_cad_pdfs', *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_dry_run_writes_nothing_and_touches_no_storage(self):
        self._zip_row(listing=self.LISTING)
        with patch('projects.management.commands.extract_cad_pdfs.download_design_file') \
                as download, patch(UPLOAD) as upload, \
                CaptureQueriesContext(connection) as ctx:
            out, _err = self._run()
        writes = [q['sql'] for q in ctx.captured_queries
                  if q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))]
        self.assertEqual(writes, [])
        download.assert_not_called()
        upload.assert_not_called()
        self.assertIn('Database host:', out.splitlines()[0])
        self.assertIn('DRY RUN', out)
        self.assertIn("'z/a.pdf'", out)
        self.assertFalse(DesignFilePdf.objects.exists())

    def test_apply_writes_once_and_a_carried_copy_is_one_zip(self):
        original = self._zip_row(listing=self.LISTING)
        attempt2 = DesignAttempt.objects.create(assignment=self.assignment, attempt_number=2)
        arka2 = ArkaSubmission.objects.create(
            attempt=attempt2, version=1, capacity_kw=Decimal('100'),
            arka_link='https://example.com/arka', submitted_by=self.designer)
        self._zip_row(attempt=attempt2, arka=arka2, carried_from=original,
                      listing=self.LISTING)
        archive = _zip([('z/a.pdf', PDF), ('z/a.dwg', b'AC1027')])
        upload, calls = _fake_upload()
        target = 'projects.management.commands.extract_cad_pdfs.download_design_file'
        with patch(target, return_value=archive) as download, \
                patch(UPLOAD, side_effect=upload):
            out, _err = self._run('--apply')
            self.assertEqual(download.call_count, 1)
            self.assertEqual(len(calls), 1)
            self.assertEqual(list(DesignFilePdf.objects.values_list(
                'design_file_id', 'entry_name', 'outcome')),
                [(original.pk, 'z/a.pdf', CAD_PDF_EXTRACTED)])
            self.assertIn('rows written: 1', out)

            out_again, _err = self._run('--apply')
            self.assertEqual(download.call_count, 1)   # nothing left to fetch
            self.assertIn('done, 1 recorded', out_again)
            self.assertEqual(DesignFilePdf.objects.count(), 1)

    def test_download_failure_goes_to_stderr_and_writes_nothing(self):
        """A command run records a storage failure on stderr ONLY: no DesignFilePdf row
        and no ActivityLog row (the upload path's DR-9 row is pinned in UploadViewTests)."""
        self._zip_row(listing=self.LISTING)
        target = 'projects.management.commands.extract_cad_pdfs.download_design_file'
        with patch(target, side_effect=DesignStorageError('Download from storage failed')):
            out, err = self._run('--apply')
        self.assertIn('FAILED', err)
        self.assertIn('Download from storage failed', err)
        self.assertIn('failed: 1', out)
        self.assertFalse(DesignFilePdf.objects.exists())
        self.assertFalse(ActivityLog.objects.exists())

    def test_upload_failure_mid_zip_keeps_earlier_rows_and_logs_no_activity(self):
        listing = [{'name': 'z/a.pdf', 'size': len(PDF)}, {'name': 'z/b.pdf', 'size': len(PDF)}]
        self._zip_row(listing=listing)
        archive = _zip([('z/a.pdf', PDF), ('z/b.pdf', PDF)])
        upload, _calls = _fake_upload()
        steps = [upload, DesignStorageError('Upload to storage failed: ConnectError')]

        def flaky(file_obj, path):
            step = steps.pop(0)
            if isinstance(step, Exception):
                raise step
            return step(file_obj, path)

        target = 'projects.management.commands.extract_cad_pdfs.download_design_file'
        with patch(target, return_value=archive), patch(UPLOAD, side_effect=flaky):
            out, err = self._run('--apply')
        self.assertIn('Upload to storage failed', err)
        self.assertIn('failed: 1', out)
        self.assertEqual(list(DesignFilePdf.objects.values_list('entry_name', flat=True)),
                         ['z/a.pdf'])
        self.assertFalse(ActivityLog.objects.exists())
