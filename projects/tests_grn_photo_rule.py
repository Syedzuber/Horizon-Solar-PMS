"""GRN-P — a Site Engineer's GRN that raises a received quantity must carry a photo.

WHAT IS PINNED
--------------
  * M1: confirm_grn only — an SCM override that raises a quantity needs no image.
  * M2: only a RAISE needs one (posted received above what the line holds, a line never
    recorded counting as 0). Damage-only, notes-only, downward corrections and a 0 on a
    never-recorded line pass without an image.
  * M3: any image in the same submission — a JPG/PNG receipt document of ANY type, or a
    damage photo. A PDF does not count; a photo already on record does not count.
  * M4: the refusal comes after G4, before any upload, joined to the document refusals
    (one message list, then "The GRN was not recorded."), draft kept.
  * Tick ruling (amends 4b A4): no no-prices tick when EVERY document is a JPG/PNG typed
    "Delivery photo"; a PDF or any other type still needs it.
  * M5/M6: the SE form's document slots carry data-grn-doc-shrink (the override form's
    and the damage inputs do not), the shrink script acts on images only at 2048 / 0.85;
    the photo-required line; slot 1 starts on "Delivery photo"; the SE heading drops
    "(optional)" (the override form's keeps it).
  * Follow-up: a PDF typed "Delivery photo" is refused (RECEIPT_PDF_NOT_A_PHOTO) on both
    forms, once per submission, joined with the other document refusals, draft kept.

Every post here is exact (photo=False): the shared helper that adds a delivery photo to
other modules' raises is NOT used, so each test posts precisely what it names.

Run with:
    python manage.py test projects.tests_grn_photo_rule --settings=solarpms.test_settings
"""
import re
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from .goods_receipts import (
    RECEIPT_DOCUMENTS_NO_PRICES, RECEIPT_PDF_NOT_A_PHOTO, RECEIPT_PHOTO_REQUIRED,
)
from .models import (
    GOODS_RECEIPT_CORRECTION, RECEIPT_DOCUMENT_OTHER, RECEIPT_DOCUMENT_PHOTO,
    RECEIPT_DOCUMENT_SIGNED_DC, DCLineDamagePhoto, DCLineItem, GoodsReceipt,
)
from .tests_grn_damage import PNG
from .tests_receipt_attachments import ReceiptDocumentBase, pdf, photo

NOT_RECORDED = 'The GRN was not recorded.'


def png(name='packing-list.png'):
    return SimpleUploadedFile(name, PNG, content_type='image/png')


class PhotoRuleBase(ReceiptDocumentBase):

    def confirm_exact(self, lines, docs=(), tick=False, description=''):
        """confirm_grn with exactly these lines and documents — no helper photo."""
        return self.confirm_docs(lines, docs, photo=False, tick=tick, description=description)

    def stored(self, line, received, damaged=0, notes=''):
        """Put a line in a recorded state directly, as an earlier GRN would have left it."""
        DCLineItem.objects.filter(pk=line.pk).update(
            received_quantity=Decimal(received), damaged_quantity=damaged, grn_notes=notes)
        line.refresh_from_db()

    def assert_recorded(self, response):
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(RECEIPT_PHOTO_REQUIRED, self.messages_of(response))
        self.assertNotIn(NOT_RECORDED, self.messages_of(response))
        self.assertEqual(GoodsReceipt.objects.count(), 1)

    def assert_photo_refusal(self, response):
        """Refused for the photo: message then the closing line, nothing uploaded or
        written, the typed values kept as the draft."""
        messages = self.messages_of(response)
        self.assertIn(RECEIPT_PHOTO_REQUIRED, messages)
        self.assertEqual(messages[-1], NOT_RECORDED)
        self.assertEqual(self.uploads(), [], 'refused before any upload')
        self.assertEqual(self.documents(), [])
        self.assertFalse(GoodsReceipt.objects.exists())
        self.assertIn('grn_draft', self.client.session, 'the draft is kept')


# ---------------------------------------------------------------------------
# M2/M3/M4 — a raise needs an image
# ---------------------------------------------------------------------------

class RaiseNeedsAnImageTests(PhotoRuleBase):

    def test_a_raise_without_an_image_is_refused_draft_kept_nothing_uploaded(self):
        response = self.confirm_exact({self.panels: ('10', '0', [])})
        self.assert_photo_refusal(response)
        self.assertEqual(self.messages_of(response), [RECEIPT_PHOTO_REQUIRED, NOT_RECORDED])
        self.panels.refresh_from_db()
        self.assertIsNone(self.panels.received_quantity)
        self.assertEqual(self.client.session['grn_draft']['lines'][str(self.panels.pk)]['received'],
                         '10')

    def test_a_raise_on_a_line_already_recorded_is_refused_too(self):
        self.stored(self.panels, '6')
        response = self.confirm_exact({self.panels: ('10', '0', [])})
        self.assert_photo_refusal(response)
        self.panels.refresh_from_db()
        self.assertEqual(self.panels.received_quantity, Decimal('6'))

    def test_a_jpg_delivery_photo_passes_with_no_tick(self):
        response = self.confirm_exact({self.panels: ('10', '0', [])},
                                      [(RECEIPT_DOCUMENT_PHOTO, photo())])
        self.assert_recorded(response)
        [doc] = self.documents()
        self.assertEqual((doc.document_type, doc.mime, doc.receipt),
                         (RECEIPT_DOCUMENT_PHOTO, 'image/jpeg', GoodsReceipt.objects.get()))
        self.assertTrue(doc.no_prices_confirmed)   # the CHECK holds it true (see goods_receipts)
        self.assertEqual(len(self.uploads()), 1)

    def test_a_png_delivery_photo_passes(self):
        response = self.confirm_exact({self.panels: ('10', '0', [])},
                                      [(RECEIPT_DOCUMENT_PHOTO, png())])
        self.assert_recorded(response)
        self.assertEqual(self.documents()[0].mime, 'image/png')

    def test_an_image_of_any_type_counts(self):
        # M3: "any type" — a JPG typed Signed DC copy (a photo of the signed challan).
        response = self.confirm_exact({self.panels: ('10', '0', [])},
                                      [(RECEIPT_DOCUMENT_SIGNED_DC, photo('signed-dc.jpg'))],
                                      tick=True)
        self.assert_recorded(response)

    def test_only_a_pdf_is_refused(self):
        response = self.confirm_exact({self.panels: ('10', '0', [])},
                                      [(RECEIPT_DOCUMENT_SIGNED_DC, pdf())], tick=True)
        self.assert_photo_refusal(response)
        self.assertEqual(self.messages_of(response), [RECEIPT_PHOTO_REQUIRED, NOT_RECORDED])

    def test_a_pdf_typed_delivery_photo_is_not_an_image(self):
        # Refused for its type AND, being no image, for the photo — one refusal.
        response = self.confirm_exact({self.panels: ('10', '0', [])},
                                      [(RECEIPT_DOCUMENT_PHOTO, pdf('photo.pdf'))], tick=True)
        self.assert_photo_refusal(response)
        self.assertEqual(self.messages_of(response),
                         [RECEIPT_PDF_NOT_A_PHOTO, RECEIPT_PHOTO_REQUIRED, NOT_RECORDED])

    def test_only_a_damage_photo_passes(self):
        response = self.confirm_exact({self.panels: ('10', '1', [self.damage_jpg()])},
                                      description='One panel cracked across the glass')
        self.assert_recorded(response)
        self.assertEqual(self.documents(), [])
        self.assertEqual(DCLineDamagePhoto.objects.count(), 1)

    def test_a_photo_already_on_record_does_not_count(self):
        # A live damage photo on the line from an earlier GRN; this raise brings none.
        self.stored(self.panels, '6', damaged=1)
        self.damage_issue()
        response = self.confirm_exact({self.panels: ('10', '1', [])})
        self.assert_photo_refusal(response)

    def test_the_photo_message_joins_the_document_refusals(self):
        # A PDF without the tick: the tick refusal and the photo refusal come together,
        # so the Site Engineer re-attaches once.
        response = self.confirm_exact({self.panels: ('10', '0', [])},
                                      [(RECEIPT_DOCUMENT_SIGNED_DC, pdf())], tick=False)
        self.assertEqual(self.messages_of(response),
                         [RECEIPT_DOCUMENTS_NO_PRICES, RECEIPT_PHOTO_REQUIRED, NOT_RECORDED])
        self.assertEqual(self.uploads(), [])

    def test_g4_comes_first(self):
        # Nothing changed at all: G4's own message, not the photo rule.
        self.stored(self.panels, '10', notes='note %d' % self.panels.pk)
        response = self.confirm_exact({self.panels: ('10', '0', [])})
        self.assertNotIn(RECEIPT_PHOTO_REQUIRED, self.messages_of(response))
        self.assertFalse(GoodsReceipt.objects.exists())

    def damage_jpg(self):
        return SimpleUploadedFile('crack.jpg', b'\xff\xd8\xff\xe0\x00\x10JFIF crack',
                                  content_type='image/jpeg')


# ---------------------------------------------------------------------------
# M2 — what is not a raise passes without an image
# ---------------------------------------------------------------------------

class NotARaiseTests(PhotoRuleBase):

    def test_a_damage_only_change_passes_without_an_image(self):
        # Received unchanged; one more unit damaged. The line already has a live damage
        # photo and the challan an open damage issue, so DG-B asks for nothing new.
        self.stored(self.panels, '10', damaged=1, notes='note %d' % self.panels.pk)
        self.damage_issue()
        response = self.confirm_exact({self.panels: ('10', '2', [])})
        self.assert_recorded(response)
        self.panels.refresh_from_db()
        self.assertEqual(self.panels.damaged_quantity, 2)

    def test_a_notes_only_change_passes_without_an_image(self):
        self.stored(self.panels, '10', notes='first look')
        response = self.confirm_exact({self.panels: ('10', '0', [])})   # posts 'note <pk>'
        self.assert_recorded(response)
        self.panels.refresh_from_db()
        self.assertEqual(self.panels.grn_notes, f'note {self.panels.pk}')

    def test_a_downward_correction_passes_without_an_image(self):
        self.stored(self.panels, '10')
        response = self.confirm_exact({self.panels: ('8', '0', [])})
        self.assert_recorded(response)
        self.panels.refresh_from_db()
        self.assertEqual(self.panels.received_quantity, Decimal('8'))

    def test_zero_on_a_never_recorded_line_passes_without_an_image(self):
        # "Not recorded" counts as 0, so 0 is not a raise (the walkthrough's WALK-DC-0004).
        response = self.confirm_exact({self.panels: ('0', '0', [])})
        self.assert_recorded(response)

    def test_one_line_raised_among_others_still_needs_the_image(self):
        self.stored(self.panels, '10')
        response = self.confirm_exact({self.panels: ('8', '0', []),
                                       self.inverter: ('2', '0', [])})
        self.assert_photo_refusal(response)


# ---------------------------------------------------------------------------
# M1 — the SCM override is not asked
# ---------------------------------------------------------------------------

class OverrideNotAskedTests(PhotoRuleBase):

    def test_an_scm_override_raise_without_an_image_passes(self):
        response = self.override_docs({self.panels: ('10', '0', [])}, tick=False,
                                      reason='Site engineer unreachable today')
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(RECEIPT_PHOTO_REQUIRED, self.messages_of(response))
        self.assertEqual(GoodsReceipt.objects.get().kind, GOODS_RECEIPT_CORRECTION)
        self.panels.refresh_from_db()
        self.assertEqual(self.panels.received_quantity, Decimal('10'))


# ---------------------------------------------------------------------------
# Tick ruling — delivery photos alone need no no-prices tick
# ---------------------------------------------------------------------------

class TickRulingTests(PhotoRuleBase):

    def test_two_delivery_photos_need_no_tick(self):
        response = self.confirm_exact({self.panels: ('10', '0', [])},
                                      [(RECEIPT_DOCUMENT_PHOTO, photo('a.jpg')),
                                       (RECEIPT_DOCUMENT_PHOTO, png('b.png'))])
        self.assert_recorded(response)
        self.assertEqual(len(self.documents()), 2)

    def test_a_delivery_photo_beside_a_pdf_needs_the_tick(self):
        response = self.confirm_exact({self.panels: ('10', '0', [])},
                                      [(RECEIPT_DOCUMENT_PHOTO, photo()),
                                       (RECEIPT_DOCUMENT_SIGNED_DC, pdf())])
        self.assertEqual(self.messages_of(response), [RECEIPT_DOCUMENTS_NO_PRICES, NOT_RECORDED])
        self.assertEqual(self.uploads(), [])
        self.assertFalse(GoodsReceipt.objects.exists())

    def test_an_image_typed_other_needs_the_tick(self):
        response = self.confirm_exact({self.panels: ('10', '0', [])},
                                      [(RECEIPT_DOCUMENT_OTHER, photo('invoice.jpg'))])
        self.assertEqual(self.messages_of(response), [RECEIPT_DOCUMENTS_NO_PRICES, NOT_RECORDED])

    def test_a_pdf_typed_delivery_photo_needs_the_tick(self):
        response = self.confirm_exact({self.panels: ('10', '0', [])},
                                      [(RECEIPT_DOCUMENT_PHOTO, pdf('photo.pdf'))])
        self.assertEqual(self.messages_of(response),
                         [RECEIPT_PDF_NOT_A_PHOTO, RECEIPT_DOCUMENTS_NO_PRICES,
                          RECEIPT_PHOTO_REQUIRED, NOT_RECORDED])


# ---------------------------------------------------------------------------
# Follow-up: a PDF cannot be typed "Delivery photo" (both forms)
# ---------------------------------------------------------------------------

class PdfNotADeliveryPhotoTests(PhotoRuleBase):

    def test_refused_beside_a_real_photo_with_the_draft_kept(self):
        # The JPG satisfies the photo rule and the tick is given: the type is the only fault.
        response = self.confirm_exact({self.panels: ('10', '0', [])},
                                      [(RECEIPT_DOCUMENT_PHOTO, photo()),
                                       (RECEIPT_DOCUMENT_PHOTO, pdf('scan.pdf'))], tick=True)
        self.assertEqual(self.messages_of(response), [RECEIPT_PDF_NOT_A_PHOTO, NOT_RECORDED])
        self.assertEqual(self.uploads(), [], 'refused before any upload')
        self.assertEqual(self.documents(), [])
        self.assertFalse(GoodsReceipt.objects.exists())
        self.assertIn('grn_draft', self.client.session, 'the draft is kept')

    def test_joined_with_the_other_document_refusals(self):
        response = self.confirm_exact({self.panels: ('10', '0', [])},
                                      [(RECEIPT_DOCUMENT_PHOTO, photo()),
                                       (RECEIPT_DOCUMENT_PHOTO, pdf('scan.pdf')),
                                       ('', pdf('untyped.pdf'))], tick=False)
        # In slot order (scan.pdf is slot 2, untyped.pdf slot 3), then the tick.
        self.assertEqual(self.messages_of(response), [
            RECEIPT_PDF_NOT_A_PHOTO,
            'untyped.pdf: choose what this document is — Signed DC copy, LR or bilty, '
            'Delivery photo or Other.',
            RECEIPT_DOCUMENTS_NO_PRICES, NOT_RECORDED])

    def test_said_once_for_two_such_pdfs(self):
        response = self.confirm_exact({self.panels: ('10', '0', [])},
                                      [(RECEIPT_DOCUMENT_PHOTO, photo()),
                                       (RECEIPT_DOCUMENT_PHOTO, pdf('a.pdf')),
                                       (RECEIPT_DOCUMENT_PHOTO, pdf('b.pdf'))], tick=True)
        self.assertEqual(self.messages_of(response).count(RECEIPT_PDF_NOT_A_PHOTO), 1)

    def test_the_same_pdf_typed_signed_dc_is_accepted(self):
        response = self.confirm_exact({self.panels: ('10', '0', [])},
                                      [(RECEIPT_DOCUMENT_PHOTO, photo()),
                                       (RECEIPT_DOCUMENT_SIGNED_DC, pdf('scan.pdf'))], tick=True)
        self.assert_recorded(response)
        self.assertEqual(len(self.documents()), 2)

    def test_the_override_form_refuses_it_too(self):
        response = self.override_docs({self.panels: ('10', '0', [])},
                                      [(RECEIPT_DOCUMENT_PHOTO, pdf('scan.pdf'))],
                                      reason='Site engineer unreachable today')
        self.assertEqual(self.messages_of(response),
                         [RECEIPT_PDF_NOT_A_PHOTO, 'The GRN was not changed.'])
        self.assertEqual(self.uploads(), [])
        self.assertFalse(GoodsReceipt.objects.exists())
        self.assertEqual(self.client.session['grn_draft']['on_behalf_reason'],
                         'Site engineer unreachable today')

    def test_the_override_form_follows_the_same_tick_ruling(self):
        # A4 is shared by both GRN forms, so the amendment is too.
        response = self.override_docs({self.panels: ('10', '0', [])},
                                      [(RECEIPT_DOCUMENT_PHOTO, photo())], tick=False,
                                      reason='Site engineer unreachable today')
        self.assertNotIn(RECEIPT_DOCUMENTS_NO_PRICES, self.messages_of(response))
        self.assertEqual(len(self.documents()), 1)


# ---------------------------------------------------------------------------
# M5/M6 — the form
# ---------------------------------------------------------------------------

class FormTests(PhotoRuleBase):

    def forms(self):
        page = self.detail(self.se)
        modal = self.detail(self.scm)
        se_form = page[page.index('action="' + reverse('confirm_grn', args=[self.site.project_id, self.dc.pk])):]
        se_form = se_form[:se_form.index('</form>')]
        scm_form = modal[modal.index('action="' + reverse('override_grn', args=[self.site.project_id, self.dc.pk])):]
        scm_form = scm_form[:scm_form.index('</form>')]
        return page, se_form, scm_form

    def test_the_se_form_says_a_photo_is_required(self):
        _page, se_form, scm_form = self.forms()
        self.assertIn('Photo required when you record received quantities.', se_form)
        self.assertNotIn('Photo required when you record received quantities.', scm_form)

    def test_the_se_heading_is_not_optional_the_override_heading_is(self):
        _page, se_form, scm_form = self.forms()
        se_docs = se_form[se_form.index('data-grn-docs'):]
        scm_docs = scm_form[scm_form.index('data-grn-docs'):]
        self.assertIn('<div class="small fw-semibold mb-1">Delivery documents</div>', se_docs)
        self.assertNotIn('(optional)', se_docs)
        self.assertIn('Delivery documents <span class="text-muted fw-normal">(optional)</span>',
                      scm_docs)

    def test_slot_one_starts_on_delivery_photo_on_the_se_form_only(self):
        _page, se_form, scm_form = self.forms()
        selected = re.findall(r'<select name="receipt_doc_type_(\d+)".*?</select>', se_form, re.S)
        self.assertEqual(len(selected), 5)
        for n in range(1, 6):
            block = re.search(r'<select name="receipt_doc_type_%d".*?</select>' % n, se_form,
                              re.S).group(0)
            chosen = re.findall(r'<option value="([^"]*)" selected', block)
            self.assertEqual(chosen, ['delivery_photo'] if n == 1 else [], f'slot {n}')
        self.assertNotIn(' selected>', scm_form[scm_form.index('data-grn-docs'):])

    def test_only_the_se_document_slots_shrink(self):
        _page, se_form, scm_form = self.forms()
        se_docs = re.findall(r'<input type="file" name="receipt_doc_\d+"[^>]*>', se_form)
        self.assertEqual(len(se_docs), 5)
        for tag in se_docs:
            self.assertIn('data-grn-doc-shrink', tag)
            self.assertNotIn('data-qaqc-photo', tag)
        for tag in re.findall(r'<input type="file" name="receipt_doc_\d+"[^>]*>', scm_form):
            self.assertNotIn('data-grn-doc-shrink', tag)
        # The damage-photo inputs keep the shared partial's script and only that.
        for form in (se_form, scm_form):
            for tag in re.findall(r'<input type="file" name="damage_photos_\d+"[^>]*>', form):
                self.assertIn('data-qaqc-photo', tag)
                self.assertNotIn('data-grn-doc-shrink', tag)

    def test_the_shrink_script_is_images_only_at_2048_and_085(self):
        page, _se_form, _scm_form = self.forms()
        start = page.index('GRN-P (M5)')
        script = page[start:page.index('</script>', start)]
        self.assertIn('var MAX_EDGE = 2048, QUALITY = 0.85;', script)
        self.assertIn("input.matches('input[data-grn-doc-shrink]')", script)
        self.assertIn('if (!file || !isImage(file)) return;   /* a PDF is never held or touched */',
                      script)
        self.assertIn("/^image\\/(jpeg|png)$/i", script)
        self.assertIn("'image/jpeg', QUALITY", script)
        # The shared partial is unchanged: 1600 / 0.8 for the damage photos.
        self.assertIn('var MAX_EDGE = 1600, QUALITY = 0.8;', page)
