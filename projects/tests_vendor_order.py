"""O1 — VendorOrder records, PaymentRequest approval statuses, the document URL helper.

What this file pins, and why each matters:

  * EVERY CHECK CONSTRAINT REFUSES A BAD ROW. They are the only enforcement O1 ships —
    there are no views yet — so a constraint that silently compiled to nothing would go
    unnoticed until O2 wrote the first real row.
  * PAID, BALANCE AND INVOICED ARE ARITHMETIC OVER CHILDREN. `paid` counts CONFIRMED
    only; a payment in any other status is money not yet paid, and one test per status
    says so. `total` is NOT (O3r): it is the stored PO figure, whatever the lines say.
  * PROTECT HOLDS. An order is a record of something that happened outside PMS; deleting
    the vendor or site it names must be refused, not cascaded.
  * THE URL HELPER ENCODES THE PATH. The legacy invoice URL is built from the raw
    filename; a space in it is a broken link.

Run with:
    python manage.py test projects.tests_vendor_order --settings=solarpms.test_settings
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase, override_settings

from .models import (
    PaymentRequest, Project, SUBJECT_PAYMENT_REQUEST, Vendor, VendorOrder,
    VendorOrderDocument, VendorOrderLine, VendorOrderSite,
    VENDOR_ORDER_DOC_INVOICE, VENDOR_ORDER_DOC_PO,
)
from .supabase_storage import vendor_order_document_url
from .utils import _subject_type_registry


def _profile(username, role):
    """A post_save signal creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.save()
    return profile


class VendorOrderFixture(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.scm = _profile('vo_scm', 'SCM')
        cls.vendor = Vendor.objects.create(name='Sunrise Modules', contact_person='R',
                                           phone='9000000001')
        cls.project = Project.objects.create(
            customer_name='Order Site', status='Active', customer_phone='9876543210',
            site_address='1 Sun Road', city='Lucknow', state='Uttar Pradesh',
            project_type='Residential', dc_capacity_kw=Decimal('5.00'))

    def make_order(self, **kwargs):
        kwargs.setdefault('total_amount', Decimal('1000.00'))
        return VendorOrder.objects.create(vendor=self.vendor, project_type='Residential',
                                          created_by=self.scm, **kwargs)

    def make_line(self, order, quantity='10', amount='1000.00'):
        return VendorOrderLine.objects.create(
            order=order, item_code='ITM-001', item_description='Solar Module 540Wp',
            item_unit='Nos', item_category='Solar Modules',
            quantity=Decimal(quantity), amount=Decimal(amount))

    def make_document(self, order, doc_type=VENDOR_ORDER_DOC_PO, **kwargs):
        return VendorOrderDocument.objects.create(
            order=order, doc_type=doc_type, file_name='po.pdf', bucket='solarpms-files',
            path='vendor-orders/1/po.pdf', uploaded_by=self.scm, **kwargs)

    def make_payment(self, order, amount, status, **kwargs):
        # O4b: an approved or paid row carries its approved amount — in full, here.
        if status in (PaymentRequest.APPROVED, PaymentRequest.CONFIRMED):
            kwargs.setdefault('approved_amount', Decimal(amount))
        return PaymentRequest.objects.create(
            project=self.project, vendor=self.vendor, vendor_order=order,
            amount=Decimal(amount), requested_by=self.scm.user, status=status, **kwargs)

    def assertRefused(self, create):
        """The database, not the model, refuses the row."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                create()


# ---------------------------------------------------------------------------
# Check constraints
# ---------------------------------------------------------------------------

class CheckConstraintTests(VendorOrderFixture):

    def test_an_invoice_document_without_a_number_is_refused(self):
        order = self.make_order()
        self.assertRefused(lambda: self.make_document(
            order, doc_type=VENDOR_ORDER_DOC_INVOICE, invoice_amount=Decimal('500.00')))

    def test_an_invoice_document_without_an_amount_is_refused(self):
        order = self.make_order()
        self.assertRefused(lambda: self.make_document(
            order, doc_type=VENDOR_ORDER_DOC_INVOICE, invoice_number='INV-9'))

    def test_an_invoice_document_with_a_zero_amount_is_refused(self):
        order = self.make_order()
        self.assertRefused(lambda: self.make_document(
            order, doc_type=VENDOR_ORDER_DOC_INVOICE, invoice_number='INV-9',
            invoice_amount=Decimal('0')))

    def test_a_complete_invoice_document_and_a_bare_po_are_accepted(self):
        order = self.make_order()
        self.make_document(order, doc_type=VENDOR_ORDER_DOC_INVOICE,
                           invoice_number='INV-9', invoice_amount=Decimal('500.00'))
        self.make_document(order)   # a PO needs neither number nor amount
        self.assertEqual(order.documents.count(), 2)

    def test_a_line_with_zero_quantity_is_refused(self):
        order = self.make_order()
        self.assertRefused(lambda: self.make_line(order, quantity='0'))

    def test_a_line_with_zero_amount_is_refused(self):
        order = self.make_order()
        self.assertRefused(lambda: self.make_line(order, amount='0'))

    def test_a_line_with_no_quantity_and_no_amount_is_accepted(self):
        """O3r: a line is the requirement snapshot, so both figures are optional — and
        each is still refused at zero when it IS given (the two tests above)."""
        order = self.make_order()
        line = VendorOrderLine.objects.create(order=order, item_description='Module',
                                              quantity=None, amount=None)
        self.assertIsNone(line.amount)

    def test_an_order_total_of_zero_is_refused(self):
        self.assertRefused(lambda: self.make_order(total_amount=Decimal('0')))

    def test_an_order_without_a_total_is_refused(self):
        self.assertRefused(lambda: self.make_order(total_amount=None))

    def test_a_rejected_payment_without_a_reason_is_refused(self):
        order = self.make_order()
        self.assertRefused(lambda: self.make_payment(order, '100', PaymentRequest.REJECTED))

    def test_an_on_hold_payment_no_longer_needs_a_reason_on_the_request(self):
        """O4 NARROWED `payment_request_refusal_needs_reason` to `rejected` alone, and
        this test was inverted with it. The rule did not weaken: a hold's reason is now
        MANDATORY on `PaymentRequestHold` (`payment_hold_needs_reason`), which also
        records who held it and SCM's answer — none of which fits in this one column,
        and the second hold of a request would have overwritten the first.

        Requiring it in both places would demand the same text twice, in two columns
        that could then disagree. `tests_payment_approval.MandatoryReasonTests` pins the
        rule where it now lives: a hold with no reason is refused and writes nothing.
        """
        order = self.make_order()
        pr = self.make_payment(order, '100', PaymentRequest.ON_HOLD)
        self.assertEqual(pr.status, PaymentRequest.ON_HOLD)
        self.assertEqual(pr.decision_reason, '')

    def test_a_rejected_payment_with_a_reason_is_accepted(self):
        order = self.make_order()
        pr = self.make_payment(order, '100', PaymentRequest.REJECTED,
                               decision_reason='Invoice does not match the PO')
        self.assertEqual(pr.status, PaymentRequest.REJECTED)


class UniqueSiteTests(VendorOrderFixture):

    def test_the_same_site_cannot_appear_twice_on_one_order(self):
        order = self.make_order()
        VendorOrderSite.objects.create(order=order, project=self.project)
        self.assertRefused(
            lambda: VendorOrderSite.objects.create(order=order, project=self.project))

    def test_the_same_site_may_appear_on_two_orders(self):
        VendorOrderSite.objects.create(order=self.make_order(), project=self.project)
        VendorOrderSite.objects.create(order=self.make_order(), project=self.project)
        self.assertEqual(VendorOrderSite.objects.filter(project=self.project).count(), 2)


# ---------------------------------------------------------------------------
# Computed totals
# ---------------------------------------------------------------------------

class ComputedTotalTests(VendorOrderFixture):

    def test_an_order_with_no_lines_or_payments_owes_its_whole_total(self):
        order = self.make_order(total_amount=Decimal('1000.00'))
        self.assertEqual((order.total, order.paid, order.balance, order.invoiced),
                         (Decimal('1000.00'), Decimal('0'), Decimal('1000.00'), Decimal('0')))

    def test_total_is_the_stored_po_figure_not_the_sum_of_line_amounts(self):
        """O3r: the lines are the requirement, with optional amounts; the order's value
        is what the PO says. Lines summing to 1,250.50 do not move a total of 1,000."""
        order = self.make_order(total_amount=Decimal('1000.00'))
        self.make_line(order, amount='1000.00')
        self.make_line(order, amount='250.50')
        self.assertEqual(order.total, Decimal('1000.00'))
        self.assertEqual(order.available_to_request, Decimal('1000.00'))

    def test_paid_counts_confirmed_payments_only_and_balance_follows(self):
        order = self.make_order()
        self.make_line(order, amount='1000.00')
        self.make_payment(order, '300.00', PaymentRequest.CONFIRMED)
        self.make_payment(order, '200.00', PaymentRequest.CONFIRMED)
        self.make_payment(order, '400.00', PaymentRequest.APPROVED)
        self.assertEqual(order.paid, Decimal('500.00'))
        self.assertEqual(order.balance, Decimal('500.00'))

    def test_invoiced_sums_invoice_documents_only(self):
        order = self.make_order()
        self.make_document(order, doc_type=VENDOR_ORDER_DOC_INVOICE,
                           invoice_number='INV-1', invoice_amount=Decimal('600.00'))
        self.make_document(order, doc_type=VENDOR_ORDER_DOC_INVOICE,
                           invoice_number='INV-2', invoice_amount=Decimal('150.00'))
        self.make_document(order)   # a PO contributes nothing
        self.assertEqual(order.invoiced, Decimal('750.00'))


class UnpaidStatusTests(VendorOrderFixture):
    """One test per non-paid status: each is money not yet paid."""

    def assert_not_paid(self, status, **kwargs):
        order = self.make_order()
        self.make_line(order, amount='1000.00')
        self.make_payment(order, '1000.00', status, **kwargs)
        self.assertEqual(order.paid, Decimal('0'))
        self.assertEqual(order.balance, Decimal('1000.00'))

    def test_pending_approval_is_not_paid(self):
        self.assert_not_paid(PaymentRequest.PENDING_APPROVAL)

    def test_approved_is_not_paid(self):
        self.assert_not_paid(PaymentRequest.APPROVED)

    def test_on_hold_is_not_paid(self):
        self.assert_not_paid(PaymentRequest.ON_HOLD, decision_reason='Awaiting GRN')

    def test_rejected_is_not_paid(self):
        self.assert_not_paid(PaymentRequest.REJECTED, decision_reason='Duplicate')


# ---------------------------------------------------------------------------
# PROTECT
# ---------------------------------------------------------------------------

class ProtectTests(VendorOrderFixture):

    def test_deleting_a_vendor_with_an_order_is_refused(self):
        self.make_order()
        with self.assertRaises(ProtectedError):
            self.vendor.delete()

    def test_deleting_a_project_on_an_order_is_refused(self):
        VendorOrderSite.objects.create(order=self.make_order(), project=self.project)
        with self.assertRaises(ProtectedError):
            self.project.delete()


# ---------------------------------------------------------------------------
# URL helper
# ---------------------------------------------------------------------------

@override_settings(SUPABASE_URL='https://example.supabase.co')
class DocumentUrlTests(TestCase):

    def test_a_filename_with_a_space_is_encoded_and_folders_survive(self):
        doc = VendorOrderDocument(bucket='solarpms-files',
                                  path='vendor-orders/7/Sunrise PO 12.pdf')
        self.assertEqual(
            vendor_order_document_url(doc),
            'https://example.supabase.co/storage/v1/object/public/'
            'solarpms-files/vendor-orders/7/Sunrise%20PO%2012.pdf')


# ---------------------------------------------------------------------------
# Status vocabulary and ledger registration
# ---------------------------------------------------------------------------

class StatusVocabularyTests(VendorOrderFixture):

    def test_a_request_created_without_a_status_awaits_approval(self):
        """O4 changed the model default from APPROVED to PENDING_APPROVAL. The raise
        paths all state the status explicitly, so this pins the SAFETY NET — a row made
        anywhere else (a shell, a fixture, a seed) must land in front of the gate rather
        than past it."""
        pr = PaymentRequest.objects.create(
            vendor_order=self.make_order(),   # O2: NOT NULL; fixture only
            project=self.project, vendor=self.vendor, amount=Decimal('10'),
            requested_by=self.scm.user)
        self.assertEqual(pr.status, PaymentRequest.PENDING_APPROVAL)

    def test_pending_is_gone_and_confirmed_reads_paid(self):
        self.assertFalse(hasattr(PaymentRequest, 'PENDING'))
        self.assertEqual(dict(PaymentRequest.STATUS_CHOICES)[PaymentRequest.CONFIRMED], 'Paid')
        self.assertEqual(PaymentRequest.CONFIRMED, 'confirmed')

    def test_payment_request_is_a_registered_ledger_subject(self):
        self.assertEqual(_subject_type_registry()[PaymentRequest], SUBJECT_PAYMENT_REQUEST)
