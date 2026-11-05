"""
TD-B1 — tender documents: models, storage wrapper and permission helpers.

WHAT IS PINNED HERE
-------------------
  enabled      OPEX + Program + not Cancelled + not deleted (R2, R8); Residential,
               CAPEX-with-a-Program, Cancelled and deleted sites refused; a test-flagged
               OPEX site enabled
  view matrix  SE on S1 reads Site team, not Management only; an SE elsewhere reads
               neither; the coordinator and PM of S1 are management and manage the
               tender's documents; a PM of another tender is neither; SCM, Finance, CEO,
               Admin are management; System Admin is NOT (R4) and manages nothing
  rows         voided, superseded and another tender's documents are never viewable;
               the replacement of a superseded row is; history is management only
  constraints  site_team needs the no-prices pair; void fields all-or-none with a
               reason of >= 10 characters; superseded_by needs superseded_at; no
               self-supersede
  storage      refused with the bucket unset (no network call); a real PDF is stored at
               program/<id>/<uuid4>.pdf with the canonical type; a fake PDF is refused
               before upload; links are signed for exactly 300 s in the bucket
               recorded on the row, even after the setting changes; an open writes one
               TenderDocumentOpen row and no ActivityLog row (R5)

Storage is mocked at tender_documents' imports — no test reaches a bucket.

Run with:
    python manage.py test projects.tests_tender_documents --settings=solarpms.test_settings
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from .delivery_storage import DeliveryStorageError
from .models import (
    TENDER_DOC_MANAGEMENT, TENDER_DOC_SITE_TEAM, ActivityLog, Program, Project,
    ProjectPhase, Task, TenderDocument, TenderDocumentOpen,
)
from .permissions import (
    TENDER_DOC_MANAGEMENT_ROLES, tender_docs_enabled_for_site,
    user_can_manage_tender_documents, user_can_view_tender_document,
    user_can_view_tender_document_history, user_is_tender_doc_management,
)
from .tender_documents import (
    TENDER_DOCUMENT_LINK_SECONDS, log_tender_document_open, signed_tender_document_url,
    store_tender_document_file,
)

CLIENT = 'projects.tender_documents._client'
SIGNER = 'projects.tender_documents.get_design_file_url'
BUCKET = 'test-delivery-bucket'
PDF_BYTES = b'%PDF-1.4 tender document stub'


def _profile(username, role):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.save()
    return profile


class TenderDocumentBase(TestCase):
    """Two OPEX tenders and a CAPEX one.

    P1  S1 (Active; pm1 its PM, coord its coordinator, se1 holds a task), S_cancel
        (Cancelled), S_test (is_test), S_deleted (soft-deleted).
    P2  S2 (pm2 its PM, se2 holds a task).
    PC  a CAPEX Program with one CAPEX site, C1.
    R1  a Residential site, no Program.
    Documents on P1: site_doc (Site team), mgmt_doc (Management only), voided_doc,
    old_doc superseded by new_doc. On P2: other_doc (Site team).
    """

    def setUp(self):
        self.pm1      = _profile('td_pm1', 'PM')
        self.pm2      = _profile('td_pm2', 'PM')
        self.coord    = _profile('td_coord', 'Project Coordinator')
        self.se1      = _profile('td_se1', 'Site Engineer')
        self.se2      = _profile('td_se2', 'Site Engineer')
        self.scm      = _profile('td_scm', 'SCM')
        self.finance  = _profile('td_fin', 'Finance')
        self.ceo      = _profile('td_ceo', 'CEO')
        self.admin    = _profile('td_admin', 'Admin')
        self.sysadmin = _profile('td_sysadmin', 'System Admin')

        self.p1 = self._program('TD-P1', 'OPEX', 'TDP1')
        self.p2 = self._program('TD-P2', 'OPEX', 'TDP2')
        self.pc = self._program('TD-PC', 'CAPEX', '')

        self.s1 = self._site('TDP1-S1', 'OPEX', self.p1, self.pm1)
        self.s1.coordinators.add(self.coord)
        self._task(self.s1, self.se1)
        self.s_cancel = self._site('TDP1-SX', 'OPEX', self.p1, self.pm1, status='Cancelled')
        self.s_test = self._site('TDP1-ST', 'OPEX', self.p1, self.pm1, is_test=True)
        self.s_deleted = self._site('TDP1-SD', 'OPEX', self.p1, self.pm1, is_deleted=True)

        self.s2 = self._site('TDP2-S2', 'OPEX', self.p2, self.pm2)
        self._task(self.s2, self.se2)

        self.c1 = self._site('TDPC-C1', 'CAPEX', self.pc, self.pm1)
        self.r1 = self._site('TDR-R1', 'Residential', None, self.pm1)

        self.site_doc = self._doc(self.p1, 'Site team spec', TENDER_DOC_SITE_TEAM)
        self.mgmt_doc = self._doc(self.p1, 'Management letter', TENDER_DOC_MANAGEMENT)
        self.voided_doc = self._doc(self.p1, 'Wrong file', TENDER_DOC_SITE_TEAM)
        TenderDocument.objects.filter(pk=self.voided_doc.pk).update(
            voided_at=timezone.now(), voided_by=self.admin,
            void_reason='Uploaded to the wrong tender')
        self.voided_doc.refresh_from_db()
        self.new_doc = self._doc(self.p1, 'Spec v2', TENDER_DOC_SITE_TEAM)
        self.old_doc = self._doc(self.p1, 'Spec v1', TENDER_DOC_SITE_TEAM)
        TenderDocument.objects.filter(pk=self.old_doc.pk).update(
            superseded_by=self.new_doc, superseded_at=timezone.now())
        self.old_doc.refresh_from_db()
        self.other_doc = self._doc(self.p2, 'Other tender spec', TENDER_DOC_SITE_TEAM)

    # -- fixtures -----------------------------------------------------------------

    def _program(self, name, program_type, code):
        return Program.objects.create(name=name, program_type=program_type,
                                      client_name='TD Client', status='Active',
                                      short_tender_code=code)

    def _site(self, code, project_type, program, pm, status='Active', is_test=False,
              is_deleted=False):
        site = Project(
            project_id=code, customer_name='TD Client', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type=project_type,
            program=program, site_code=code if program else None,
            dc_capacity_kw=Decimal('100.00'), status=status, assigned_pm=pm,
            is_test=is_test, is_deleted=is_deleted)
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


class EnabledForSiteTests(TenderDocumentBase):
    """R2, R8: which sites take the card at all."""

    def test_opex_site_under_a_program_is_enabled(self):
        self.assertTrue(tender_docs_enabled_for_site(self.s1))

    def test_test_flagged_opex_site_is_enabled(self):
        self.assertTrue(tender_docs_enabled_for_site(self.s_test))

    def test_residential_site_is_not_enabled(self):
        self.assertFalse(tender_docs_enabled_for_site(self.r1))

    def test_capex_site_with_a_program_is_not_enabled(self):
        self.assertIsNotNone(self.c1.program_id)
        self.assertFalse(tender_docs_enabled_for_site(self.c1))

    def test_cancelled_site_is_not_enabled(self):
        self.assertFalse(tender_docs_enabled_for_site(self.s_cancel))

    def test_soft_deleted_site_is_not_enabled(self):
        self.assertFalse(tender_docs_enabled_for_site(self.s_deleted))

    def test_none_is_not_enabled(self):
        self.assertFalse(tender_docs_enabled_for_site(None))


class ViewMatrixTests(TenderDocumentBase):
    """R4, R5 of the view matrix, against S1 in P1."""

    def assertSees(self, profile, document, project=None):
        self.assertTrue(user_can_view_tender_document(profile.user, document,
                                                      project or self.s1))

    def assertBlind(self, profile, document, project=None):
        self.assertFalse(user_can_view_tender_document(profile.user, document,
                                                       project or self.s1))

    def test_se_on_s1_sees_site_team_but_not_management(self):
        self.assertSees(self.se1, self.site_doc)
        self.assertBlind(self.se1, self.mgmt_doc)
        self.assertFalse(user_is_tender_doc_management(self.se1.user, self.s1))

    def test_se_on_a_site_in_another_program_sees_nothing_of_s1(self):
        self.assertBlind(self.se2, self.site_doc)
        self.assertBlind(self.se2, self.mgmt_doc)
        # Nor is P1's document shown on the SE's own site's card.
        self.assertBlind(self.se2, self.site_doc, project=self.s2)

    def test_coordinator_of_s1_is_management_and_manages_documents(self):
        self.assertTrue(user_is_tender_doc_management(self.coord.user, self.s1))
        self.assertSees(self.coord, self.mgmt_doc)
        self.assertSees(self.coord, self.site_doc)
        self.assertTrue(user_can_manage_tender_documents(self.coord.user, self.p1))

    def test_pm_of_s1_is_management_and_manages_documents(self):
        self.assertTrue(user_is_tender_doc_management(self.pm1.user, self.s1))
        self.assertTrue(user_can_manage_tender_documents(self.pm1.user, self.p1))

    def test_pm_of_another_program_only_neither_sees_nor_manages(self):
        self.assertFalse(user_is_tender_doc_management(self.pm2.user, self.s1))
        self.assertBlind(self.pm2, self.mgmt_doc)
        self.assertBlind(self.pm2, self.site_doc)
        self.assertFalse(user_can_manage_tender_documents(self.pm2.user, self.p1))
        # ...while managing their own tender's documents.
        self.assertTrue(user_can_manage_tender_documents(self.pm2.user, self.p2))

    def test_scm_finance_ceo_admin_are_management(self):
        for profile in (self.scm, self.finance, self.ceo, self.admin):
            with self.subTest(role=profile.role):
                self.assertTrue(user_is_tender_doc_management(profile.user, self.s1))
                self.assertSees(profile, self.mgmt_doc)
                self.assertSees(profile, self.site_doc)

    def test_system_admin_is_not_management(self):
        self.assertNotIn(self.sysadmin.role, TENDER_DOC_MANAGEMENT_ROLES)
        self.assertFalse(user_is_tender_doc_management(self.sysadmin.user, self.s1))
        self.assertBlind(self.sysadmin, self.mgmt_doc)
        self.assertFalse(user_can_view_tender_document_history(self.sysadmin.user, self.s1))
        # System Admin sees the site, so reads what the whole site team reads.
        self.assertSees(self.sysadmin, self.site_doc)

    def test_only_admin_and_site_managers_manage_documents(self):
        self.assertTrue(user_can_manage_tender_documents(self.admin.user, self.p1))
        for profile in (self.scm, self.finance, self.ceo, self.sysadmin, self.se1):
            with self.subTest(role=profile.role):
                self.assertFalse(user_can_manage_tender_documents(profile.user, self.p1))

    def test_capex_program_documents_are_managed_by_nobody(self):
        self.assertFalse(user_can_manage_tender_documents(self.admin.user, self.pc))
        self.assertFalse(user_can_manage_tender_documents(self.pm1.user, self.pc))

    def test_nothing_is_viewable_on_a_site_without_the_card(self):
        for site in (self.s_cancel, self.r1, self.c1):
            with self.subTest(site=site.project_id):
                self.assertBlind(self.admin, self.site_doc, project=site)


class DocumentRowTests(TenderDocumentBase):
    """R7: voided, superseded and foreign rows are never on the card; history is
    management only."""

    def test_voided_document_is_not_viewable_even_by_admin(self):
        self.assertBlind_all(self.voided_doc)

    def test_superseded_document_is_not_viewable_but_its_replacement_is(self):
        self.assertBlind_all(self.old_doc)
        self.assertTrue(user_can_view_tender_document(self.se1.user, self.new_doc, self.s1))

    def test_document_from_another_program_is_not_viewable_on_s1(self):
        self.assertBlind_all(self.other_doc)

    def test_history_is_management_only(self):
        for profile in (self.pm1, self.coord, self.scm, self.finance, self.ceo, self.admin):
            with self.subTest(role=profile.role, user=profile.user.username):
                self.assertTrue(user_can_view_tender_document_history(profile.user, self.s1))
        for profile in (self.se1, self.sysadmin, self.pm2):
            with self.subTest(role=profile.role, user=profile.user.username):
                self.assertFalse(user_can_view_tender_document_history(profile.user, self.s1))

    def test_history_is_closed_where_the_card_is_hidden(self):
        self.assertFalse(user_can_view_tender_document_history(self.admin.user, self.s_cancel))

    def assertBlind_all(self, document):
        for profile in (self.admin, self.pm1, self.se1):
            with self.subTest(role=profile.role):
                self.assertFalse(user_can_view_tender_document(profile.user, document,
                                                               self.s1))


class ConstraintTests(TenderDocumentBase):
    """The database CHECKs, each against a direct write."""

    def assertRefused(self, **fields):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._doc(self.p1, 'Refused', TENDER_DOC_MANAGEMENT, **fields)

    def test_site_team_without_no_prices_fields_is_refused(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TenderDocument.objects.create(
                    program=self.p1, title='No tick', category='hse',
                    visibility=TENDER_DOC_SITE_TEAM, storage_bucket=BUCKET,
                    storage_path='program/x/a.pdf',
                    original_filename='a.pdf', content_type='application/pdf',
                    size_bytes=10, uploaded_by=self.pm1)

    def test_half_a_no_prices_confirmation_is_refused(self):
        self.assertRefused(no_prices_confirmed_by=self.pm1)

    def test_partial_void_fields_are_refused(self):
        self.assertRefused(voided_at=timezone.now())
        self.assertRefused(voided_at=timezone.now(), voided_by=self.admin)
        self.assertRefused(void_reason='Wrong tender entirely')

    def test_void_reason_of_nine_characters_is_refused(self):
        self.assertRefused(voided_at=timezone.now(), voided_by=self.admin,
                           void_reason='123456789')

    def test_void_reason_of_ten_characters_is_accepted(self):
        doc = self._doc(self.p1, 'Ten', TENDER_DOC_MANAGEMENT, voided_at=timezone.now(),
                        voided_by=self.admin, void_reason='1234567890')
        self.assertIsNotNone(doc.pk)

    def test_superseded_by_without_superseded_at_is_refused(self):
        self.assertRefused(superseded_by=self.new_doc)

    def test_superseded_at_without_superseded_by_is_refused(self):
        self.assertRefused(superseded_at=timezone.now())

    def test_a_document_cannot_supersede_itself(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TenderDocument.objects.filter(pk=self.mgmt_doc.pk).update(
                    superseded_by=self.mgmt_doc, superseded_at=timezone.now())

    def test_default_visibility_is_management_only(self):
        doc = TenderDocument.objects.create(
            program=self.p1, title='Default', category='other',
            storage_bucket=BUCKET, storage_path='program/x/default.pdf',
            original_filename='d.pdf',
            content_type='application/pdf', size_bytes=10, uploaded_by=self.pm1)
        self.assertEqual(doc.visibility, TENDER_DOC_MANAGEMENT)


class StorageWrapperTests(TenderDocumentBase):
    """tender_documents.py with the client and signer mocked."""

    def _pdf(self, name='Spec.PDF', content=PDF_BYTES):
        return SimpleUploadedFile(name, content, content_type='application/pdf')

    @override_settings(SUPABASE_DELIVERY_BUCKET='')
    def test_store_is_refused_when_the_bucket_is_not_configured(self):
        with patch(CLIENT) as client:
            with self.assertRaises(DeliveryStorageError) as caught:
                store_tender_document_file(self.p1.pk, self._pdf())
        self.assertIn('not configured', str(caught.exception))
        client.assert_not_called()

    @override_settings(SUPABASE_DELIVERY_BUCKET=BUCKET)
    def test_store_uploads_a_real_pdf_under_the_program_prefix(self):
        with patch(CLIENT) as client:
            bucket, path, content_type, size = store_tender_document_file(
                self.p1.pk, self._pdf())
        self.assertEqual(bucket, BUCKET)
        self.assertRegex(path, rf'^program/{self.p1.pk}/[0-9a-f]{{32}}\.pdf$')
        self.assertEqual(content_type, 'application/pdf')
        self.assertEqual(size, len(PDF_BYTES))
        client.return_value.storage.from_.assert_called_once_with(BUCKET)
        client.return_value.storage.from_.return_value.upload.assert_called_once_with(
            path=path, file=PDF_BYTES, file_options={'content-type': 'application/pdf'})

    @override_settings(SUPABASE_DELIVERY_BUCKET=BUCKET)
    def test_store_refuses_a_file_whose_bytes_are_not_a_pdf(self):
        with patch(CLIENT) as client:
            with self.assertRaises(DeliveryStorageError):
                store_tender_document_file(self.p1.pk,
                                           self._pdf(content=b'not really a pdf'))
        client.assert_not_called()

    @override_settings(SUPABASE_DELIVERY_BUCKET=BUCKET)
    def test_signed_url_is_minted_for_300_seconds_in_the_recorded_bucket(self):
        self.assertEqual(TENDER_DOCUMENT_LINK_SECONDS, 300)
        with patch(SIGNER, return_value='https://signed.example/x') as signer:
            url = signed_tender_document_url(self.site_doc)
        self.assertEqual(url, 'https://signed.example/x')
        signer.assert_called_once_with(BUCKET, self.site_doc.storage_path, expires_in=300)

    @override_settings(SUPABASE_DELIVERY_BUCKET='renamed-delivery-bucket')
    def test_signed_url_uses_the_row_bucket_after_the_setting_changes(self):
        # TD-B1a: the file was stored in BUCKET; the setting now names another bucket.
        # The link is still signed where the file is.
        with patch(SIGNER, return_value='https://signed.example/y') as signer:
            url = signed_tender_document_url(self.site_doc)
        self.assertEqual(url, 'https://signed.example/y')
        signer.assert_called_once_with(BUCKET, self.site_doc.storage_path, expires_in=300)

    @override_settings(SUPABASE_DELIVERY_BUCKET='')
    def test_signed_url_ignores_an_unset_setting_when_the_row_has_a_bucket(self):
        with patch(SIGNER, return_value='https://signed.example/z') as signer:
            self.assertEqual(signed_tender_document_url(self.site_doc),
                             'https://signed.example/z')
        signer.assert_called_once_with(BUCKET, self.site_doc.storage_path, expires_in=300)

    @override_settings(SUPABASE_DELIVERY_BUCKET=BUCKET)
    def test_signed_url_is_none_when_the_row_has_no_bucket(self):
        # The column is NOT NULL but may hold ''; an unsaved instance stands in for it.
        document = TenderDocument(storage_bucket='', storage_path='program/1/x.pdf')
        with patch(SIGNER) as signer:
            self.assertIsNone(signed_tender_document_url(document))
        signer.assert_not_called()

    def test_an_open_is_logged_to_its_own_table_and_never_the_timeline(self):
        activity_before = ActivityLog.objects.count()
        row = log_tender_document_open(self.site_doc, self.se1.user, self.s1)
        self.assertEqual(TenderDocumentOpen.objects.count(), 1)
        self.assertEqual((row.document, row.user, row.project),
                         (self.site_doc, self.se1, self.s1))
        self.assertIsNotNone(row.opened_at)
        self.assertEqual(ActivityLog.objects.count(), activity_before)

    def test_no_role_or_status_string_in_the_storage_wrapper(self):
        import projects.tender_documents as module
        with open(module.__file__, encoding='utf-8') as handle:
            source = handle.read()
        for word in ('Admin', 'SCM', 'Finance', 'CEO', 'Site Engineer', 'Cancelled',
                     "'PM'", 'Project Coordinator', '.role'):
            with self.subTest(word=word):
                self.assertNotIn(word, source)
