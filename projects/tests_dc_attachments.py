"""
DC-B2: the vendor's challan documents on a delivery challan — attached at create,
opened through a signed link, withdrawn (never deleted) by SCM or Admin.

WHAT IS PINNED HERE:
  * create: a PDF, a JPG and a PNG are accepted and stored with their canonical type;
    a wrong type, a fake extension, an empty file, a file over 20 MB, more than ten
    files, or files without the no-prices tick refuse the WHOLE challan — nothing
    uploaded, nothing written; storage off refuses only when a file is chosen; a
    challan with no file is created exactly as before; a failure inside the
    transaction discards what was uploaded; a refused form says files must be attached
    again and never comes back with the tick set;
  * open: the detail page's audience opens a live file (redirect to a link signed for
    300 s, one activity row per open); a withdrawn one is a 404 for everyone but SCM
    and Admin, as is another challan's or another project's file and any user the
    detail page would not admit;
  * withdraw: SCM and Admin only, POST only, reason of at least ten characters, never
    twice; the stored object is never removed; one activity row;
  * detail page: live list, "No challan document attached", Withdraw control and the
    withdrawn list for SCM/Admin only; the documents cost exactly one query;
  * the overview's Recent activity hides opens and shows withdrawals;
  * the model: both CHECKs, append-only save/delete; DC_DETAIL_ROLES matches the role
    tuple inline in delivery_challan_detail.

Storage is never reached: delivery_storage._client and design_storage._client are both
patched with one mock, which records every upload, remove and signing call.

Run with:
    python manage.py test projects.tests_dc_attachments --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from .delivery_storage import DC_DOCUMENT_LINK_SECONDS, DC_DOCUMENT_MAX_BYTES
from .models import (
    ActivityLog, AppendOnlyViolation, DCLineItem, DeliveryChallan,
    DeliveryChallanAttachment, Project, ProjectPhase, Task, UserProfile,
)
from .permissions import (
    DC_DETAIL_ROLES, user_can_manage_dc_documents, user_can_open_dc_document,
)

BUCKET = 'Horizon-PMS-Delivery-test'
SIGNED = 'https://signed.example/challan?token=t'

PDF = b'%PDF-1.4 challan\n'
JPG = b'\xff\xd8\xff\xe0\x00\x10JFIF challan'
PNG = b'\x89PNG\r\n\x1a\n challan'


def _profile(username, role):
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title(), last_name='Test')
    profile = user.profile          # auto-created by the post_save signal
    profile.role = role
    profile.save(update_fields=['role'])
    return profile


def _file(name, content, mime):
    return SimpleUploadedFile(name, content, content_type=mime)


def _project(project_id, pm=None):
    return Project.objects.create(
        project_id=project_id, customer_name=project_id, customer_phone='9876543210',
        site_address='1 Sun Road', city='Delhi', project_type='Residential',
        dc_capacity_kw=Decimal('5.00'), status='Active', assigned_pm=pm,
        target_commissioning_date=date.today() + timedelta(days=90))


@override_settings(SUPABASE_DELIVERY_BUCKET=BUCKET)
class DcAttachmentBase(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.scm = _profile('dca_scm', 'SCM')
        cls.admin = _profile('dca_admin', 'Admin')
        cls.ceo = _profile('dca_ceo', 'CEO')
        cls.sysadmin = _profile('dca_sysadmin', 'System Admin')
        cls.finance = _profile('dca_fin', 'Finance')
        cls.pm = _profile('dca_pm', 'PM')
        cls.se = _profile('dca_se', 'Site Engineer')
        cls.se_elsewhere = _profile('dca_se2', 'Site Engineer')
        cls.site = _project('DCA-SITE', pm=cls.pm)
        cls.other = _project('DCA-OTHER')
        phase = ProjectPhase.objects.create(project=cls.site, phase_name='Install',
                                            phase_order=1)
        Task.objects.create(phase=phase, task_name='Receive material', task_order=1,
                            assigned_role=Task.SITE_ENGINEER, assigned_to=cls.se)
        other_phase = ProjectPhase.objects.create(project=cls.other, phase_name='Install',
                                                  phase_order=1)
        Task.objects.create(phase=other_phase, task_name='Receive material', task_order=1,
                            assigned_role=Task.SITE_ENGINEER, assigned_to=cls.se_elsewhere)
        cls.dc = DeliveryChallan.objects.create(project=cls.site, dc_number='DCA-1',
                                                dc_date=date.today())
        cls.dc2 = DeliveryChallan.objects.create(project=cls.site, dc_number='DCA-2',
                                                 dc_date=date.today())
        cls.other_dc = DeliveryChallan.objects.create(project=cls.other, dc_number='OTH-1',
                                                      dc_date=date.today())

    def setUp(self):
        self.storage = mock.MagicMock()
        self.bucket_api = self.storage.storage.from_.return_value
        self.bucket_api.create_signed_url.return_value = {'signedURL': SIGNED}
        for target in ('projects.delivery_storage._client', 'projects.design_storage._client'):
            patcher = mock.patch(target, return_value=self.storage)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = Client(SERVER_NAME='localhost')

    def login(self, profile):
        self.client.force_login(profile.user)

    @classmethod
    def attach(cls, challan, name='vendor-dc.pdf', uploader=None, withdrawn_by=None):
        row = DeliveryChallanAttachment.objects.create(
            challan=challan, uploaded_by=uploader or cls.scm,
            original_name=name, bucket=BUCKET,
            path=f'{challan.project.project_id}/challan/{challan.pk}-{name}',
            size=2048, mime='application/pdf', no_prices_confirmed=True)
        if withdrawn_by is not None:
            DeliveryChallanAttachment.objects.filter(pk=row.pk).update(
                withdrawn_at=timezone.now(), withdrawn_by=withdrawn_by,
                withdrawn_reason='Shows unit rates on page 2')
            row.refresh_from_db()
        return row

    def uploads(self):
        return self.bucket_api.upload.call_args_list

    def removes(self):
        return self.bucket_api.remove.call_args_list


class CreateWithDocumentsTests(DcAttachmentBase):

    def setUp(self):
        super().setUp()
        self.login(self.scm)
        self.url = reverse('create_delivery_challan', args=[self.site.project_id])

    def post(self, files=(), tick=True, **overrides):
        data = {
            'dc_number': 'NEW-1', 'dc_date': date.today().isoformat(),
            'line_item_category_0': DCLineItem.CATEGORY_CHOICES[0][0],
            'line_item_description_0': 'Mono PERC 540 Wp', 'line_item_qty_0': '12',
            'line_item_unit_0': 'Nos',
        }
        if tick:
            data['no_prices_confirmed'] = '1'
        data.update(overrides)
        if files:
            data['challan_documents'] = list(files)
        return self.client.post(self.url, data)

    def assert_refused_clean(self, response, text):
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, text)
        self.assertFalse(DeliveryChallan.objects.filter(dc_number='NEW-1').exists())
        self.assertFalse(DeliveryChallanAttachment.objects.exists())
        self.assertEqual(self.uploads(), [])

    def test_pdf_jpg_and_png_are_stored_with_their_canonical_types(self):
        response = self.post([_file('dc.pdf', PDF, 'application/pdf'),
                              _file('dc-photo.jpg', JPG, 'image/jpeg'),
                              _file('dc-scan.png', PNG, 'image/png')])
        challan = DeliveryChallan.objects.get(dc_number='NEW-1')
        self.assertRedirects(response, reverse('delivery_challan_detail',
                                               args=[self.site.project_id, challan.pk]),
                             fetch_redirect_response=False)
        rows = list(challan.attachments.order_by('pk'))
        self.assertEqual([r.original_name for r in rows],
                         ['dc.pdf', 'dc-photo.jpg', 'dc-scan.png'])
        self.assertEqual([r.mime for r in rows],
                         ['application/pdf', 'image/jpeg', 'image/png'])
        for row in rows:
            self.assertEqual(row.bucket, BUCKET)
            self.assertTrue(row.path.startswith('DCA-SITE/challan/'))
            # The uploader's file name never reaches the storage path (uuid filename).
            self.assertNotIn(row.original_name.rsplit('.', 1)[0], row.path)
            self.assertTrue(row.no_prices_confirmed)
            self.assertEqual(row.uploaded_by, self.scm)
            self.assertIsNone(row.withdrawn_at)
        self.assertEqual(len(self.uploads()), 3)
        self.assertEqual([c.kwargs['file_options']['content-type'] for c in self.uploads()],
                         ['application/pdf', 'image/jpeg', 'image/png'])
        self.storage.storage.from_.assert_called_with(BUCKET)

    def test_a_challan_without_documents_is_created_as_before(self):
        response = self.post(tick=False)
        self.assertEqual(response.status_code, 302)
        challan = DeliveryChallan.objects.get(dc_number='NEW-1')
        self.assertEqual(challan.line_items.count(), 1)
        self.assertFalse(challan.attachments.exists())
        self.storage.storage.from_.assert_not_called()

    def test_a_wrong_type_is_refused_with_nothing_uploaded(self):
        response = self.post([_file('dc.pdf', PDF, 'application/pdf'),
                              _file('rates.docx', b'PK\x03\x04', 'application/msword')])
        self.assert_refused_clean(response, 'rates.docx: must be a PDF, JPG or PNG.')

    def test_a_fake_extension_is_refused_by_its_bytes(self):
        response = self.post([_file('dc.pdf', PNG, 'application/pdf')])
        self.assert_refused_clean(response, 'the contents are not a .pdf file')
        response = self.post([_file('dc.png', JPG, 'image/png')])
        self.assert_refused_clean(response, 'the contents are not a .png file')

    def test_a_mime_type_that_contradicts_the_extension_is_refused(self):
        response = self.post([_file('dc.jpg', JPG, 'application/pdf')])
        self.assert_refused_clean(response, 'this does not look like a .jpg file')

    def test_an_empty_file_is_refused(self):
        response = self.post([_file('dc.pdf', b'', 'application/pdf')])
        self.assert_refused_clean(response, 'dc.pdf: the file is empty.')

    def test_a_file_over_20_mb_is_refused_with_nothing_uploaded(self):
        big = PDF + b'0' * (DC_DOCUMENT_MAX_BYTES + 1 - len(PDF))
        response = self.post([_file('dc.pdf', PDF, 'application/pdf'),
                              _file('huge.pdf', big, 'application/pdf')])
        self.assert_refused_clean(response, 'huge.pdf: 20.0 MB — the limit is 20 MB.')

    def test_more_than_ten_files_are_refused(self):
        files = [_file(f'dc-{n}.pdf', PDF, 'application/pdf') for n in range(11)]
        response = self.post(files)
        self.assert_refused_clean(response, 'At most 10 challan documents can be attached')

    def test_ten_files_are_accepted(self):
        files = [_file(f'dc-{n}.pdf', PDF, 'application/pdf') for n in range(10)]
        self.post(files)
        self.assertEqual(DeliveryChallanAttachment.objects.count(), 10)

    def test_files_without_the_no_prices_tick_are_refused(self):
        response = self.post([_file('dc.pdf', PDF, 'application/pdf')], tick=False)
        self.assert_refused_clean(response, 'These documents show no rates, values or amounts')
        self.assertContains(response, 'Site Engineers will see them. Nothing was saved.')

    def test_a_refused_form_with_files_says_attach_again_and_is_never_pre_ticked(self):
        response = self.post([_file('dc.pdf', PDF, 'application/pdf')], dc_number='')
        self.assert_refused_clean(response, 'The files you chose were not kept.')
        self.assertContains(
            response, '<input class="form-check-input" type="checkbox" '
                      'name="no_prices_confirmed" value="1" id="noPricesConfirmed">',
            html=False)

    def test_a_refused_form_without_files_has_no_attach_again_note(self):
        response = self.post(dc_number='')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'The files you chose were not kept.')

    @override_settings(SUPABASE_DELIVERY_BUCKET='')
    def test_storage_off_refuses_a_chosen_file(self):
        response = self.post([_file('dc.pdf', PDF, 'application/pdf')])
        self.assert_refused_clean(response, 'the private delivery bucket is not configured')

    @override_settings(SUPABASE_DELIVERY_BUCKET='')
    def test_storage_off_leaves_a_challan_without_files_alone(self):
        self.assertEqual(self.post(tick=False).status_code, 302)
        self.assertTrue(DeliveryChallan.objects.filter(dc_number='NEW-1').exists())

    def test_a_failed_upload_discards_the_files_already_stored(self):
        self.bucket_api.upload.side_effect = [None, RuntimeError('storage down')]
        response = self.post([_file('a.pdf', PDF, 'application/pdf'),
                              _file('b.pdf', PDF, 'application/pdf')])
        self.assertContains(response, 'Upload to storage failed: RuntimeError')
        self.assertFalse(DeliveryChallan.objects.filter(dc_number='NEW-1').exists())
        first_path = self.uploads()[0].kwargs['path']
        self.assertEqual(self.removes(), [mock.call([first_path])])

    def test_a_database_failure_discards_every_upload(self):
        with mock.patch('projects.views.record_transition',
                        side_effect=RuntimeError('db down')):
            with self.assertRaises(RuntimeError):
                self.post([_file('a.pdf', PDF, 'application/pdf'),
                           _file('b.png', PNG, 'image/png')])
        self.assertFalse(DeliveryChallan.objects.filter(dc_number='NEW-1').exists())
        self.assertFalse(DeliveryChallanAttachment.objects.exists())
        uploaded = [c.kwargs['path'] for c in self.uploads()]
        self.assertEqual(len(uploaded), 2)
        self.assertEqual(self.removes(), [mock.call([p]) for p in uploaded])

    def test_only_scm_may_create(self):
        self.login(self.admin)
        response = self.post([_file('dc.pdf', PDF, 'application/pdf')])
        self.assertNotEqual(response.status_code, 302)
        self.assertEqual(self.uploads(), [])


class OpenTests(DcAttachmentBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.live = cls.attach(cls.dc, 'live.pdf')
        cls.gone = cls.attach(cls.dc, 'priced.pdf', withdrawn_by=cls.scm)
        cls.foreign = cls.attach(cls.other_dc, 'other-site.pdf')

    def open(self, profile, doc, project=None, challan=None):
        self.login(profile)
        project = project or doc.challan.project
        challan = challan or doc.challan
        return self.client.get(reverse('delivery_challan_attachment_open',
                                       args=[project.project_id, challan.pk, doc.pk]))

    def opened_rows(self):
        return ActivityLog.objects.filter(action_code='dc_document_opened')

    def test_site_engineer_opens_a_live_file_through_a_signed_link(self):
        response = self.open(self.se, self.live)
        self.assertRedirects(response, SIGNED, fetch_redirect_response=False)
        self.bucket_api.create_signed_url.assert_called_once_with(
            self.live.path, DC_DOCUMENT_LINK_SECONDS)
        self.assertEqual(DC_DOCUMENT_LINK_SECONDS, 300)
        row = self.opened_rows().get()
        self.assertEqual((row.project, row.actor, row.entity_type, row.entity_id),
                         (self.site, self.se, 'DeliveryChallanAttachment', self.live.pk))
        self.assertEqual(row.action, 'Opened a challan document on DC DCA-1')

    def test_an_open_of_a_withdrawn_file_does_not_name_it_in_the_log(self):
        # Ruling Q3: the timeline is read by everyone on the site.
        self.open(self.scm, self.gone)
        row = self.opened_rows().get()
        self.assertEqual(row.action, 'Opened a challan document on DC DCA-1')
        self.assertNotIn('priced', row.action)
        self.assertEqual(row.entity_id, self.gone.pk)

    def test_every_open_writes_its_own_row(self):
        self.open(self.pm, self.live)
        self.open(self.scm, self.live)
        self.assertEqual(self.opened_rows().count(), 2)

    def test_withdrawn_file_is_a_404_outside_scm_and_admin(self):
        for profile in (self.se, self.pm):
            with self.subTest(role=profile.role):
                self.assertEqual(self.open(profile, self.gone).status_code, 404)
        self.bucket_api.create_signed_url.assert_not_called()
        self.assertFalse(self.opened_rows().exists())

    def test_scm_and_admin_open_a_withdrawn_file(self):
        for profile in (self.scm, self.admin):
            with self.subTest(role=profile.role):
                response = self.open(profile, self.gone)
                self.assertRedirects(response, SIGNED, fetch_redirect_response=False)
        self.assertEqual(self.opened_rows().count(), 2)

    def test_ceo_and_system_admin_are_outside_the_detail_audience(self):
        # Ruling Q1: the detail page's role list is not widened, so neither opens even
        # a live file, nor a withdrawn one.
        for profile in (self.ceo, self.sysadmin, self.finance):
            for doc in (self.live, self.gone):
                with self.subTest(role=profile.role, doc=doc.original_name):
                    self.assertEqual(self.open(profile, doc).status_code, 404)

    def test_another_projects_file_is_a_404(self):
        # The other site's document, asked for under this site's URL.
        self.assertEqual(self.open(self.scm, self.foreign, project=self.site,
                                   challan=self.dc).status_code, 404)
        self.assertEqual(self.open(self.scm, self.foreign, project=self.site,
                                   challan=self.other_dc).status_code, 404)

    def test_another_challans_file_is_a_404(self):
        self.assertEqual(self.open(self.scm, self.live, challan=self.dc2).status_code, 404)

    def test_an_unrelated_site_engineer_gets_a_404(self):
        self.assertEqual(self.open(self.se_elsewhere, self.live).status_code, 404)
        self.assertFalse(self.opened_rows().exists())

    def test_a_deleted_project_is_a_404(self):
        Project.objects.filter(pk=self.site.pk).update(is_deleted=True)
        self.assertEqual(self.open(self.scm, self.live).status_code, 404)

    def test_login_required(self):
        response = self.client.get(reverse('delivery_challan_attachment_open',
                                           args=[self.site.project_id, self.dc.pk,
                                                 self.live.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])

    def test_a_failed_signing_logs_nothing_and_says_so(self):
        self.bucket_api.create_signed_url.side_effect = RuntimeError('down')
        response = self.open(self.se, self.live, )
        self.assertEqual(response.status_code, 302)
        self.assertIn('#dc-documents', response['Location'])
        self.assertFalse(self.opened_rows().exists())

    @override_settings(SUPABASE_DELIVERY_BUCKET='')
    def test_storage_off_mints_no_link(self):
        response = self.open(self.se, self.live)
        self.assertIn('#dc-documents', response['Location'])
        self.bucket_api.create_signed_url.assert_not_called()


class WithdrawTests(DcAttachmentBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.live = cls.attach(cls.dc, 'live.pdf')

    def withdraw(self, profile, doc=None, reason='Shows unit rates in the table'):
        self.login(profile)
        doc = doc or self.live
        return self.client.post(
            reverse('delivery_challan_attachment_withdraw',
                    args=[doc.challan.project.project_id, doc.challan.pk, doc.pk]),
            {'withdrawn_reason': reason})

    def test_scm_withdraws_and_the_stored_object_is_kept(self):
        response = self.withdraw(self.scm, reason='  Shows unit rates in the table  ')
        self.assertEqual(response.status_code, 302)
        self.live.refresh_from_db()
        self.assertIsNotNone(self.live.withdrawn_at)
        self.assertEqual(self.live.withdrawn_by, self.scm)
        self.assertEqual(self.live.withdrawn_reason, 'Shows unit rates in the table')
        self.bucket_api.remove.assert_not_called()
        row = ActivityLog.objects.get(action_code='dc_document_withdrawn')
        self.assertEqual((row.actor, row.entity_id), (self.scm, self.live.pk))
        self.assertNotIn('unit rates', row.action)

    def test_admin_may_withdraw(self):
        self.assertEqual(self.withdraw(self.admin).status_code, 302)
        self.live.refresh_from_db()
        self.assertEqual(self.live.withdrawn_by, self.admin)

    def test_site_engineer_and_pm_cannot_withdraw(self):
        for profile in (self.se, self.pm):
            with self.subTest(role=profile.role):
                self.assertEqual(self.withdraw(profile).status_code, 403)
        self.live.refresh_from_db()
        self.assertIsNone(self.live.withdrawn_at)
        self.assertFalse(ActivityLog.objects.filter(action_code='dc_document_withdrawn')
                         .exists())

    def test_ceo_and_system_admin_cannot_withdraw(self):
        for profile in (self.ceo, self.sysadmin):
            with self.subTest(role=profile.role):
                self.assertEqual(self.withdraw(profile).status_code, 403)
        self.live.refresh_from_db()
        self.assertIsNone(self.live.withdrawn_at)

    def test_a_short_reason_is_refused(self):
        self.withdraw(self.scm, reason='  too short ')
        self.withdraw(self.scm, reason='123456789')
        self.live.refresh_from_db()
        self.assertIsNone(self.live.withdrawn_at)

    def test_ten_characters_is_enough(self):
        self.withdraw(self.scm, reason='1234567890')
        self.live.refresh_from_db()
        self.assertIsNotNone(self.live.withdrawn_at)

    def test_an_already_withdrawn_file_is_refused(self):
        self.withdraw(self.scm)
        self.live.refresh_from_db()
        first = (self.live.withdrawn_at, self.live.withdrawn_by, self.live.withdrawn_reason)
        self.withdraw(self.admin, reason='A second, different reason')
        self.live.refresh_from_db()
        self.assertEqual((self.live.withdrawn_at, self.live.withdrawn_by,
                          self.live.withdrawn_reason), first)
        self.assertEqual(ActivityLog.objects.filter(action_code='dc_document_withdrawn')
                         .count(), 1)

    def test_get_is_not_allowed(self):
        self.login(self.scm)
        response = self.client.get(reverse('delivery_challan_attachment_withdraw',
                                           args=[self.site.project_id, self.dc.pk,
                                                 self.live.pk]))
        self.assertEqual(response.status_code, 405)

    def test_another_challans_file_is_a_404(self):
        self.login(self.scm)
        response = self.client.post(
            reverse('delivery_challan_attachment_withdraw',
                    args=[self.site.project_id, self.dc2.pk, self.live.pk]),
            {'withdrawn_reason': 'Shows unit rates in the table'})
        self.assertEqual(response.status_code, 404)


class DetailPageTests(DcAttachmentBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.live = cls.attach(cls.dc, 'live-dc.pdf')
        cls.gone = cls.attach(cls.dc, 'priced-dc.pdf', withdrawn_by=cls.scm)

    def detail(self, profile, challan=None):
        self.login(profile)
        return self.client.get(reverse('delivery_challan_detail',
                                       args=[self.site.project_id, (challan or self.dc).pk]))

    def test_everyone_on_the_page_sees_the_live_file(self):
        for profile in (self.se, self.pm, self.scm, self.admin):
            with self.subTest(role=profile.role):
                response = self.detail(profile)
                self.assertContains(response, 'Challan documents')
                self.assertContains(response, 'live-dc.pdf')
                self.assertContains(response, reverse(
                    'delivery_challan_attachment_open',
                    args=[self.site.project_id, self.dc.pk, self.live.pk]))

    def test_withdrawn_file_and_withdraw_control_are_hidden_from_se_and_pm(self):
        for profile in (self.se, self.pm):
            with self.subTest(role=profile.role):
                response = self.detail(profile)
                self.assertNotContains(response, 'priced-dc.pdf')
                self.assertNotContains(response, 'Shows unit rates')
                self.assertNotContains(response, 'Withdrawn')
                self.assertNotContains(response, 'withdrawn_reason')

    def test_scm_and_admin_see_withdrawn_files_and_the_control(self):
        for profile in (self.scm, self.admin):
            with self.subTest(role=profile.role):
                response = self.detail(profile)
                self.assertContains(response, 'priced-dc.pdf')
                self.assertContains(response, 'Shows unit rates on page 2')
                self.assertContains(response, 'Withdrawn (visible to SCM and Admin only)')
                self.assertContains(response, reverse(
                    'delivery_challan_attachment_withdraw',
                    args=[self.site.project_id, self.dc.pk, self.live.pk]))
                # No Withdraw control on a file already withdrawn.
                self.assertNotContains(response, reverse(
                    'delivery_challan_attachment_withdraw',
                    args=[self.site.project_id, self.dc.pk, self.gone.pk]))

    def test_a_challan_without_documents_says_so(self):
        self.assertContains(self.detail(self.se, self.dc2), 'No challan document attached.')

    def test_only_withdrawn_documents_still_reads_none_attached_for_se(self):
        DeliveryChallanAttachment.objects.filter(pk=self.live.pk).update(
            withdrawn_at=timezone.now(), withdrawn_by=self.scm,
            withdrawn_reason='Also shows rates')
        self.assertContains(self.detail(self.se), 'No challan document attached.')

    def _count(self, url):
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(self.client.get(url).status_code, 200)
        return len(ctx)

    def test_the_documents_cost_one_query(self):
        # BEFORE vs AFTER inside the test, as tests_dc_site_count does: everything DC-B2
        # added to the view — the documents' queryset and the SCM/Admin predicate —
        # swapped for constants, then the real ones.
        url = reverse('delivery_challan_detail', args=[self.site.project_id, self.dc.pk])
        for profile in (self.scm, self.admin, self.pm, self.se):
            with self.subTest(role=profile.role):
                self.login(profile)
                self.client.get(url)       # warm per-process caches
                # Every call on the stub returns a MagicMock, which iterates as empty.
                with mock.patch('projects.views.DeliveryChallanAttachment'), \
                        mock.patch('projects.views.user_can_manage_dc_documents',
                                   return_value=False):
                    before = self._count(url)
                self.assertEqual(self._count(url) - before, 1)

    def test_more_documents_cost_nothing_more(self):
        url = reverse('delivery_challan_detail', args=[self.site.project_id, self.dc.pk])
        self.login(self.scm)
        self.client.get(url)
        two = self._count(url)
        self.attach(self.dc, 'third.pdf', uploader=self.admin)
        self.assertEqual(self._count(url), two)


class FeedTests(DcAttachmentBase):

    def test_overview_hides_opens_and_shows_withdrawals(self):
        doc = self.attach(self.dc, 'feed.pdf')
        self.login(self.se)
        self.client.get(reverse('delivery_challan_attachment_open',
                                args=[self.site.project_id, self.dc.pk, doc.pk]))
        self.login(self.scm)
        self.client.post(reverse('delivery_challan_attachment_withdraw',
                                 args=[self.site.project_id, self.dc.pk, doc.pk]),
                         {'withdrawn_reason': 'Shows unit rates in the table'})
        response = self.client.get(reverse('project_overview', args=[self.site.project_id]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Opened a challan document')
        self.assertContains(response, 'Withdrew a challan document from DC DCA-1')
        self.assertTrue(ActivityLog.objects.filter(action_code='dc_document_opened').exists())


class ModelAndPredicateTests(DcAttachmentBase):

    def _row(self, **fields):
        values = dict(challan=self.dc, uploaded_by=self.scm, original_name='x.pdf',
                      bucket=BUCKET, path='DCA-SITE/challan/x.pdf', size=1,
                      mime='application/pdf', no_prices_confirmed=True)
        values.update(fields)
        return DeliveryChallanAttachment(**values)

    def test_no_prices_confirmed_must_be_true(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._row(no_prices_confirmed=False).save()

    def test_withdrawn_needs_who_and_why(self):
        for fields in ({'withdrawn_at': timezone.now(), 'withdrawn_by': self.scm},
                       {'withdrawn_at': timezone.now(), 'withdrawn_reason': 'Shows rates'}):
            with self.subTest(fields=sorted(fields)):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    self._row(**fields).save()

    def test_who_and_why_need_withdrawn_at(self):
        for fields in ({'withdrawn_by': self.scm}, {'withdrawn_reason': 'Shows rates'}):
            with self.subTest(fields=sorted(fields)):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    self._row(**fields).save()

    def test_append_only(self):
        row = self._row()
        row.save()
        row.original_name = 'renamed.pdf'
        with self.assertRaises(AppendOnlyViolation):
            row.save()
        with self.assertRaises(AppendOnlyViolation):
            row.delete()

    def test_a_challan_with_a_document_cannot_be_deleted(self):
        from django.db.models import ProtectedError
        self._row().save()
        with self.assertRaises(ProtectedError):
            self.dc.delete()

    def test_dc_detail_roles_match_the_detail_page_role_by_role(self):
        url = reverse('delivery_challan_detail', args=[self.site.project_id, self.dc.pk])
        for role, _label in UserProfile.ROLE_CHOICES:
            with self.subTest(role=role):
                profile = _profile(f'dca_role_{role.replace(" ", "_").lower()}', role)
                self.login(profile)
                refused_by_role = self.client.get(url).status_code == 403
                self.assertEqual(refused_by_role, role not in DC_DETAIL_ROLES)
                if role not in DC_DETAIL_ROLES:
                    self.assertFalse(user_can_open_dc_document(profile.user, self.site))
                self.assertEqual(user_can_manage_dc_documents(profile.user, self.site),
                                 role in ('SCM', 'Admin'))
