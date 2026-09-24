"""O3 / O3r — the group raise: a payment request against a PO / PI, optionally recorded
against sites from several groups and several tenders.

What this file pins, and why each matters:

  * EVERY SUBMISSION RAISES A PAYMENT (O3r). The order total from the PO and the payment
    wanted now are both required; a payment above the total is refused.
  * THE ORDER'S VALUE IS THE PO FIGURE, NOT THE LINES. Lines are a read-only snapshot of
    the added sites' requirement, quantities worked out on the server, amounts optional.
    Item amounts that do not add up to the total save, with a warning.
  * NOT LIMITED TO ONE GROUP OR ONE TENDER. Sites from two locked groups in one tender,
    and from two tenders at once, make ONE order — with a VendorOrderProgram row per
    tender and the order appearing in each tender's list.
  * ZERO SITES AND ZERO LINES IS A FIRST-CLASS CASE (O2d). It names its tenders, anchors
    its payment to NULL, and still reaches its tender's list.
  * WHAT IS REFUSED, AND NOTHING IS CREATED WHEN IT IS: no payment, no total, a payment
    above the total, a bad item amount, a mixture of project types, every role but SCM,
    and a site the caller cannot see.
  * THE UNFROZEN FLAG IS A FACT ABOUT THE MOMENT OF RAISING, recorded as before and shown
    on the record page only.
  * THE PAGE DOES NOT GET SLOWER AS THE TENDER FILLS UP. Its query count is measured at 3
    sites and at 30 and must be identical.
  * MIGRATION 0097 fills total_amount from the lines, and refuses an order with none.

Run with:
    python manage.py test projects.tests_vendor_order_group --settings=solarpms.test_settings
"""
import json
import re
import uuid
from datetime import timedelta
from decimal import Decimal
from importlib import import_module
from unittest.mock import MagicMock, patch

from django.apps import apps as django_apps
from django.contrib.messages import WARNING
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from .design_views import aggregate_group_boq
from .models import (
    BOQ, BOQItem, BOQItemMaster, DesignAssignment, PaymentRequest, Program, Project,
    SiteGroup, SiteGroupMembership, VendorOrder, VendorOrderDocument, VendorOrderLine,
    VendorOrderProgram, VendorOrderSite, DESIGN_RELEASED, GROUP_TYPE_PROCUREMENT,
    SITE_GROUP_DRAFT, SITE_GROUP_LOCKED, VENDOR_ORDER_DOC_PO,
)
from .order_views import _parse_group_sites
from .tests_vendor_order_raise import RaiseFixture, _client, _pdf, _profile


class GroupRaiseFixture(RaiseFixture):
    """Two OPEX tenders, four procurement groups, an ungrouped site and a catalogue.

    Built on RaiseFixture so the Residential site, its BOQ and the six role profiles come
    for free — and so a mixture of Residential and OPEX sites is something this file can
    actually post.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.designer = _profile('o3_designer', 'Design')

        cls.opex_module = BOQItemMaster.objects.create(
            code='OPX-901', description='Module 550Wp', unit='Nos',
            category='Modules', project_type='OPEX', sort_order=1)
        cls.opex_inverter = BOQItemMaster.objects.create(
            code='OPX-902', description='String Inverter 100kW', unit='Nos',
            category='Inverter', project_type='OPEX', sort_order=2)

        cls.tender = Program.objects.create(
            program_type='OPEX', name='Alpha Tender', client_name='Alpha',
            short_tender_code='ALPHA', status='Active')
        cls.tender_b = Program.objects.create(
            program_type='OPEX', name='Bravo Tender', client_name='Bravo',
            short_tender_code='BRAVO', status='Active')

        cls.locked_a = cls.group('Alpha batch 1', cls.tender, SITE_GROUP_LOCKED)
        cls.locked_b = cls.group('Alpha batch 2', cls.tender, SITE_GROUP_LOCKED)
        cls.draft_a  = cls.group('Alpha draft',   cls.tender, SITE_GROUP_DRAFT)
        cls.locked_c = cls.group('Bravo batch 1', cls.tender_b, SITE_GROUP_LOCKED)

        cls.a1 = cls.site('A1', cls.tender, modules=100, inverters=2, group=cls.locked_a)
        cls.a2 = cls.site('A2', cls.tender, modules=60,  inverters=1, group=cls.locked_a)
        cls.a3 = cls.site('A3', cls.tender, modules=40,  inverters=1, group=cls.locked_b)
        cls.a4 = cls.site('A4', cls.tender, modules=25,  inverters=1, group=cls.draft_a)
        # In no group at all.
        cls.a5 = cls.site('A5', cls.tender, modules=10,  inverters=1, group=None)
        cls.b1 = cls.site('B1', cls.tender_b, modules=80, inverters=2, group=cls.locked_c)

    # ── fixture builders ───────────────────────────────────────────────────────

    @classmethod
    def group(cls, name, program, status):
        return SiteGroup.objects.create(
            program=program, name=name, status=status, created_by=cls.scm,
            group_type=GROUP_TYPE_PROCUREMENT)

    @classmethod
    def site(cls, code, program, modules, inverters, group, unlinked=None):
        project = Project(
            project_id=f'{program.short_tender_code}-{code}', customer_name=program.client_name,
            customer_phone='9876543210', site_address='1 Sun Road', city='Delhi',
            state='Delhi', project_type='OPEX', program=program, site_code=code,
            dc_capacity_kw=Decimal('100.00'), status='Draft')
        project.save()
        DesignAssignment.objects.create(
            project=project, assigned_to=cls.designer, status=DESIGN_RELEASED,
            released_at=timezone.now() - timedelta(days=3))
        boq = BOQ.objects.create(project=project)
        BOQItem.objects.create(
            boq=boq, serial_no=1, category='Modules', description='Module 550Wp',
            uom='Nos', item_master=cls.opex_module, boq_quantity=Decimal(modules))
        BOQItem.objects.create(
            boq=boq, serial_no=2, category='Inverter', description='String Inverter 100kW',
            uom='Nos', item_master=cls.opex_inverter, boq_quantity=Decimal(inverters))
        if unlinked:
            BOQItem.objects.create(
                boq=boq, serial_no=3, category='Other', description=unlinked,
                uom='Lot', item_master=None, boq_quantity=Decimal('1'))
        if group is not None:
            SiteGroupMembership.objects.create(
                group=group, project=project, added_by=cls.scm)
        return project

    # ── posting ────────────────────────────────────────────────────────────────

    def url(self, program=None):
        base = reverse('vendor_order_create_group')
        return f'{base}?program={program.pk}' if program else base

    def payload(self, sites=(), programs=(), amounts=None, **overrides):
        """A complete, valid submission: a PO document, an order total of ₹10,00,000 and
        a payment of ₹2,00,000. `sites` is an iterable of Project or of (Project,
        SiteGroup); `amounts` maps BOQItemMaster -> item amount. A None override drops
        the key."""
        data = {
            'client_uuid':    str(uuid.uuid4()),
            'vendor_id':      str(self.vendor.pk),
            'po_number':      'PO-GROUP-1',
            'order_total':    '1000000',
            'payment_amount': '200000',
            'site':           [],
            'program':        [str(program.pk) for program in programs],
            'doc_type_0':     VENDOR_ORDER_DOC_PO,
            'doc_file_0':     _pdf('po.pdf'),
        }
        for entry in sites:
            project, group = entry if isinstance(entry, tuple) else (entry, None)
            data['site'].append(str(project.pk))
            if group is not None:
                data[f'via_group_{project.pk}'] = str(group.pk)
        for master, amount in (amounts or {}).items():
            data[f'amount_{master.pk}'] = str(amount)
        data.update(overrides)
        return {k: v for k, v in data.items() if v is not None}

    def post(self, profile=None, storage=None, follow=False, **payload_kwargs):
        storage = storage or MagicMock()
        with patch('projects.supabase_storage.get_supabase_client', return_value=storage):
            response = _client(profile or self.scm).post(
                reverse('vendor_order_create_group'), self.payload(**payload_kwargs),
                follow=follow)
        return response, storage

    def assertNothingCreated(self):
        self.assertEqual(VendorOrder.objects.count(), 0)
        self.assertEqual(VendorOrderSite.objects.count(), 0)
        self.assertEqual(VendorOrderProgram.objects.count(), 0)
        self.assertEqual(VendorOrderLine.objects.count(), 0)
        self.assertEqual(VendorOrderDocument.objects.count(), 0)
        self.assertEqual(PaymentRequest.objects.count(), 0)


# ---------------------------------------------------------------------------
# The payment requests this page exists to raise
# ---------------------------------------------------------------------------

class GroupRaiseTests(GroupRaiseFixture):

    def test_three_sites_across_two_locked_groups_in_one_tender(self):
        ids = [self.a1.pk, self.a2.pk, self.a3.pk]
        response, _ = self.post(
            sites=[(self.a1, self.locked_a), (self.a2, self.locked_a),
                   (self.a3, self.locked_b)],
            programs=[self.tender], order_total='2800000', payment_amount='500000')
        order = VendorOrder.objects.get()
        self.assertRedirects(response, reverse('vendor_order_detail', args=[order.pk]),
                             fetch_redirect_response=False)

        self.assertEqual(order.project_type, 'OPEX')
        self.assertEqual(order.created_by, self.scm)
        self.assertEqual({site.project_id for site in order.sites.all()}, set(ids))
        self.assertEqual(order.programs.get().program, self.tender)

        # THE VALUE IS THE PO FIGURE, and every money rule follows it.
        self.assertEqual(order.total_amount, Decimal('2800000'))
        self.assertEqual(order.total, Decimal('2800000'))
        self.assertEqual(order.available_to_request, Decimal('2300000'))

        # THE LINES ARE THE REQUIREMENT: one per catalogue item, summed on the server,
        # unpriced because no amount was given.
        lines = {line.item_master_id: line for line in order.lines.all()}
        self.assertEqual(set(lines), {self.opex_module.pk, self.opex_inverter.pk})
        self.assertEqual(lines[self.opex_module.pk].quantity, Decimal('200'))   # 100+60+40
        self.assertEqual(lines[self.opex_inverter.pk].quantity, Decimal('4'))   # 2+1+1
        self.assertIsNone(lines[self.opex_module.pk].amount)
        self.assertEqual(lines[self.opex_module.pk].item_code, 'OPX-901')
        self.assertEqual(lines[self.opex_module.pk].item_category, 'Modules')
        # A GROUP LINE HAS NO SINGLE BOQ ROW — it is the sum of three of them.
        self.assertIsNone(lines[self.opex_module.pk].boq_item)

        self.assertFalse(order.raised_with_unfrozen_quantities)
        by_project = {site.project_id: site for site in order.sites.all()}
        self.assertEqual(by_project[self.a1.pk].via_site_group, self.locked_a)
        self.assertEqual(by_project[self.a3.pk].via_site_group, self.locked_b)

    def test_every_submission_raises_its_payment(self):
        self.post(sites=[(self.a1, self.locked_a)], programs=[self.tender],
                  payment_amount='150000', payment_note='advance')
        payment = PaymentRequest.objects.get()
        self.assertEqual(payment.vendor_order, VendorOrder.objects.get())
        self.assertEqual(payment.amount, Decimal('150000'))
        self.assertEqual(payment.note, 'advance')
        self.assertEqual(payment.status, PaymentRequest.PENDING_APPROVAL)
        self.assertEqual(payment.project, self.a1)

    def test_sites_across_two_tenders_make_one_order_listed_in_both(self):
        self.post(sites=[(self.a1, self.locked_a), (self.b1, self.locked_c)],
                  programs=[self.tender, self.tender_b])
        order = VendorOrder.objects.get()
        self.assertEqual(order.sites.count(), 2)
        self.assertEqual({link.program_id for link in order.programs.all()},
                         {self.tender.pk, self.tender_b.pk})

        for program in (self.tender, self.tender_b):
            with self.subTest(tender=program.name):
                response = _client(self.scm).get(
                    reverse('program_vendor_order_list', args=[program.pk]))
                self.assertEqual(response.status_code, 200)
                self.assertEqual([row['order'] for row in response.context['rows']],
                                 [order])
                self.assertEqual(response.context['rows'][0]['total'],
                                 Decimal('1000000'))
                self.assertContains(response, 'spans 2 tenders')

    def test_an_order_with_zero_sites_and_zero_lines_saves(self):
        response, _ = self.post(sites=[], programs=[self.tender])
        order = VendorOrder.objects.get()
        self.assertRedirects(response, reverse('vendor_order_detail', args=[order.pk]),
                             fetch_redirect_response=False)
        self.assertEqual(order.sites.count(), 0)
        self.assertEqual(order.lines.count(), 0)
        self.assertEqual(order.project_type, 'OPEX')
        self.assertEqual(order.total, Decimal('1000000'))
        self.assertEqual(order.programs.get().program, self.tender)
        # THE ANCHOR IS NULL — there is no site to file the payment under (O2d).
        payment = PaymentRequest.objects.get()
        self.assertIsNone(payment.project)
        self.assertEqual(payment.vendor_order, order)

        listing = _client(self.scm).get(
            reverse('program_vendor_order_list', args=[self.tender.pk]))
        self.assertEqual([row['order'] for row in listing.context['rows']], [order])
        # And its record page draws with no lines.
        detail = _client(self.scm).get(reverse('vendor_order_detail', args=[order.pk]))
        self.assertContains(detail, 'No lines recorded.')

    def test_a_site_from_a_draft_group_raises_the_unfrozen_flag(self):
        self.post(sites=[(self.a1, self.locked_a), (self.a4, self.draft_a)],
                  programs=[self.tender])
        self.assertTrue(VendorOrder.objects.get().raised_with_unfrozen_quantities)

    def test_a_site_in_no_group_raises_the_unfrozen_flag_and_records_no_group(self):
        self.post(sites=[self.a5], programs=[self.tender])
        order = VendorOrder.objects.get()
        self.assertTrue(order.raised_with_unfrozen_quantities)
        self.assertIsNone(order.sites.get().via_site_group)

    def test_the_payment_anchors_to_the_lowest_project_id(self):
        self.post(sites=[(self.a3, self.locked_b), (self.a1, self.locked_a)],
                  programs=[self.tender])
        self.assertEqual(PaymentRequest.objects.get().project,
                         min((self.a1, self.a3), key=lambda p: p.pk))

    def test_requirement_quantities_come_from_the_sites_not_the_post(self):
        """The requirement is read-only on the page and its quantities are not posted —
        one that is, is ignored."""
        self.post(sites=[(self.a1, self.locked_a), (self.a2, self.locked_a)],
                  programs=[self.tender], **{f'qty_{self.opex_module.pk}': '999'})
        line = VendorOrderLine.objects.get(item_master=self.opex_module)
        self.assertEqual(line.quantity, Decimal('160'))

    def test_item_amounts_are_optional_and_stored_where_given(self):
        self.post(sites=[(self.a1, self.locked_a)], programs=[self.tender],
                  order_total='1000000', amounts={self.opex_module: '1000000'})
        lines = {line.item_master_id: line.amount for line in VendorOrderLine.objects.all()}
        self.assertEqual(lines, {self.opex_module.pk: Decimal('1000000'),
                                 self.opex_inverter.pk: None})

    def test_item_amounts_not_summing_to_the_total_saves_with_the_warning(self):
        response, _ = self.post(
            sites=[(self.a1, self.locked_a)], programs=[self.tender],
            order_total='1000000',
            amounts={self.opex_module: '700000', self.opex_inverter: '200000'},
            follow=True)
        order = VendorOrder.objects.get()
        self.assertEqual(order.total, Decimal('1000000'))     # the PO figure, unmoved
        self.assertEqual(sorted(line.amount for line in order.lines.all()),
                         [Decimal('200000'), Decimal('700000')])
        warnings = [str(m) for m in response.context['messages'] if m.level == WARNING]
        self.assertEqual(len(warnings), 1)
        self.assertIn('do not add up to the order total', warnings[0])

    def test_item_amounts_that_add_up_give_no_warning(self):
        response, _ = self.post(
            sites=[(self.a1, self.locked_a)], programs=[self.tender],
            order_total='1000000',
            amounts={self.opex_module: '800000', self.opex_inverter: '200000'},
            follow=True)
        self.assertEqual(VendorOrder.objects.count(), 1)
        self.assertFalse([m for m in response.context['messages'] if m.level == WARNING])

    def test_an_amount_for_an_item_outside_the_requirement_is_ignored(self):
        cable = BOQItemMaster.objects.create(code='OPX-903', description='DC Cable',
                                             unit='Mtr', project_type='OPEX')
        self.post(sites=[(self.a1, self.locked_a)], programs=[self.tender],
                  amounts={cable: '5000'})
        self.assertFalse(VendorOrderLine.objects.filter(item_master=cable).exists())
        self.assertEqual(VendorOrderLine.objects.count(), 2)

    def test_the_same_site_posted_twice_makes_one_site_row(self):
        payload = self.payload(sites=[(self.a1, self.locked_a)], programs=[self.tender])
        payload['site'] = [str(self.a1.pk), str(self.a1.pk)]
        with patch('projects.supabase_storage.get_supabase_client',
                   return_value=MagicMock()):
            _client(self.scm).post(reverse('vendor_order_create_group'), payload)
        order = VendorOrder.objects.get()
        self.assertEqual(order.sites.get().project, self.a1)

    def test_the_same_client_uuid_twice_creates_one_order(self):
        key = str(uuid.uuid4())
        first, _ = self.post(sites=[(self.a1, self.locked_a)], programs=[self.tender],
                             client_uuid=key)
        second, storage = self.post(sites=[(self.a1, self.locked_a)],
                                    programs=[self.tender], client_uuid=key)
        self.assertEqual(VendorOrder.objects.count(), 1)
        self.assertEqual(PaymentRequest.objects.count(), 1)
        self.assertEqual(second['Location'], first['Location'])
        storage.storage.from_.return_value.upload.assert_not_called()

    def test_the_feed_records_the_order_on_every_site_and_the_payment_once(self):
        from .models import ActivityLog
        self.post(sites=[(self.a1, self.locked_a), (self.a2, self.locked_a)],
                  programs=[self.tender])
        logs = ActivityLog.objects.filter(action_code='vendor_order_raised')
        self.assertEqual({log.project_id for log in logs}, {self.a1.pk, self.a2.pk})
        self.assertEqual(
            ActivityLog.objects.filter(action_code='payment_request_raised').count(), 1)


# ---------------------------------------------------------------------------
# Refusals — and nothing is created by any of them
# ---------------------------------------------------------------------------

class GroupRefusalTests(GroupRaiseFixture):

    def test_submitting_without_a_payment_amount_is_refused(self):
        for blank in (None, '', '0'):
            with self.subTest(payment_amount=blank):
                response, storage = self.post(sites=[(self.a1, self.locked_a)],
                                              programs=[self.tender], payment_amount=blank)
                self.assertEqual(response.status_code, 400)
                self.assertContains(response, 'Enter the payment requested now',
                                    status_code=400)
                self.assertNothingCreated()
                storage.storage.from_.return_value.upload.assert_not_called()

    def test_the_order_total_is_required(self):
        for blank in (None, '', '0', 'abc'):
            with self.subTest(order_total=blank):
                response, _ = self.post(sites=[(self.a1, self.locked_a)],
                                        programs=[self.tender], order_total=blank)
                self.assertEqual(response.status_code, 400)
                self.assertContains(response, 'Enter the order total from the PO',
                                    status_code=400)
                self.assertNothingCreated()

    def test_a_payment_above_the_order_total_is_refused(self):
        response, _ = self.post(sites=[(self.a1, self.locked_a)], programs=[self.tender],
                                order_total='100000', payment_amount='100000.01')
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'is more than the order total', status_code=400)
        self.assertNothingCreated()

    def test_a_payment_equal_to_the_order_total_is_accepted(self):
        self.post(sites=[(self.a1, self.locked_a)], programs=[self.tender],
                  order_total='100000', payment_amount='100000')
        self.assertEqual(PaymentRequest.objects.get().amount, Decimal('100000'))

    def test_a_bad_item_amount_is_refused(self):
        for bad in ('0', '-5', 'lots'):
            with self.subTest(amount=bad):
                response, _ = self.post(sites=[(self.a1, self.locked_a)],
                                        programs=[self.tender],
                                        amounts={self.opex_module: bad})
                self.assertEqual(response.status_code, 400)
                self.assertContains(response, 'an item amount must be more than 0',
                                    status_code=400)
                self.assertNothingCreated()

    def test_mixing_residential_and_opex_sites_is_refused(self):
        response, storage = self.post(
            sites=[(self.a1, self.locked_a), self.project], programs=[self.tender])
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'same kind of project', status_code=400)
        self.assertNothingCreated()
        storage.storage.from_.return_value.upload.assert_not_called()

    def test_every_role_but_scm_is_refused(self):
        for profile in (self.finance, self.pm, self.designer, self.admin):
            with self.subTest(role=profile.role):
                response, storage = self.post(
                    profile=profile, sites=[(self.a1, self.locked_a)],
                    programs=[self.tender])
                self.assertEqual(response.status_code, 403)
                self.assertNothingCreated()
                storage.storage.from_.return_value.upload.assert_not_called()

    def test_every_role_but_scm_is_refused_the_page_itself(self):
        for profile in (self.finance, self.pm, self.designer, self.admin):
            with self.subTest(role=profile.role):
                self.assertEqual(
                    _client(profile).get(reverse('vendor_order_create_group')).status_code,
                    403)

    def test_a_deleted_site_is_refused(self):
        Project.objects.filter(pk=self.a2.pk).update(is_deleted=True)
        response, _ = self.post(
            sites=[(self.a1, self.locked_a), (self.a2, self.locked_a)],
            programs=[self.tender])
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'no longer exists', status_code=400)
        self.assertNothingCreated()

    def test_a_site_the_user_cannot_see_is_refused(self):
        """THE VISIBILITY TERM, asked directly.

        It cannot be reached end-to-end today: the only role the page admits is SCM, and
        user_can_view_project() makes SCM portfolio-wide, so no site is invisible to a
        caller who got past user_can_raise_group_order(). The term is there for the
        narrowing D-4 will bring, and this test is what keeps it honest until then.
        """
        request = RequestFactory().post(
            reverse('vendor_order_create_group'), {'site': [str(self.a1.pk)]})
        request.user = self.pm_b.user           # assigned to neither site nor tender
        errors = []
        sites, programs, project_type, unfrozen = _parse_group_sites(request, errors)
        self.assertEqual(sites, [])
        self.assertIn('A chosen site is not one you can see.', errors)

    def test_a_refused_submission_gives_every_entry_back(self):
        response, _ = self.post(
            sites=[(self.a1, self.locked_a)], programs=[self.tender],
            amounts={self.opex_module: '777000'}, order_total='1234567',
            payment_amount='2000000')                # refused: above the total
        self.assertEqual(response.status_code, 400)
        html = response.content.decode()
        self.assertIn('value="1234567"', html)
        self.assertIn('value="777000"', html)
        # The added site comes back ADDED: its hidden input enabled, its group claim too.
        self.assertIn(f'name="site" value="{self.a1.pk}" class="js-vo-site-input">', html)
        self.assertIn(f'name="via_group_{self.a1.pk}" value="{self.locked_a.pk}" '
                      'class="js-vo-via">', html)
        # A site that was not added comes back disabled.
        self.assertIn(f'name="site" value="{self.a2.pk}" class="js-vo-site-input" disabled>',
                      html)
        # An amount came back, so the requirement is drawn open.
        self.assertRegex(html, r'<details class="card[^"]*" id="voRequirement" open>')
        self.assertNothingCreated()


# ---------------------------------------------------------------------------
# The page itself
# ---------------------------------------------------------------------------

class GroupRaisePageTests(GroupRaiseFixture):

    def test_the_page_is_a_payment_request_in_four_sections_in_order(self):
        response = _client(self.scm).get(self.url(self.tender))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('<h4 class="mb-0">Raise Payment Request</h4>', html)
        self.assertIn('Submit payment request</button>', html)
        positions = [html.index(marker) for marker in
                     ('1 · PO / PI', '2 · Amounts', '3 · Recorded against',
                      '4 · Requirement')]
        self.assertEqual(positions, sorted(positions))
        # Both amounts are required inputs.
        self.assertRegex(html, r'name="order_total"[^>]*required')
        self.assertRegex(html, r'name="payment_amount"[^>]*required')
        # Requirement collapsed by default; no line table to build, no quantity posted.
        self.assertRegex(html, r'<details class="card[^"]*" id="voRequirement">')
        self.assertNotIn('name="qty_', html)
        self.assertNotIn('name="line"', html)

    def test_the_page_offers_every_group_and_every_tender(self):
        response = _client(self.scm).get(self.url(self.tender))
        for group in (self.locked_a, self.locked_b, self.draft_a, self.locked_c):
            with self.subTest(group=group.name):
                self.assertContains(response, f'value="g{group.pk}"')
        self.assertContains(response, f'name="program" autocomplete="off"\n'
                                      f'                 value="{self.tender_b.pk}"')

    def test_the_entering_tender_is_pre_ticked(self):
        response = _client(self.scm).get(self.url(self.tender))
        ticked = [t['program'] for t in response.context['tenders'] if t['ticked']]
        self.assertEqual(ticked, [self.tender])
        self.assertEqual(response.context['entering_program'], self.tender)

    def test_no_entering_tender_pre_ticks_nothing(self):
        response = _client(self.scm).get(reverse('vendor_order_create_group'))
        self.assertFalse([t for t in response.context['tenders'] if t['ticked']])
        self.assertIsNone(response.context['entering_program'])

    def test_the_page_starts_with_no_sites_added(self):
        response = _client(self.scm).get(self.url(self.tender))
        html = response.content.decode()
        self.assertEqual(html.count('class="js-vo-site-input">'), 0)
        self.assertEqual(html.count('class="js-vo-site-input" disabled>'), 6)

    def test_ticking_a_group_adds_its_sites_and_its_tender(self):
        """RENDER TEST — the wiring the browser acts on. Ticking a group runs
        setAdded(member, true, true) for every pk in its data-members and ticks the tender
        named by its data-program; this pins that the markup carries the right members,
        the right tender, and a chip and a tender box for each to act on. The behaviour
        itself is driven in a real browser at verification time."""
        html = _client(self.scm).get(self.url()).content.decode()

        for group, members, tender in (
                (self.locked_a, {self.a1, self.a2}, self.tender),
                (self.locked_c, {self.b1}, self.tender_b)):
            with self.subTest(group=group.name):
                box = re.search(r'<input class="form-check-input js-vo-group"[^>]*'
                                rf'value="g{group.pk}"[^>]*>', html, re.S).group(0)
                self.assertIn(f'data-program="{tender.pk}"', box)
                posted = json.loads(re.search(r'data-members="([^"]*)"', box).group(1)
                                    .replace('&quot;', '"'))
                self.assertEqual(set(posted), {str(site.pk) for site in members})
                # The tender it ticks is a real tender box, posted as `program`.
                self.assertRegex(html, r'class="btn-check js-vo-program" name="program"'
                                       rf'[^>]*value="{tender.pk}"')
                for site in members:
                    chip = re.search(rf'<span class="[^"]*js-vo-chip[^"]*"\s+'
                                     rf'data-site="{site.pk}"[^>]*>', html).group(0)
                    self.assertIn(f'data-program="{tender.pk}"', chip)
                    self.assertIn(f'data-group="{group.pk}"', chip)
        # And the handler does both halves.
        self.assertIn('setAdded(pk, box.checked, true)', html)
        self.assertIn('if (box.checked) tickTender(box.dataset.program)', html)

    def test_every_site_of_every_tender_is_searchable(self):
        response = _client(self.scm).get(self.url(self.tender))
        html = response.content.decode()
        results = re.findall(r'class="[^"]*js-vo-result[^"]*"\s+data-site="(\d+)"', html)
        self.assertEqual(set(results), {str(p.pk) for p in
                                        (self.a1, self.a2, self.a3, self.a4, self.a5,
                                         self.b1)})
        # "N of M sites" per tender.
        self.assertIn('Alpha Tender · <span class="js-vo-n">0</span> of 5 sites', html)
        self.assertIn('Bravo Tender · <span class="js-vo-n">0</span> of 1 site', html)

    def test_the_unfrozen_flag_is_shown_on_the_record_page_only(self):
        page = _client(self.scm).get(self.url(self.tender))
        self.assertNotContains(page, 'unfrozen')
        self.post(sites=[self.a5], programs=[self.tender])
        order = VendorOrder.objects.get()
        self.assertTrue(order.raised_with_unfrozen_quantities)
        detail = _client(self.scm).get(reverse('vendor_order_detail', args=[order.pk]))
        self.assertContains(detail, 'Raised against unfrozen quantities')
        self.assertContains(detail, '<h4 class="mb-0">PO / PI record</h4>', html=False)
        listing = _client(self.scm).get(
            reverse('program_vendor_order_list', args=[self.tender.pk]))
        self.assertNotContains(listing, 'unfrozen')

    def test_the_page_renders_with_no_procurement_groups_at_all(self):
        SiteGroupMembership.objects.all().delete()
        SiteGroup.objects.all().delete()
        response = _client(self.scm).get(reverse('vendor_order_create_group'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['group_rows'], [])
        self.assertContains(response, 'No procurement groups yet')
        self.post(sites=[self.a1], programs=[self.tender])
        order = VendorOrder.objects.get()
        self.assertEqual(order.sites.get().project, self.a1)

    def test_unlinked_rows_render_read_only_and_are_not_on_the_order(self):
        site = self.site('A6', self.tender, modules=5, inverters=1,
                         group=self.locked_a, unlinked='Site-specific civil work')
        response = _client(self.scm).get(self.url(self.tender))
        self.assertContains(response, 'no catalogue item')
        fragment = re.search(r'<li class="d-none js-vo-unlinked"[^>]*>.*?</li>',
                             response.content.decode(), re.S).group(0)
        self.assertIn('Site-specific civil work', fragment)
        self.assertNotIn('<input', fragment)

        self.post(sites=[(site, self.locked_a)], programs=[self.tender])
        self.assertEqual(
            sorted(line.item_description for line in VendorOrderLine.objects.all()),
            ['Module 550Wp', 'String Inverter 100kW'])

    def test_the_scm_tender_row_offers_the_two_entry_points(self):
        self.post(sites=[(self.a1, self.locked_a)], programs=[self.tender])
        # The tender section is drawn only under the Tenders context.
        response = _client(self.scm).get(reverse('dashboard_scm') + '?context=tenders')
        self.assertContains(response, 'Raise payment request</a>')
        self.assertContains(response, 'PO / PI records (1)</a>')
        self.assertNotContains(response, '>Raise order</a>')

    def test_the_raise_page_query_count_does_not_grow_with_site_count(self):
        """Every candidate site and requirement row is rendered once and the browser
        filters them, so the cost is fixed. Measured at three sites in one group and
        again at thirty."""
        client = _client(self.scm)
        client.get(self.url(self.tender))        # warm the session lookup

        self.site('Y00', self.tender, modules=10, inverters=1, group=self.locked_a)
        self.assertEqual(self.locked_a.memberships.filter(
            removed_at__isnull=True).count(), 3)
        with _Capture(self) as small:
            client.get(self.url(self.tender))

        for index in range(27):
            self.site(f'X{index:02d}', self.tender, modules=10, inverters=1,
                      group=self.locked_a)
        self.assertEqual(self.locked_a.memberships.filter(
            removed_at__isnull=True).count(), 30)

        with self.assertNumQueries(small.count):
            response = client.get(self.url(self.tender))
        self.assertEqual(response.status_code, 200)
        # PINNED, AND MEASURED RATHER THAN DERIVED: session, user and profile, the
        # entering tender, the tenders, the sites, the groups, their memberships,
        # aggregate_group_boq()'s three, the navbar's unread-notification count and the
        # vendors. It no longer grows with the number of tenders either (O3r dropped
        # post_qc_pool() per tender). If this number moves, a query was added — say
        # which.
        self.assertEqual(small.count, 13,
                         'the raise page cost changed — say so in the report')


class _Capture:
    """assertNumQueries without an expected number: run the block, keep the count."""

    def __init__(self, case):
        self.case, self.count = case, 0

    def __enter__(self):
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        self._inner = CaptureQueriesContext(connection)
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        result = self._inner.__exit__(*exc)
        self.count = len(self._inner.captured_queries)
        return result


class TenderOrderListTests(GroupRaiseFixture):

    def raise_one(self, po):
        self.post(sites=[(self.a1, self.locked_a)], programs=[self.tender], po_number=po)

    def test_the_list_costs_the_same_for_one_order_as_for_ten(self):
        self.raise_one('PO-1')
        client = _client(self.scm)
        url = reverse('program_vendor_order_list', args=[self.tender.pk])
        client.get(url)
        with _Capture(self) as one:
            client.get(url)
        for index in range(9):
            self.raise_one(f'PO-{index + 2}')
        self.assertEqual(VendorOrder.objects.count(), 10)
        with self.assertNumQueries(one.count):
            response = client.get(url)
        self.assertEqual(len(response.context['rows']), 10)

    def test_a_pm_on_a_site_in_the_tender_may_read_the_list(self):
        self.raise_one('PO-1')
        Project.objects.filter(pk=self.a1.pk).update(assigned_pm=self.pm)
        url = reverse('program_vendor_order_list', args=[self.tender.pk])
        self.assertEqual(_client(self.pm).get(url).status_code, 200)
        self.assertEqual(_client(self.pm_b).get(url).status_code, 403)

    def test_the_portfolio_roles_read_the_list(self):
        self.raise_one('PO-1')
        url = reverse('program_vendor_order_list', args=[self.tender.pk])
        for profile in (self.scm, self.finance, self.admin):
            with self.subTest(role=profile.role):
                self.assertEqual(_client(profile).get(url).status_code, 200)


# ---------------------------------------------------------------------------
# The Residential raise stores its total; migration 0097 fills the old rows
# ---------------------------------------------------------------------------

class ResidentialTotalTests(RaiseFixture):

    def test_the_residential_raise_stores_the_sum_of_its_lines(self):
        """Backend only (O3r): the Residential page still prices every line, and the
        order's stored total is their sum — 45,000 + 15,000 in RaiseFixture.payload()."""
        self.post()
        self.assertEqual(VendorOrder.objects.get().total_amount, Decimal('60000'))


class TotalAmountMigrationTests(GroupRaiseFixture):

    def setUp(self):
        self.backfill = import_module(
            'projects.migrations.0097_vendor_order_total_amount').backfill_total_amount

    def make(self, po, *amounts):
        # The column is NOT NULL once 0098 has run, so a placeholder stands in for the
        # NULL 0097 would find; the backfill must overwrite it.
        order = VendorOrder.objects.create(vendor=self.vendor, project_type='OPEX',
                                           po_number=po, total_amount=Decimal('1'),
                                           created_by=self.scm)
        for amount in amounts:
            VendorOrderLine.objects.create(order=order, item_description='Module',
                                           quantity=Decimal('1'), amount=Decimal(amount))
        return order

    def test_total_amount_migration_backfills_from_lines(self):
        first = self.make('PO-M1', '45000', '15000.50')
        second = self.make('PO-M2', '999')
        self.backfill(django_apps, None)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.total_amount, Decimal('60000.50'))
        self.assertEqual(second.total_amount, Decimal('999'))

    def test_the_backfill_refuses_an_order_with_no_priced_line_and_changes_nothing(self):
        priced = self.make('PO-M3', '500')
        bare = self.make('PO-M4')
        with self.assertRaisesRegex(RuntimeError, rf'0097 refused: 1 .*pk {bare.pk}\)'):
            self.backfill(django_apps, None)
        priced.refresh_from_db()
        self.assertEqual(priced.total_amount, Decimal('1'))    # untouched
