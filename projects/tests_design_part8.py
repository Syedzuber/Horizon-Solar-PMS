"""
Part 8 verification — auto due dates, the extension flow, and CAD zip upload.

WHY THIS FILE EXISTS
--------------------
Part 8 inverts the Part 2 due-date handshake, and the inversion has one failure mode that
is invisible from any single screen: a designer requesting an extension could clear their
own overdue flag. `is_overdue()` returns False for an unapproved commitment, and an
extension request takes over `is_current`, so a system that reads the current row would
stop counting the site the moment its designer asked for more time.

The tests below pin the effective/pending split that prevents that, from BOTH surfaces
that report a due date (design_metrics.tender_metrics and the designer dashboard), so
neither can regress without the other noticing.
"""
import io
import zipfile
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from .design_metrics import (
    effective_commitment, is_overdue, pending_extension, tender_metrics,
)
from .design_storage import (
    DESIGN_ACCEPTED_MIME_TYPES, DESIGN_MIME_TYPE_MAP, DesignStorageError,
    MAX_CAD_ZIP_UNCOMPRESSED_BYTES, validate_cad_zip, validate_design_file,
)
from .design_views import (
    PROGRESSION_CAD_KINDS, _allocate_one, _effective_commitment, _pending_extension,
    _maybe_advance_to_artifacts_uploaded,
)
from .models import (
    Program, Project, UserProfile, DesignAssignment, DueDateCommitment, DesignAttempt,
    ArkaSubmission, DesignFile,
    DESIGN_AWAITING_ALLOCATION, DESIGN_IN_DESIGN, DESIGN_ARKA_SUBMITTED,
    DESIGN_ARTIFACTS_UPLOADED, DESIGN_RELEASED,
    DESIGN_FILE_CAD_ZIP, DESIGN_FILE_CAD_PDF, DESIGN_FILE_CAD_DWG,
    ARKA_APPROVED,
)
from .utils import design_due_date, is_working_day, next_working_day


def _profile(username, role, is_design_head=False):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create.

    The Design Head is the `is_design_head` FLAG, not a role string — Part 6.5b removed
    'Design Head' from ROLE_CHOICES (migration 0053). Setting role='Design Head' here
    would produce a user every design view refuses with 403.
    """
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.is_design_head = is_design_head
    profile.save()
    return profile


def _zip_bytes(entries, name='cad.zip'):
    """entries: list of (filename, content_bytes)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for fn, content in entries:
            zf.writestr(fn, content)
    buf.seek(0)
    return SimpleUploadedFile(name, buf.read(), content_type='application/zip')


# ===========================================================================
# 1. Working-day helper
# ===========================================================================

class WorkingDayTests(TestCase):
    """August 2026 has five Saturdays (1, 8, 15, 22, 29) — every case in one month."""

    def test_sundays_are_never_working_days(self):
        for day in (2, 9, 16, 23, 30):
            self.assertFalse(is_working_day(date(2026, 8, day)), f'Aug {day}')

    def test_second_and_fourth_saturdays_are_off(self):
        self.assertFalse(is_working_day(date(2026, 8, 8)),  '2nd Saturday')
        self.assertFalse(is_working_day(date(2026, 8, 22)), '4th Saturday')

    def test_first_third_and_fifth_saturdays_are_working(self):
        self.assertTrue(is_working_day(date(2026, 8, 1)),  '1st Saturday')
        self.assertTrue(is_working_day(date(2026, 8, 15)), '3rd Saturday')
        self.assertTrue(is_working_day(date(2026, 8, 29)), '5th Saturday')

    def test_spec_worked_examples(self):
        # Monday -> Wednesday
        self.assertEqual(design_due_date(date(2026, 8, 3)), date(2026, 8, 5))
        # Friday -> +2 = Sunday -> Monday
        self.assertEqual(design_due_date(date(2026, 8, 7)), date(2026, 8, 10))
        # Thursday -> +2 on a 1st Saturday -> stays
        self.assertEqual(design_due_date(date(2026, 7, 30)), date(2026, 8, 1))
        # Thursday -> +2 on a 3rd Saturday -> stays
        self.assertEqual(design_due_date(date(2026, 8, 13)), date(2026, 8, 15))
        # Thursday -> +2 on a 2nd Saturday -> Monday
        self.assertEqual(design_due_date(date(2026, 8, 6)), date(2026, 8, 10))
        # Thursday -> +2 on a 4th Saturday -> Monday
        self.assertEqual(design_due_date(date(2026, 8, 20)), date(2026, 8, 24))

    def test_next_working_day_only_rolls_forward(self):
        # A 2nd Saturday and its Sunday are two consecutive non-working days.
        self.assertEqual(next_working_day(date(2026, 8, 8)), date(2026, 8, 10))
        self.assertEqual(next_working_day(date(2026, 8, 10)), date(2026, 8, 10))


# ===========================================================================
# 2-7. Allocation, auto due date, and the extension flow
# ===========================================================================

class Part8Base(TestCase):
    def setUp(self):
        self.head     = _profile('head8', 'Design', is_design_head=True)
        self.designer = _profile('des8',  'Design')
        self.program  = Program.objects.create(
            name='Test-Part8', program_type='OPEX', client_name='P8Client',
            status='Active', short_tender_code='P8')

    def _site(self, code):
        site = Project(
            project_id=code, customer_name='P8Client', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code=code,
            capacity_kw=Decimal('100.00'), status='Draft')
        site.save()
        a = DesignAssignment.objects.create(
            project=site, status=DESIGN_AWAITING_ALLOCATION,
            survey_file_bucket='b', survey_file_path=f'{code}/survey/x.pdf')
        return site, a


class AllocationSetsTheDueDateTests(Part8Base):

    def test_allocation_creates_an_auto_approved_current_commitment(self):
        """VERIFICATION 2."""
        _site, a = self._site('P8-1')
        due = _allocate_one(a, self.designer, self.head, allocated_on=date(2026, 8, 3))

        a.refresh_from_db()
        self.assertEqual(a.status, DESIGN_IN_DESIGN)
        self.assertEqual(due, date(2026, 8, 5))

        rows = list(a.due_date_commitments.all())
        self.assertEqual(len(rows), 1)
        c = rows[0]
        self.assertEqual(c.proposed_date, date(2026, 8, 5))
        self.assertEqual(c.proposed_by_id, self.head.pk)
        self.assertEqual(c.approved_by_id, self.head.pk)
        self.assertIsNotNone(c.approved_at)
        self.assertTrue(c.is_current)

    def test_allocation_never_passes_through_allocated_or_due_date_proposed(self):
        _site, a = self._site('P8-2')
        _allocate_one(a, self.designer, self.head)
        a.refresh_from_db()
        self.assertEqual(a.status, DESIGN_IN_DESIGN)

    def test_bulk_allocation_gives_every_site_the_same_date(self):
        """VERIFICATION 3 — one timestamp for the batch."""
        batch = date(2026, 8, 6)          # +2 lands on the 2nd Saturday
        dues = []
        for n in (3, 4, 5):
            _site, a = self._site(f'P8-{n}')
            dues.append(_allocate_one(a, self.designer, self.head, allocated_on=batch))
        self.assertEqual(dues, [date(2026, 8, 10)] * 3)
        self.assertEqual(len(set(dues)), 1)


class ExtensionFlowTests(Part8Base):

    def setUp(self):
        super().setUp()
        self.site, self.a = self._site('P8-EXT')
        # Allocate against a date far enough back that the site is overdue today.
        self.agreed = timezone.localdate() - timedelta(days=5)
        DueDateCommitment.objects.create(
            assignment=self.a, proposed_date=self.agreed,
            proposed_by=self.head, approved_by=self.head,
            approved_at=timezone.now(), is_current=True)
        self.a.assigned_to = self.designer
        self.a.status = DESIGN_IN_DESIGN
        self.a.save()

    def _request_extension(self, new_date, reason='needs longer'):
        with transaction.atomic():
            self.a.due_date_commitments.filter(is_current=True).update(is_current=False)
            return DueDateCommitment.objects.create(
                assignment=self.a, proposed_date=new_date,
                proposed_by=self.designer, change_reason=reason, is_current=True)

    def test_extension_without_a_reason_is_refused_by_the_view(self):
        """VERIFICATION 4 (first half) — enforced in design_due_date_propose."""
        from django.test import Client
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.designer.user)
        before = self.a.due_date_commitments.count()
        resp = c.post(f'/design/{self.site.project_id}/due-date/propose/',
                      {'proposed_date': str(self.agreed + timedelta(days=7)),
                       'change_reason': '   '})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.a.due_date_commitments.count(), before,
                         'a blank reason must not create a commitment')

    def test_extension_with_a_reason_creates_exactly_one_current_row(self):
        """VERIFICATION 4 (second half)."""
        new_date = self.agreed + timedelta(days=7)
        ext = self._request_extension(new_date)

        rows = list(self.a.due_date_commitments.order_by('pk'))
        self.assertEqual(len(rows), 2)
        old, new = rows
        self.assertFalse(old.is_current)
        self.assertIsNotNone(old.approved_at)
        self.assertTrue(new.is_current)
        self.assertIsNone(new.approved_at)
        self.assertEqual(new.change_reason, 'needs longer')
        self.assertEqual(
            self.a.due_date_commitments.filter(is_current=True).count(), 1)

    def test_two_current_rows_are_impossible(self):
        """The Part 1 partial unique constraint is what forces the flip-before-insert."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                DueDateCommitment.objects.create(
                    assignment=self.a, proposed_date=self.agreed + timedelta(days=3),
                    proposed_by=self.designer, is_current=True)

    # ---- VERIFICATION 5 — the pending request must not move the effective date ----

    def test_pending_extension_does_not_change_the_effective_date(self):
        new_date = self.agreed + timedelta(days=30)
        self._request_extension(new_date)

        eff = _effective_commitment(self.a)
        self.assertEqual(eff.proposed_date, self.agreed,
                         'the AGREED date must still be in force')
        self.assertEqual(_pending_extension(self.a).proposed_date, new_date)

    def test_pending_extension_does_not_clear_the_overdue_flag(self):
        """The failure this whole split exists to prevent."""
        self.assertTrue(is_overdue(self.a, _effective_commitment(self.a)),
                        'precondition: the site is overdue before the request')
        self._request_extension(timezone.localdate() + timedelta(days=30))
        self.assertTrue(is_overdue(self.a, _effective_commitment(self.a)),
                        'requesting an extension must NOT clear overdue')

    def test_dashboard_and_tender_metrics_both_read_the_approved_date(self):
        """Both Part 5 surfaces, not just one."""
        self._request_extension(timezone.localdate() + timedelta(days=30))

        m = tender_metrics(self.program)
        row = next(s for s in m['sites'] if s['assignment'].pk == self.a.pk)
        self.assertEqual(row['commitment'].proposed_date, self.agreed)
        self.assertTrue(row['overdue'], 'still counted overdue')
        self.assertEqual(row['pending_extension'].proposed_date,
                         timezone.localdate() + timedelta(days=30))

        rows = list(self.a.due_date_commitments.all())
        self.assertEqual(effective_commitment(rows).proposed_date, self.agreed)
        self.assertEqual(pending_extension(rows).proposed_date,
                         timezone.localdate() + timedelta(days=30))

    # ---- VERIFICATION 6 / 7 — reject and approve ----

    def test_rejecting_restores_the_previous_commitment_as_current(self):
        """VERIFICATION 6."""
        from django.test import Client
        self._request_extension(timezone.localdate() + timedelta(days=30))
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.head.user)
        resp = c.post(f'/design/{self.site.project_id}/due-date/reject/',
                      {'reason': 'no'})
        self.assertEqual(resp.status_code, 302)

        current = self.a.due_date_commitments.filter(is_current=True)
        self.assertEqual(current.count(), 1)
        self.assertEqual(current.first().proposed_date, self.agreed)
        self.assertIsNotNone(current.first().approved_at)
        # The refused row is stood down, NOT deleted.
        self.assertEqual(self.a.due_date_commitments.count(), 2)
        self.assertTrue(is_overdue(self.a, _effective_commitment(self.a)))

    def test_approving_makes_the_new_date_effective(self):
        """VERIFICATION 7."""
        from django.test import Client
        future = timezone.localdate() + timedelta(days=30)
        self._request_extension(future)
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.head.user)
        resp = c.post(f'/design/{self.site.project_id}/due-date/approve/', {})
        self.assertEqual(resp.status_code, 302)

        eff = _effective_commitment(self.a)
        self.assertEqual(eff.proposed_date, future)
        self.assertIsNotNone(eff.approved_at)
        self.assertEqual(eff.approved_by_id, self.head.pk)
        self.assertIsNone(_pending_extension(self.a))
        self.assertFalse(is_overdue(self.a, eff),
                         'a future approved date is not overdue')

    def test_released_sites_are_never_overdue(self):
        self.a.status = DESIGN_RELEASED
        self.a.save()
        self.assertFalse(is_overdue(self.a, _effective_commitment(self.a)))


# ===========================================================================
# 9-13. CAD zip
# ===========================================================================

class BrowserMimeTypeTests(TestCase):
    """validate_design_file() must accept the content types real browsers actually send.

    REGRESSION TEST FOR A REPORTED BUG. `.zip` was mapped to the single string
    `application/zip`, but Chrome on Windows reads the type from the HKCR registry entry
    and sends `application/x-zip-compressed`. Every Windows Chrome user — which is most of
    them — got "File content type does not match its extension" and could not upload a CAD
    archive at all. The file was valid; the check was too narrow.

    These are typo guards, not security controls: `content_type` comes from the client and
    is trivially forged. The real protections are the extension whitelist, the size limit,
    and validate_cad_zip(), which opens the archive rather than trusting any header.
    """

    def _check(self, filename, content_type):
        return validate_design_file(
            SimpleUploadedFile(filename, b'x' * 64, content_type=content_type))

    def test_windows_chrome_zip_type_is_accepted(self):
        """The exact string that was being refused."""
        self.assertEqual(self._check('MB-004.zip', 'application/x-zip-compressed'), 'zip')

    def test_every_common_zip_type_is_accepted(self):
        for ct in ('application/zip', 'application/x-zip-compressed', 'application/x-zip',
                   'application/x-compressed', 'multipart/x-zip',
                   'application/octet-stream'):
            self.assertEqual(self._check('cad.zip', ct), 'zip', ct)

    def test_dwg_types_in_the_wild_are_accepted(self):
        for ct in ('application/acad', 'image/vnd.dwg', 'application/x-dwg',
                   'drawing/dwg', 'application/octet-stream'):
            self.assertEqual(self._check('model.dwg', ct), 'dwg', ct)

    def test_a_missing_content_type_is_accepted(self):
        """Some clients send none; refusing would block a valid file over nothing."""
        self.assertEqual(self._check('sheet.pdf', ''), 'pdf')

    def test_the_check_still_catches_a_genuine_mismatch(self):
        with self.assertRaises(DesignStorageError) as ctx:
            self._check('sheet.pdf', 'image/png')
        # The message names BOTH sides, so the uploader can see what went wrong.
        self.assertIn('.pdf', str(ctx.exception))
        self.assertIn('image/png', str(ctx.exception))

    def test_the_extension_whitelist_still_bites(self):
        with self.assertRaises(DesignStorageError):
            self._check('payload.exe', 'application/octet-stream')

    def test_stored_type_is_the_canonical_one_not_what_the_browser_claimed(self):
        """What we ACCEPT is wide; what we STORE is the one correct value."""
        self.assertEqual(DESIGN_MIME_TYPE_MAP['zip'], 'application/zip')
        self.assertIn('application/x-zip-compressed', DESIGN_ACCEPTED_MIME_TYPES['zip'])
        self.assertNotIn('application/x-zip-compressed', DESIGN_MIME_TYPE_MAP.values())


class CadZipValidationTests(TestCase):

    def test_zip_with_pdf_and_dwg_is_accepted_and_listed(self):
        """VERIFICATION 9."""
        f = _zip_bytes([('01-layout.pdf', b'%PDF-1.4 xxxx'),
                        ('01-layout.dwg', b'AC1032 yyyy')])
        listing = validate_cad_zip(f)
        names = sorted(e['name'] for e in listing)
        self.assertEqual(names, ['01-layout.dwg', '01-layout.pdf'])
        self.assertTrue(all(e['size'] > 0 for e in listing))

    def test_case_insensitive_extensions(self):
        f = _zip_bytes([('SHEET.PDF', b'x' * 10), ('MODEL.DWG', b'y' * 10)])
        self.assertEqual(len(validate_cad_zip(f)), 2)

    def test_zip_with_no_dwg_is_refused_naming_what_is_missing(self):
        """VERIFICATION 10."""
        f = _zip_bytes([('01-layout.pdf', b'%PDF-1.4')])
        with self.assertRaises(DesignStorageError) as ctx:
            validate_cad_zip(f)
        self.assertIn('.DWG', str(ctx.exception))
        self.assertNotIn('.PDF file', str(ctx.exception))

    def test_zip_with_no_pdf_is_refused_naming_what_is_missing(self):
        f = _zip_bytes([('01-layout.dwg', b'AC1032')])
        with self.assertRaises(DesignStorageError) as ctx:
            validate_cad_zip(f)
        self.assertIn('.PDF', str(ctx.exception))

    def test_zip_missing_both_names_both(self):
        f = _zip_bytes([('notes.txt', b'hello')])
        with self.assertRaises(DesignStorageError) as ctx:
            validate_cad_zip(f)
        self.assertIn('.PDF', str(ctx.exception))
        self.assertIn('.DWG', str(ctx.exception))

    def test_non_zip_renamed_zip_is_refused(self):
        """VERIFICATION 11."""
        f = SimpleUploadedFile('cad.zip', b'this is plainly not a zip archive',
                               content_type='application/zip')
        with self.assertRaises(DesignStorageError) as ctx:
            validate_cad_zip(f)
        self.assertIn('not a readable zip archive', str(ctx.exception))

    def test_archive_exceeding_the_uncompressed_limit_is_refused(self):
        """VERIFICATION 12 — refused WITHOUT decompressing.

        A 300 MB run of one repeated byte deflates to a few hundred KB, so the upload
        itself is tiny; only the declared uncompressed total is over the limit. If the
        validator decompressed to measure, this test would need 300 MB of memory.
        """
        oversized = MAX_CAD_ZIP_UNCOMPRESSED_BYTES + (100 * 1024 * 1024)
        f = _zip_bytes([('01-layout.pdf', b'%PDF'),
                        ('01-layout.dwg', b'AC1032'),
                        ('bomb.bin', b'\0' * oversized)])
        self.assertLess(f.size, 5 * 1024 * 1024,
                        'the archive itself must stay small — else this is not a bomb test')
        with self.assertRaises(DesignStorageError) as ctx:
            validate_cad_zip(f)
        self.assertIn('uncompressed', str(ctx.exception))

    def test_directory_entries_do_not_count_as_contents(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('drawings/', b'')
            zf.writestr('drawings/a.pdf', b'%PDF')
            zf.writestr('drawings/a.dwg', b'AC1032')
        buf.seek(0)
        f = SimpleUploadedFile('cad.zip', buf.read(), content_type='application/zip')
        listing = validate_cad_zip(f)
        self.assertEqual(len(listing), 2, 'the directory entry must not be listed')

    def test_empty_archive_is_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w'):
            pass
        buf.seek(0)
        f = SimpleUploadedFile('cad.zip', buf.read(), content_type='application/zip')
        with self.assertRaises(DesignStorageError):
            validate_cad_zip(f)


class ProgressionRuleTests(Part8Base):
    """VERIFICATION 13 and 14."""

    def setUp(self):
        super().setUp()
        self.site, self.a = self._site('P8-PROG')
        self.a.assigned_to = self.designer
        self.a.status = DESIGN_ARKA_SUBMITTED
        self.a.save()
        self.attempt = DesignAttempt.objects.create(
            assignment=self.a, attempt_number=1, opened_reason='initial')
        # PART 9: an Arka is "approved" for artifact purposes only once BOTH gates have
        # passed it — _approved_arka() tests head_verdict, not verdict. This fixture sets
        # both because the progression rule under test is Part 8's and needs a fully
        # approved Arka to fire; setting only `verdict` would now leave the Arka sitting
        # at gate 2 and the rule would correctly refuse to advance.
        self.arka = ArkaSubmission.objects.create(
            attempt=self.attempt, version=1, capacity_kw=Decimal('100'),
            arka_link='https://example.com/a', submitted_by=self.designer,
            verdict=ARKA_APPROVED, head_verdict=ARKA_APPROVED, is_current=True)
        self.attempt.boq_submitted_at = timezone.now()
        self.attempt.boq_submitted_by = self.designer
        self.attempt.save()

    def _file(self, kind, listing=None):
        return DesignFile.objects.create(
            attempt=self.attempt, kind=kind, version=1, bucket='b',
            path=f'p/{kind}', original_filename=f'f.{kind}',
            archive_listing=listing or [],
            derived_from_arka=self.arka, uploaded_by=self.designer, is_current=True)

    def test_valid_cad_zip_plus_arka_and_boq_reaches_artifacts_uploaded(self):
        """VERIFICATION 13."""
        self._file(DESIGN_FILE_CAD_ZIP,
                   [{'name': 'a.pdf', 'size': 10}, {'name': 'a.dwg', 'size': 20}])
        advanced = _maybe_advance_to_artifacts_uploaded(self.a, self.attempt, self.head)
        self.assertTrue(advanced)
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_ARTIFACTS_UPLOADED)

    def test_legacy_cad_pdf_alone_does_not_advance(self):
        self._file(DESIGN_FILE_CAD_PDF)
        self.assertFalse(
            _maybe_advance_to_artifacts_uploaded(self.a, self.attempt, self.head))
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_ARKA_SUBMITTED)

    def test_legacy_rows_remain_readable(self):
        """VERIFICATION 14 — the legacy kinds keep their labels and stay queryable."""
        pdf = self._file(DESIGN_FILE_CAD_PDF)
        dwg = self._file(DESIGN_FILE_CAD_DWG)
        self.assertEqual(pdf.get_kind_display(), 'CAD (PDF) — legacy')
        self.assertEqual(dwg.get_kind_display(), 'CAD (DWG) — legacy')
        # They carry an empty listing, which reads as "not an archive".
        self.assertEqual(pdf.archive_listing, [])
        # And they are still fetchable by pk, which is what download needs.
        self.assertEqual(DesignFile.objects.filter(pk__in=[pdf.pk, dwg.pk]).count(), 2)

    def test_progression_kinds_excludes_legacy(self):
        self.assertEqual(tuple(PROGRESSION_CAD_KINDS), (DESIGN_FILE_CAD_ZIP,))
