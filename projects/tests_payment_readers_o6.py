"""O6 — the payment readers rewritten around orders, the legacy fields and views retired.

What this file pins, and why each matters:

  * ONE COUNT. payments.payment_counts() is what the queue's tabs, the Finance dashboard's
    payment tiles and the CEO dashboard's finance tile read, so for the same payments —
    a site-less one, one on a Draft OPEX site, a partially approved one — the three
    figures are identical, under every ?context=. Sums are effective_amount (A1).
  * ONE AMOUNT RULE (A2, A3). One figure when requested equals effective; "₹X requested ·
    ₹Y approved" for a standing partial approval; "₹X requested · ₹Y previously approved"
    on a request back awaiting approval or on hold with an approval on record.
  * CARD 4b IS ORDERS. Every order sized against the site — including a group order whose
    display anchor is a DIFFERENT site — with the model's five labels, and no confirm
    button for anyone: Finance pays from the queue.
  * THE OLD DOORS ARE GONE. The detail URL redirects to the order; raise and confirm 404.
  * THE DROP MIGRATION REFUSES a legacy value rather than discarding it.
  * THE APPROVER FLAG belongs to Finance and CEO only.
  * record_transition(notify=False) sends nothing; notify=True sends.

Run with:
    python manage.py test projects.tests_payment_readers_o6 --settings=solarpms.test_settings
"""
import importlib
import re
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import AdminUserEditForm
from .models import (
    PaymentRequest, Project, StatusTransition, SUBJECT_PAYMENT_REQUEST, VendorOrder,
    VendorOrderLine,
)
from .payments import payment_counts
from .tests_payment_queue import QueueFixture, _make_order, _make_payment
from .tests_vendor_order_raise import _client, _profile, _project
from .utils import record_transition


def _text(response_or_html):
    """The page's visible text, tags stripped and whitespace collapsed, so an assertion
    reads the words a person sees rather than the markup around them."""
    html = (response_or_html if isinstance(response_or_html, str)
            else response_or_html.content.decode())
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', html))


def _card_4b(response):
    """Card 4b's HTML alone — from its id to the next card's heading."""
    html = response.content.decode()
    start = html.index('id="vendor-orders-card"')
    return html[start:html.index('Project Documents', start)]


class ReadersFixture(QueueFixture):
    """QueueFixture's four payments (Residential pending; APPROVED on a DRAFT OPEX site;
    SITE-LESS OPEX on hold; SITE-LESS CAPEX pending) plus the two O6 needs: a site-less
    OPEX request APPROVED FOR LESS than it asked, and an approved Residential one."""

    def setUp(self):
        super().setUp()
        self.partial_order = _make_order(self, 'OPEX', program=self.central_tender,
                                         po='PO-PART-1')
        self.partial_pay = PaymentRequest.objects.create(
            vendor_order=self.partial_order, project=None, vendor=self.vendor,
            amount=Decimal('30000'), approved_amount=Decimal('12000'),
            requested_by=self.scm.user, status=PaymentRequest.APPROVED,
            approved_by=self.approver, approved_at=timezone.now())
        self.res_approved = _make_payment(self, self.res_order, amount='5000',
                                          status=PaymentRequest.APPROVED,
                                          approved_by=self.approver)


# ---------------------------------------------------------------------------
# 1. One count function — and the three figures agree
# ---------------------------------------------------------------------------

class PaymentCountsTests(ReadersFixture):

    def test_every_status_is_present_and_zero_filled(self):
        counts = payment_counts(['CAPEX'])
        self.assertEqual(set(counts), {value for value, _ in PaymentRequest.STATUS_CHOICES})
        self.assertEqual(counts[PaymentRequest.APPROVED].count, 0)
        self.assertEqual(counts[PaymentRequest.APPROVED].amount, Decimal('0'))

    def test_sums_are_effective_amount_never_amount(self):
        """A1. The partial approval counts at 12,000, not the 30,000 it asked for."""
        approved = payment_counts(['OPEX'])[PaymentRequest.APPROVED]
        self.assertEqual(approved.count, 2)
        self.assertEqual(approved.amount, Decimal('32000'))      # 20,000 + 12,000
        self.assertEqual(approved.requested, Decimal('50000'))   # 20,000 + 30,000

    def test_none_is_every_type_and_a_list_narrows(self):
        everything = payment_counts()[PaymentRequest.APPROVED]
        self.assertEqual((everything.count, everything.amount), (3, Decimal('37000')))
        tenders = payment_counts(['OPEX', 'CAPEX'])[PaymentRequest.APPROVED]
        self.assertEqual((tenders.count, tenders.amount), (2, Decimal('32000')))

    def test_a_siteless_and_a_draft_site_payment_are_counted(self):
        pending = payment_counts(['CAPEX'])[PaymentRequest.PENDING_APPROVAL]
        self.assertEqual(pending.count, 1)                      # the site-less CAPEX one
        self.assertIsNone(self.capex_pay.project)
        self.assertEqual(self.opex_site.status, 'Draft')
        self.assertEqual(payment_counts(['OPEX'])[PaymentRequest.ON_HOLD].count, 1)


class ThreeWayAgreementTests(ReadersFixture):
    """dashboard_finance, the CEO tile and the queue, same data, same numbers — including
    the site-less payment, the Draft OPEX site and the partial approval."""

    def _queue_to_pay(self, context=None):
        """The queue's approved-to-pay count and value, summed over the tabs a context
        covers — read from the queue's own tab labels and tiles."""
        tabs = [t['value'] for t in self.queue().context['tabs']]
        wanted = {None: tabs, 'residential': ['Residential'],
                  'tenders': ['OPEX', 'CAPEX']}[context]
        count, amount = 0, Decimal('0')
        for tab in tabs:
            if tab not in wanted:
                continue
            response = self.queue(tab=tab)
            label = next(t for t in response.context['tabs'] if t['value'] == tab)
            tile_count, tile_amount = response.context['tiles']['to_pay']
            self.assertEqual(label['to_pay'], tile_count)
            count += tile_count
            amount += tile_amount
        return count, amount

    def _finance(self, context=None):
        url = reverse('dashboard_finance') + (f'?context={context}' if context else '')
        ctx = _client(self.finance).get(url).context
        return ctx['total_payment_requests'], ctx['total_payment_request_value']

    def _ceo(self, context=None):
        url = reverse('dashboard_ceo') + (f'?context={context}' if context else '')
        ctx = _client(self.ceo).get(url).context
        return ctx['fin_payment_requests_pending'], ctx['fin_vendor_payments_outstanding']

    def test_the_three_figures_are_identical(self):
        queue, finance, ceo = self._queue_to_pay(), self._finance(), self._ceo()
        print(f'\n[O6 three-way] queue={queue} finance={finance} ceo={ceo}')
        self.assertEqual(queue, (3, Decimal('37000')))
        self.assertEqual(finance, queue)
        self.assertEqual(ceo, queue)

    def test_they_agree_under_each_context(self):
        for context in ('residential', 'tenders'):
            with self.subTest(context=context):
                queue = self._queue_to_pay(context)
                self.assertEqual(self._finance(context), queue)
                self.assertEqual(self._ceo(context), queue)
        self.assertEqual(self._queue_to_pay('residential'), (1, Decimal('5000')))
        self.assertEqual(self._queue_to_pay('tenders'), (2, Decimal('32000')))


# ---------------------------------------------------------------------------
# A2 / A3 — one amount rule
# ---------------------------------------------------------------------------

class AmountRuleTests(ReadersFixture):

    def test_the_dashboard_tiles_show_both_figures_when_they_differ(self):
        """A2. 55,000 was asked for, 37,000 approved."""
        finance = _text(_client(self.finance).get(reverse('dashboard_finance')))
        self.assertIn('₹55000 requested · ₹37000 approved', finance)
        ceo = _text(_client(self.ceo).get(reverse('dashboard_ceo')))
        self.assertIn('₹55000 requested · ₹37000 approved', ceo)

    def test_the_dashboard_tiles_show_one_figure_when_they_agree(self):
        PaymentRequest.objects.filter(pk=self.partial_pay.pk).update(
            approved_amount=Decimal('30000'))
        finance = _text(_client(self.finance).get(reverse('dashboard_finance')))
        self.assertIn('₹55000', finance)
        self.assertNotIn('requested ·', finance)

    def test_the_finance_rows_name_the_order_and_follow_the_rule(self):
        """S2. PO number, else PI number, else "Order #<pk>" — and the amount rule."""
        Project.objects.filter(pk=self.project.pk).update(status='Active')
        PaymentRequest.objects.filter(pk=self.res_approved.pk).update(
            approved_amount=Decimal('4000'))
        text = _text(_client(self.finance).get(reverse('dashboard_finance')))
        self.assertIn('PO PO-RES-1 ·', text)
        self.assertIn('₹5000 requested · ₹4000 approved', text)
        self.assertNotIn('Inv #', text)

    def test_the_finance_row_falls_back_to_pi_then_order_number(self):
        for po, pi, expected in (('', 'PI-9', 'PI PI-9 ·'), ('', '', 'Order #')):
            with self.subTest(expected=expected):
                VendorOrder.objects.filter(pk=self.res_order.pk).update(
                    po_number=po, pi_number=pi)
                text = _text(_client(self.finance).get(reverse('dashboard_finance')))
                self.assertIn(expected, text)
        self.assertIn(f'Order #{self.res_order.pk} ·', text)

    def test_the_queue_and_the_order_page_say_previously_approved_on_hold(self):
        """A3. Held after an approval for 600 of 1,000: both screens say so."""
        order = _make_order(self, 'Residential', site=self.project, po='PO-A3')
        pay = _make_payment(self, order, amount='1000', status=PaymentRequest.ON_HOLD)
        PaymentRequest.objects.filter(pk=pay.pk).update(approved_amount=Decimal('600'))
        expected = '₹1000.00 requested · ₹600.00 previously approved'
        self.assertIn(expected, _text(self.queue(tab='Residential')))
        self.assertIn(expected, _text(_client(self.finance).get(
            reverse('vendor_order_detail', args=[order.pk]))))

    def test_previously_approved_also_on_a_request_back_awaiting_approval(self):
        """A3 reads "approved_amount is set": an approval in full, reopened, says so too."""
        order = _make_order(self, 'Residential', site=self.project, po='PO-A3B')
        pay = _make_payment(self, order, amount='1000')
        PaymentRequest.objects.filter(pk=pay.pk).update(approved_amount=Decimal('1000'))
        self.assertIn('₹1000.00 requested · ₹1000.00 previously approved',
                      _text(self.queue(tab='Residential', status='pending_approval')))

    def test_a_standing_partial_approval_says_approved(self):
        self.assertIn('₹30000.00 requested · ₹12000.00 approved',
                      _text(self.queue(tab='OPEX')))

    def test_a_plain_request_is_one_figure(self):
        text = _text(self.queue(tab='CAPEX'))
        self.assertIn('20000.00', text)
        self.assertNotIn('20000.00 requested', text)


# ---------------------------------------------------------------------------
# A5 — the CAPEX tab
# ---------------------------------------------------------------------------

class CapexTabTests(QueueFixture):

    def _remove_capex(self):
        self.capex_pay.delete()
        VendorOrderLine.objects.filter(order=self.capex_order).delete()
        self.capex_order.delete()

    def test_the_capex_tab_is_drawn_while_a_capex_order_exists(self):
        tabs = [t['value'] for t in self.queue().context['tabs']]
        self.assertEqual(tabs, ['Residential', 'OPEX', 'CAPEX'])

    def test_an_order_with_no_payment_is_enough(self):
        self.capex_pay.delete()
        tabs = [t['value'] for t in self.queue().context['tabs']]
        self.assertIn('CAPEX', tabs)

    def test_no_capex_order_no_capex_tab(self):
        self._remove_capex()
        response = self.queue()
        self.assertEqual([t['value'] for t in response.context['tabs']],
                         ['Residential', 'OPEX'])
        self.assertNotContains(response, 'CAPEX ·')

    def test_asking_for_the_missing_tab_lands_on_the_first(self):
        self._remove_capex()
        self.assertEqual(self.queue(tab='CAPEX').context['tab'], 'Residential')


# ---------------------------------------------------------------------------
# 2. Card 4b
# ---------------------------------------------------------------------------

class Card4bTests(QueueFixture):

    def overview(self, profile, project=None):
        return _client(profile).get(
            reverse('project_overview', args=[(project or self.project).project_id]))

    def test_it_lists_a_group_order_anchored_to_a_different_site(self):
        """The payment's anchor is site A (the lower pk); site B's card still lists the
        order, because B is one of the sites it was sized against."""
        site_a = _project('Group Site A', self.pm, project_type='OPEX', program=self.tender)
        site_b = _project('Group Site B', self.pm, project_type='OPEX', program=self.tender)
        order = _make_order(self, 'OPEX', site=site_a, po='PO-GRP-1')
        order.sites.create(project=site_b)
        pay = _make_payment(self, order)
        self.assertEqual(pay.project, site_a)

        response = self.overview(self.pm, site_b)
        self.assertEqual([e['order'] for e in response.context['site_orders']], [order])
        card = _card_4b(response)
        self.assertIn('PO PO-GRP-1', card)
        self.assertIn(reverse('vendor_order_detail', args=[order.pk]), card)

    def test_each_of_the_five_labels_is_the_models(self):
        order = _make_order(self, 'Residential', site=self.project, po='PO-FIVE')
        for status in (PaymentRequest.APPROVED, PaymentRequest.ON_HOLD,
                       PaymentRequest.CONFIRMED):
            _make_payment(self, order, amount='1000', status=status)
        PaymentRequest.objects.create(
            vendor_order=order, project=self.project, vendor=self.vendor,
            amount=Decimal('1000'), requested_by=self.scm.user,
            status=PaymentRequest.REJECTED, decision_reason='Duplicate')
        card = _card_4b(self.overview(self.finance))
        # res_pay (fixture) is the PENDING_APPROVAL one, on res_order.
        for _, label in PaymentRequest.STATUS_CHOICES:
            with self.subTest(label=label):
                self.assertIn(f'<span class="payment-status">{label}</span>', card)
        self.assertNotIn('>Pending<', card)
        self.assertNotIn('>Confirmed<', card)

    def test_it_shows_total_and_paid_newest_order_first(self):
        paid = _make_order(self, 'Residential', site=self.project, po='PO-PAID',
                           total=Decimal('40000'))
        _make_payment(self, paid, amount='15000', status=PaymentRequest.CONFIRMED)
        response = self.overview(self.finance)
        self.assertEqual([e['order'] for e in response.context['site_orders']],
                         [paid, self.res_order])
        self.assertIn('Order ₹40000 · paid ₹15000', _text(_card_4b(response)))

    def test_no_confirm_button_for_anyone(self):
        _make_payment(self, self.res_order, status=PaymentRequest.APPROVED,
                      approved_by=self.approver)
        for profile in (self.finance, self.pm, self.scm, self.admin):
            with self.subTest(role=profile.role):
                response = self.overview(profile)
                html = response.content.decode()
                self.assertNotIn('js-confirm-payment', html)
                self.assertNotIn('confirmPaymentModal', html)
                self.assertNotIn('/confirm/', _card_4b(response))
                self.assertNotIn('<button', _card_4b(response))
                self.assertNotIn('<form', _card_4b(response))

    def test_the_queue_link_follows_the_queue_predicate(self):
        for profile, shown in ((self.finance, True), (self.admin, True),
                               (self.pm, False), (self.scm, False)):
            with self.subTest(role=profile.role):
                card = _card_4b(self.overview(profile))
                self.assertEqual('Pay from the payments queue' in card, shown)

    def test_it_links_the_sites_order_list(self):
        card = _card_4b(self.overview(self.pm))
        self.assertIn(reverse('vendor_order_list', args=[self.project.pk]), card)


# ---------------------------------------------------------------------------
# 3 / 4. The detail URL and My Documents
# ---------------------------------------------------------------------------

class DetailAndMyDocumentsTests(ReadersFixture):

    def test_the_old_detail_url_redirects_to_the_payments_row(self):
        response = _client(self.pm).get(
            reverse('payment_request_detail', args=[self.project.project_id, self.res_pay.pk]))
        self.assertRedirects(
            response,
            reverse('vendor_order_detail', args=[self.res_order.pk]) + f'#payment-{self.res_pay.pk}',
            fetch_redirect_response=False)

    def test_the_order_page_carries_the_anchor(self):
        response = _client(self.finance).get(
            reverse('vendor_order_detail', args=[self.res_order.pk]))
        self.assertContains(response, f'id="payment-{self.res_pay.pk}"')

    def test_an_unknown_payment_is_404(self):
        response = _client(self.pm).get(
            reverse('payment_request_detail', args=[self.project.project_id, 999999]))
        self.assertEqual(response.status_code, 404)

    def test_my_documents_shows_a_siteless_payment_linked_to_its_order(self):
        response = _client(self.scm).get(reverse('my_documents'))
        self.assertIn(self.central_pay, list(response.context['pr_list']))
        text = _text(response)
        self.assertIn('PI PI-CENTRAL', text)
        self.assertIn('Central Stock Tender', text)                 # scope_label
        self.assertContains(response, reverse('vendor_order_detail',
                                              args=[self.central_order.pk]))
        self.assertIn('On hold', text)

    def test_my_documents_follows_the_amount_rule(self):
        text = _text(_client(self.scm).get(reverse('my_documents')))
        self.assertIn('₹30000 requested · ₹12000 approved', text)


# ---------------------------------------------------------------------------
# 5. Retired views and the drop migration
# ---------------------------------------------------------------------------

class RetiredUrlTests(QueueFixture):

    def test_every_removed_url_returns_404(self):
        paths = [f'/projects/{self.project.project_id}/payment-requests/raise/',
                 f'/projects/{self.project.project_id}/payment-requests/{self.res_pay.pk}/confirm/']
        for profile in (self.scm, self.finance):
            for path in paths:
                with self.subTest(role=profile.role, path=path):
                    self.assertEqual(_client(profile).get(path).status_code, 404)
                    self.assertEqual(_client(profile).post(path, {}).status_code, 404)


_MIGRATION = importlib.import_module('projects.migrations.0101_payment_request_drop_legacy_fields')


class DropMigrationRefusesTests(TestCase):
    """0101's check, run against a real table carrying the five legacy columns — the
    suite's own schema is built from today's models, which no longer have them. The same
    SQL runs in the migration against projects_paymentrequest."""

    TABLE = 'o6_legacy_probe'

    def setUp(self):
        with connection.cursor() as cursor:
            cursor.execute(
                f'CREATE TABLE {self.TABLE} (id integer PRIMARY KEY, boq_item_id integer NULL, '
                f'invoice_number varchar(100) NOT NULL, '
                f'invoice_document_name varchar(255) NOT NULL, '
                f'invoice_document_url varchar(1000) NOT NULL, '
                f'invoice_document_path varchar(500) NOT NULL)')
            # Row 1 is what every O2-onward raise wrote: blank text, NULL FK.
            cursor.execute(f"INSERT INTO {self.TABLE} VALUES (1, NULL, '', '', '', '')")

    def _run(self):
        apps = SimpleNamespace(get_model=lambda app, model: SimpleNamespace(
            _meta=SimpleNamespace(db_table=self.TABLE)))
        _MIGRATION.refuse_legacy_values(apps, SimpleNamespace(connection=connection))

    def test_blank_rows_pass(self):
        self._run()

    def test_a_value_in_any_one_column_refuses_and_names_the_row(self):
        for column, value in (('boq_item_id', '7'), ('invoice_number', "'INV-1'"),
                              ('invoice_document_name', "'i.pdf'"),
                              ('invoice_document_url', "'http://x/i.pdf'"),
                              ('invoice_document_path', "'p/i.pdf'")):
            with self.subTest(column=column):
                with connection.cursor() as cursor:
                    cursor.execute(f"INSERT INTO {self.TABLE} VALUES (42, NULL, '', '', '', '')")
                    cursor.execute(f'UPDATE {self.TABLE} SET {column} = {value} WHERE id = 42')
                with self.assertRaisesRegex(RuntimeError, r'0101 refused: 1 .* pk 42\. '):
                    self._run()
                with connection.cursor() as cursor:
                    cursor.execute(f'DELETE FROM {self.TABLE} WHERE id = 42')

    def test_the_migration_runs_the_check_before_any_drop(self):
        from django.db.migrations import RemoveField
        operations = _MIGRATION.Migration.operations
        self.assertIs(operations[0].code, _MIGRATION.refuse_legacy_values)
        self.assertEqual(
            sorted(op.name for op in operations if isinstance(op, RemoveField)),
            ['boq_item', 'invoice_document_name', 'invoice_document_path',
             'invoice_document_url', 'invoice_number'])


# ---------------------------------------------------------------------------
# 6. The approver flag
# ---------------------------------------------------------------------------

class ApproverFlagRoleTests(TestCase):

    def _form(self, role, flag, username='o6_flag_user'):
        user = _profile(username, role).user
        data = {'first_name': 'Asha', 'last_name': 'Rao', 'username': username,
                'email': f'{username}@example.com', 'phone_number': '9876543210',
                'role': role}
        if flag:
            data['is_payment_approver'] = 'on'
        return AdminUserEditForm(data=data, instance_user=user)

    def test_refused_for_scm_pm_and_admin(self):
        for role in ('SCM', 'PM', 'Admin'):
            with self.subTest(role=role):
                form = self._form(role, flag=True, username=f'o6_{role.lower()}')
                self.assertFalse(form.is_valid())
                self.assertEqual(list(form.errors), ['is_payment_approver'])
                self.assertIn('Only a Finance or CEO user may be a payment approver',
                              form.errors['is_payment_approver'][0])

    def test_the_same_roles_save_without_the_flag(self):
        for role in ('SCM', 'PM', 'Admin'):
            with self.subTest(role=role):
                self.assertTrue(self._form(role, flag=False,
                                           username=f'o6_{role.lower()}_plain').is_valid())

    def test_accepted_for_finance_and_ceo(self):
        for role in ('Finance', 'CEO'):
            with self.subTest(role=role):
                form = self._form(role, flag=True, username=f'o6_{role.lower()}')
                self.assertTrue(form.is_valid(), form.errors)


# ---------------------------------------------------------------------------
# 7. Notification suppression
# ---------------------------------------------------------------------------

class NotifySuppressionTests(QueueFixture):
    """A raise (from '' to PENDING_APPROVAL) tells every approver but the requester."""

    def _record(self, **kwargs):
        with patch('projects.payments.send_notification') as sender:
            with self.captureOnCommitCallbacks(execute=True):
                record_transition(self.res_pay, to_status=PaymentRequest.PENDING_APPROVAL,
                                  from_status='', actor=self.scm, **kwargs)
        return sender

    def test_notify_false_sends_nothing(self):
        self._record(notify=False).assert_not_called()

    def test_notify_true_sends(self):
        sender = self._record(notify=True)
        self.assertTrue(sender.called)
        self.assertEqual({c.kwargs['recipient'] for c in sender.call_args_list},
                         {self.approver, self.approver_b})

    def test_the_default_is_to_send(self):
        self.assertTrue(self._record().called)

    def test_suppression_still_writes_the_ledger_row(self):
        self._record(notify=False)
        self.assertTrue(StatusTransition.objects.filter(
            subject_type=SUBJECT_PAYMENT_REQUEST, subject_id=self.res_pay.pk).exists())
