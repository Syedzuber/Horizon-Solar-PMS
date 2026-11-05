"""NTF-DCG 4b — optional documents attached at receipt (signed DC copy, LR/bilty,
delivery photo), tied to the GoodsReceipt the GRN submission writes.

WHAT IS PINNED
--------------
  * A1/A2: a document is a DeliveryChallanAttachment row with `receipt` and a
    `document_type`; the CHECK holds "type iff receipt" both ways.
  * A3/A5: confirm_grn and override_grn link up to five files to the receipt / the
    correction they write, in the 'grn-document' folder. Every refusal — no type, unknown
    type, no tick, too many, a refused file, storage off — comes before any upload; a
    DG-L "changed meanwhile", a rollback and a storage failure part way discard every
    upload (photos included) and leave no row.
  * Q2: documents alone are refused by the 4a empty check, in their own words.
  * A7/Q3: the Receipts section lists each receipt's documents; a withdrawn one is left
    out for a Site Engineer and listed for SCM. Opening and withdrawing are DC-B2's views.
  * Q1: a receipt document never appears on the Challan Documents card and never counts
    toward DC-B3's cap of 10.
  * The forms: RECEIPT_DOCUMENTS_MAX_FILES slots, the type options are the model's
    choices, no capture attribute (Q4), no client-side shrinking (Q5), the help text (Q8).

Storage is stubbed exactly as tests_grn_damage stubs it (the Supabase client is a mock;
any HTTP request fails the test), so a real upload is checked only on dev/production.

Run with:
    python manage.py test projects.tests_receipt_attachments --settings=solarpms.test_settings
"""
import re
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import grn_damage
from .dc_edit import live_document_count
from .delivery_storage import DC_DOCUMENTS_MAX_FILES
from .goods_receipts import (
    DOCUMENTS_NEED_A_QUANTITY, RECEIPT_DOCUMENT_STORAGE_OFF, RECEIPT_DOCUMENTS_MAX_FILES,
    RECEIPT_DOCUMENTS_NO_PRICES,
)
from .models import (
    GOODS_RECEIPT_CORRECTION, GOODS_RECEIPT_RECEIPT, RECEIPT_DOCUMENT_LR,
    RECEIPT_DOCUMENT_OTHER, RECEIPT_DOCUMENT_PHOTO, RECEIPT_DOCUMENT_SIGNED_DC,
    RECEIPT_DOCUMENT_TYPE_CHOICES, DCLineDamagePhoto, DeliveryChallanAttachment,
    GoodsReceipt,
)
from .tests_grn_damage import BUCKET, JPG, PDF, GrnDamageBase, jpg

SIGNED_COPY = 'signed-copy.pdf'


def pdf(name=SIGNED_COPY):
    return SimpleUploadedFile(name, PDF, content_type='application/pdf')


def photo(name='truck.jpg'):
    return SimpleUploadedFile(name, JPG, content_type='image/jpeg')


class ReceiptDocumentBase(GrnDamageBase):

    def payload(self, lines, docs=(), tick=True, description='', reason=''):
        """lines: {item: (received, damaged, [photos])}; docs: [(type, file)] put in slots
        1, 2, … in order."""
        data = self._payload(lines, description, reason)
        for slot, (doc_type, upload) in enumerate(docs, start=1):
            data[f'receipt_doc_type_{slot}'] = doc_type
            data[f'receipt_doc_{slot}'] = upload
        if tick:
            data['receipt_docs_no_prices'] = '1'
        return data

    def post(self, view, data, actor):
        self.login(actor)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse(view, args=[self.site.project_id, self.dc.pk]),
                                    data)

    def confirm_docs(self, lines, docs=(), **kwargs):
        return self.post('confirm_grn', self.payload(lines, docs, **kwargs), self.se)

    def override_docs(self, lines, docs=(), **kwargs):
        return self.post('override_grn', self.payload(lines, docs, **kwargs), self.scm)

    def documents(self):
        return list(DeliveryChallanAttachment.objects.filter(receipt__isnull=False)
                    .order_by('pk'))

    def challan_document(self, name='vendor-dc.pdf', withdrawn=False):
        """A DC-B2 challan-level document on self.dc."""
        row = DeliveryChallanAttachment.objects.create(
            challan=self.dc, uploaded_by=self.scm, original_name=name, bucket=BUCKET,
            path=f'GD-SITE/challan/{name}-{timezone.now().timestamp()}', size=1024,
            mime='application/pdf', no_prices_confirmed=True)
        if withdrawn:
            DeliveryChallanAttachment.objects.filter(pk=row.pk).update(
                withdrawn_at=timezone.now(), withdrawn_by=self.scm,
                withdrawn_reason='Shows the vendor rates')
        return row

    def detail(self, profile):
        self.login(profile)
        return self.client.get(reverse('delivery_challan_detail',
                                       args=[self.site.project_id, self.dc.pk])).content.decode()

    @staticmethod
    def card(page, card_id):
        """The HTML of one card on the DC page, from its id to the next card."""
        start = page.index(f'id="{card_id}"')
        end = page.find('<div class="card shadow-sm', start)
        return page[start:end if end != -1 else None]

    def assert_refused_before_upload(self, response, message):
        self.assertEqual(response.status_code, 302)
        self.assertIn(message, self.messages_of(response))
        self.assertEqual(self.uploads(), [], 'refused before any upload')
        self.assertEqual(self.documents(), [])
        self.assertFalse(GoodsReceipt.objects.exists())
        self.panels.refresh_from_db()
        self.assertIsNone(self.panels.received_quantity)
        self.assertIn('grn_draft', self.client.session, 'the draft is kept')


# ---------------------------------------------------------------------------
# Linked to the receipt
# ---------------------------------------------------------------------------

class LinkTests(ReceiptDocumentBase):

    def test_confirm_with_two_files_links_both_to_the_receipt_with_their_types(self):
        response = self.confirm_docs(
            {self.panels: ('10', '0', [])},
            [(RECEIPT_DOCUMENT_SIGNED_DC, pdf()), (RECEIPT_DOCUMENT_PHOTO, photo())])
        receipt = GoodsReceipt.objects.get()
        self.assertEqual(receipt.kind, GOODS_RECEIPT_RECEIPT)
        docs = self.documents()
        self.assertEqual([(d.original_name, d.document_type, d.mime) for d in docs],
                         [(SIGNED_COPY, RECEIPT_DOCUMENT_SIGNED_DC, 'application/pdf'),
                          ('truck.jpg', RECEIPT_DOCUMENT_PHOTO, 'image/jpeg')])
        for doc in docs:
            self.assertEqual(doc.receipt, receipt)
            self.assertEqual(doc.challan, self.dc)
            self.assertEqual(doc.uploaded_by, self.se)
            self.assertTrue(doc.no_prices_confirmed)
            self.assertTrue(doc.path.startswith('GD-SITE/grn-document/'))
            self.assertIsNone(doc.withdrawn_at)
        self.assertEqual(len(self.uploads()), 2)
        self.assertEqual(self.removes(), [])
        self.assertTrue(any(m.endswith(f'2 documents attached to {receipt.number}.')
                            for m in self.messages_of(response)))

    def test_override_with_a_file_links_it_to_the_correction(self):
        self.confirm_docs({self.panels: ('10', '0', [])})
        self.override_docs({self.panels: ('9', '0', [])}, [(RECEIPT_DOCUMENT_LR, pdf('lr.pdf'))])
        correction = GoodsReceipt.objects.get(kind=GOODS_RECEIPT_CORRECTION)
        [doc] = self.documents()
        self.assertEqual(doc.receipt, correction)
        self.assertEqual(doc.document_type, RECEIPT_DOCUMENT_LR)
        self.assertEqual(doc.uploaded_by, self.scm)

    def test_documents_ride_alongside_damage_photos(self):
        self.confirm_docs({self.panels: ('10', '1', [jpg()])},
                          [(RECEIPT_DOCUMENT_OTHER, pdf('note.pdf'))],
                          description='One panel cracked across the glass')
        receipt = GoodsReceipt.objects.get()
        self.assertEqual(DCLineDamagePhoto.objects.get().receipt, receipt)
        self.assertEqual(self.documents()[0].receipt, receipt)
        self.assertEqual(sorted(p.split('/')[1] for p in self.uploads()),
                         ['grn-damage', 'grn-document'])

    def test_a_grn_without_documents_is_unchanged(self):
        response = self.confirm_docs({self.panels: ('10', '0', [])}, tick=False)
        self.assertEqual(self.documents(), [])
        self.assertEqual(self.uploads(), [])
        self.assertIn('GRN confirmed. DC status: Partially Received.',
                      self.messages_of(response))

    def test_a_type_chosen_with_no_file_records_nothing(self):
        data = self.payload({self.panels: ('10', '0', [])}, tick=False)
        data['receipt_doc_type_1'] = RECEIPT_DOCUMENT_LR
        self.post('confirm_grn', data, self.se)
        self.assertEqual(GoodsReceipt.objects.count(), 1)
        self.assertEqual(self.documents(), [])


# ---------------------------------------------------------------------------
# Refused before any upload
# ---------------------------------------------------------------------------

class RefusalTests(ReceiptDocumentBase):

    def test_no_prices_tick_missing_is_refused_with_the_draft_kept(self):
        response = self.confirm_docs({self.panels: ('10', '0', [])},
                                     [(RECEIPT_DOCUMENT_SIGNED_DC, pdf())], tick=False)
        self.assert_refused_before_upload(response, RECEIPT_DOCUMENTS_NO_PRICES)
        self.assertIn('The GRN was not recorded.', self.messages_of(response))
        self.assertEqual(self.client.session['grn_draft']['lines'][str(self.panels.pk)]['received'],
                         '10')

    def test_a_file_without_a_type_is_refused(self):
        response = self.confirm_docs({self.panels: ('10', '0', [])}, [('', pdf())])
        self.assert_refused_before_upload(
            response, f'{SIGNED_COPY}: choose what this document is — Signed DC copy, '
                      f'LR or bilty, Delivery photo or Other.')

    def test_an_unknown_type_is_refused(self):
        response = self.confirm_docs({self.panels: ('10', '0', [])}, [('invoice', pdf())])
        self.assertIn(f'{SIGNED_COPY}: choose what this document is — Signed DC copy, '
                      f'LR or bilty, Delivery photo or Other.', self.messages_of(response))
        self.assertEqual(self.uploads(), [])

    def test_a_wrong_file_type_is_refused_before_upload(self):
        bad = SimpleUploadedFile('dc.docx', b'PK\x03\x04', content_type='application/msword')
        response = self.confirm_docs({self.panels: ('10', '0', [])},
                                     [(RECEIPT_DOCUMENT_SIGNED_DC, bad)])
        self.assert_refused_before_upload(response, 'dc.docx: must be a PDF, JPG or PNG.')

    def test_an_oversize_file_is_refused_before_upload(self):
        with mock.patch('projects.delivery_storage.DC_DOCUMENT_MAX_BYTES', 10):
            response = self.confirm_docs({self.panels: ('10', '0', [])},
                                         [(RECEIPT_DOCUMENT_SIGNED_DC, pdf())])
        self.assertEqual(self.uploads(), [])
        self.assertTrue(any(m.startswith(f'{SIGNED_COPY}: ') and 'the limit is' in m
                            for m in self.messages_of(response)))
        self.assertFalse(GoodsReceipt.objects.exists())

    def test_six_files_are_refused(self):
        docs = [(RECEIPT_DOCUMENT_OTHER, pdf(f'd{n}.pdf')) for n in range(6)]
        response = self.confirm_docs({self.panels: ('10', '0', [])}, docs)
        self.assert_refused_before_upload(
            response, 'At most 5 documents can be attached to one receipt; 6 were chosen.')

    def test_two_files_in_one_slot_count_towards_the_five(self):
        data = self.payload({self.panels: ('10', '0', [])},
                            [(RECEIPT_DOCUMENT_OTHER, pdf(f'd{n}.pdf')) for n in range(4)])
        data['receipt_doc_4'] = [pdf('d3.pdf'), pdf('extra.pdf'), pdf('extra2.pdf')]
        response = self.post('confirm_grn', data, self.se)
        self.assert_refused_before_upload(
            response, 'At most 5 documents can be attached to one receipt; 6 were chosen.')

    @override_settings(SUPABASE_DELIVERY_BUCKET='')
    def test_storage_off_is_refused_before_upload(self):
        response = self.confirm_docs({self.panels: ('10', '0', [])},
                                     [(RECEIPT_DOCUMENT_SIGNED_DC, pdf())])
        self.assert_refused_before_upload(response, RECEIPT_DOCUMENT_STORAGE_OFF)

    def test_documents_alone_are_refused_in_their_own_words(self):
        self.confirm_docs({self.panels: ('10', '0', [])})
        for which, send in (('confirm', self.confirm_docs), ('override', self.override_docs)):
            with self.subTest(form=which):
                # Posted back unchanged, as the pre-filled form does, plus a signed copy.
                self.detail(self.se)   # shows, and so drains, the earlier messages
                response = send({self.panels: ('10', '0', [])},
                                [(RECEIPT_DOCUMENT_SIGNED_DC, pdf())])
                self.assertEqual(self.messages_of(response), [DOCUMENTS_NEED_A_QUANTITY])
                self.assertEqual(GoodsReceipt.objects.count(), 1)
                self.assertEqual(self.documents(), [])
                self.assertEqual(len(self.uploads()), 0)

    def test_documents_alone_win_over_their_own_refusals(self):
        # No tick and no type, but no quantity change either: the real reason first.
        self.confirm_docs({self.panels: ('10', '0', [])})
        self.detail(self.se)   # shows, and so drains, the first GRN's success message
        response = self.confirm_docs({self.panels: ('10', '0', [])}, [('', pdf())], tick=False)
        self.assertEqual(self.messages_of(response), [DOCUMENTS_NEED_A_QUANTITY])

    def test_an_override_refusal_keeps_the_reason_and_uploads_nothing(self):
        response = self.override_docs({self.panels: ('10', '0', [])},
                                      [(RECEIPT_DOCUMENT_LR, pdf('lr.pdf'))], tick=False,
                                      reason='Site engineer unreachable today')
        self.assert_refused_before_upload(response, RECEIPT_DOCUMENTS_NO_PRICES)
        self.assertIn('The GRN was not changed.', self.messages_of(response))
        self.assertEqual(self.client.session['grn_draft']['on_behalf_reason'],
                         'Site engineer unreachable today')


# ---------------------------------------------------------------------------
# Uploaded, then discarded
# ---------------------------------------------------------------------------

class DiscardTests(ReceiptDocumentBase):

    def assert_all_discarded(self):
        uploaded = self.uploads()
        removed = [path for call in self.removes() for path in call.args[0]]
        self.assertTrue(uploaded)
        self.assertEqual(sorted(removed), sorted(uploaded), 'every upload discarded')
        self.assertEqual(self.documents(), [])
        self.assertFalse(DCLineDamagePhoto.objects.exists())
        self.assertFalse(GoodsReceipt.objects.exists())

    def test_changed_meanwhile_discards_the_uploads_and_writes_no_row(self):
        with mock.patch('projects.views.reload_grn_lines',
                        side_effect=grn_damage.ChallanChangedMeanwhile(
                            grn_damage.GRN_CHANGED_MEANWHILE)):
            response = self.confirm_docs(
                {self.panels: ('10', '1', [jpg()])},
                [(RECEIPT_DOCUMENT_SIGNED_DC, pdf()), (RECEIPT_DOCUMENT_PHOTO, photo())],
                description='One panel cracked across the glass')
        self.assertIn(grn_damage.GRN_CHANGED_MEANWHILE, self.messages_of(response))
        self.assertEqual(len(self.uploads()), 3)
        self.assert_all_discarded()
        self.assertIn('grn_draft', self.client.session)

    def test_changed_meanwhile_on_an_override_discards_too(self):
        with mock.patch('projects.views.reload_grn_lines',
                        side_effect=grn_damage.ChallanChangedMeanwhile(
                            grn_damage.GRN_CHANGED_MEANWHILE)):
            self.override_docs({self.panels: ('10', '0', [])},
                               [(RECEIPT_DOCUMENT_LR, pdf('lr.pdf'))],
                               reason='Site engineer unreachable today')
        self.assert_all_discarded()

    def test_a_rollback_discards_the_uploads(self):
        with mock.patch('projects.views.recalculate_dc_status',
                        side_effect=RuntimeError('db down')):
            with self.assertRaises(RuntimeError):
                self.confirm_docs({self.panels: ('10', '0', [])},
                                  [(RECEIPT_DOCUMENT_SIGNED_DC, pdf())])
        self.assert_all_discarded()

    def test_a_storage_failure_part_way_discards_documents_and_photos(self):
        calls = {'n': 0}

        def fail_on_third(**kwargs):
            calls['n'] += 1
            if calls['n'] == 3:
                raise RuntimeError('network gone')
            return {}

        self.bucket_api.upload.side_effect = fail_on_third
        response = self.confirm_docs(
            {self.panels: ('10', '1', [jpg()])},
            [(RECEIPT_DOCUMENT_SIGNED_DC, pdf()), (RECEIPT_DOCUMENT_PHOTO, photo())],
            description='One panel cracked across the glass')
        self.assertTrue(any('Upload to storage failed' in m for m in self.messages_of(response)))
        # Three attempted, the first two stored (photo, signed copy) and both removed.
        removed = [path for call in self.removes() for path in call.args[0]]
        self.assertEqual(sorted(removed), sorted(self.uploads()[:2]))
        self.assertEqual(self.documents(), [])
        self.assertFalse(GoodsReceipt.objects.exists())


# ---------------------------------------------------------------------------
# Display, open, withdraw
# ---------------------------------------------------------------------------

class DisplayTests(ReceiptDocumentBase):

    def setUp(self):
        super().setUp()
        self.confirm_docs({self.panels: ('10', '0', [])},
                          [(RECEIPT_DOCUMENT_SIGNED_DC, pdf()),
                           (RECEIPT_DOCUMENT_LR, pdf('bilty.pdf'))])
        self.signed, self.bilty = self.documents()
        DeliveryChallanAttachment.objects.filter(pk=self.bilty.pk).update(
            withdrawn_at=timezone.now(), withdrawn_by=self.scm,
            withdrawn_reason='Bilty shows the freight rate')

    def test_the_receipt_lists_its_documents_with_type_name_and_uploader(self):
        section = self.card(self.detail(self.se), 'dc-receipts')
        self.assertIn('Signed DC copy', section)
        self.assertIn(SIGNED_COPY, section)
        self.assertIn(self.se.user.get_full_name(), section)
        self.assertIn(reverse('delivery_challan_attachment_open',
                              args=[self.site.project_id, self.dc.pk, self.signed.pk]), section)
        self.assertIn('· 1 document', section)

    def test_a_withdrawn_file_is_hidden_from_se_and_shown_to_scm(self):
        se_section = self.card(self.detail(self.se), 'dc-receipts')
        self.assertNotIn('bilty.pdf', se_section)
        self.assertNotIn('Withdrawn', se_section)
        self.assertNotIn('attachment_withdraw', se_section)
        self.assertNotIn('/withdraw/', se_section)

        scm_section = self.card(self.detail(self.scm), 'dc-receipts')
        self.assertIn('bilty.pdf', scm_section)
        self.assertIn('Withdrawn', scm_section)
        self.assertIn('Bilty shows the freight rate', scm_section)
        self.assertIn(reverse('delivery_challan_attachment_withdraw',
                              args=[self.site.project_id, self.dc.pk, self.signed.pk]),
                      scm_section)

    def test_the_open_view_follows_the_dc_b2_rule(self):
        def open_as(profile, doc):
            self.login(profile)
            return self.client.get(reverse('delivery_challan_attachment_open',
                                           args=[self.site.project_id, self.dc.pk, doc.pk]))
        self.assertEqual(open_as(self.se, self.signed).status_code, 302)
        self.assertEqual(open_as(self.se, self.bilty).status_code, 404)
        self.assertEqual(open_as(self.scm, self.bilty).status_code, 302)

    def test_scm_withdraws_a_receipt_document_through_the_dc_b2_view(self):
        self.login(self.scm)
        self.client.post(reverse('delivery_challan_attachment_withdraw',
                                 args=[self.site.project_id, self.dc.pk, self.signed.pk]),
                         {'withdrawn_reason': 'Shows the unit rate in the corner'})
        self.signed.refresh_from_db()
        self.assertFalse(self.signed.is_live)
        self.assertEqual(self.signed.receipt.number, GoodsReceipt.objects.get().number)
        self.assertNotIn(SIGNED_COPY, self.card(self.detail(self.se), 'dc-receipts'))

    def test_more_documents_cost_nothing_more(self):
        self.detail(self.scm)
        with CaptureQueriesContext(connection) as before:
            self.detail(self.scm)
        self.override_docs({self.panels: ('9', '0', [])},
                           [(RECEIPT_DOCUMENT_OTHER, pdf('a.pdf')),
                            (RECEIPT_DOCUMENT_PHOTO, photo('b.jpg'))])
        self.detail(self.scm)
        with CaptureQueriesContext(connection) as after:
            self.detail(self.scm)
        self.assertEqual(len(after), len(before))


class ChallanDocumentsUnaffectedTests(ReceiptDocumentBase):
    """Ruling Q1: the two kinds never mix on the Challan Documents card or in the cap."""

    def setUp(self):
        super().setUp()
        self.confirm_docs({self.panels: ('10', '0', [])},
                          [(RECEIPT_DOCUMENT_SIGNED_DC, pdf())])

    def test_receipt_files_never_appear_on_the_challan_documents_card(self):
        self.challan_document('vendor-dc.pdf')
        for profile in (self.se, self.scm):
            with self.subTest(role=profile.role):
                card = self.card(self.detail(profile), 'dc-documents')
                self.assertIn('vendor-dc.pdf', card)
                self.assertNotIn(SIGNED_COPY, card)

    def test_only_receipt_files_still_reads_none_attached(self):
        self.assertIn('No challan document attached.',
                      self.card(self.detail(self.se), 'dc-documents'))

    def test_receipt_files_never_count_toward_the_cap_of_ten(self):
        for n in range(DC_DOCUMENTS_MAX_FILES - 1):
            self.challan_document(f'v{n}.pdf')
        self.challan_document('gone.pdf', withdrawn=True)
        self.assertEqual(live_document_count(self.dc), DC_DOCUMENTS_MAX_FILES - 1)
        self.login(self.scm)
        page = self.client.get(reverse('delivery_challan_edit',
                                       args=[self.site.project_id, self.dc.pk]))
        self.assertEqual(page.context['live_documents'], DC_DOCUMENTS_MAX_FILES - 1)


# ---------------------------------------------------------------------------
# The model and the forms
# ---------------------------------------------------------------------------

class ModelTests(ReceiptDocumentBase):

    def row(self, **kwargs):
        fields = dict(challan=self.dc, uploaded_by=self.scm, original_name='x.pdf',
                      bucket=BUCKET, path=f'p-{timezone.now().timestamp()}', size=1,
                      mime='application/pdf', no_prices_confirmed=True)
        fields.update(kwargs)
        return DeliveryChallanAttachment.objects.create(**fields)

    def test_a_receipt_document_must_say_what_it_is(self):
        receipt = GoodsReceipt.objects.create(challan=self.dc, kind=GOODS_RECEIPT_RECEIPT,
                                              recorded_by=self.se,
                                              receipt_date=timezone.localdate())
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.row(receipt=receipt)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.row(receipt=receipt, document_type='invoice')
        self.assertEqual(self.row(receipt=receipt, document_type=RECEIPT_DOCUMENT_LR).receipt,
                         receipt)

    def test_a_challan_document_never_carries_a_type(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.row(document_type=RECEIPT_DOCUMENT_SIGNED_DC)
        self.assertEqual(self.row().document_type, '')


class FormTests(ReceiptDocumentBase):

    def forms(self):
        page = self.detail(self.se)
        modal = self.detail(self.scm)
        se_form = page[page.index('action="' + reverse('confirm_grn', args=[self.site.project_id, self.dc.pk])):]
        se_form = se_form[:se_form.index('</form>')]
        scm_form = modal[modal.index('action="' + reverse('override_grn', args=[self.site.project_id, self.dc.pk])):]
        scm_form = scm_form[:scm_form.index('</form>')]
        return {'confirm': se_form, 'override': scm_form}

    def test_both_forms_carry_the_document_slots(self):
        expected_options = [value for value, _ in RECEIPT_DOCUMENT_TYPE_CHOICES]
        for which, form in self.forms().items():
            with self.subTest(form=which):
                files = re.findall(r'<input type="file" name="receipt_doc_(\d+)"[^>]*>', form)
                self.assertEqual(files, [str(n) for n in range(1, RECEIPT_DOCUMENTS_MAX_FILES + 1)])
                selects = re.findall(r'<select name="receipt_doc_type_(\d+)"', form)
                self.assertEqual(len(selects), RECEIPT_DOCUMENTS_MAX_FILES)
                first = form[form.index('name="receipt_doc_type_1"'):]
                first = first[:first.index('</select>')]
                self.assertEqual(re.findall(r'<option value="([^"]+)"', first), expected_options)
                self.assertIn('name="receipt_docs_no_prices" value="1"', form)
                self.assertIn('Signed challan copy, LR/bilty or delivery photo. Site Engineers '
                              'and Finance will see this file — no rates or amounts.', form)

    def test_no_capture_and_no_client_side_shrinking(self):
        for which, form in self.forms().items():
            with self.subTest(form=which):
                for tag in re.findall(r'<input type="file" name="receipt_doc_\d+"[^>]*>', form):
                    self.assertNotIn('capture', tag)
                    self.assertNotIn('data-qaqc-photo', tag)
                    self.assertNotIn('multiple', tag)
                    self.assertIn('accept=".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png"',
                                  tag)

    def test_the_refusal_alert_names_photos_and_documents(self):
        self.confirm_docs({self.panels: ('10', '0', [])},
                          [(RECEIPT_DOCUMENT_SIGNED_DC, pdf())], tick=False)
        page = ' '.join(self.detail(self.se).split())
        self.assertIn('attach the photos and documents again', page)
