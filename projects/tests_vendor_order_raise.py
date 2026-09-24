"""O2 — raising a Residential vendor order, and reading it back.

What this file pins, and why each matters:

  * ONE SUBMISSION, ONE ORDER. The whole raise — order, site, lines, documents, first
    payment, ledger row — is one write, and a repeated client_uuid makes nothing new.
  * REFUSAL CREATES NOTHING AND UPLOADS NOTHING. Every check runs before the first file
    reaches storage; a refused submission leaves no row and no stored file.
  * A FAILED SAVE CLEANS UP ITS FILES. The orphan-file finding, closed for this path.
  * WHO. Only SCM raises, only on a live Residential site; the order page is visible to
    the portfolio roles and to anyone who can see a site on it.

Run with:
    python manage.py test projects.tests_vendor_order_raise --settings=solarpms.test_settings
"""
import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError
from django.test import Client, TestCase
from django.urls import reverse

from .models import (
    BOQ, BOQItem, BOQItemMaster, PaymentRequest, Project, StatusTransition,
    SUBJECT_PAYMENT_REQUEST, Vendor, VendorOrder, VendorOrderDocument, VendorOrderLine,
    VendorOrderSite, VENDOR_ORDER_DOC_INVOICE, VENDOR_ORDER_DOC_PI, VENDOR_ORDER_DOC_PO,
)


def _profile(username, role):
    """A post_save signal creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.save()
    return profile


def _client(profile):
    client = Client(SERVER_NAME='localhost')
    client.force_login(profile.user)
    return client


def _pdf(name):
    return SimpleUploadedFile(name, b'%PDF-1.4 fake', content_type='application/pdf')


def _project(name, pm, project_type='Residential', **extra):
    return Project.objects.create(
        customer_name=name, status='Active', customer_phone='9876543210',
        site_address='1 Sun Road', city='Lucknow', state='Uttar Pradesh',
        project_type=project_type, dc_capacity_kw=Decimal('5.00'), assigned_pm=pm,
        **extra)


class RaiseFixture(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.scm     = _profile('o2_scm', 'SCM')
        cls.finance = _profile('o2_fin', 'Finance')
        cls.pm      = _profile('o2_pm', 'PM')
        cls.pm_b    = _profile('o2_pm_b', 'PM')
        cls.se      = _profile('o2_se', 'Site Engineer')
        cls.admin   = _profile('o2_admin', 'Admin')
        cls.vendor  = Vendor.objects.create(name='Sunrise Modules', contact_person='R',
                                            phone='9000000001')

        cls.module = BOQItemMaster.objects.create(
            code='ITM-901', description='Solar Module 540Wp', unit='Nos',
            category='Solar Modules', project_type='Residential', sort_order=1)
        cls.inverter = BOQItemMaster.objects.create(
            code='ITM-902', description='Inverter 5kW', unit='Nos',
            category='Inverter', project_type='Residential', sort_order=2)

        cls.project = _project('Order Site', cls.pm)
        boq = BOQ.objects.create(project=cls.project)
        cls.item_module = BOQItem.objects.create(
            boq=boq, serial_no=1, category='Solar Modules', description='Solar Module 540Wp',
            uom='Nos', item_master=cls.module, boq_quantity=Decimal('10'))
        cls.item_inverter = BOQItem.objects.create(
            boq=boq, serial_no=2, category='Inverter', description='Inverter 5kW',
            uom='Nos', item_master=cls.inverter, boq_quantity=Decimal('1'))

        cls.other_project = _project('Other Site', cls.pm_b)
        other_boq = BOQ.objects.create(project=cls.other_project)
        cls.other_item = BOQItem.objects.create(
            boq=other_boq, serial_no=1, category='Solar Modules',
            description='Solar Module 540Wp', uom='Nos', item_master=cls.module,
            boq_quantity=Decimal('4'))

    def url(self, project=None):
        return reverse('vendor_order_create', args=[(project or self.project).pk])

    def payload(self, **overrides):
        """A complete, valid submission: 2 lines (total 60,000), a PO and a PI file,
        and a first payment of 25,000."""
        data = {
            'client_uuid': str(uuid.uuid4()),
            'vendor_id': str(self.vendor.pk),
            'po_number': 'PO-100', 'pi_number': 'PI-7',
            'line': [str(self.item_module.pk), str(self.item_inverter.pk)],
            f'qty_{self.item_module.pk}': '10', f'amount_{self.item_module.pk}': '45000',
            f'qty_{self.item_inverter.pk}': '1', f'amount_{self.item_inverter.pk}': '15000',
            'doc_type_0': VENDOR_ORDER_DOC_PO, 'doc_file_0': _pdf('po.pdf'),
            'doc_type_1': VENDOR_ORDER_DOC_PI, 'doc_file_1': _pdf('pi.pdf'),
            'request_payment': '1', 'payment_amount': '25000', 'payment_note': 'advance',
        }
        data.update(overrides)
        return {k: v for k, v in data.items() if v is not None}

    def post(self, profile=None, project=None, storage=None, **overrides):
        storage = storage or MagicMock()
        with patch('projects.supabase_storage.get_supabase_client', return_value=storage):
            response = _client(profile or self.scm).post(
                self.url(project), self.payload(**overrides))
        return response, storage

    def assertNothingCreated(self):
        self.assertEqual(VendorOrder.objects.count(), 0)
        self.assertEqual(VendorOrderSite.objects.count(), 0)
        self.assertEqual(VendorOrderLine.objects.count(), 0)
        self.assertEqual(VendorOrderDocument.objects.count(), 0)
        self.assertEqual(PaymentRequest.objects.count(), 0)

    def assertNothingUploaded(self, storage):
        storage.storage.from_.return_value.upload.assert_not_called()


class RaiseTests(RaiseFixture):

    def test_scm_raises_an_order_with_lines_documents_and_a_first_payment(self):
        response, storage = self.post()
        order = VendorOrder.objects.get()
        self.assertRedirects(response, reverse('vendor_order_detail', args=[order.pk]),
                             fetch_redirect_response=False)

        self.assertEqual(order.project_type, 'Residential')
        self.assertEqual(order.created_by, self.scm)
        self.assertEqual(VendorOrderSite.objects.get().project, self.project)
        self.assertIsNone(VendorOrderSite.objects.get().via_site_group)

        lines = VendorOrderLine.objects.order_by('item_code')
        self.assertEqual(lines.count(), 2)
        self.assertEqual([l.item_code for l in lines], ['ITM-901', 'ITM-902'])
        self.assertEqual(lines[0].item_unit, 'Nos')
        self.assertEqual(lines[0].item_category, 'Solar Modules')
        self.assertEqual(lines[0].boq_item, self.item_module)
        self.assertEqual(order.total, Decimal('60000'))

        docs = VendorOrderDocument.objects.all()
        self.assertEqual(docs.count(), 2)
        self.assertEqual({d.doc_type for d in docs}, {VENDOR_ORDER_DOC_PO, VENDOR_ORDER_DOC_PI})
        for doc in docs:
            self.assertTrue(doc.path.startswith(f'vendor-orders/{order.client_uuid}/'))
        self.assertEqual(storage.storage.from_.return_value.upload.call_count, 2)

        pr = PaymentRequest.objects.get()
        self.assertEqual(pr.vendor_order, order)
        # O4: the raise puts the payment in front of the approval gate, not in front of
        # Finance. Was APPROVED until O4; the rest of this assertion is unchanged.
        self.assertEqual(pr.status, PaymentRequest.PENDING_APPROVAL)
        self.assertEqual(pr.amount, Decimal('25000'))
        self.assertEqual(pr.requested_by, self.scm.user)

        transitions = StatusTransition.objects.filter(subject_type=SUBJECT_PAYMENT_REQUEST)
        self.assertEqual(transitions.count(), 1)
        self.assertEqual(transitions.get().to_status, PaymentRequest.PENDING_APPROVAL)
        self.assertEqual(transitions.get().subject_id, pr.pk)

    def test_the_same_client_uuid_twice_creates_one_order(self):
        key = str(uuid.uuid4())
        first, _ = self.post(client_uuid=key)
        second, storage = self.post(client_uuid=key)
        self.assertEqual(VendorOrder.objects.count(), 1)
        self.assertEqual(PaymentRequest.objects.count(), 1)
        self.assertEqual(second['Location'], first['Location'])
        self.assertNothingUploaded(storage)

    def test_an_order_without_a_payment_creates_no_payment(self):
        self.post(request_payment=None)
        self.assertEqual(VendorOrder.objects.count(), 1)
        self.assertEqual(PaymentRequest.objects.count(), 0)

    def test_the_page_renders_this_projects_boq_only(self):
        response = _client(self.scm).get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'name="qty_{self.item_module.pk}"')
        self.assertNotContains(response, f'name="qty_{self.other_item.pk}"')


class RefusalTests(RaiseFixture):

    def test_other_roles_are_refused(self):
        for profile in (self.finance, self.pm, self.se, self.admin):
            with self.subTest(role=profile.role):
                response, storage = self.post(profile=profile)
                self.assertEqual(response.status_code, 403)
                self.assertNothingUploaded(storage)
        self.assertNothingCreated()

    def test_scm_is_refused_on_an_opex_project(self):
        opex = _project('Tender Site', self.pm, project_type='OPEX')
        response, _ = self.post(project=opex)
        self.assertEqual(response.status_code, 403)
        self.assertNothingCreated()

    def test_scm_is_refused_on_a_deleted_project(self):
        Project.objects.filter(pk=self.project.pk).update(is_deleted=True)
        response, _ = self.post()
        self.assertEqual(response.status_code, 403)
        self.assertNothingCreated()

    def test_a_boq_item_from_another_project_is_refused(self):
        response, storage = self.post(**{
            'line': [str(self.item_module.pk), str(self.other_item.pk)],
            f'qty_{self.other_item.pk}': '1', f'amount_{self.other_item.pk}': '100',
        })
        self.assertEqual(response.status_code, 400)
        self.assertNothingCreated()
        self.assertNothingUploaded(storage)

    def test_a_duplicated_boq_item_is_refused(self):
        response, storage = self.post(line=[str(self.item_module.pk)] * 2)
        self.assertEqual(response.status_code, 400)
        self.assertNothingCreated()
        self.assertNothingUploaded(storage)

    def test_a_payment_above_the_order_total_is_refused(self):
        response, storage = self.post(payment_amount='60000.01')
        self.assertEqual(response.status_code, 400)
        self.assertNothingCreated()
        self.assertNothingUploaded(storage)

    def test_an_invoice_without_number_or_amount_is_refused(self):
        for number, amount in (('', '1000'), ('INV-1', ''), ('INV-1', '0')):
            with self.subTest(number=number, amount=amount):
                response, storage = self.post(**{
                    'doc_type_2': VENDOR_ORDER_DOC_INVOICE, 'doc_file_2': _pdf('inv.pdf'),
                    'doc_invoice_number_2': number, 'doc_invoice_amount_2': amount,
                })
                self.assertEqual(response.status_code, 400)
                self.assertNothingUploaded(storage)
        self.assertNothingCreated()

    def test_a_complete_invoice_is_accepted(self):
        self.post(**{
            'doc_type_2': VENDOR_ORDER_DOC_INVOICE, 'doc_file_2': _pdf('inv.pdf'),
            'doc_invoice_number_2': 'INV-1', 'doc_invoice_amount_2': '1000',
        })
        self.assertEqual(VendorOrder.objects.get().invoiced, Decimal('1000'))

    def test_no_po_or_pi_document_is_refused(self):
        response, storage = self.post(**{
            'doc_type_0': 'other', 'doc_file_1': None, 'doc_type_1': None,
        })
        self.assertEqual(response.status_code, 400)
        self.assertNothingCreated()
        self.assertNothingUploaded(storage)

    def test_a_refused_submission_keeps_the_users_entries(self):
        response, _ = self.post(payment_amount='999999')
        self.assertContains(response, 'value="45000"', status_code=400)
        self.assertContains(response, 'value="PO-100"', status_code=400)
        self.assertContains(response, f'value="{self.item_module.pk}" id="voLine{self.item_module.pk}" checked',
                            status_code=400)

    def test_a_db_failure_after_upload_removes_the_uploaded_files(self):
        with patch('projects.order_views.VendorOrderLine.objects.bulk_create',
                   side_effect=DatabaseError('boom')):
            response, storage = self.post()
        self.assertEqual(response.status_code, 400)
        self.assertNothingCreated()

        bucket = storage.storage.from_.return_value
        uploaded = [call.kwargs['path'] for call in bucket.upload.call_args_list]
        removed  = [call.args[0][0] for call in bucket.remove.call_args_list]
        self.assertEqual(len(uploaded), 2)
        self.assertEqual(sorted(removed), sorted(uploaded))


class DetailTests(RaiseFixture):

    def setUp(self):
        self.post()
        self.order = VendorOrder.objects.get()
        self.detail_url = reverse('vendor_order_detail', args=[self.order.pk])

    def test_scm_finance_and_the_sites_pm_can_read_it(self):
        for profile in (self.scm, self.finance, self.pm):
            with self.subTest(role=profile.role):
                self.assertEqual(_client(profile).get(self.detail_url).status_code, 200)

    def test_another_pm_is_refused(self):
        self.assertEqual(_client(self.pm_b).get(self.detail_url).status_code, 403)

    def test_invoice_awaited_shows_when_paid_exceeds_invoiced(self):
        response = _client(self.scm).get(self.detail_url)
        self.assertNotContains(response, 'Invoice awaited')

        PaymentRequest.objects.create(
            vendor_order=self.order, project=self.project, vendor=self.vendor,
            amount=Decimal('5000'), requested_by=self.scm.user,
            status=PaymentRequest.CONFIRMED, approved_amount=Decimal('5000'))
        response = _client(self.scm).get(self.detail_url)
        self.assertContains(response, 'Invoice awaited')

    def test_the_old_raise_endpoint_is_gone(self):
        """Retired in O2 (a redirect and a 410), deleted in O6: the URL no longer exists.
        The path is written out because its name no longer reverses."""
        old = f'/projects/{self.project.project_id}/payment-requests/raise/'
        self.assertEqual(_client(self.scm).get(old).status_code, 404)
        self.assertEqual(_client(self.scm).post(old, {}).status_code, 404)


class DetailQueryCountTests(RaiseFixture):

    def _order_with(self, n_lines, n_docs):
        order = VendorOrder.objects.create(vendor=self.vendor, project_type='Residential',
                                           po_number='PO-Q', created_by=self.scm,
                                           total_amount=Decimal(10 * n_lines or 1))
        VendorOrderSite.objects.create(order=order, project=self.project)
        for i in range(n_lines):
            VendorOrderLine.objects.create(order=order, item_description=f'Item {i}',
                                           quantity=Decimal('1'), amount=Decimal('10'))
        for i in range(n_docs):
            VendorOrderDocument.objects.create(order=order, doc_type=VENDOR_ORDER_DOC_PO,
                                               file_name=f'{i}.pdf', bucket='b',
                                               path=f'p/{i}.pdf', uploaded_by=self.scm)
        return order

    def _count(self, order, profile):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        client = _client(profile)
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(reverse('vendor_order_detail', args=[order.pk]))
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries)

    def test_queries_do_not_grow_with_lines_or_documents(self):
        small = self._count(self._order_with(1, 1), self.pm)
        large = self._count(self._order_with(10, 5), self.pm)
        self.assertEqual(small, large)
