"""O8a — the purchases & payments workspace: the list, Add PO / PI, and Raise payment
request.

What this file pins, and why each matters:

  * ADD PO / PI WRITES THE RECORD THE GROUP RAISE WRITES. The same recorded-against
    posted to both pages produces the same order, site, tender and line rows — the proof
    that the new page reuses _write_order_record() rather than a copy of it.
  * THE PAYMENT IS OPTIONAL, and when ticked it is Request payment's own writer:
    PENDING_APPROVAL, one ledger row, in the record's transaction.
  * WHAT A RECORD IS FOLLOWS FROM WHAT IT IS RECORDED AGAINST. Residential projects make
    a Residential record (its program derived, as vendor_order_create derives it); RESCO
    tenders and sites a RESCO one; the two together are refused; nothing recorded against
    needs the explicit choice.
  * RAISE PAYMENT REQUEST IS vendor_order_add_payment's PATH: the same balance refusal,
    the same client_uuid idempotency, and a record with nothing left is not offered.
  * WHO. SCM acts; Finance, CEO, Admin and System Admin read the list without the two
    actions; PM, Design and Site Engineer are refused.
  * THE LIST COSTS THE SAME FOR ONE ROW AS FOR FIFTY.
  * THE GROUP RAISE IS UNCHANGED. Its own file passes untouched; here, only that its page
    still draws no Residential row now that the picker is shared.

Run with:
    python manage.py test projects.tests_purchases_workspace --settings=solarpms.test_settings
"""
import re
import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.urls import reverse

from .models import (
    PaymentRequest, Project, StatusTransition, VendorOrder, VendorOrderDocument,
    VendorOrderLine, VendorOrderProgram, VendorOrderSite,
    SUBJECT_PAYMENT_REQUEST, VENDOR_ORDER_DOC_INVOICE, VENDOR_ORDER_DOC_PO,
)
from .tests_vendor_order_group import GroupRaiseFixture, _Capture
from .tests_vendor_order_raise import _client, _pdf, _profile


class WorkspaceFixture(GroupRaiseFixture):
    """GroupRaiseFixture's two tenders, four groups and six RESCO sites, plus its
    Residential project with a two-line BOQ — and the roles the workspace admits or
    refuses."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ceo      = _profile('o8_ceo', 'CEO')
        cls.sysadmin = _profile('o8_sysadmin', 'System Admin')

    # ── posting ────────────────────────────────────────────────────────────────

    def new_payload(self, sites=(), programs=(), amounts=None, pay=None, **overrides):
        """A valid Add PO / PI submission: a PO document and an order total of
        ₹10,00,000. `sites` as GroupRaiseFixture.payload takes them; `pay` ticks the
        payment box with that amount. A None override drops the key."""
        data = self.payload(sites=sites, programs=programs, amounts=amounts)
        data.pop('payment_amount')
        if pay is not None:
            data['request_payment'] = '1'
            data['payment_amount'] = str(pay)
            data['payment_note'] = 'advance'
        data.update(overrides)
        return {k: v for k, v in data.items() if v is not None}

    def post_new(self, profile=None, storage=None, **kwargs):
        storage = storage or MagicMock()
        with patch('projects.supabase_storage.get_supabase_client', return_value=storage):
            response = _client(profile or self.scm).post(
                reverse('purchases_new'), self.new_payload(**kwargs))
        return response, storage

    def post_pay(self, order, amount, profile=None, client_uuid=None, **extra):
        data = {'order': str(order.pk) if order is not None else '',
                'client_uuid': client_uuid or str(uuid.uuid4()),
                'payment_amount': str(amount), 'payment_note': 'second', **extra}
        return _client(profile or self.scm).post(reverse('purchases_pay'), data)

    def record(self, total='1000', project=None, po='PO-EXISTING', vendor=None):
        """An existing record, written directly — as the payments tests write theirs."""
        order = VendorOrder.objects.create(
            vendor=vendor or self.vendor, project_type='Residential', po_number=po,
            created_by=self.scm, total_amount=Decimal(total))
        if project is not None:
            VendorOrderSite.objects.create(order=order, project=project)
        return order

    def pay_existing(self, order, amount, status=PaymentRequest.PENDING_APPROVAL):
        return PaymentRequest.objects.create(
            vendor_order=order, vendor=order.vendor, amount=Decimal(amount),
            requested_by=self.scm.user, status=status,
            decision_reason='no' if status == PaymentRequest.REJECTED else '')


def _rows(order):
    """An order's written rows, in a form two orders can be compared by."""
    return {
        'project_type': order.project_type,
        'total':        order.total_amount,
        'unfrozen':     order.raised_with_unfrozen_quantities,
        'sites':        sorted((s.project_id, s.via_site_group_id) for s in order.sites.all()),
        'programs':     sorted(p.program_id for p in order.programs.all()),
        'lines':        sorted((l.item_master_id, l.boq_item_id, l.item_code,
                                l.item_description, l.item_unit, l.item_category,
                                l.quantity, l.amount) for l in order.lines.all()),
    }


# ---------------------------------------------------------------------------
# Add PO / PI
# ---------------------------------------------------------------------------

class AddRecordTests(WorkspaceFixture):

    def test_no_payment_and_nothing_recorded_against_choosing_resco(self):
        response, storage = self.post_new(project_type='OPEX')
        order = VendorOrder.objects.get()
        self.assertRedirects(response, reverse('vendor_order_detail', args=[order.pk]),
                             fetch_redirect_response=False)
        self.assertEqual(order.project_type, 'OPEX')
        self.assertEqual(order.total_amount, Decimal('1000000'))
        self.assertEqual(order.created_by, self.scm)
        self.assertEqual(PaymentRequest.objects.count(), 0)
        self.assertFalse(order.sites.exists())
        self.assertFalse(order.programs.exists())
        self.assertFalse(order.lines.exists())
        self.assertEqual(order.documents.get().doc_type, VENDOR_ORDER_DOC_PO)
        storage.storage.from_.return_value.upload.assert_called_once()

    def test_choosing_residential_with_nothing_recorded_against(self):
        self.post_new(project_type='Residential')
        self.assertEqual(VendorOrder.objects.get().project_type, 'Residential')

    def test_the_payment_tick_raises_one_pending_request_with_its_ledger_row(self):
        self.post_new(project_type='OPEX', pay='250000')
        order = VendorOrder.objects.get()
        pr = PaymentRequest.objects.get()
        self.assertEqual(pr.vendor_order, order)
        self.assertEqual(pr.status, PaymentRequest.PENDING_APPROVAL)
        self.assertEqual(pr.amount, Decimal('250000'))
        self.assertEqual(pr.note, 'advance')
        self.assertEqual(pr.requested_by, self.scm.user)
        self.assertEqual(pr.vendor, self.vendor)
        self.assertIsNone(pr.project)                 # nothing recorded against: no anchor
        transitions = StatusTransition.objects.filter(subject_type=SUBJECT_PAYMENT_REQUEST)
        self.assertEqual(transitions.count(), 1)
        self.assertEqual(transitions.get().subject_id, pr.pk)
        self.assertEqual(transitions.get().to_status, PaymentRequest.PENDING_APPROVAL)
        self.assertEqual(order.available_to_request, Decimal('750000'))

    def test_a_ticked_payment_needs_an_amount_and_is_capped_at_the_total(self):
        for amount, text in (('', 'Enter the payment requested now'),
                             ('2000000', 'is more than the order total')):
            with self.subTest(amount=amount):
                response, storage = self.post_new(project_type='OPEX', pay=amount)
                self.assertContains(response, text, status_code=400)
                self.assertNothingCreated()
                storage.storage.from_.return_value.upload.assert_not_called()

    def test_an_unticked_payment_amount_is_ignored(self):
        self.post_new(project_type='OPEX', payment_amount='5000')
        self.assertEqual(PaymentRequest.objects.count(), 0)

    def test_two_tenders_and_three_sites_write_the_rows_the_group_page_writes(self):
        recorded = dict(
            sites=[(self.a1, self.locked_a), (self.a3, self.locked_b),
                   (self.b1, self.locked_c)],
            programs=[self.tender, self.tender_b],
            amounts={self.opex_module: '600000'})
        self.post(**recorded)                           # the group raise
        group_order = VendorOrder.objects.get()
        self.post_new(pay='200000', **recorded)         # Add PO / PI
        new_order = VendorOrder.objects.exclude(pk=group_order.pk).get()

        self.assertEqual(_rows(new_order), _rows(group_order))
        written = _rows(new_order)
        self.assertEqual(written['project_type'], 'OPEX')
        self.assertEqual(written['sites'], sorted([(self.a1.pk, self.locked_a.pk),
                                                   (self.a3.pk, self.locked_b.pk),
                                                   (self.b1.pk, self.locked_c.pk)]))
        self.assertEqual(written['programs'], sorted([self.tender.pk, self.tender_b.pk]))
        # Modules 100 + 40 + 80, inverters 2 + 1 + 2 — worked out on the server.
        quantities = {line[0]: (line[6], line[7]) for line in written['lines']}
        self.assertEqual(quantities, {
            self.opex_module.pk:   (Decimal('220'), Decimal('600000')),
            self.opex_inverter.pk: (Decimal('5'), None)})
        # And the payment anchors to the lowest pk, exactly as the group raise's does.
        self.assertEqual(new_order.payments.get().project_id,
                         group_order.payments.get().project_id)

    def test_a_residential_project_makes_a_residential_record_with_its_site_row(self):
        self.post_new(sites=[self.project])
        order = VendorOrder.objects.get()
        self.assertEqual(order.project_type, 'Residential')
        site = order.sites.get()
        self.assertEqual((site.project, site.via_site_group), (self.project, None))
        self.assertFalse(order.programs.exists())      # it has no tender
        self.assertFalse(order.raised_with_unfrozen_quantities)
        self.assertEqual(
            sorted((l.item_code, l.quantity) for l in order.lines.all()),
            [('ITM-901', Decimal('10')), ('ITM-902', Decimal('1'))])

    def test_a_residential_projects_program_is_recorded_when_it_has_one(self):
        """Unreachable through save() — Project._validate_program_link() forbids it — so
        written with update(). Pins that the derivation matches vendor_order_create's."""
        Project.objects.filter(pk=self.project.pk).update(program=self.tender)
        self.post_new(sites=[self.project])
        order = VendorOrder.objects.get()
        self.assertEqual(order.project_type, 'Residential')
        self.assertEqual([p.program for p in order.programs.all()], [self.tender])

    def test_a_residential_payment_anchors_to_the_project(self):
        self.post_new(sites=[self.project], pay='5000')
        self.assertEqual(PaymentRequest.objects.get().project, self.project)

    def test_mixing_a_residential_project_and_a_resco_site_is_refused(self):
        response, storage = self.post_new(sites=[self.project, self.a1],
                                          programs=[self.tender], pay='5000')
        self.assertContains(response, 'same kind of project', status_code=400)
        self.assertNothingCreated()
        storage.storage.from_.return_value.upload.assert_not_called()

    def test_a_residential_project_with_a_ticked_tender_is_refused(self):
        response, _ = self.post_new(sites=[self.project], programs=[self.tender])
        self.assertContains(response, 'not both', status_code=400)
        self.assertNothingCreated()

    def test_nothing_recorded_and_no_project_type_is_refused(self):
        for choice in (None, '', 'CAPEX', 'junk'):
            with self.subTest(choice=choice):
                response, _ = self.post_new(project_type=choice)
                self.assertContains(response, 'choose whether this PO / PI is Residential '
                                              'or RESCO', status_code=400)
                self.assertNothingCreated()

    def test_a_ticked_tender_alone_makes_a_resco_record_and_ignores_the_choice(self):
        self.post_new(programs=[self.tender], project_type='Residential')
        order = VendorOrder.objects.get()
        self.assertEqual(order.project_type, 'OPEX')
        self.assertEqual(order.programs.get().program, self.tender)

    def test_the_same_client_uuid_twice_creates_one_record(self):
        key = str(uuid.uuid4())
        first, _ = self.post_new(project_type='OPEX', pay='100', client_uuid=key)
        second, storage = self.post_new(project_type='OPEX', pay='100', client_uuid=key)
        self.assertEqual(VendorOrder.objects.count(), 1)
        self.assertEqual(PaymentRequest.objects.count(), 1)
        self.assertEqual(second['Location'], first['Location'])
        storage.storage.from_.return_value.upload.assert_not_called()

    def test_a_refused_submission_gives_every_entry_back(self):
        response, _ = self.post_new(sites=[self.project], programs=[self.tender],
                                    pay='777', order_total='4321')
        html = response.content.decode()
        self.assertIn('value="4321"', html)
        self.assertIn('value="777"', html)
        self.assertRegex(html, r'id="pwPayTick" checked')
        self.assertIn(f'name="site" value="{self.project.pk}" class="js-vo-site-input">',
                      html)

    def test_the_feed_records_the_site_and_the_payment(self):
        from .models import ActivityLog
        self.post_new(sites=[self.project], pay='5000')
        codes = sorted(ActivityLog.objects.filter(entity_type__in=['VendorOrder',
                                                                   'PaymentRequest'])
                       .values_list('action_code', flat=True))
        self.assertEqual(codes, ['payment_request_raised', 'vendor_order_raised'])

    def test_only_scm_may_open_or_post(self):
        for profile in (self.finance, self.ceo, self.admin, self.sysadmin, self.pm,
                        self.designer, self.se):
            with self.subTest(role=profile.role):
                self.assertEqual(_client(profile).get(reverse('purchases_new')).status_code,
                                 403)
                response, _ = self.post_new(profile=profile, project_type='OPEX')
                self.assertEqual(response.status_code, 403)
        self.assertNothingCreated()


class AddRecordPageTests(WorkspaceFixture):

    def test_the_page_is_in_its_sections_in_order_with_the_payment_optional(self):
        html = _client(self.scm).get(reverse('purchases_new')).content.decode()
        positions = [html.index(marker) for marker in
                     ('1 · PO / PI', '2 · Order total', '3 · Recorded against',
                      'Nothing is recorded against — is this PO / PI',
                      '4 · Requirement', 'Also raise a payment request now')]
        self.assertEqual(positions, sorted(positions))
        self.assertRegex(html, r'name="order_total"[^>]*required')
        self.assertNotRegex(html, r'name="payment_amount"[^>]*required')
        self.assertRegex(html, r'<details class="card[^"]*" id="voRequirement">')

    def test_the_picker_offers_resco_sites_and_residential_projects_labelled(self):
        html = _client(self.scm).get(reverse('purchases_new')).content.decode()
        results = re.findall(r'class="[^"]*js-vo-result[^"]*"\s+data-site="(\d+)"'
                             r'[^>]*?data-kind="res"', html)
        self.assertEqual(set(results), {str(self.project.pk), str(self.other_project.pk)})
        rescos = re.findall(r'class="[^"]*js-vo-result[^"]*"\s+data-site="(\d+)"\s+'
                            r'data-program="\d+"', html)
        self.assertEqual(set(rescos), {str(p.pk) for p in
                                       (self.a1, self.a2, self.a3, self.a4, self.a5,
                                        self.b1)})
        self.assertIn('>Residential</span>', html)
        self.assertIn('RESCO · Alpha Tender', html)
        self.assertIn('Residential projects · <span class="js-vo-n">0</span>', html)

    def test_program_prefill_ticks_that_tender(self):
        response = _client(self.scm).get(f"{reverse('purchases_new')}?program={self.tender.pk}")
        self.assertEqual([t['program'] for t in response.context['tenders'] if t['ticked']],
                         [self.tender])

    def test_project_prefill_adds_a_resco_site_and_ticks_its_tender(self):
        response = _client(self.scm).get(f"{reverse('purchases_new')}?project={self.b1.pk}")
        self.assertEqual([t['program'] for t in response.context['tenders'] if t['ticked']],
                         [self.tender_b])
        self.assertContains(response, f'name="site" value="{self.b1.pk}" '
                                      'class="js-vo-site-input">')

    def test_project_prefill_adds_a_residential_project(self):
        response = _client(self.scm).get(
            f"{reverse('purchases_new')}?project={self.project.pk}")
        self.assertFalse([t for t in response.context['tenders'] if t['ticked']])
        added = [r['project'] for r in response.context['residential_rows'] if r['added']]
        self.assertEqual(added, [self.project])
        self.assertContains(response, f'name="site" value="{self.project.pk}" '
                                      'class="js-vo-site-input">')

    def test_the_group_page_still_draws_no_residential_row(self):
        html = _client(self.scm).get(reverse('vendor_order_create_group')).content.decode()
        self.assertNotRegex(html, r'<(li|span)[^>]*data-kind="res"')
        self.assertNotIn('Residential projects ·', html)
        self.assertIn('Tick a tender to search its sites.', html)


# ---------------------------------------------------------------------------
# Raise payment request
# ---------------------------------------------------------------------------

class RaisePaymentTests(WorkspaceFixture):

    def test_a_payment_against_an_existing_record(self):
        order = self.record(total='1000', project=self.project)
        key = str(uuid.uuid4())
        response = self.post_pay(order, '400', client_uuid=key)
        self.assertRedirects(response, reverse('vendor_order_detail', args=[order.pk]),
                             fetch_redirect_response=False)
        pr = PaymentRequest.objects.get()
        self.assertEqual((pr.vendor_order, pr.amount, pr.status, pr.project),
                         (order, Decimal('400'), PaymentRequest.PENDING_APPROVAL,
                          self.project))
        self.assertEqual(str(pr.client_uuid), key)
        self.assertEqual(StatusTransition.objects.filter(
            subject_type=SUBJECT_PAYMENT_REQUEST, subject_id=pr.pk).count(), 1)

    def test_above_the_available_amount_is_refused_as_add_payment_refuses_it(self):
        order = self.record(total='1000', project=self.project)
        self.pay_existing(order, '700')
        response = self.post_pay(order, '301')
        self.assertContains(response, 'is more than the balance still available to '
                                      'request (₹300', status_code=400)
        self.assertEqual(PaymentRequest.objects.count(), 1)
        # The refusal keeps the record chosen and the entries typed.
        self.assertEqual(response.context['chosen']['order'], order)
        self.assertContains(response, 'value="301"', status_code=400)
        # Exactly the available amount is accepted.
        self.post_pay(order, '300')
        self.assertEqual(PaymentRequest.objects.count(), 2)

    def test_the_same_client_uuid_twice_creates_one_request(self):
        order = self.record(total='1000', project=self.project)
        key = str(uuid.uuid4())
        first = self.post_pay(order, '100', client_uuid=key)
        second = self.post_pay(order, '100', client_uuid=key)
        self.assertEqual(PaymentRequest.objects.count(), 1)
        self.assertEqual(second['Location'], first['Location'])

    def test_no_record_chosen_is_refused(self):
        response = self.post_pay(None, '100')
        self.assertContains(response, 'Choose the PO / PI record', status_code=400)
        self.assertEqual(PaymentRequest.objects.count(), 0)

    def test_a_record_on_a_deleted_site_is_refused_as_add_payment_refuses_it(self):
        order = self.record(total='1000', project=self.other_project)
        Project.objects.filter(pk=self.other_project.pk).update(is_deleted=True)
        self.assertEqual(self.post_pay(order, '100').status_code, 403)
        self.assertEqual(PaymentRequest.objects.count(), 0)

    def test_records_with_nothing_available_are_not_offered(self):
        full = self.record(total='1000', po='PO-FULL')
        self.pay_existing(full, '1000')
        freed = self.record(total='1000', po='PO-FREED')
        self.pay_existing(freed, '1000', status=PaymentRequest.REJECTED)
        held = self.record(total='1000', po='PO-HELD')
        self.pay_existing(held, '1000', status=PaymentRequest.ON_HOLD)
        open_ = self.record(total='1000', po='PO-OPEN')
        self.pay_existing(open_, '250')

        response = _client(self.scm).get(reverse('purchases_pay'))
        offered = {o['order']: o['available'] for o in response.context['options']}
        self.assertEqual(offered, {freed: Decimal('1000'), open_: Decimal('750')})
        self.assertContains(response, '₹750.00 available')

        response = _client(self.scm).get(f"{reverse('purchases_pay')}?order={full.pk}")
        self.assertTrue(response.context['not_offered'])
        self.assertContains(response, 'nothing left to request')

    def test_order_param_preselects_the_record(self):
        order = self.record(total='1000', project=self.project)
        response = _client(self.scm).get(f"{reverse('purchases_pay')}?order={order.pk}")
        self.assertEqual(response.context['chosen']['order'], order)
        self.assertContains(response, f'value="{order.pk}" data-available="1000.00"')
        self.assertRegex(response.content.decode(),
                         rf'value="{order.pk}"[^>]*selected>')
        self.assertContains(response, 'Add the PO / PI first</a>')

    def test_only_scm_may_open_or_post(self):
        order = self.record(total='1000', project=self.project)
        for profile in (self.finance, self.ceo, self.admin, self.sysadmin, self.pm,
                        self.designer, self.se):
            with self.subTest(role=profile.role):
                self.assertEqual(_client(profile).get(reverse('purchases_pay')).status_code,
                                 403)
                self.assertEqual(self.post_pay(order, '10', profile=profile).status_code, 403)
        self.assertEqual(PaymentRequest.objects.count(), 0)


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------

class WorkspaceListTests(WorkspaceFixture):

    def url(self, **params):
        query = '&'.join(f'{k}={v}' for k, v in params.items())
        return reverse('purchases_workspace') + (f'?{query}' if query else '')

    def setUp(self):
        # Three records: one against a site in each tender, and one Residential.
        self.post(sites=[(self.a1, self.locked_a)], programs=[self.tender],
                  po_number='PO-ALPHA')
        self.alpha = VendorOrder.objects.get(po_number='PO-ALPHA')
        self.post(sites=[(self.b1, self.locked_c)], programs=[self.tender_b],
                  po_number='PO-BRAVO')
        self.bravo = VendorOrder.objects.get(po_number='PO-BRAVO')
        self.post_new(sites=[self.project], po_number='PO-HOME')
        self.home = VendorOrder.objects.get(po_number='PO-HOME')

    def listed(self, response):
        return [row['order'] for row in response.context['rows']]

    def test_newest_first_with_money_badges_and_links_for_scm(self):
        response = _client(self.scm).get(self.url())
        self.assertEqual(self.listed(response), [self.home, self.bravo, self.alpha])
        alpha = next(r for r in response.context['rows'] if r['order'] == self.alpha)
        self.assertEqual((alpha['total'], alpha['paid'], alpha['available'], alpha['awaiting']),
                         (Decimal('1000000'), Decimal('0'), Decimal('800000'), 1))
        self.assertEqual(alpha['against']['tenders'], ['Alpha Tender'])
        self.assertEqual(alpha['against']['sites'], [self.a1.project_id])
        home = next(r for r in response.context['rows'] if r['order'] == self.home)
        self.assertEqual(home['awaiting'], 0)
        self.assertContains(response, '1 awaiting approval', count=2)
        self.assertContains(response, 'Add PO / PI</a>')
        self.assertContains(response, 'Raise payment request</a>')
        self.assertContains(response, f'?order={self.alpha.pk}" class="btn btn-sm '
                                      'btn-outline-secondary py-0">Request payment</a>')
        self.assertContains(response, reverse('vendor_order_detail', args=[self.alpha.pk]))

    def test_on_hold_and_invoice_awaited_badges(self):
        pr = self.alpha.payments.get()
        PaymentRequest.objects.filter(pk=pr.pk).update(status=PaymentRequest.ON_HOLD)
        VendorOrderDocument.objects.create(
            order=self.bravo, doc_type=VENDOR_ORDER_DOC_INVOICE, invoice_number='INV-1',
            invoice_amount=Decimal('10'), file_name='i.pdf', bucket='b', path='p',
            uploaded_by=self.scm)
        PaymentRequest.objects.filter(vendor_order=self.bravo).update(
            status=PaymentRequest.CONFIRMED, approved_amount=Decimal('200000'))
        response = _client(self.scm).get(self.url())
        self.assertContains(response, '1 on hold')
        self.assertContains(response, 'Invoice awaited', count=1)

    def test_no_request_payment_link_when_nothing_is_available(self):
        self.pay_existing(self.home, '1000000')
        response = _client(self.scm).get(self.url())
        self.assertNotContains(response, f'?order={self.home.pk}"')
        self.assertContains(response, f'?order={self.alpha.pk}"')

    def test_a_record_against_nothing_says_so(self):
        self.post_new(project_type='OPEX', po_number='PO-CENTRAL')
        self.assertContains(_client(self.scm).get(self.url(q='PO-CENTRAL')),
                            'Not recorded against any tender')

    def test_search(self):
        cases = {
            'Sunrise':        {self.alpha, self.bravo, self.home},   # vendor
            'po-bravo':       {self.bravo},                          # PO
            'PI-':            set(),
            'bravo tender':   {self.bravo},                          # tender name
            self.a1.project_id: {self.alpha},                        # site code
            'order site':     {self.home},                           # customer
        }
        for q, expected in cases.items():
            with self.subTest(q=q):
                response = _client(self.scm).get(self.url(q=q.replace(' ', '+')))
                self.assertEqual(set(self.listed(response)), expected)

    def test_project_type_filter(self):
        self.assertEqual(self.listed(_client(self.scm).get(self.url(type='Residential'))),
                         [self.home])
        self.assertEqual(self.listed(_client(self.scm).get(self.url(type='OPEX'))),
                         [self.bravo, self.alpha])

    def test_program_filter_reads_either_arm(self):
        # A central purchase that ticked Alpha and named no site is in Alpha's list; so
        # is the record naming a1, a site in Alpha.
        self.post_new(programs=[self.tender], po_number='PO-CENTRAL')
        central = VendorOrder.objects.get(po_number='PO-CENTRAL')
        response = _client(self.scm).get(self.url(program=self.tender.pk))
        self.assertEqual(self.listed(response), [central, self.alpha])
        self.assertContains(response, 'recorded against Alpha Tender')
        # The Add PO / PI button carries the tender, so it starts ticked.
        self.assertContains(response, f'href="{reverse("purchases_new")}?program='
                                      f'{self.tender.pk}"')

    def test_project_filter(self):
        response = _client(self.scm).get(self.url(project=self.project.pk))
        self.assertEqual(self.listed(response), [self.home])
        self.assertEqual(self.listed(_client(self.scm).get(self.url(project=self.b1.pk))),
                         [self.bravo])

    def test_finance_ceo_admin_and_system_admin_read_without_the_actions(self):
        for profile in (self.finance, self.ceo, self.admin, self.sysadmin):
            with self.subTest(role=profile.role):
                response = _client(profile).get(self.url())
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(self.listed(response)), 3)
                self.assertNotContains(response, 'Add PO / PI</a>')
                self.assertNotContains(response, 'Raise payment request</a>')
                self.assertNotContains(response, 'Request payment</a>')
                self.assertContains(response, 'Open</a>')

    def test_pm_design_and_site_engineer_are_refused(self):
        for profile in (self.pm, self.designer, self.se):
            with self.subTest(role=profile.role):
                self.assertEqual(_client(profile).get(self.url()).status_code, 403)

    def test_the_nav_entry_follows_the_predicate(self):
        for profile in (self.scm, self.finance, self.ceo, self.admin, self.sysadmin):
            with self.subTest(role=profile.role):
                page = _client(profile).get(reverse('notifications'))
                self.assertContains(page, reverse('purchases_workspace'))
        for profile in (self.pm, self.designer, self.se):
            with self.subTest(role=profile.role):
                page = _client(profile).get(reverse('notifications'))
                self.assertNotContains(page, reverse('purchases_workspace'))

    def test_the_list_costs_the_same_for_one_row_as_for_fifty(self):
        client = _client(self.scm)
        url = self.url(program=self.tender.pk)
        client.get(url)                                   # warm the session lookup
        with _Capture(self) as one:
            response = client.get(url)
        self.assertEqual(len(self.listed(response)), 1)

        for index in range(49):
            order = VendorOrder.objects.create(
                vendor=self.vendor, project_type='OPEX', po_number=f'PO-BULK-{index}',
                created_by=self.scm, total_amount=Decimal('1000'))
            VendorOrderSite.objects.create(order=order, project=self.a2)
            VendorOrderProgram.objects.create(order=order, program=self.tender)
            VendorOrderLine.objects.create(order=order, item_description='Modules',
                                           quantity=Decimal('1'))
            self.pay_existing(order, '100')
        with _Capture(self) as fifty:
            response = client.get(url)
        self.assertEqual(len(self.listed(response)), 50)
        print(f'\n[O8a list query count] 1 row: {one.count} queries; '
              f'50 rows: {fifty.count} queries')
        self.assertEqual(fifty.count, one.count)
