"""
SNAP-3b: the Finance counts per tender in the daily snapshot (snapshot.tender_finance_counts,
finance_line), rulings F1-F5 and answers Q2-Q6 of 20 Nov 2026.

WHAT THIS PINS
    Statuses       each of the five request statuses lands in the right counts:
                   approved = approved + paid (Q2), pending = awaiting + on hold +
                   approved, paid = confirmed; a rejected request is a request and
                   nothing else, never pending; PaymentRequest has exactly five statuses,
                   so a new one (withdrawn, cancelled) stops this file until it is ruled on
    Invoices       invoice files on the tender's orders and contractor bills on its sites
                   (Q3); a PO / PI / other file is no invoice; a withdrawn bill is none,
                   a rejected bill still is
    Attribution    picked tender, site tender, both at once (counted once), two tenders
                   (counted in both, F3), a bill's payment by its site's tender (Q5); no
                   tender link -> counted nowhere; test data dropped by the CEO strip's
                   rule (Q4); a deleted site links nothing
    Payload        `finance` per section, keyed by Program id as text, five whole
                   numbers in FINANCE_KEYS order, surviving a JSON round trip (F5); a
                   switched-off tender (SNAP-3a) has no section and is not even counted
    No money       FIVE queries, the same with 1 request or 20, and no SQL statement
                   names an amount column (F1)
    Drawn          'N invoices · N payment requests · N approved · N pending · N paid',
                   zeros as 0, plain ink, no arrow (Q6), plurals as "DC" does; fits
                   1080 px with 3-digit counts; the dry run prints it

Run with:
    python manage.py test projects.tests_snapshot_finance --settings=solarpms.test_settings
"""
import io
import json
import tempfile
from decimal import Decimal
from unittest import mock

from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from . import snapshot as S
from .models import (
    APPROVAL_APPROVED, APPROVAL_KIND_CONTRACTOR_BILL, APPROVAL_OPEN, APPROVAL_REJECTED,
    APPROVAL_TERMINAL_STATUSES, APPROVAL_WITHDRAWN, VENDOR_KIND_CONTRACTOR,
    VENDOR_ORDER_DOC_INVOICE, VENDOR_ORDER_DOC_PI, VENDOR_ORDER_DOC_PO, ApprovalRequest,
    ContractorBillDetail, PaymentRequest, Vendor, VendorOrder, VendorOrderDocument,
    VendorOrderProgram, VendorOrderSite,
)
from .tests_dashboard_snapshot import NOW, _Recorder, _section
from .tests_snapshot_tender_switch import SwitchFixture

PR = PaymentRequest
ZERO = dict.fromkeys(S.FINANCE_KEYS, 0)


def _counts(**values):
    return {**ZERO, **values}


class FinanceFixture(SwitchFixture):
    """SwitchFixture: ALPHA (opex_a1, opex_a2) and GAMMA (opex_c) each have a section;
    resco is OPEX with no tender; res is Residential and already carries one pending
    request on a Residential order (SnapshotFixture's), which belongs to no tender."""

    _seq = 0

    def setUp(self):
        super().setUp()
        self.vendor = Vendor.objects.create(name='Finance Vendor')
        self.contractor = Vendor.objects.create(name='Finance Contractor',
                                                kind=VENDOR_KIND_CONTRACTOR)

    def _order(self, sites=(), programs=()):
        order = VendorOrder.objects.create(vendor=self.vendor, project_type='OPEX',
                                           created_by=self.scm,
                                           total_amount=Decimal('900000'))
        for site in sites:
            VendorOrderSite.objects.create(order=order, project=site)
        for program in programs:
            VendorOrderProgram.objects.create(order=order, program=program)
        return order

    def _request(self, order=None, bill=None, status=PR.PENDING_APPROVAL):
        approved = status in (PR.APPROVED, PR.CONFIRMED)
        return PR.objects.create(
            vendor_order=order, contractor_bill=bill,
            vendor=self.contractor if bill else self.vendor,
            amount=Decimal('1000'), requested_by=self.scm.user, status=status,
            # payment_request_approval_has_amount / _refusal_needs_reason.
            approved_amount=Decimal('1000') if approved else None,
            decision_reason='Duplicate' if status == PR.REJECTED else '')

    def _document(self, order, doc_type=VENDOR_ORDER_DOC_INVOICE):
        FinanceFixture._seq += 1
        invoice = doc_type == VENDOR_ORDER_DOC_INVOICE
        return VendorOrderDocument.objects.create(
            order=order, doc_type=doc_type,
            invoice_number=f'INV-{FinanceFixture._seq}' if invoice else '',
            invoice_amount=Decimal('500') if invoice else None,
            file_name='file.pdf', bucket='orders', path=f'o/{FinanceFixture._seq}.pdf',
            uploaded_by=self.scm)

    def _bill(self, site, status=APPROVAL_APPROVED):
        FinanceFixture._seq += 1
        number = f'B-{FinanceFixture._seq}'
        approval = ApprovalRequest.objects.create(
            kind=APPROVAL_KIND_CONTRACTOR_BILL, status=status,
            closed_at=timezone.now() if status in APPROVAL_TERMINAL_STATUSES else None,
            withdrawal_note='Raised twice' if status == APPROVAL_WITHDRAWN else '',
            title=f'Civil works {number}', description='Walls.',
            vendor=self.contractor, raised_by=self.scm)
        return ContractorBillDetail.objects.create(
            request=approval, project=site, amount=Decimal('100'), bill_number=number,
            bill_date=timezone.localdate(), pdf_file_name=f'{number}.pdf',
            pdf_bucket='bills-private', pdf_path=f'{site.pk}/{number}.pdf', pdf_size_kb=80)

    def _finance(self, *programs):
        programs = programs or (self.tender_a, self.tender_c)
        return S.tender_finance_counts([p.pk for p in programs])


# ---------------------------------------------------------------------------
# 1. Statuses
# ---------------------------------------------------------------------------

class StatusTests(FinanceFixture):

    def test_payment_request_has_exactly_the_five_statuses_this_file_rules_on(self):
        # F2 leaves out withdrawn / cancelled requests "if such a status exists". None
        # does; if one is ever added, this fails until the counts are ruled on for it.
        self.assertEqual({value for value, _ in PR.STATUS_CHOICES},
                         {PR.PENDING_APPROVAL, PR.APPROVED, PR.ON_HOLD, PR.REJECTED,
                          PR.CONFIRMED})

    def test_each_status_lands_in_the_right_counts(self):
        expected = {
            PR.PENDING_APPROVAL: _counts(requests=1, payment_pending=1),
            PR.ON_HOLD:          _counts(requests=1, payment_pending=1),
            PR.APPROVED:         _counts(requests=1, approved=1, payment_pending=1),
            PR.CONFIRMED:        _counts(requests=1, approved=1, paid=1),
            PR.REJECTED:         _counts(requests=1),
        }
        for status, counts in expected.items():
            with self.subTest(status=status):
                PR.objects.filter(vendor_order__sites__project=self.opex_a1).delete()
                self._request(self._order(sites=[self.opex_a1]), status=status)
                self.assertEqual(self._finance(self.tender_a)[self.tender_a.pk], counts)

    def test_all_five_together_read_as_a_funnel(self):
        order = self._order(sites=[self.opex_a1])
        for status, _ in PR.STATUS_CHOICES:
            self._request(order, status=status)
        alpha = self._finance()[self.tender_a.pk]
        self.assertEqual(alpha, _counts(requests=5, approved=2, payment_pending=3, paid=1))
        # Q2: requests >= approved >= paid.
        self.assertGreaterEqual(alpha['requests'], alpha['approved'])
        self.assertGreaterEqual(alpha['approved'], alpha['paid'])

    def test_rejected_is_never_pending(self):
        order = self._order(sites=[self.opex_a1])
        for _ in range(3):
            self._request(order, status=PR.REJECTED)
        self.assertEqual(self._finance()[self.tender_a.pk], _counts(requests=3))
        self.assertNotIn(PR.REJECTED, S.FINANCE_PENDING | S.FINANCE_APPROVED)


# ---------------------------------------------------------------------------
# 2. Invoices
# ---------------------------------------------------------------------------

class InvoiceTests(FinanceFixture):

    def test_order_invoice_files_count_and_other_files_do_not(self):
        order = self._order(sites=[self.opex_a1])
        self._document(order)
        self._document(order)
        for doc_type in (VENDOR_ORDER_DOC_PO, VENDOR_ORDER_DOC_PI, 'other'):
            self._document(order, doc_type)
        self.assertEqual(self._finance()[self.tender_a.pk], _counts(invoices=2))

    def test_contractor_bills_count_as_invoices_unless_withdrawn(self):
        for status in (APPROVAL_OPEN, APPROVAL_APPROVED, APPROVAL_REJECTED):
            self._bill(self.opex_a1, status)
        self._bill(self.opex_a2, APPROVAL_WITHDRAWN)
        self.assertEqual(self._finance()[self.tender_a.pk], _counts(invoices=3))

    def test_a_bill_and_an_order_invoice_add_up_on_the_same_tender(self):
        self._document(self._order(sites=[self.opex_c]))
        self._bill(self.opex_c)
        self.assertEqual(self._finance()[self.tender_c.pk], _counts(invoices=2))


# ---------------------------------------------------------------------------
# 3. Which tender
# ---------------------------------------------------------------------------

class AttributionTests(FinanceFixture):

    def test_picked_tender_alone_and_site_tender_alone_each_count(self):
        self._request(self._order(programs=[self.tender_a]))   # no site at all
        self._request(self._order(sites=[self.opex_c]))         # no picked tender
        finance = self._finance()
        self.assertEqual(finance[self.tender_a.pk], _counts(requests=1, payment_pending=1))
        self.assertEqual(finance[self.tender_c.pk], _counts(requests=1, payment_pending=1))

    def test_picked_and_site_tender_the_same_count_once(self):
        order = self._order(sites=[self.opex_a1, self.opex_a2], programs=[self.tender_a])
        self._request(order)
        self._document(order)
        self.assertEqual(self._finance()[self.tender_a.pk],
                         _counts(invoices=1, requests=1, payment_pending=1))

    def test_an_order_on_two_tenders_counts_in_both(self):
        # Picked ALPHA, sized against GAMMA's site: both links (Q5), both tenders (F3).
        order = self._order(sites=[self.opex_c], programs=[self.tender_a])
        self._request(order, status=PR.CONFIRMED)
        self._document(order)
        both = _counts(invoices=1, requests=1, approved=1, paid=1)
        finance = self._finance()
        self.assertEqual(finance[self.tender_a.pk], both)
        self.assertEqual(finance[self.tender_c.pk], both)

    def test_a_bill_payment_counts_in_its_sites_tender(self):
        self._request(bill=self._bill(self.opex_c), status=PR.APPROVED)
        finance = self._finance()
        self.assertEqual(finance[self.tender_c.pk],
                         _counts(invoices=1, requests=1, approved=1, payment_pending=1))
        self.assertEqual(finance[self.tender_a.pk], ZERO)

    def test_no_tender_link_counts_nowhere(self):
        # The RESCO site has no tender; SnapshotFixture's Residential request has none
        # either. Nothing lands anywhere.
        order = self._order(sites=[self.resco])
        self._request(order)
        self._document(order)
        self._request(bill=self._bill(self.resco))
        self._request(self._order())                          # no site, no tender
        self.assertEqual(self._finance(), {self.tender_a.pk: ZERO, self.tender_c.pk: ZERO})

    def test_only_the_tenders_asked_for_are_counted(self):
        order = self._order(sites=[self.opex_c], programs=[self.tender_a])
        self._request(order)
        self.assertEqual(set(self._finance(self.tender_a)), {self.tender_a.pk})
        self.assertEqual(S.tender_finance_counts([]), {})


class TestDataTests(FinanceFixture):
    """Q4: the CEO strip's rule (payments.exclude_test_site_payments) and its restatement
    for invoice files; a test or deleted site links no tender."""

    def setUp(self):
        super().setUp()
        self.test_site = self._bare(project_type='OPEX', program=self.tender_a, is_test=True)
        self.gone_site = self._bare(project_type='OPEX', program=self.tender_a,
                                    is_deleted=True)

    def test_an_order_on_test_sites_only_is_dropped_even_with_a_picked_tender(self):
        order = self._order(sites=[self.test_site], programs=[self.tender_a])
        self._request(order)
        self._document(order)
        self.assertEqual(self._finance()[self.tender_a.pk], ZERO)

    def test_one_real_site_keeps_the_order(self):
        order = self._order(sites=[self.test_site, self.opex_a1])
        self._request(order)
        self._document(order)
        self.assertEqual(self._finance()[self.tender_a.pk],
                         _counts(invoices=1, requests=1, payment_pending=1))

    def test_a_bill_on_a_test_site_and_its_payment_are_dropped(self):
        self._request(bill=self._bill(self.test_site))
        self.assertEqual(self._finance()[self.tender_a.pk], ZERO)

    def test_a_deleted_site_links_no_tender(self):
        order = self._order(sites=[self.gone_site])
        self._request(order)
        self._document(order)
        self._request(bill=self._bill(self.gone_site))
        self.assertEqual(self._finance()[self.tender_a.pk], ZERO)


# ---------------------------------------------------------------------------
# 4. The payload, and no money
# ---------------------------------------------------------------------------

class PayloadTests(FinanceFixture):

    def setUp(self):
        super().setUp()
        order = self._order(sites=[self.opex_a1])
        self._request(order, status=PR.APPROVED)
        self._document(order)

    def test_finance_is_stored_per_tender_by_program_id(self):
        payload = json.loads(json.dumps(self._collect()))   # as the JSONField stores it
        sections = payload['tender_sections']
        self.assertEqual(sections[str(self.tender_a.pk)]['finance'],
                         _counts(invoices=1, requests=1, approved=1, payment_pending=1))
        self.assertEqual(sections[str(self.tender_c.pk)]['finance'], ZERO)
        for section in sections.values():
            self.assertEqual(tuple(section['finance']), S.FINANCE_KEYS)
            self.assertTrue(all(type(v) is int for v in section['finance'].values()))

    def test_a_switched_off_tender_has_no_finance_row_and_is_not_counted(self):
        self._switch(self.tender_a, False)
        with mock.patch.object(S, 'tender_finance_counts',
                               wraps=S.tender_finance_counts) as spy:
            payload = self._collect()
        self.assertEqual(set(payload['tender_sections']), {str(self.tender_c.pk)})
        spy.assert_called_once()
        self.assertEqual(set(spy.call_args.args[0]), {self.tender_c.pk})

    def test_finance_keys_share_nothing_with_the_breakdown(self):
        # _lines() looks a figure's colour up by key across both rows.
        self.assertFalse(set(S.FINANCE_KEYS) & set(S.BREAKDOWN_KEYS))


class NoMoneyTests(FinanceFixture):

    def _queries(self):
        with CaptureQueriesContext(connection) as ctx:
            self._finance()
        return [q['sql'] for q in ctx.captured_queries]

    def test_five_queries_however_many_requests(self):
        order = self._order(sites=[self.opex_a1], programs=[self.tender_c])
        self._request(order)
        self._document(order)
        self._request(bill=self._bill(self.opex_c))
        few = self._queries()
        for _ in range(20):
            self._request(self._order(sites=[self.opex_a2]), status=PR.CONFIRMED)
            self._document(order)
            self._request(bill=self._bill(self.opex_c))
        self.assertEqual(len(few), 5)
        self.assertEqual(len(self._queries()), 5)

    def test_no_query_names_an_amount_column(self):
        # Data in every arm, so no query is skipped or short-circuited.
        order = self._order(sites=[self.opex_a1], programs=[self.tender_c])
        self._request(order, status=PR.CONFIRMED)
        self._document(order)
        self._request(bill=self._bill(self.opex_c), status=PR.APPROVED)
        queries = self._queries()
        self.assertEqual(len(queries), 5)
        for sql in queries:
            # amount, approved_amount, invoice_amount, total_amount: selected or filtered.
            self.assertNotIn('amount', sql.lower(), sql)

    def test_the_counts_ignore_the_amounts(self):
        order = self._order(sites=[self.opex_a1])
        small = self._request(order, status=PR.CONFIRMED)
        before = self._finance()
        PR.objects.filter(pk=small.pk).update(amount=Decimal('99999999'),
                                              approved_amount=Decimal('1'))
        self.assertEqual(self._finance(), before)


# ---------------------------------------------------------------------------
# 5. What is drawn
# ---------------------------------------------------------------------------

class DrawnTests(SimpleTestCase):

    def test_zeros_are_written_as_0(self):
        label, runs = S.finance_line({'finance': dict(ZERO)})
        self.assertEqual(label, 'Finance')
        self.assertEqual(S.line_text(runs),
                         '0 invoices · 0 payment requests · 0 approved · 0 pending · 0 paid')

    def test_plurals_follow_the_dc_rule(self):
        one = dict.fromkeys(S.FINANCE_KEYS, 1)
        self.assertEqual(S.line_text(S.finance_line({'finance': one})[1]),
                         '1 invoice · 1 payment request · 1 approved · 1 pending · 1 paid')
        many = dict(invoices=12, requests=1234, approved=9, payment_pending=3, paid=0)
        self.assertEqual(S.line_text(S.finance_line({'finance': many})[1]),
                         '12 invoices · 1,234 payment requests · 9 approved · 3 pending '
                         '· 0 paid')

    def test_finance_is_the_last_line_in_the_box(self):
        lines = S.section_lines(_section(1, 'T1'))
        self.assertEqual([label for label, _ in lines],
                         ['Design', 'Procurement', '', 'Finance'])

    def test_drawn_in_plain_ink_zeros_included_and_without_arrows(self):
        section = _section(1, 'T1', invoices=0, requests=40, approved=31,
                           payment_pending=17, paid=0)
        # A previous payload with other counts: still no arrow on the Finance line (Q6).
        before = {'tender_sections': {'1': _section(1, 'T1', invoices=5, requests=2,
                                                    approved=1, payment_pending=1,
                                                    paid=9)}}
        deltas = S.section_deltas(section, before)
        for key in S.FINANCE_KEYS:
            self.assertNotIn(key, deltas)
        self.assertEqual([kind for _, kind, _ in S.finance_line(section)[1]
                          if kind == 'arrow'], [])
        draw = _Recorder()
        S._lines(draw, S._fonts(), section, deltas, 0)
        finance = [(text, fill) for text, fill, _ in draw.texts
                   if text in ('40', '31', '17') or text.startswith(' payment request')]
        self.assertEqual(finance, [('40', S.INK), (' payment requests', S.MUTED),
                                   ('31', S.INK), ('17', S.INK)])
        self.assertIn(('Finance', S.INK), [(t, f) for t, f, _ in draw.texts])
        for key in S.FINANCE_KEYS:
            self.assertEqual(S.line_colour(key, 0), S.INK)
            self.assertEqual(S.line_colour(key, 5), S.INK)

    def test_three_digit_counts_fit_on_one_line(self):
        from PIL import Image, ImageDraw
        draw = ImageDraw.Draw(Image.new('RGB', (10, 10)))
        fonts = S._fonts()
        label, runs = S.finance_line({'finance': dict.fromkeys(S.FINANCE_KEYS, 888)})
        room = (S.IMAGE_WIDTH - S.MARGIN - 20) - (S.MARGIN + 20 + S.LINE_LABEL_W)
        fonts_of = {'num': fonts.line_num, 'text': fonts.line_text}
        width = sum(draw.textlength(t, font=fonts_of[kind]) for t, kind, _ in runs)
        self.assertLessEqual(width, room)
        self.assertLessEqual(draw.textlength(label, font=fonts.line_label),
                             S.LINE_LABEL_W - 8)

    def test_the_box_grew_by_one_line_and_one_rule(self):
        self.assertEqual(S.LINES_H, 2 * S.LINES_PAD + 4 * S.LINE_H + 2 * S.LINE_RULE)
        self.assertEqual(S.SECTION_H, 668)


class DryRunTests(FinanceFixture):

    def test_the_dry_run_prints_the_finance_line(self):
        self._request(self._order(sites=[self.opex_a1]), status=PR.CONFIRMED)
        out = io.StringIO()
        with mock.patch('django.utils.timezone.now', return_value=NOW), \
                mock.patch('projects.notifications._zeptomail_post') as post:
            call_command('send_dashboard_snapshot', '--dry-run', '--out-dir',
                         tempfile.mkdtemp(), stdout=out)
        out = out.getvalue()
        self.assertIn('  Finance      0 invoices · 1 payment request · 1 approved · '
                      '0 pending · 1 paid', out)
        self.assertIn('  Finance      0 invoices · 0 payment requests · 0 approved · '
                      '0 pending · 0 paid', out)
        self.assertNotIn('₹', out)
        post.assert_not_called()
