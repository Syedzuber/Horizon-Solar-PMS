"""
FIN-M-B8a — the Finance work page (/finance/work/).

WHAT THIS PINS
    Access       Finance, CEO, Admin, System Admin open it; SCM, PM, Project Coordinator,
                 BD, Site Engineer and Design get 403; anonymous goes to login; POST is 405
    Milestones   Residential only, deleted sites out (R2, R3); M1 Pending = confirm advance
                 (R4); M2/M3 only when due per R8, with "trigger N/A" / "no trigger task"
                 (ruling 4); not-yet-due counted, not listed; Draft and Cancelled sites out
                 of "to act on", every status in "invoiced" (ruling 3)
    Invoiced     oldest invoice first; due = invoice amount - received; no invoice amount
                 reads "amount not set", no fallback (ruling 5)
    Money        contract value only where user_can_see_contract_value says so (R12);
                 Indian grouping; a blank agreed amount reads "amount not set", never ₹0
    Actions      links to the project page's #payment-milestones card (Finance verbs for
                 Finance, "Open site" for readers), the payments queue, the DC page; the
                 page has no POST form; the card carries the anchor
    Payments     per-type buttons and counts, CAPEX only when the queue shows it, the 5
                 oldest open requests of the chosen type, rows link into the queue
    Receipts     by receipt date, 7 days, imported out, every type with a tag; damaged red
                 and first, partial neutral, complete plain (ruling 6); "confirmed with
                 photo"; the DC link only for roles that may read a challan
    Tiles        each equals its section; search and focus live in the URL
    Queries      the same number for 3 and 30 sites and for 0 and 10 payment requests

Run with:
    python manage.py test projects.tests_finance_work --settings=solarpms.test_settings
"""
import re
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import resolve, reverse
from django.utils import timezone

from .models import (DCLineDamagePhoto, DCLineItem, DeliveryChallan,  # noqa: F401
                     DeliveryChallanAttachment, GoodsReceipt, GoodsReceiptLine,
                     PaymentMilestone, PaymentRequest, Project, ProjectPhase, Task, Vendor,
                     VendorOrder, GOODS_RECEIPT_CORRECTION, GOODS_RECEIPT_IMPORTED,
                     GOODS_RECEIPT_RECEIPT)
from .views_finance_work import TRIGGER_TASKS, rupees

URL = '/finance/work/'
PO_BC = TRIGGER_TASKS['M2']
HANDOVER = TRIGGER_TASKS['M3']


def _profile(username, role, **flags):
    """A user whose auto-created profile carries `role` (a post_save signal creates it)."""
    user = User.objects.create_user(username=username, password='pw12345',
                                    first_name=username.title())
    profile = user.profile
    profile.role = role
    profile.is_active = True
    for name, value in flags.items():
        setattr(profile, name, value)
    profile.save()
    return profile


class FinanceWorkFixture(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.finance = _profile('fw_fin', 'Finance')
        cls.ceo = _profile('fw_ceo', 'CEO')
        cls.admin = _profile('fw_admin', 'Admin')
        cls.sysadmin = _profile('fw_sysadmin', 'System Admin')
        cls.scm = _profile('fw_scm', 'SCM')
        cls.pm = _profile('fw_pm', 'PM')
        cls.coord = _profile('fw_coord', 'Project Coordinator')
        cls.bd = _profile('fw_bd', 'BD')
        cls.se = _profile('fw_se', 'Site Engineer')
        cls.designer = _profile('fw_design', 'Design')
        cls.vendor = Vendor.objects.create(name='Surya Modules', contact_person='S',
                                           phone='9000000001')

    _seq = 0

    def setUp(self):
        self.today = timezone.localdate()

    # -- builders ----------------------------------------------------------

    def site(self, project_type='Residential', status='Active', **fields):
        FinanceWorkFixture._seq += 1
        n = FinanceWorkFixture._seq
        fields.setdefault('activated_at', timezone.now() - timedelta(days=10))
        return Project.objects.create(
            project_id=f'FW-{n:03d}', customer_name=f'Customer {n}', customer_phone='9876543210',
            site_address='1 Road', city='Delhi', project_type=project_type, status=status,
            assigned_pm=self.pm, **fields)

    def milestone(self, site, name, status=PaymentMilestone.PENDING, **fields):
        return PaymentMilestone.objects.create(project=site, milestone_name=name, status=status,
                                               **fields)

    def trigger(self, site, name, status='Done', done_days_ago=3, **fields):
        phase = ProjectPhase.objects.create(project=site, phase_name=f'P {name}', phase_order=1)
        if status == 'Done':
            fields.setdefault('completed_at', timezone.now() - timedelta(days=done_days_ago))
        return Task.objects.create(phase=phase, task_name=name, task_order=1, status=status,
                                   **fields)

    def payment(self, project_type='Residential', status=PaymentRequest.PENDING_APPROVAL,
                amount='100000', approved=None, days_ago=1):
        order = VendorOrder.objects.create(vendor=self.vendor, project_type=project_type,
                                           total_amount=Decimal('10000000'),
                                           created_by=self.scm, po_number='PO-77')
        p = PaymentRequest.objects.create(
            vendor_order=order, vendor=self.vendor, amount=Decimal(amount),
            requested_by=self.scm.user, status=status,
            approved_amount=Decimal(approved) if approved else None)
        # requested_date is auto_now_add, so the age is set after the insert.
        PaymentRequest.objects.filter(pk=p.pk).update(
            requested_date=timezone.now() - timedelta(days=days_ago))
        return p

    def receipt(self, site, *, days_ago=0, kind=GOODS_RECEIPT_RECEIPT, lines=((10, 10, 0),),
                photo=False):
        """A receipt on a new challan; `lines` = (ordered, received_after, damaged_after)."""
        dc = DeliveryChallan.objects.create(project=site, vendor=self.vendor,
                                            dc_number=f'DC-{site.project_id}-{days_ago}',
                                            dc_date=self.today, po_number='PO-9')
        receipt = GoodsReceipt.objects.create(
            challan=dc, kind=kind, receipt_date=self.today - timedelta(days=days_ago),
            recorded_by=None if kind == GOODS_RECEIPT_IMPORTED else self.se)
        for i, (ordered, received, damaged) in enumerate(lines):
            item = DCLineItem.objects.create(challan=dc, boq_category='BOS',
                                             item_description=f'Item {i}',
                                             ordered_quantity=Decimal(ordered))
            GoodsReceiptLine.objects.create(
                receipt=receipt, dc_line=item, received_after=Decimal(received),
                damaged_before=0, damaged_after=damaged, item_description=f'Item {i}',
                unit='Nos', ordered_quantity=Decimal(ordered))
        if photo:
            DeliveryChallanAttachment.objects.create(
                challan=dc, uploaded_by=self.se, original_name='p.jpg', bucket='b',
                path=f'grn/{receipt.pk}.jpg', size=10, mime='image/jpeg',
                no_prices_confirmed=True, receipt=receipt, document_type='delivery_photo')
        return receipt

    # -- reading -----------------------------------------------------------

    def get(self, profile, url=URL):
        client = Client()
        client.force_login(profile.user)
        return client.get(url)

    def ctx(self, profile=None, url=URL):
        response = self.get(profile or self.finance, url)
        self.assertEqual(response.status_code, 200)
        return response.context

    def tile(self, ctx, label):
        return next(t for t in ctx['tiles'] if t['label'] == label)


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

class AccessTests(FinanceWorkFixture):

    def test_finance_and_the_read_roles_open_the_page(self):
        for profile in (self.finance, self.ceo, self.admin, self.sysadmin):
            with self.subTest(role=profile.role):
                self.assertEqual(self.get(profile).status_code, 200)

    def test_every_other_role_is_refused(self):
        for profile in (self.scm, self.pm, self.coord, self.bd, self.se, self.designer):
            with self.subTest(role=profile.role):
                self.assertEqual(self.get(profile).status_code, 403)

    def test_anonymous_goes_to_login(self):
        response = Client().get(URL)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/login/')

    def test_post_is_refused_the_page_writes_nothing(self):
        client = Client()
        client.force_login(self.finance.user)
        self.assertEqual(client.post(URL, {}).status_code, 405)

    def test_the_url_is_the_finance_work_view(self):
        self.assertEqual(resolve(URL).url_name, 'finance_work')
        self.assertEqual(reverse('finance_work'), URL)


# ---------------------------------------------------------------------------
# Milestones to act on (R2, R3, R4, R8, rulings 3 and 4)
# ---------------------------------------------------------------------------

class MilestoneTests(FinanceWorkFixture):

    def setUp(self):
        super().setUp()
        self.s = self.site()
        self.m1 = self.milestone(self.s, 'M1')
        self.m2 = self.milestone(self.s, 'M2', amount=Decimal('150000'))
        self.m3 = self.milestone(self.s, 'M3')
        self.done = self.trigger(self.s, PO_BC, done_days_ago=4)
        self.trigger(self.s, HANDOVER, status='Not Started')

    def rows(self, ctx):
        return {(r.milestone.pk): r for r in ctx['to_act']}

    def test_m1_pending_is_confirm_advance(self):
        row = self.rows(self.ctx())[self.m1.pk]
        self.assertEqual(row.kind, 'advance')
        self.assertEqual(row.action, 'Confirm advance on the site page')
        self.assertEqual(row.since, timezone.localdate(self.s.activated_at))

    def test_m2_is_due_when_its_trigger_is_done_and_m3_is_only_counted(self):
        ctx = self.ctx()
        rows = self.rows(ctx)
        self.assertEqual(rows[self.m2.pk].kind, 'invoice')
        self.assertEqual(rows[self.m2.pk].since, timezone.localdate(self.done.completed_at))
        self.assertEqual(rows[self.m2.pk].waiting, 4)
        self.assertNotIn(self.m3.pk, rows)
        self.assertEqual(ctx['not_due'], 1)

    def test_trigger_not_applicable_counts_as_due(self):
        other = self.site()
        m3 = self.milestone(other, 'M3')
        self.trigger(other, HANDOVER, status='Not Started', is_not_applicable=True)
        row = self.rows(self.ctx())[m3.pk]
        self.assertEqual((row.kind, row.note, row.since), ('invoice', 'trigger N/A', None))
        self.assertContains(self.get(self.finance), 'trigger N/A')

    def test_missing_trigger_task_counts_as_due(self):
        other = self.site()
        m2 = self.milestone(other, 'M2')
        row = self.rows(self.ctx())[m2.pk]
        self.assertEqual((row.kind, row.note), ('invoice', 'no trigger task'))

    def test_rows_with_no_date_come_first_with_a_check_pill(self):
        na_site, bare_site = self.site(), self.site()
        na = self.milestone(na_site, 'M3')
        self.trigger(na_site, HANDOVER, status='Not Started', is_not_applicable=True)
        bare = self.milestone(bare_site, 'M2')                 # no trigger task
        order = [r.milestone.pk for r in self.ctx()['to_act']]
        self.assertEqual(set(order[:2]), {na.pk, bare.pk})
        self.assertEqual(set(order[2:]), {self.m1.pk, self.m2.pk})
        html = self.get(self.finance).content.decode()
        for m in (na, bare):
            block = html.split(f'data-milestone="{m.pk}"')[1].split('class="qdet"')[0]
            self.assertIn('data-check>check</span>', block)
        dated = html.split(f'data-milestone="{self.m2.pk}"')[1].split('class="qdet"')[0]
        self.assertNotIn('data-check', dated)

    def test_deleted_and_non_residential_milestones_are_absent(self):
        gone = [self.milestone(self.site(is_deleted=True), 'M1'),
                self.milestone(self.site(project_type='OPEX'), 'M1'),
                self.milestone(self.site(project_type='CAPEX'), 'M1')]
        listed = self.rows(self.ctx())
        for m in gone:
            self.assertNotIn(m.pk, listed)

    def test_draft_and_cancelled_sites_are_out_other_statuses_in(self):
        out = [self.milestone(self.site(status=s), 'M1') for s in ('Draft', 'Cancelled')]
        kept = [self.milestone(self.site(status=s), 'M1')
                for s in ('In Progress', 'Commissioned', 'On Hold')]
        listed = self.rows(self.ctx())
        for m in out:
            self.assertNotIn(m.pk, listed)
        for m in kept:
            self.assertIn(m.pk, listed)

    def test_received_milestones_are_done(self):
        self.m1.status = PaymentMilestone.RECEIVED
        self.m1.save()
        self.assertNotIn(self.m1.pk, self.rows(self.ctx()))

    def test_blank_agreed_amount_reads_amount_not_set_never_zero(self):
        html = self.get(self.finance).content.decode()
        m1 = html.split(f'data-milestone="{self.m1.pk}"')[1].split('data-milestone=')[0]
        self.assertIn('amount not set', m1)
        self.assertNotIn('₹0', m1)
        m2 = html.split(f'data-milestone="{self.m2.pk}"')[1].split('data-milestone=')[0]
        self.assertIn('₹1,50,000', m2)          # M2's agreed amount, Indian grouping

    def test_finance_gets_verbs_readers_get_open_site_all_link_to_the_card(self):
        anchor = reverse('project_overview', args=[self.s.project_id]) + '#payment-milestones'
        fin = self.rows(self.ctx())[self.m2.pk]
        self.assertEqual(fin.action, 'Invoice M2 on the site page')
        self.assertEqual(fin.href, anchor)
        for reader in (self.ceo, self.admin, self.sysadmin):
            with self.subTest(role=reader.role):
                row = self.rows(self.ctx(reader))[self.m2.pk]
                self.assertEqual(row.action, 'Open site')
                self.assertEqual(row.href, anchor)


# ---------------------------------------------------------------------------
# Invoiced, awaiting payment (rulings 3 and 5)
# ---------------------------------------------------------------------------

class InvoicedTests(FinanceWorkFixture):

    def setUp(self):
        super().setUp()
        self.new = self.milestone(self.site(), 'M2', status=PaymentMilestone.INVOICED,
                                  invoice_number='INV-2', invoice_amount=Decimal('250000'),
                                  invoice_date=self.today - timedelta(days=5))
        self.old = self.milestone(self.site(status='Cancelled'), 'M3',
                                  status=PaymentMilestone.INVOICED, invoice_number='INV-1',
                                  invoice_amount=Decimal('1234567'),
                                  amount_received=Decimal('234567'),
                                  invoice_date=self.today - timedelta(days=40))
        self.blank = self.milestone(self.site(), 'M2', status=PaymentMilestone.INVOICED,
                                    invoice_number='INV-3', amount=Decimal('99000'),
                                    invoice_date=self.today - timedelta(days=1))

    def test_oldest_invoice_first_every_status_due_is_invoice_less_received(self):
        rows = self.ctx()['invoiced']
        self.assertEqual([r.milestone.pk for r in rows], [self.old.pk, self.new.pk, self.blank.pk])
        self.assertEqual(rows[0].due, '₹10,00,000')
        self.assertEqual(rows[0].waiting, 40)
        self.assertEqual(rows[1].due, '₹2,50,000')

    def test_no_invoice_amount_reads_amount_not_set_with_no_fallback(self):
        row = next(r for r in self.ctx()['invoiced'] if r.milestone.pk == self.blank.pk)
        self.assertIsNone(row.due)
        self.assertIsNone(row.invoiced)
        html = self.get(self.finance).content.decode()
        block = html.split(f'data-milestone="{self.blank.pk}"')[1].split('data-due')[1][:40]
        self.assertIn('amount not set', block)

    def test_tile_counts_the_section_and_sums_in_indian_grouping(self):
        ctx = self.ctx()
        tile = self.tile(ctx, 'Invoiced, awaiting payment')
        self.assertEqual(tile['value'], len(ctx['invoiced']))
        self.assertEqual(tile['detail'], '₹12,50,000 due · 1 amount not set · 1 over 30 days')
        self.assertEqual(tile['tone'], 'crit')

    def test_deleted_site_invoice_is_absent(self):
        gone = self.milestone(self.site(is_deleted=True), 'M2', status=PaymentMilestone.INVOICED,
                              invoice_amount=Decimal('1'), invoice_date=self.today)
        self.assertNotIn(gone.pk, [r.milestone.pk for r in self.ctx()['invoiced']])

    def test_finance_records_the_receipt_on_the_card_readers_open_the_site(self):
        self.assertEqual(self.ctx()['invoiced'][0].action, 'Record receipt on the site page')
        self.assertEqual(self.ctx(self.ceo)['invoiced'][0].action, 'Open site')


# ---------------------------------------------------------------------------
# Contract value (R12)
# ---------------------------------------------------------------------------

class ContractValueTests(FinanceWorkFixture):

    def setUp(self):
        super().setUp()
        self.milestone(self.site(contract_value=Decimal('1234567')), 'M1')

    def test_every_r12_viewer_sees_it_in_indian_grouping(self):
        for profile in (self.finance, self.ceo, self.admin, self.sysadmin):
            with self.subTest(role=profile.role):
                html = self.get(profile).content.decode()
                self.assertIn('data-contract', html)
                self.assertIn('₹12,34,567', html)

    def test_the_page_asks_the_r12_helper_and_hides_it_when_refused(self):
        with mock.patch('projects.views_finance_work.user_can_see_contract_value',
                        return_value=False):
            html = self.get(self.finance).content.decode()
        self.assertNotIn('data-contract', html)
        self.assertNotIn('₹12,34,567', html)


# ---------------------------------------------------------------------------
# Payment requests (ruling 8)
# ---------------------------------------------------------------------------

class PaymentTests(FinanceWorkFixture):

    def test_buttons_per_type_capex_hidden_until_the_queue_shows_it(self):
        ctx = self.ctx()
        self.assertEqual([b['label'] for b in ctx['payments']['buttons']], ['Residential', 'RESCO'])
        self.payment('CAPEX')
        ctx = self.ctx()
        self.assertEqual([b['label'] for b in ctx['payments']['buttons']],
                         ['Residential', 'RESCO', 'CAPEX'])

    def test_five_oldest_open_of_the_chosen_type_linking_into_the_queue(self):
        made = [self.payment(days_ago=d) for d in range(7, 0, -1)]   # oldest first
        self.payment(status=PaymentRequest.CONFIRMED, approved='100000', days_ago=30)
        resco = self.payment('OPEX', status=PaymentRequest.APPROVED, approved='60000')
        ctx = self.ctx()
        pay = ctx['payments']
        self.assertEqual([r.payment.pk for r in pay['rows']], [p.pk for p in made[:5]])
        self.assertEqual(pay['awaiting'], 7)
        self.assertEqual(pay['more'], 2)
        self.assertEqual(pay['rows'][0].href,
                         reverse('payment_queue') + '?tab=Residential&status=pending_approval')
        resco_ctx = self.ctx(url=URL + '?ptype=OPEX')['payments']
        self.assertEqual([r.payment.pk for r in resco_ctx['rows']], [resco.pk])
        self.assertEqual(resco_ctx['rows'][0].amount, '₹60,000')
        self.assertEqual(resco_ctx['rows'][0].requested, '₹1,00,000')

    def test_tiles_count_every_type(self):
        self.payment()
        self.payment('OPEX', status=PaymentRequest.APPROVED, approved='250000', amount='250000')
        ctx = self.ctx()
        self.assertEqual(self.tile(ctx, 'Payments awaiting approval')['value'], 1)
        approved = self.tile(ctx, 'Approved, to pay')
        self.assertEqual((approved['value'], approved['detail']), (1, 'All types · ₹2,50,000'))
        self.assertTrue(self.tile(ctx, 'Payments awaiting approval')['detail']
                        .startswith('All types · '))

    def test_an_unknown_type_falls_back_to_residential(self):
        self.assertEqual(self.ctx(url=URL + '?ptype=CAPEX')['payments']['ptype'], 'Residential')


# ---------------------------------------------------------------------------
# Goods received (rulings 6 and 7, R14)
# ---------------------------------------------------------------------------

class ReceiptTests(FinanceWorkFixture):

    def setUp(self):
        super().setUp()
        self.res = self.site()
        self.resco = self.site(project_type='OPEX')
        self.complete = self.receipt(self.res, days_ago=0, photo=True)
        self.partial = self.receipt(self.resco, days_ago=2, lines=((10, 6, 0), (5, 5, 0)))
        self.damaged = self.receipt(self.res, days_ago=6, kind=GOODS_RECEIPT_CORRECTION,
                                    lines=((10, 10, 2),))
        self.too_old = self.receipt(self.res, days_ago=7)
        self.imported = self.receipt(self.res, days_ago=1, kind=GOODS_RECEIPT_IMPORTED)
        self.deleted = self.receipt(self.site(is_deleted=True), days_ago=1)

    def test_window_kinds_and_order(self):
        rows = self.ctx()['receipts']
        self.assertEqual([r.receipt.pk for r in rows],
                         [self.damaged.pk, self.complete.pk, self.partial.pk])
        self.assertEqual([r.state for r in rows], ['damaged', 'complete', 'partial'])

    def test_pills_damaged_red_partial_neutral_complete_plain_never_short(self):
        html = self.get(self.finance).content.decode()
        def block(receipt):
            return html.split(f'data-receipt="{receipt.number}"')[1].split('class="qdet"')[0]
        self.assertIn('pill crit">Damaged', block(self.damaged))
        self.assertIn('pill mut">Partial', block(self.partial))
        self.assertNotIn('Damaged', block(self.complete))
        self.assertNotIn('Partial', block(self.complete))
        self.assertNotIn('hort', html.split('id="sec-receipts"')[1])

    def test_type_tag_photo_and_recorder(self):
        rows = {r.receipt.pk: r for r in self.ctx()['receipts']}
        self.assertEqual(rows[self.partial.pk].type_label, 'RESCO')
        self.assertEqual(rows[self.complete.pk].type_label, 'Residential')
        self.assertTrue(rows[self.complete.pk].with_photo)
        self.assertFalse(rows[self.partial.pk].with_photo)
        self.assertFalse(rows[self.damaged.pk].with_photo)     # an SCM correction never is
        self.assertEqual(rows[self.damaged.pk].who, 'SCM correction')

    def test_dc_link_only_for_roles_that_read_challans(self):
        href = (reverse('delivery_challan_detail',
                        args=[self.res.project_id, self.complete.challan_id])
                + '#' + self.complete.number.lower())
        for profile in (self.finance, self.admin):
            with self.subTest(role=profile.role):
                row = next(r for r in self.ctx(profile)['receipts'] if r.receipt.pk == self.complete.pk)
                self.assertEqual(row.href, href)
        for profile in (self.ceo, self.sysadmin):
            with self.subTest(role=profile.role):
                row = next(r for r in self.ctx(profile)['receipts'] if r.receipt.pk == self.complete.pk)
                self.assertIsNone(row.href)

    def test_tile_counts_damaged_and_partial(self):
        tile = self.tile(self.ctx(), 'Receipts with damage')
        self.assertEqual((tile['value'], tile['detail']), (1, 'last 7 days · 1 partial'))


# ---------------------------------------------------------------------------
# Tiles, search, focus, links
# ---------------------------------------------------------------------------

class PageTests(FinanceWorkFixture):

    def setUp(self):
        super().setUp()
        self.a = self.site()
        self.b = self.site()
        self.milestone(self.a, 'M1')
        self.milestone(self.b, 'M1')
        self.milestone(self.b, 'M2')                           # no trigger task: due
        self.receipt(self.a)

    def test_each_tile_equals_its_section(self):
        ctx = self.ctx()
        self.assertEqual(self.tile(ctx, 'M1 advances to confirm')['value'],
                         sum(1 for r in ctx['to_act'] if r.kind == 'advance'))
        self.assertEqual(self.tile(ctx, 'M2/M3 due to invoice')['value'],
                         sum(1 for r in ctx['to_act'] if r.kind == 'invoice'))
        self.assertEqual(self.tile(ctx, 'M2/M3 due to invoice')['detail'], '0 more not yet due')

    def test_search_cuts_sections_and_tiles_together(self):
        ctx = self.ctx(url=URL + '?q=' + self.b.project_id)
        self.assertEqual({r.project.pk for r in ctx['to_act']}, {self.b.pk})
        self.assertEqual(self.tile(ctx, 'M1 advances to confirm')['value'], 1)
        self.assertEqual(ctx['receipts'], [])
        self.assertIn('q=' + self.b.project_id, ctx['payment_search_href'])

    def test_focus_shows_one_section_and_the_tile_toggles_it_off(self):
        ctx = self.ctx(url=URL + '?focus=receipts')
        self.assertEqual(ctx['show'], {'milestones': False, 'invoiced': False,
                                       'payments': False, 'receipts': True})
        self.assertEqual(self.tile(ctx, 'Receipts with damage')['href'], '?')
        html = self.get(self.finance, URL + '?focus=receipts').content.decode()
        self.assertNotIn('id="sec-milestones"', html)
        self.assertIn('id="sec-receipts"', html)

    def test_no_write_form_and_every_link_is_an_existing_page(self):
        html = self.get(self.finance).content.decode()
        # This page's own markup only: base.html carries the site-wide "Block task" modal
        # (a POST form) on every page, which is not this page's.
        page = html.split('<div class="sd">', 1)[1].split('// Draw the Lucide icons once')[0]
        self.assertNotIn('method="post"', page.lower())
        self.assertIn('method="get"', page.lower())
        for href in set(re.findall(r'href="(/[^"?#]*)', page)):
            with self.subTest(href=href):
                self.assertIn(resolve(href).url_name,
                              {'project_overview', 'payment_queue', 'delivery_challan_detail',
                               'site_dashboard'})

    def test_the_project_page_card_carries_the_anchor(self):
        response = self.get(self.finance, reverse('project_overview', args=[self.a.project_id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="payment-milestones"', count=1)

    def test_rupees_groups_in_lakhs_and_keeps_a_sign(self):
        self.assertEqual(rupees(Decimal('12345678.40')), '₹1,23,45,678')
        self.assertEqual(rupees(Decimal('-50000')), '−₹50,000')
        self.assertIsNone(rupees(None))


# ---------------------------------------------------------------------------
# Query count
# ---------------------------------------------------------------------------

class QueryCountTests(FinanceWorkFixture):
    """The page's queries do not grow with sites, milestones, payments or receipts."""

    def add_sites(self, n):
        for _ in range(n):
            s = self.site()
            self.milestone(s, 'M1')
            self.milestone(s, 'M2')
            self.trigger(s, PO_BC)
            self.milestone(s, 'M3', status=PaymentMilestone.INVOICED,
                           invoice_amount=Decimal('1000'), invoice_date=self.today)
            self.receipt(s, lines=((5, 4, 0), (3, 3, 1)), photo=True)

    def count(self):
        client = Client()
        client.force_login(self.finance.user)
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(URL)
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries), response.context

    def test_three_and_thirty_sites_zero_and_ten_payments_cost_the_same(self):
        self.add_sites(3)
        small, ctx = self.count()
        self.assertEqual(len(ctx['receipts']), 3)
        self.assertEqual(len(ctx['invoiced']), 3)

        self.add_sites(27)
        large, ctx = self.count()
        self.assertEqual(len(ctx['receipts']), 30)
        self.assertEqual(len(ctx['to_act']), 60)

        for i in range(10):
            self.payment('OPEX' if i % 2 else 'Residential', days_ago=i)
        with_payments, ctx = self.count()
        self.assertEqual(len(ctx['payments']['rows']), 5)

        print(f'\nFIN-M-B8a queries: 3 sites/0 payments={small}, 30 sites/0 payments={large}, '
              f'30 sites/10 payments={with_payments}')
        self.assertEqual(small, large)
        self.assertEqual(large, with_payments)
