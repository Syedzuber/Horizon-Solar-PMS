"""
FIN-M-B5-4a — Finance acts on a client receipt: the receipt page (views_receipts.
receipt_detail), its five action URLs (confirm, query, answer, reject, undo), the proof
link, the record page's row links, the two client-receipt sections on Finance work, the
writer's new unconfirm_receipt() and confirm note rule, and the shared rupee formatter.

Pins: the CLIENT_RECEIPTS_LIVE switch (off: every URL 404, no Finance work section); who
may open a receipt (Finance, the recorder, the PM side, the site's BD on M1 — 403 for
everyone else who can see the site, 404 for those who cannot and on a deleted site);
confirm (FR11, FR20 with the note rule of ruling 3, FR24, not while queried); query and
answer (FR19/FR27, the raiser never answers, the PM side answers for a recorder who has
left); reject (from recorded and queried, final, allowed on a Cancelled site); undo
(FR35: any Finance user, note required, stamps cleared, history kept, one transition and
one log line, FR11 again on re-confirm); likely duplicates; the proof (signed per click,
each open logged, refused opens log nothing); the return to the receipt page or to
Finance work (ruling 1); the Finance work lists, tile, search and fixed query count; the
formatter; and F10 — no action touches the milestone or its tasks (each action's own
FR36 alert, FIN-M-B5-4b, is asserted in the F10 test, callbacks executed).

Run with:
    python manage.py test projects.tests_receipt_finance --settings=solarpms.test_settings
"""
import datetime
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import views as views_module
from . import views_finance_work
from .models import (
    ActivityLog, NotificationLog, PaymentMilestone, PaymentReceipt,
    PaymentReceiptQuery, Project, StatusTransition, SUBJECT_PAYMENT_RECEIPT, Task,
)
from .money_format import format_inr, format_rupees
from .receipts import (
    NOT_ALLOWED, ReceiptRefused, confirm_receipt, query_receipt, reject_receipt,
    unconfirm_receipt,
)
from .tests_receipt_form import LIVE, ReceiptFormBase
from .views_receipts import ANSWERED, CONFIRMED, REJECTED, UNCONFIRMED

SIGNED = 'https://signed.example.test/receipt_proofs/1/abc.pdf?token=t'
SIGNER = 'projects.views_receipts.signed_receipt_proof_url'


class FinanceReceiptBase(ReceiptFormBase):
    """ReceiptFormBase's site and people (Finance fin and fin_b, PM, coordinators, BD,
    another BD, CEO, Admin, System Admin, an SE on the site and one elsewhere)."""

    def detail_url(self, receipt, **query):
        url = reverse('receipt_detail', args=[receipt.milestone.project.project_id, receipt.pk])
        if query:
            from urllib.parse import urlencode
            url += '?' + urlencode(query)
        return url

    def action_url(self, name, receipt):
        return reverse(f'receipt_{name}',
                       args=[receipt.milestone.project.project_id, receipt.pk])

    def act(self, who, name, receipt, follow=False, **data):
        return self.client_for(who).post(self.action_url(name, receipt), data, follow=follow)

    def messages_of(self, response):
        return [str(m) for m in response.context['messages']]

    def fresh(self, receipt):
        return PaymentReceipt.objects.get(pk=receipt.pk)

    def ledger(self):
        return StatusTransition.objects.filter(subject_type=SUBJECT_PAYMENT_RECEIPT).count()

    def receipt_lines(self):
        return ActivityLog.objects.filter(entity_type='PaymentReceipt').count()

    def written(self):
        """Everything a receipt action may write."""
        return (self.ledger(), self.receipt_lines(), PaymentReceiptQuery.objects.count(),
                list(PaymentReceipt.objects.order_by('pk').values_list(
                    'pk', 'status', 'confirmed_by_id', 'overpayment_note', 'rejected_by_id')))


# ===========================================================================
# 1. The switch
# ===========================================================================

class SwitchTests(FinanceReceiptBase):

    def test_off_every_url_is_404_and_finance_work_has_no_receipt_section(self):
        receipt = self.record(self.pm, self.m1)
        PaymentReceipt.objects.filter(pk=receipt.pk).update(
            proof_bucket='delivery-bkt', proof_path='receipt_proofs/1/abc.pdf')
        with override_settings(CLIENT_RECEIPTS_LIVE=False):
            client = self.client_for(self.fin_b)
            self.assertEqual(client.get(self.detail_url(receipt)).status_code, 404)
            with mock.patch(SIGNER, return_value=SIGNED):
                self.assertEqual(client.get(self.action_url('proof_open', receipt)).status_code,
                                 404)
            for name in ('confirm', 'query', 'answer', 'reject', 'unconfirm'):
                with self.subTest(action=name):
                    self.assertEqual(client.post(self.action_url(name, receipt),
                                                 {'reason': 'x', 'note': 'x', 'answer': 'x'})
                                     .status_code, 404)
            html = client.get(reverse('finance_work')).content.decode()
            self.assertNotIn('sec-to-confirm', html)
            self.assertNotIn('Receipts to confirm', html)
        self.assertEqual(self.fresh(receipt).status, PaymentReceipt.RECORDED)
        self.assertFalse(ActivityLog.objects.filter(action_code='receipt_proof_opened').exists())


# ===========================================================================
# 2. Who may open the receipt page (F5)
# ===========================================================================

@LIVE
class AccessTests(FinanceReceiptBase):

    def setUp(self):
        super().setUp()
        self.receipt = self.record(self.pm, self.m1)

    def test_allowed_viewers_get_200(self):
        for who in (self.fin, self.fin_b, self.pm, self.coord, self.bd):
            with self.subTest(who=who.user.username):
                self.assertEqual(self.client_for(who).get(self.detail_url(self.receipt))
                                 .status_code, 200)

    def test_others_who_can_see_the_site_get_403(self):
        for who in (self.bd_other, self.ceo, self.admin_p, self.sysadmin, self.se):
            with self.subTest(who=who.user.username):
                self.assertEqual(self.client_for(who).get(self.detail_url(self.receipt))
                                 .status_code, 403)

    def test_cannot_see_the_site_404_and_deleted_site_404(self):
        self.assertEqual(self.client_for(self.se_elsewhere).get(self.detail_url(self.receipt))
                         .status_code, 404)
        Project.objects.filter(pk=self.project.pk).update(is_deleted=True)
        self.assertEqual(self.client_for(self.fin).get(self.detail_url(self.receipt))
                         .status_code, 404)

    def test_receipt_of_another_site_is_404(self):
        other = self.site(name='Other site')
        url = reverse('receipt_detail', args=[other.project_id, self.receipt.pk])
        self.assertEqual(self.client_for(self.fin).get(url).status_code, 404)

    def test_bd_on_m2_gets_403(self):
        on_m2 = self.record(self.pm, self.m2)
        self.assertEqual(self.client_for(self.bd).get(self.detail_url(on_m2)).status_code, 403)

    def test_pm_reads_without_finance_buttons(self):
        html = self.client_for(self.pm).get(self.detail_url(self.receipt)).content.decode()
        for form in ('rd-confirm-form', 'rd-query', 'rd-reject', 'rd-unconfirm'):
            self.assertNotIn(f'id="{form}"', html)
        self.assertIn('Waiting for Finance', html)
        finance = self.client_for(self.fin).get(self.detail_url(self.receipt)).content.decode()
        for form in ('rd-confirm-form', 'rd-query', 'rd-reject'):
            self.assertIn(f'id="{form}"', finance)
        self.assertNotIn('id="rd-unconfirm"', finance)

    def test_get_on_action_urls_is_405(self):
        client = self.client_for(self.fin)
        for name in ('confirm', 'query', 'answer', 'reject', 'unconfirm'):
            with self.subTest(action=name):
                self.assertEqual(client.get(self.action_url(name, self.receipt)).status_code, 405)

    def test_ceo_hand_made_post_is_403_and_writes_nothing(self):
        before = self.written()
        for name in ('confirm', 'query', 'reject', 'unconfirm'):
            response = self.act(self.ceo, name, self.receipt, reason='x', note='x')
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.written(), before)


# ===========================================================================
# 3. Confirm (FR2, FR11, FR20, FR24, D4; ruling 3)
# ===========================================================================

@LIVE
class ConfirmTests(FinanceReceiptBase):

    def test_a_second_finance_user_confirms(self):
        receipt = self.record(self.pm, self.m1)
        response = self.act(self.fin, 'confirm', receipt, follow=True)
        self.assertEqual(response.redirect_chain[-1][0], self.detail_url(receipt))
        self.assertIn(CONFIRMED, self.messages_of(response))
        row = self.fresh(receipt)
        self.assertEqual(row.status, PaymentReceipt.CONFIRMED)
        self.assertEqual(row.confirmed_by, self.fin)

    def test_the_recorder_even_as_finance_is_refused(self):
        receipt = self.record(self.fin, self.m1)
        before = self.written()
        response = self.act(self.fin, 'confirm', receipt, follow=True)
        self.assertIn(NOT_ALLOWED, self.messages_of(response))
        self.assertEqual(self.written(), before)
        html = self.client_for(self.fin).get(self.detail_url(receipt)).content.decode()
        self.assertNotIn('id="rd-confirm-form"', html)

    def test_over_the_agreed_amount_needs_a_note(self):
        PaymentMilestone.objects.filter(pk=self.m1.pk).update(amount=Decimal('100000.00'))
        receipt = self.record(self.pm, self.m1, amount='150000')
        html = self.client_for(self.fin).get(self.detail_url(receipt)).content.decode()
        self.assertIn('name="overpayment_note"', html)
        before = self.written()
        response = self.act(self.fin, 'confirm', receipt, follow=True)
        text = ' '.join(self.messages_of(response))
        self.assertIn('to ₹1,50,000, more than its agreed amount of ₹1,00,000', text)
        self.assertEqual(self.written(), before)
        self.act(self.fin, 'confirm', receipt, overpayment_note='Client paid M2 early')
        row = self.fresh(receipt)
        self.assertEqual(row.status, PaymentReceipt.CONFIRMED)
        self.assertEqual(row.overpayment_note, 'Client paid M2 early')
        self.assertTrue(ActivityLog.objects.filter(
            action_code='receipt_confirmed',
            action__contains='over the agreed amount: Client paid M2 early').exists())

    def test_a_note_when_not_over_is_ignored(self):
        # Ruling 3: nothing stored, no "over the agreed amount" in history or log.
        PaymentMilestone.objects.filter(pk=self.m1.pk).update(amount=Decimal('100000.00'))
        receipt = self.record(self.pm, self.m1, amount='40000')
        html = self.client_for(self.fin).get(self.detail_url(receipt)).content.decode()
        self.assertNotIn('name="overpayment_note"', html)
        self.act(self.fin, 'confirm', receipt, overpayment_note='stale note')
        row = self.fresh(receipt)
        self.assertEqual(row.status, PaymentReceipt.CONFIRMED)
        self.assertEqual(row.overpayment_note, '')
        log = ActivityLog.objects.get(action_code='receipt_confirmed', entity_id=receipt.pk)
        self.assertNotIn('over the agreed amount', log.action)
        transition = StatusTransition.objects.get(subject_type=SUBJECT_PAYMENT_RECEIPT,
                                                  subject_id=receipt.pk,
                                                  to_status=PaymentReceipt.CONFIRMED)
        self.assertEqual(transition.remark, '')

    def test_a_note_when_over_is_stored_by_the_writer(self):
        PaymentMilestone.objects.filter(pk=self.m1.pk).update(amount=Decimal('100.00'))
        receipt = self.record(self.pm, self.m1, amount='500')
        confirm_receipt(self.fin.user, receipt, overpayment_note='  agreed by CEO  ')
        self.assertEqual(self.fresh(receipt).overpayment_note, 'agreed by CEO')

    def test_no_agreed_amount_confirms_without_a_note(self):
        receipt = self.record(self.pm, self.m1, amount='9999999')
        html = self.client_for(self.fin).get(self.detail_url(receipt)).content.decode()
        self.assertIn('No agreed amount yet', html)
        self.assertNotIn('name="overpayment_note"', html)
        self.act(self.fin, 'confirm', receipt)
        self.assertEqual(self.fresh(receipt).status, PaymentReceipt.CONFIRMED)

    def test_a_queried_receipt_cannot_be_confirmed(self):
        receipt = self.record(self.pm, self.m1)
        query_receipt(self.fin.user, receipt, 'Which account?')
        before = self.written()
        response = self.act(self.fin_b, 'confirm', receipt, follow=True)
        self.assertIn(NOT_ALLOWED, self.messages_of(response))
        self.assertEqual(self.written(), before)


# ===========================================================================
# 4. Query and answer (FR19, FR27, FR28)
# ===========================================================================

@LIVE
class QueryAnswerTests(FinanceReceiptBase):

    def setUp(self):
        super().setUp()
        self.receipt = self.record(self.pm, self.m1)

    def test_reason_required(self):
        before = self.written()
        response = self.act(self.fin, 'query', self.receipt, reason='   ', follow=True)
        self.assertIn('A query must say what is wrong', ' '.join(self.messages_of(response)))
        self.assertEqual(self.written(), before)

    def test_query_names_the_recorder_and_a_second_query_is_refused(self):
        response = self.act(self.fin, 'query', self.receipt, reason='Which bank?', follow=True)
        self.assertIn('Query sent to Rf_Pm Test.', self.messages_of(response))
        self.assertEqual(self.fresh(self.receipt).status, PaymentReceipt.QUERIED)
        before = self.written()
        response = self.act(self.fin_b, 'query', self.receipt, reason='Again', follow=True)
        self.assertIn(NOT_ALLOWED, self.messages_of(response))
        self.assertEqual(self.written(), before)

    def test_recorder_answers_and_it_waits_for_finance_again(self):
        query_receipt(self.fin.user, self.receipt, 'Which bank?')
        html = self.client_for(self.pm).get(self.detail_url(self.receipt)).content.decode()
        self.assertIn('id="rd-answer-form"', html)
        response = self.act(self.pm, 'answer', self.receipt, answer='HDFC, UTR 123',
                            follow=True)
        self.assertIn(ANSWERED, self.messages_of(response))
        self.assertEqual(self.fresh(self.receipt).status, PaymentReceipt.RECORDED)
        query = PaymentReceiptQuery.objects.get(receipt=self.receipt)
        self.assertEqual((query.answer, query.answered_by), ('HDFC, UTR 123', self.pm))
        html = self.client_for(self.pm).get(self.detail_url(self.receipt)).content.decode()
        self.assertIn('Waiting for Finance', html)
        self.assertIn('HDFC, UTR 123', html)

    def test_the_raiser_cannot_answer(self):
        query_receipt(self.fin.user, self.receipt, 'Which bank?')
        html = self.client_for(self.fin).get(self.detail_url(self.receipt)).content.decode()
        self.assertNotIn('id="rd-answer-form"', html)
        before = self.written()
        response = self.act(self.fin, 'answer', self.receipt, answer='me', follow=True)
        self.assertIn(NOT_ALLOWED, self.messages_of(response))
        self.assertEqual(self.written(), before)

    def test_pm_side_answers_when_the_recorder_has_left(self):
        coord_receipt = self.record(self.coord, self.m2)
        query_receipt(self.fin.user, coord_receipt, 'Which bank?')
        self.coord.is_active = False
        self.coord.save()
        self.act(self.pm, 'answer', coord_receipt, answer='Answered by the PM')
        self.assertEqual(self.fresh(coord_receipt).status, PaymentReceipt.RECORDED)

    def test_bd_cannot_answer_on_m2(self):
        on_m2 = self.record(self.pm, self.m2)
        query_receipt(self.fin.user, on_m2, 'Which bank?')
        before = self.written()
        self.assertEqual(self.act(self.bd, 'answer', on_m2, answer='x').status_code, 403)
        self.assertEqual(self.written(), before)

    def test_record_page_row_links_and_answer_prompt(self):
        query_receipt(self.fin.user, self.receipt, 'Which bank?')
        pm_html = self.client_for(self.pm).get(self.url(self.m1)).content.decode()
        self.assertIn(f'href="{self.detail_url(self.receipt)}#rd-queries"', pm_html)
        self.assertIn('Queried by Finance — answer', pm_html)
        # The raiser sees the row link, never the answer prompt.
        fin_html = self.client_for(self.fin).get(self.url(self.m1)).content.decode()
        self.assertIn(f'href="{self.detail_url(self.receipt)}"', fin_html)
        self.assertNotIn('Queried by Finance — answer', fin_html)


# ===========================================================================
# 5. Reject (FR19, FR28)
# ===========================================================================

@LIVE
class RejectTests(FinanceReceiptBase):

    def test_reason_required(self):
        receipt = self.record(self.pm, self.m1)
        before = self.written()
        response = self.act(self.fin, 'reject', receipt, reason='', follow=True)
        self.assertIn('A rejection must say why', ' '.join(self.messages_of(response)))
        self.assertEqual(self.written(), before)

    def test_from_recorded_and_from_queried_then_no_actions_left(self):
        recorded = self.record(self.pm, self.m1)
        queried = self.record(self.pm, self.m1, amount='777')
        query_receipt(self.fin.user, queried, 'Which bank?')
        for receipt in (recorded, queried):
            response = self.act(self.fin_b, 'reject', receipt, reason='Wrong figures',
                                follow=True)
            self.assertIn(REJECTED, self.messages_of(response))
            self.assertEqual(self.fresh(receipt).status, PaymentReceipt.REJECTED)
            for who in (self.fin, self.fin_b, self.pm):
                html = self.client_for(who).get(self.detail_url(receipt)).content.decode()
                with self.subTest(receipt=receipt.pk, who=who.user.username):
                    self.assertNotIn('id="rd-actions"', html)
                    self.assertNotIn('id="rd-answer-form"', html)
        # Ruling Q5: the rejected receipt's query stays unanswered and reads as overruled.
        html = self.client_for(self.fin).get(self.detail_url(queried)).content.decode()
        self.assertIn('Not answered — the receipt was rejected.', html)

    def test_allowed_on_a_cancelled_site(self):
        receipt = self.record(self.pm, self.m1)
        Project.objects.filter(pk=self.project.pk).update(status='Cancelled')
        self.act(self.fin, 'reject', receipt, reason='Site cancelled; refunded')
        self.assertEqual(self.fresh(receipt).status, PaymentReceipt.REJECTED)


# ===========================================================================
# 6. Undo a confirmation (FR35)
# ===========================================================================

@LIVE
class UnconfirmTests(FinanceReceiptBase):

    def setUp(self):
        super().setUp()
        PaymentMilestone.objects.filter(pk=self.m1.pk).update(amount=Decimal('100.00'))
        # The recorder is a Finance user, so FR11 can be checked on re-confirming.
        self.receipt = self.record(self.fin, self.m1, amount='500')
        confirm_receipt(self.fin_b.user, self.receipt, overpayment_note='Agreed by CEO')

    def test_the_original_confirmer_undoes_with_a_note(self):
        ledger, lines = self.ledger(), self.receipt_lines()
        response = self.act(self.fin_b, 'unconfirm', self.receipt, note='Bank reversed it',
                            follow=True)
        self.assertIn(UNCONFIRMED, self.messages_of(response))
        row = self.fresh(self.receipt)
        self.assertEqual(row.status, PaymentReceipt.RECORDED)
        self.assertIsNone(row.confirmed_by)
        self.assertIsNone(row.confirmed_at)
        self.assertEqual(row.overpayment_note, '')
        self.assertEqual(self.ledger(), ledger + 1)
        self.assertEqual(self.receipt_lines(), lines + 1)
        last = StatusTransition.objects.filter(
            subject_type=SUBJECT_PAYMENT_RECEIPT, subject_id=self.receipt.pk).latest('pk')
        self.assertEqual((last.from_status, last.to_status, last.remark),
                         (PaymentReceipt.CONFIRMED, PaymentReceipt.RECORDED, 'Bank reversed it'))
        log = ActivityLog.objects.get(action_code='receipt_unconfirmed')
        self.assertIn('Bank reversed it', log.action)
        self.assertEqual(log.entity_id, self.receipt.pk)
        # The history keeps both the confirmation (with its note) and the undo.
        html = self.client_for(self.fin).get(self.detail_url(self.receipt)).content.decode()
        self.assertIn('Confirmation undone', html)
        self.assertIn('Bank reversed it', html)
        self.assertIn('<strong>Confirmed</strong>', html)
        self.assertIn('Agreed by CEO', html)

    def test_the_recorder_may_undo_but_never_reconfirm(self):
        unconfirm_receipt(self.fin.user, self.fresh(self.receipt), 'Wrong account')
        self.assertEqual(self.fresh(self.receipt).status, PaymentReceipt.RECORDED)
        with self.assertRaises(ReceiptRefused):
            confirm_receipt(self.fin.user, self.fresh(self.receipt), overpayment_note='x')
        # Re-confirming follows FR11 as before: another Finance user, here the same one
        # who confirmed first.
        self.act(self.fin_b, 'confirm', self.receipt, overpayment_note='Checked again')
        row = self.fresh(self.receipt)
        self.assertEqual((row.status, row.confirmed_by, row.overpayment_note),
                         (PaymentReceipt.CONFIRMED, self.fin_b, 'Checked again'))

    def test_note_required(self):
        before = self.written()
        response = self.act(self.fin, 'unconfirm', self.receipt, note='  ', follow=True)
        self.assertIn('Undoing a confirmation must say why', ' '.join(self.messages_of(response)))
        self.assertEqual(self.written(), before)

    def test_refused_on_recorded_queried_and_rejected(self):
        recorded = self.record(self.pm, self.m1, amount='11')
        queried = self.record(self.pm, self.m1, amount='12')
        query_receipt(self.fin.user, queried, 'Which bank?')
        rejected = self.record(self.pm, self.m1, amount='13')
        reject_receipt(self.fin.user, rejected, 'Wrong')
        for receipt in (recorded, queried, rejected):
            before = self.written()
            response = self.act(self.fin_b, 'unconfirm', receipt, note='x', follow=True)
            with self.subTest(status=receipt.status):
                self.assertIn(NOT_ALLOWED, self.messages_of(response))
                self.assertEqual(self.written(), before)

    def test_non_finance_hand_made_post_refused(self):
        before = self.written()
        response = self.act(self.pm, 'unconfirm', self.receipt, note='x', follow=True)
        self.assertIn(NOT_ALLOWED, self.messages_of(response))
        self.assertEqual(self.written(), before)

    def test_allowed_on_a_cancelled_site(self):
        Project.objects.filter(pk=self.project.pk).update(status='Cancelled')
        self.act(self.fin, 'unconfirm', self.receipt, note='Site cancelled')
        self.assertEqual(self.fresh(self.receipt).status, PaymentReceipt.RECORDED)

    def test_undo_form_only_on_a_confirmed_receipt(self):
        html = self.client_for(self.fin).get(self.detail_url(self.receipt)).content.decode()
        self.assertIn('id="rd-unconfirm"', html)
        self.assertNotIn('id="rd-confirm-form"', html)


# ===========================================================================
# 7. Likely duplicates (F1)
# ===========================================================================

@LIVE
class DuplicateTests(FinanceReceiptBase):

    def test_listed_without_itself_or_rejected_ones(self):
        day = timezone.localdate()
        first = self.record(self.pm, self.m1, amount='5000', date=day)
        twin = self.record(self.coord, self.m1, amount='5000', date=day)
        same_ref = self.record(self.pm, self.m1, amount='42', date=day, reference='UTR9')
        PaymentReceipt.objects.filter(pk=first.pk).update(reference='utr9')
        dropped = self.record(self.pm, self.m1, amount='5000', date=day)
        reject_receipt(self.fin.user, dropped, 'Duplicate')
        response = self.client_for(self.fin).get(self.detail_url(first))
        hrefs = [d['href'] for d in response.context['duplicates']]
        self.assertIn(self.detail_url(twin), hrefs)
        self.assertIn(self.detail_url(same_ref), hrefs)
        self.assertNotIn(self.detail_url(first), hrefs)
        self.assertNotIn(self.detail_url(dropped), hrefs)


# ===========================================================================
# 8. The proof (F5)
# ===========================================================================

@LIVE
class ProofTests(FinanceReceiptBase):

    def setUp(self):
        super().setUp()
        PaymentMilestone.objects.filter(pk=self.m1.pk).update(amount=Decimal('100000.00'))
        self.receipt = self.record(self.pm, self.m1, amount='50000')
        PaymentReceipt.objects.filter(pk=self.receipt.pk).update(
            proof_bucket='delivery-bkt', proof_path='receipt_proofs/1/abc.pdf',
            proof_name='client name.pdf')
        self.proof_url = self.action_url('proof_open', self.receipt)

    def opens(self):
        return ActivityLog.objects.filter(action_code='receipt_proof_opened')

    def test_link_shown_to_permitted_viewers_and_never_a_signed_url(self):
        with mock.patch(SIGNER, return_value=SIGNED) as signer:
            for who in (self.fin, self.pm):
                html = self.client_for(who).get(self.detail_url(self.receipt)).content.decode()
                self.assertIn(f'href="{self.proof_url}"', html)
                self.assertNotIn(SIGNED, html)
            signer.assert_not_called()

    def test_get_mints_a_signed_link_and_logs_the_open(self):
        with mock.patch(SIGNER, return_value=SIGNED) as signer:
            response = self.client_for(self.fin).get(self.proof_url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], SIGNED)
        signer.assert_called_once()
        line = self.opens().get()
        self.assertEqual(line.action, 'Opened the proof of the ₹50,000 receipt on M1')
        self.assertEqual((line.entity_type, line.entity_id), ('PaymentReceipt', self.receipt.pk))
        self.assertNotIn('client name', line.action)

    def test_not_permitted_is_403_and_nothing_logged(self):
        with mock.patch(SIGNER, return_value=SIGNED) as signer:
            for who in (self.ceo, self.bd_other, self.se):
                self.assertEqual(self.client_for(who).get(self.proof_url).status_code, 403)
            signer.assert_not_called()
        self.assertFalse(self.opens().exists())

    def test_failed_signing_logs_nothing_and_no_proof_is_404(self):
        with mock.patch(SIGNER, return_value=None):
            response = self.client_for(self.fin).get(self.proof_url, follow=True)
        self.assertIn('The proof cannot be opened right now', ' '.join(self.messages_of(response)))
        self.assertFalse(self.opens().exists())
        bare = self.record(self.pm, self.m1, amount='1')
        self.assertEqual(self.client_for(self.fin).get(self.action_url('proof_open', bare))
                         .status_code, 404)

    def test_open_line_is_hidden_from_recent_activity(self):
        with mock.patch(SIGNER, return_value=SIGNED):
            self.client_for(self.fin).get(self.proof_url)
        response = self.client_for(self.fin).get(self.overview())
        actions = [e.action for e in response.context['recent_activity']]
        self.assertFalse(any('Opened the proof' in a for a in actions))


# ===========================================================================
# 9. Return after an action (F8, ruling 1)
# ===========================================================================

@LIVE
class ReturnTests(FinanceReceiptBase):

    def test_each_action_returns_to_the_receipt_page_with_its_message(self):
        a = self.record(self.pm, self.m1, amount='1')
        b = self.record(self.pm, self.m1, amount='2')
        cases = (
            (self.fin, 'query', a, {'reason': 'Which bank?'}, 'Query sent to Rf_Pm Test.'),
            (self.pm, 'answer', a, {'answer': 'HDFC'}, ANSWERED),
            (self.fin, 'confirm', a, {}, CONFIRMED),
            (self.fin_b, 'unconfirm', a, {'note': 'Oops'}, UNCONFIRMED),
            (self.fin, 'reject', b, {'reason': 'Wrong'}, REJECTED),
        )
        for who, name, receipt, data, message in cases:
            with self.subTest(action=name):
                response = self.act(who, name, receipt, follow=True, **data)
                self.assertEqual(response.redirect_chain[-1][0], self.detail_url(receipt))
                self.assertIn(message, self.messages_of(response))

    def test_finance_work_marker_returns_to_the_section(self):
        receipt = self.record(self.pm, self.m1)
        response = self.act(self.fin, 'confirm', receipt, **{'return': 'finance_work',
                                                             'rq': 'q=RF'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'],
                         reverse('finance_work') + '?q=RF#sec-to-confirm')

    def test_refusal_with_a_marker_stays_on_the_receipt_page(self):
        receipt = self.record(self.fin, self.m1)
        response = self.act(self.fin, 'confirm', receipt, **{'return': 'finance_work',
                                                             'rq': 'q=RF'})
        self.assertEqual(response['Location'],
                         self.detail_url(receipt, **{'return': 'finance_work', 'rq': 'q=RF'}))

    def test_a_marker_from_someone_who_cannot_open_finance_work_is_ignored(self):
        receipt = self.record(self.pm, self.m1)
        query_receipt(self.fin.user, receipt, 'Which bank?')
        response = self.act(self.pm, 'answer', receipt, answer='HDFC',
                            **{'return': 'finance_work'})
        self.assertEqual(response['Location'], self.detail_url(receipt))

    def test_page_opened_from_finance_work_carries_the_marker(self):
        receipt = self.record(self.pm, self.m1)
        html = self.client_for(self.fin).get(
            self.detail_url(receipt, **{'return': 'finance_work', 'rq': 'q=RF'})).content.decode()
        self.assertIn('name="return" value="finance_work"', html)
        self.assertIn('name="rq" value="q=RF"', html)
        self.assertIn('Back to Finance work', html)


# ===========================================================================
# 10. Finance work: "Receipts to confirm" and "Confirmed, last 14 days" (F7)
# ===========================================================================

@LIVE
class FinanceWorkTests(FinanceReceiptBase):

    def page(self, who=None, **query):
        client = self.client_for(who or self.fin)
        return client.get(reverse('finance_work'), query)

    def ids(self, rows):
        return [r.receipt.pk for r in rows]

    def test_lists_recorded_and_queried_oldest_first_on_live_sites(self):
        older = self.record(self.pm, self.m1, amount='1')
        PaymentReceipt.objects.filter(pk=older.pk).update(
            recorded_at=timezone.now() - datetime.timedelta(days=3))
        queried = self.record(self.pm, self.m1, amount='2')
        query_receipt(self.fin.user, queried, 'Which bank?')
        confirmed = self.record(self.pm, self.m1, amount='3')
        confirm_receipt(self.fin.user, confirmed)
        rejected = self.record(self.pm, self.m1, amount='4')
        reject_receipt(self.fin.user, rejected, 'x')
        cancelled_site = self.site(name='Cancelled site')
        on_cancelled = self.record(self.pm, self.milestones(cancelled_site)[0], amount='5')
        Project.objects.filter(pk=cancelled_site.pk).update(status='Cancelled')
        test_site = self.site(name='Test site')
        Project.objects.filter(pk=test_site.pk).update(is_test=True)
        on_test = self.record(self.pm, self.milestones(test_site)[0], amount='6')
        deleted_site = self.site(name='Deleted site')
        on_deleted = self.record(self.pm, self.milestones(deleted_site)[0], amount='7')
        Project.objects.filter(pk=deleted_site.pk).update(is_deleted=True)

        response = self.page()
        rows = response.context['to_confirm']
        self.assertEqual(self.ids(rows), [older.pk, queried.pk, on_cancelled.pk, on_test.pk])
        self.assertEqual([r.queried for r in rows], [False, True, False, False])
        self.assertEqual(rows[0].waiting, 3)
        html = response.content.decode()
        self.assertIn('Queried — waiting for the recorder', html)
        self.assertIn(f'data-client-receipt="{older.pk}"', html)
        self.assertNotIn(f'data-client-receipt="{on_deleted.pk}"', html)
        self.assertNotIn(f'data-client-receipt="{rejected.pk}"', html)
        tile = [t for t in response.context['tiles'] if t['key'] == 'to_confirm'][0]
        self.assertEqual(tile['value'], 4)
        self.assertIn('1 queried', tile['detail'])

    def test_row_links_carry_the_return_marker(self):
        receipt = self.record(self.pm, self.m1)
        row = self.page(q='rf').context['to_confirm'][0]
        self.assertEqual(row.href, self.detail_url(receipt, **{'return': 'finance_work',
                                                              'rq': 'q=rf'}))

    def test_confirmed_last_14_days(self):
        recent = self.record(self.pm, self.m1, amount='1')
        confirm_receipt(self.fin.user, recent)
        old = self.record(self.pm, self.m1, amount='2')
        confirm_receipt(self.fin.user, old)
        PaymentReceipt.objects.filter(pk=old.pk).update(
            confirmed_at=timezone.now() - datetime.timedelta(days=20))
        edge = self.record(self.pm, self.m1, amount='3')
        confirm_receipt(self.fin.user, edge)
        start = timezone.make_aware(datetime.datetime.combine(
            timezone.localdate() - datetime.timedelta(days=13), datetime.time.min))
        PaymentReceipt.objects.filter(pk=edge.pk).update(confirmed_at=start)
        waiting = self.record(self.pm, self.m1, amount='4')
        response = self.page()
        self.assertEqual(self.ids(response.context['confirmed_recent']), [recent.pk, edge.pk])
        self.assertEqual(self.ids(response.context['to_confirm']), [waiting.pk])
        self.assertFalse(response.context['confirmed_open'])
        self.assertTrue(self.page(focus='confirmed_recent').context['confirmed_open'])

    def test_search_filters_both_sections(self):
        a = self.record(self.pm, self.m1, amount='1', reference='UTR-ALPHA')
        b = self.record(self.coord, self.m1, amount='2')
        c = self.record(self.pm, self.m2, amount='3', reference='UTR-BETA')
        confirm_receipt(self.fin.user, c)
        self.assertEqual(self.ids(self.page(q='utr-alpha').context['to_confirm']), [a.pk])
        self.assertEqual(self.ids(self.page(q='rf_coord').context['to_confirm']), [b.pk])
        response = self.page(q='utr-beta')
        self.assertEqual(self.ids(response.context['to_confirm']), [])
        self.assertEqual(self.ids(response.context['confirmed_recent']), [c.pk])
        code = self.project.site_code or self.project.project_id
        self.assertEqual(len(self.page(q=code.lower()).context['to_confirm']), 2)
        self.assertEqual(len(self.page(q='RF Site').context['to_confirm']), 2)

    def test_not_shown_to_ceo_admin_or_system_admin(self):
        self.record(self.pm, self.m1)
        for who in (self.ceo, self.admin_p, self.sysadmin):
            with self.subTest(who=who.user.username):
                response = self.page(who)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.context['client_receipts_shown'])
                self.assertNotIn('sec-to-confirm', response.content.decode())
                self.assertNotIn('to_confirm', [t['key'] for t in response.context['tiles']])
                self.assertNotIn('to_confirm', response.context['show'])
                # A focus on a receipt section is dropped, never an empty page.
                self.assertEqual(self.page(who, focus='to_confirm').context['params']['focus'], '')

    def queries(self, who):
        client = self.client_for(who)
        client.get(reverse('finance_work'))  # warm the session
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(reverse('finance_work'))
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries)

    def test_fixed_query_count_for_0_1_and_20_receipts(self):
        counts = [self.queries(self.fin)]
        self.record(self.pm, self.m1, amount='1')
        counts.append(self.queries(self.fin))
        for n in range(19):
            receipt = self.record(self.pm if n % 2 else self.coord, self.m1, amount=str(10 + n))
            if n % 3 == 0:
                query_receipt(self.fin.user, receipt, 'Which bank?')
            elif n % 3 == 1:
                confirm_receipt(self.fin.user, receipt)
        self.assertEqual(PaymentReceipt.objects.count(), 20)
        counts.append(self.queries(self.fin))
        self.assertEqual(len(set(counts)), 1, counts)
        # The sections cost exactly one query, and nothing for anyone who does not see them.
        with override_settings(CLIENT_RECEIPTS_LIVE=False):
            off = self.queries(self.fin)
        self.assertEqual(counts[0], off + 1)

    def test_switch_off_is_todays_page(self):
        self.record(self.pm, self.m1)
        with override_settings(CLIENT_RECEIPTS_LIVE=False):
            response = self.page()
            off = self.queries(self.fin)
            ceo_off = self.queries(self.ceo)
        self.assertFalse(response.context['client_receipts_shown'])
        self.assertEqual(list(response.context['show']),
                         ['milestones', 'invoiced', 'payments', 'receipts'])
        self.assertNotIn('Receipts to confirm', response.content.decode())
        # With the switch on the CEO's page (no receipt sections) costs what it did off;
        # Finance's costs exactly one more (test_fixed_query_count_for_0_1_and_20_receipts).
        self.assertEqual(self.queries(self.ceo), ceo_off)
        self.assertEqual(self.queries(self.fin), off + 1)


# ===========================================================================
# 11. The shared rupee formatter (F9)
# ===========================================================================

class FormatterTests(FinanceReceiptBase):

    def test_outputs(self):
        cases = ((Decimal('0'), '₹0', '0'),
                 (Decimal('1234.5'), '₹1,234.50', '1,235'),
                 (Decimal('110000'), '₹1,10,000', '1,10,000'),
                 (Decimal('12345678.9'), '₹1,23,45,678.90', '1,23,45,679'))
        for value, rupees, whole in cases:
            with self.subTest(value=value):
                self.assertEqual(format_rupees(value), rupees)
                self.assertEqual(format_inr(value), whole)
        self.assertEqual(format_rupees(None), 'blank')
        self.assertEqual(format_rupees(Decimal('-50000')), '-₹50,000')

    def test_existing_pages_use_the_same_functions(self):
        # views.py's old names are the shared functions, so every existing page, the
        # milestone log and the Finance work amounts read exactly as before.
        self.assertIs(views_module._format_inr, format_inr)
        self.assertIs(views_module._milestone_rupees, format_rupees)
        self.assertEqual(views_finance_work.rupees(Decimal('135000.40')), '₹1,35,000')
        self.assertEqual(views_module._milestone_rupees(Decimal('100000.40')), '₹1,00,000.40')

    @LIVE
    def test_record_page_amounts_unchanged(self):
        PaymentMilestone.objects.filter(pk=self.m1.pk).update(amount=Decimal('135000.00'))
        self.record(self.pm, self.m1, amount='50000.50')
        html = self.client_for(self.pm).get(self.url(self.m1)).content.decode()
        self.assertIn('Agreed amount: <strong>₹1,35,000</strong>', html)
        self.assertIn('<strong>₹50,000.50</strong>', html)


# ===========================================================================
# 12. No side effects (F10) and refusals write nothing
# ===========================================================================

@LIVE
class NoSideEffectTests(FinanceReceiptBase):

    def snapshot(self):
        milestones = list(PaymentMilestone.objects.filter(project=self.project).order_by('pk')
                          .values_list('pk', 'status', 'amount', 'amount_received',
                                       'received_date', 'invoice_amount'))
        tasks = list(Task.objects.filter(phase__project=self.project).order_by('pk')
                     .values_list('pk', 'status', 'assigned_to_id', 'due_date'))
        return milestones, tasks

    def test_every_action_leaves_milestone_and_tasks_alone_and_sends_its_own_alert(self):
        """F10: no action touches the milestone or its tasks. FIN-M-B5-4b (FR36): each
        action sends its own receipt alert after commit, so callbacks are executed and
        the event each step logs is asserted; answer and the proof open send nothing, and
        nothing ever sends the old milestone "Payment received"."""
        PaymentMilestone.objects.filter(pk=self.m1.pk).update(amount=Decimal('100.00'))
        a = self.record(self.pm, self.m1, amount='500')
        b = self.record(self.pm, self.m1, amount='5')
        before = self.snapshot()
        with mock.patch(SIGNER, return_value=SIGNED):
            PaymentReceipt.objects.filter(pk=a.pk).update(
                proof_bucket='delivery-bkt', proof_path='receipt_proofs/1/x.pdf')
            steps = (
                (self.fin, 'query', a, {'reason': 'Which bank?'}, {'receipt_queried'}),
                (self.pm, 'answer', a, {'answer': 'HDFC'}, set()),
                (self.fin, 'confirm', a, {'overpayment_note': 'Agreed'},
                 {'receipt_payment_received'}),
                (self.fin_b, 'unconfirm', a, {'note': 'Bank reversed'},
                 {'receipt_confirmation_undone'}),
                (self.fin_b, 'confirm', a, {'overpayment_note': 'Again'},
                 {'receipt_payment_received'}),
                (self.fin, 'reject', b, {'reason': 'Wrong'}, {'receipt_rejected'}),
            )
            for who, name, receipt, data, events in steps:
                mark = NotificationLog.objects.count()
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.act(who, name, receipt, **data)
                self.assertEqual(response.status_code, 302, name)
                self.assertEqual(self.snapshot(), before, name)
                self.assertEqual(set(NotificationLog.objects.order_by('pk')[mark:]
                                     .values_list('event_code', flat=True)), events, name)
            mark = NotificationLog.objects.count()
            with self.captureOnCommitCallbacks(execute=True):
                self.client_for(self.fin).get(self.action_url('proof_open', a))
            self.assertEqual(NotificationLog.objects.count(), mark)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(NotificationLog.objects.filter(
            event_code='payment_milestone_reached').exists())
        self.assertEqual(self.fresh(a).status, PaymentReceipt.CONFIRMED)
        self.assertEqual(self.fresh(b).status, PaymentReceipt.REJECTED)

    def test_refused_posts_write_no_transition_and_no_log_line(self):
        receipt = self.record(self.fin, self.m1)
        before = self.written()
        refused = (
            (self.fin, 'confirm', {}),                    # own record (FR11)
            (self.fin, 'query', {'reason': 'x'}),         # own record
            (self.fin_b, 'query', {'reason': ''}),        # blank reason
            (self.fin_b, 'reject', {'reason': ''}),       # blank reason
            (self.fin_b, 'unconfirm', {'note': 'x'}),     # not confirmed
            (self.pm, 'answer', {'answer': 'x'}),         # nothing queried
            (self.pm, 'confirm', {}),                     # not Finance
            (self.pm, 'reject', {'reason': 'x'}),         # not Finance
        )
        for who, name, data in refused:
            with self.subTest(who=who.user.username, action=name):
                response = self.act(who, name, receipt, **data)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(self.written(), before)
