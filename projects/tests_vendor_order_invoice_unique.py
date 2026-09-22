"""O2c — one invoice number per order.

What this file pins:

  * A REPEATED INVOICE IS REFUSED BEFORE ANY UPLOAD, whether it repeats within one
    submission or repeats one already on the order. The comparison is trimmed and
    case-insensitive. The refusal stores no file and creates no row.
  * THE SCOPE IS ONE ORDER, AND INVOICES ONLY. The same number on another order, or
    on a non-invoice document, is fine.
  * THE UNIQUE INDEX IS THE BACKSTOP. When the check is bypassed (a race), the database
    refuses, the user sees the same message, and the uploaded files are removed.

Run with:
    python manage.py test projects.tests_vendor_order_invoice_unique --settings=solarpms.test_settings
"""
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.db import IntegrityError, transaction
from django.urls import reverse

from .models import VendorOrderDocument, VendorOrder, VENDOR_ORDER_DOC_INVOICE
from .tests_vendor_order_payments import _make_order
from .tests_vendor_order_raise import RaiseFixture, _client, _pdf


def _invoice(fixture, order, number):
    return VendorOrderDocument.objects.create(
        order=order, doc_type=VENDOR_ORDER_DOC_INVOICE, invoice_number=number,
        invoice_amount=Decimal('100'), file_name='inv.pdf', bucket='b',
        path=f'p/{order.pk}/{number}.pdf', uploaded_by=fixture.scm)


class InvoiceFixture(RaiseFixture):

    def setUp(self):
        self.order = _make_order(self)
        _invoice(self, self.order, 'INV-7')

    def post_docs(self, order=None, **data):
        storage = MagicMock()
        with patch('projects.supabase_storage.get_supabase_client', return_value=storage):
            response = _client(self.scm).post(
                reverse('vendor_order_add_documents', args=[(order or self.order).pk]), data)
        return response, storage

    def invoice_slot(self, index, number, amount='100'):
        return {
            f'doc_type_{index}': VENDOR_ORDER_DOC_INVOICE,
            f'doc_file_{index}': _pdf(f'inv{index}.pdf'),
            f'doc_invoice_number_{index}': number,
            f'doc_invoice_amount_{index}': amount,
        }

    def assertRefusedCleanly(self, response, storage, number, before):
        self.assertContains(response, f'Invoice {number} is already recorded on this order.',
                            status_code=400)
        storage.storage.from_.return_value.upload.assert_not_called()
        self.assertEqual(VendorOrderDocument.objects.count(), before)


class DuplicateRefusedTests(InvoiceFixture):

    def test_a_repeat_within_one_append_is_refused(self):
        before = VendorOrderDocument.objects.count()
        response, storage = self.post_docs(**self.invoice_slot(0, 'INV-8'),
                                           **self.invoice_slot(1, ' inv-8 '))
        self.assertRefusedCleanly(response, storage, 'inv-8', before)

    def test_a_repeat_within_one_raise_is_refused(self):
        response, storage = self.post(**self.invoice_slot(2, 'INV-1'),
                                      **self.invoice_slot(3, 'inv-1'))
        self.assertContains(response, 'Invoice inv-1 is already recorded on this order.',
                            status_code=400)
        storage.storage.from_.return_value.upload.assert_not_called()
        # Only the fixture's order exists; the raise created nothing.
        self.assertEqual(VendorOrder.objects.count(), 1)

    def test_a_repeat_of_an_existing_invoice_is_refused_trimmed_and_caseless(self):
        before = VendorOrderDocument.objects.count()
        response, storage = self.post_docs(**self.invoice_slot(0, '  inv-7 '))
        self.assertRefusedCleanly(response, storage, 'inv-7', before)


class ScopeTests(InvoiceFixture):

    def test_the_same_number_on_another_order_is_allowed(self):
        other = _make_order(self, po='PO-OTHER')
        response, _ = self.post_docs(order=other, **self.invoice_slot(0, 'INV-7'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(VendorOrderDocument.objects.filter(
            doc_type=VENDOR_ORDER_DOC_INVOICE, invoice_number='INV-7').count(), 2)

    def test_a_non_invoice_document_is_unaffected_by_any_number(self):
        response, _ = self.post_docs(**{
            'doc_type_0': 'other', 'doc_file_0': _pdf('note.pdf'),
            'doc_invoice_number_0': 'INV-7', 'doc_invoice_amount_0': '100',
        })
        self.assertEqual(response.status_code, 302)
        # At the database too: the index covers invoices only.
        for _ in range(2):
            VendorOrderDocument.objects.create(
                order=self.order, doc_type='other', invoice_number='INV-7',
                file_name='x.pdf', bucket='b', path='p/x.pdf', uploaded_by=self.scm)

    def test_save_trims_and_the_index_then_refuses_the_same_number(self):
        doc = _invoice(self, self.order, '  INV-9  ')
        doc.refresh_from_db()
        self.assertEqual(doc.invoice_number, 'INV-9')
        with self.assertRaises(IntegrityError), transaction.atomic():
            _invoice(self, self.order, 'INV-9 ')


class BackstopTests(InvoiceFixture):

    def test_the_integrity_error_path_removes_uploads_and_says_which_invoice(self):
        # The check runs twice: before upload (made to miss, as in a race) and after
        # the index refuses (real answer, to word the message).
        before = VendorOrderDocument.objects.count()
        with patch('projects.order_views._duplicate_invoice_numbers',
                   side_effect=[[], ['INV-7']]):
            response, storage = self.post_docs(**self.invoice_slot(0, 'INV-7'))
        self.assertContains(response, 'Invoice INV-7 is already recorded on this order.',
                            status_code=400)
        self.assertEqual(VendorOrderDocument.objects.count(), before)
        bucket = storage.storage.from_.return_value
        uploaded = [call.kwargs['path'] for call in bucket.upload.call_args_list]
        removed = [call.args[0][0] for call in bucket.remove.call_args_list]
        self.assertEqual(len(uploaded), 1)
        self.assertEqual(removed, uploaded)
