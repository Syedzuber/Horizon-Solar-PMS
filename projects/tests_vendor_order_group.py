"""O3 — one vendor order sized against sites from several groups and several tenders.

What this file pins, and why each matters:

  * THE ORDER IS NOT LIMITED TO ONE GROUP OR ONE TENDER. Sites from two locked groups in
    one tender, and from two tenders at once, make ONE order — with a VendorOrderProgram
    row per tender and the order appearing in each tender's list.
  * ZERO SITES IS A FIRST-CLASS CASE (O2d). A central purchase names its tenders, takes
    its lines by hand, anchors its payment to NULL, and still reaches its tender's list.
  * THE AGGREGATE IS A PREFILL, NOT A RULE. A quantity edited away from
    aggregate_group_boq()'s total is stored exactly as entered.
  * WHAT IS REFUSED, AND NOTHING IS CREATED WHEN IT IS: a mixture of project types, a
    hand-added line from the wrong catalogue, no lines at all, every role but SCM, and a
    site the caller cannot see.
  * THE UNFROZEN FLAG IS A FACT ABOUT THE MOMENT OF RAISING. A draft group or a pool site
    sets it; sites drawn only from locked groups do not.
  * THE PAGE DOES NOT GET SLOWER AS THE TENDER FILLS UP. Its query count is measured at 3
    sites and at 30 and must be identical.

Run with:
    python manage.py test projects.tests_vendor_order_group --settings=solarpms.test_settings
"""
import re
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

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
    """Two OPEX tenders, four procurement groups, a post-QC pool and a catalogue.

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
        cls.opex_cable = BOQItemMaster.objects.create(
            code='OPX-903', description='DC Cable 4sqmm', unit='Mtr',
            category='Cables', project_type='OPEX', sort_order=3)

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

        # Two sites in one locked group, a third in the second — the first test's
        # "3 sites across 2 locked groups in ONE tender".
        cls.a1 = cls.site('A1', cls.tender, modules=100, inverters=2, group=cls.locked_a)
        cls.a2 = cls.site('A2', cls.tender, modules=60,  inverters=1, group=cls.locked_a)
        cls.a3 = cls.site('A3', cls.tender, modules=40,  inverters=1, group=cls.locked_b)
        cls.a4 = cls.site('A4', cls.tender, modules=25,  inverters=1, group=cls.draft_a)
        # Released and in no group: the post-QC pool.
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

    def payload(self, sites=(), programs=(), lines=(), **overrides):
        """A complete, valid submission. `sites` is an iterable of Project or of
        (Project, SiteGroup); `lines` is an iterable of (BOQItemMaster, qty, amount)."""
        data = {
            'client_uuid': str(uuid.uuid4()),
            'vendor_id':   str(self.vendor.pk),
            'po_number':   'PO-GROUP-1',
            'site':        [],
            'program':     [str(program.pk) for program in programs],
            'line':        [],
            'doc_type_0':  VENDOR_ORDER_DOC_PO,
            'doc_file_0':  _pdf('po.pdf'),
        }
        for entry in sites:
            project, group = entry if isinstance(entry, tuple) else (entry, None)
            data['site'].append(str(project.pk))
            if group is not None:
                data[f'via_group_{project.pk}'] = str(group.pk)
        for master, quantity, amount in lines:
            data['line'].append(str(master.pk))
            data[f'qty_{master.pk}']    = str(quantity)
            data[f'amount_{master.pk}'] = str(amount)
        data.update(overrides)
        return data

    def post(self, profile=None, storage=None, **payload_kwargs):
        storage = storage or MagicMock()
        with patch('projects.supabase_storage.get_supabase_client', return_value=storage):
            response = _client(profile or self.scm).post(
                reverse('vendor_order_create_group'), self.payload(**payload_kwargs))
        return response, storage

    def assertNothingCreated(self):
        self.assertEqual(VendorOrder.objects.count(), 0)
        self.assertEqual(VendorOrderSite.objects.count(), 0)
        self.assertEqual(VendorOrderProgram.objects.count(), 0)
        self.assertEqual(VendorOrderLine.objects.count(), 0)
        self.assertEqual(VendorOrderDocument.objects.count(), 0)
        self.assertEqual(PaymentRequest.objects.count(), 0)


# ---------------------------------------------------------------------------
# The orders this page exists to raise
# ---------------------------------------------------------------------------

class GroupRaiseTests(GroupRaiseFixture):

    def test_three_sites_across_two_locked_groups_in_one_tender(self):
        ids = [self.a1.pk, self.a2.pk, self.a3.pk]
        agg = aggregate_group_boq(ids)
        totals = {line['item_master']: line['total_quantity'] for line in agg['lines']}
        self.assertEqual(totals[self.opex_module.pk], Decimal('200'))    # 100 + 60 + 40
        self.assertEqual(totals[self.opex_inverter.pk], Decimal('4'))    # 2 + 1 + 1

        response, _ = self.post(
            sites=[(self.a1, self.locked_a), (self.a2, self.locked_a),
                   (self.a3, self.locked_b)],
            programs=[self.tender],
            lines=[(self.opex_module, '200', '2000000'),
                   (self.opex_inverter, '4', '800000')])
        order = VendorOrder.objects.get()
        self.assertRedirects(response, reverse('vendor_order_detail', args=[order.pk]),
                             fetch_redirect_response=False)

        self.assertEqual(order.project_type, 'OPEX')
        self.assertEqual(order.created_by, self.scm)
        self.assertEqual(order.sites.count(), 3)
        self.assertEqual({site.project_id for site in order.sites.all()}, set(ids))
        self.assertEqual(order.programs.count(), 1)
        self.assertEqual(order.programs.get().program, self.tender)

        lines = {line.item_master_id: line for line in order.lines.all()}
        self.assertEqual(lines[self.opex_module.pk].quantity,
                         totals[self.opex_module.pk])
        self.assertEqual(lines[self.opex_inverter.pk].quantity,
                         totals[self.opex_inverter.pk])
        self.assertEqual(lines[self.opex_module.pk].item_code, 'OPX-901')
        # A GROUP LINE HAS NO SINGLE BOQ ROW — it is the sum of three of them.
        self.assertIsNone(lines[self.opex_module.pk].boq_item)
        self.assertEqual(order.total, Decimal('2800000'))

        # Every site was in a LOCKED group, so nothing was moving under this order.
        self.assertFalse(order.raised_with_unfrozen_quantities)
        # via_site_group is the group each site was picked through.
        by_project = {site.project_id: site for site in order.sites.all()}
        self.assertEqual(by_project[self.a1.pk].via_site_group, self.locked_a)
        self.assertEqual(by_project[self.a3.pk].via_site_group, self.locked_b)

    def test_sites_across_two_tenders_make_one_order_listed_in_both(self):
        self.post(sites=[(self.a1, self.locked_a), (self.b1, self.locked_c)],
                  programs=[self.tender, self.tender_b],
                  lines=[(self.opex_module, '180', '1800000')])
        order = VendorOrder.objects.get()
        self.assertEqual(order.sites.count(), 2)
        self.assertEqual(order.programs.count(), 2)
        self.assertEqual({link.program_id for link in order.programs.all()},
                         {self.tender.pk, self.tender_b.pk})

        for program in (self.tender, self.tender_b):
            with self.subTest(tender=program.name):
                response = _client(self.scm).get(
                    reverse('program_vendor_order_list', args=[program.pk]))
                self.assertEqual(response.status_code, 200)
                self.assertEqual([row['order'] for row in response.context['rows']],
                                 [order])
                self.assertContains(response, 'spans 2 tenders')

    def test_a_site_from_a_draft_group_raises_the_unfrozen_flag(self):
        self.post(sites=[(self.a1, self.locked_a), (self.a4, self.draft_a)],
                  programs=[self.tender],
                  lines=[(self.opex_module, '125', '1250000')])
        order = VendorOrder.objects.get()
        self.assertEqual(order.sites.count(), 2)
        self.assertTrue(order.raised_with_unfrozen_quantities)

    def test_a_pool_site_raises_the_unfrozen_flag_and_records_no_group(self):
        self.post(sites=[self.a5], programs=[self.tender],
                  lines=[(self.opex_module, '10', '100000')])
        order = VendorOrder.objects.get()
        self.assertTrue(order.raised_with_unfrozen_quantities)
        self.assertIsNone(order.sites.get().via_site_group)

    def test_zero_sites_with_hand_entered_lines_and_one_tender(self):
        self.post(sites=[], programs=[self.tender],
                  lines=[(self.opex_cable, '5000', '450000')],
                  request_payment='1', payment_amount='100000')
        order = VendorOrder.objects.get()
        self.assertEqual(order.sites.count(), 0)
        self.assertEqual(order.project_type, 'OPEX')      # taken from the line
        self.assertEqual(order.programs.get().program, self.tender)
        self.assertEqual(order.lines.get().item_master, self.opex_cable)
        # THE ANCHOR IS NULL — there is no site to file the payment under (O2d).
        payment = PaymentRequest.objects.get()
        self.assertIsNone(payment.project)
        self.assertEqual(payment.vendor_order, order)

        response = _client(self.scm).get(
            reverse('program_vendor_order_list', args=[self.tender.pk]))
        self.assertEqual([row['order'] for row in response.context['rows']], [order])

    def test_the_payment_anchors_to_the_lowest_project_id(self):
        self.post(sites=[(self.a3, self.locked_b), (self.a1, self.locked_a)],
                  programs=[self.tender],
                  lines=[(self.opex_module, '140', '1400000')],
                  request_payment='1', payment_amount='50000')
        self.assertEqual(PaymentRequest.objects.get().project,
                         min((self.a1, self.a3), key=lambda p: p.pk))

    def test_a_quantity_edited_away_from_the_aggregate_is_stored_as_entered(self):
        agg = aggregate_group_boq([self.a1.pk, self.a2.pk])
        aggregate = {line['item_master']: line['total_quantity']
                     for line in agg['lines']}[self.opex_module.pk]
        self.assertEqual(aggregate, Decimal('160'))

        self.post(sites=[(self.a1, self.locked_a), (self.a2, self.locked_a)],
                  programs=[self.tender],
                  lines=[(self.opex_module, '175', '1750000')])
        line = VendorOrderLine.objects.get()
        self.assertEqual(line.quantity, Decimal('175'))
        self.assertNotEqual(line.quantity, aggregate)

    def test_the_same_site_posted_twice_makes_one_site_row(self):
        """A site ticked through two sources posts twice. It must still be ONE
        VendorOrderSite — uniq_vendor_order_site would refuse the second, and refusing
        the whole submission over a duplicate the page produced is worse than collapsing
        it."""
        payload = self.payload(
            sites=[(self.a1, self.locked_a)], programs=[self.tender],
            lines=[(self.opex_module, '100', '1000000')])
        payload['site'] = [str(self.a1.pk), str(self.a1.pk)]
        with patch('projects.supabase_storage.get_supabase_client',
                   return_value=MagicMock()):
            _client(self.scm).post(reverse('vendor_order_create_group'), payload)
        order = VendorOrder.objects.get()
        self.assertEqual(order.sites.count(), 1)
        self.assertEqual(order.sites.get().project, self.a1)

    def test_the_same_client_uuid_twice_creates_one_order(self):
        key = str(uuid.uuid4())
        first, _ = self.post(sites=[(self.a1, self.locked_a)], programs=[self.tender],
                             lines=[(self.opex_module, '100', '1000000')],
                             client_uuid=key)
        second, storage = self.post(sites=[(self.a1, self.locked_a)],
                                    programs=[self.tender],
                                    lines=[(self.opex_module, '100', '1000000')],
                                    client_uuid=key)
        self.assertEqual(VendorOrder.objects.count(), 1)
        self.assertEqual(second['Location'], first['Location'])
        storage.storage.from_.return_value.upload.assert_not_called()

    def test_the_feed_records_the_order_on_every_site(self):
        from .models import ActivityLog
        self.post(sites=[(self.a1, self.locked_a), (self.a2, self.locked_a)],
                  programs=[self.tender],
                  lines=[(self.opex_module, '160', '1600000')])
        logs = ActivityLog.objects.filter(action_code='vendor_order_raised')
        self.assertEqual({log.project_id for log in logs}, {self.a1.pk, self.a2.pk})


# ---------------------------------------------------------------------------
# Refusals — and nothing is created by any of them
# ---------------------------------------------------------------------------

class GroupRefusalTests(GroupRaiseFixture):

    def test_mixing_residential_and_opex_sites_is_refused(self):
        response, storage = self.post(
            sites=[(self.a1, self.locked_a), self.project],
            programs=[self.tender],
            lines=[(self.opex_module, '100', '1000000')])
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'same kind of project', status_code=400)
        self.assertNothingCreated()
        storage.storage.from_.return_value.upload.assert_not_called()

    def test_a_hand_added_line_of_the_wrong_project_type_is_refused(self):
        response, _ = self.post(
            sites=[(self.a1, self.locked_a)], programs=[self.tender],
            lines=[(self.opex_module, '100', '1000000'),
                   (self.module, '4', '40000')])       # ITM-901, Residential
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'Residential catalogue item', status_code=400)
        self.assertNothingCreated()

    def test_two_catalogues_on_a_site_less_order_are_refused(self):
        response, _ = self.post(
            sites=[], programs=[self.tender],
            lines=[(self.opex_cable, '100', '10000'), (self.module, '4', '40000')])
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'same catalogue', status_code=400)
        self.assertNothingCreated()

    def test_no_lines_at_all_is_refused(self):
        response, _ = self.post(sites=[(self.a1, self.locked_a)],
                                programs=[self.tender], lines=[])
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'at least one item', status_code=400)
        self.assertNothingCreated()

    def test_every_role_but_scm_is_refused(self):
        for profile in (self.finance, self.pm, self.designer, self.admin):
            with self.subTest(role=profile.role):
                response, storage = self.post(
                    profile=profile, sites=[(self.a1, self.locked_a)],
                    programs=[self.tender],
                    lines=[(self.opex_module, '100', '1000000')])
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
            programs=[self.tender], lines=[(self.opex_module, '160', '1600000')])
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'no longer exists', status_code=400)
        self.assertNothingCreated()

    def test_a_site_the_user_cannot_see_is_refused(self):
        """THE VISIBILITY TERM, asked directly.

        It cannot be reached end-to-end today: the only role the page admits is SCM, and
        user_can_view_project() makes SCM portfolio-wide, so no site is invisible to a
        caller who got past user_can_raise_group_order(). The term is there for the
        narrowing D-4 will bring, and this test is what keeps it honest until then — the
        parser is handed a PM who has no claim on the site and must refuse it.
        """
        request = RequestFactory().post(
            reverse('vendor_order_create_group'), {'site': [str(self.a1.pk)]})
        request.user = self.pm_b.user           # assigned to neither site nor tender
        errors = []
        sites, programs, project_type, unfrozen = _parse_group_sites(request, errors)
        self.assertEqual(sites, [])
        self.assertIn('A chosen site is not one you can see.', errors)

    def test_a_refused_submission_gives_the_line_table_back(self):
        response, _ = self.post(
            sites=[(self.a1, self.locked_a)], programs=[self.tender],
            lines=[(self.opex_module, '177', '1770000')],
            po_number='', pi_number='')          # refused on the header
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'value="177"', status_code=400)
        self.assertContains(response, f'value="{self.a1.pk}" checked', status_code=400)
        self.assertNothingCreated()


# ---------------------------------------------------------------------------
# The page itself
# ---------------------------------------------------------------------------

class GroupRaisePageTests(GroupRaiseFixture):

    def test_the_page_offers_every_group_across_every_tender(self):
        response = _client(self.scm).get(self.url(self.tender))
        self.assertEqual(response.status_code, 200)
        for group in (self.locked_a, self.locked_b, self.draft_a, self.locked_c):
            with self.subTest(group=group.name):
                self.assertContains(response, f'value="g{group.pk}"')
        # Entered from Alpha, but Bravo's tender tick is offered too.
        self.assertContains(response, f'value="{self.tender_b.pk}"')

    def test_the_entering_tender_is_pre_ticked(self):
        response = _client(self.scm).get(self.url(self.tender))
        self.assertEqual(response.context['ticked_programs'], {str(self.tender.pk)})
        self.assertEqual(response.context['entering_program'], self.tender)

    def test_no_entering_tender_pre_ticks_nothing(self):
        response = _client(self.scm).get(reverse('vendor_order_create_group'))
        self.assertEqual(response.context['ticked_programs'], set())
        self.assertIsNone(response.context['entering_program'])

    def test_the_page_starts_with_no_sites_and_an_empty_line_table(self):
        response = _client(self.scm).get(self.url(self.tender))
        self.assertEqual(response.context['site_rows'] and
                         [row for row in response.context['site_rows'] if row['checked']],
                         [])
        self.assertEqual(response.context['hand_rows'], [])
        self.assertContains(response, 'No items yet')

    def test_the_page_renders_with_no_procurement_groups_at_all(self):
        """A fresh deployment has no groups. The page must still be usable — a zero-site
        order with hand-entered lines is exactly what it is for — and section A's site
        half is not drawn at all, so nothing in the script may assume it is there."""
        SiteGroupMembership.objects.all().delete()
        SiteGroup.objects.all().delete()
        response = _client(self.scm).get(reverse('vendor_order_create_group'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['sources'], [])
        self.assertEqual(response.context['site_rows'], [])
        self.assertContains(response, 'No procurement groups exist yet')
        self.assertContains(response, 'Add a line')

    def test_an_order_raised_with_no_groups_in_the_system_still_works(self):
        SiteGroupMembership.objects.all().delete()
        SiteGroup.objects.all().delete()
        self.post(sites=[], programs=[self.tender],
                  lines=[(self.opex_cable, '100', '9000')])
        order = VendorOrder.objects.get()
        self.assertEqual(order.sites.count(), 0)
        self.assertEqual(order.programs.get().program, self.tender)

    def test_unlinked_rows_render_read_only_and_are_not_orderable(self):
        site = self.site('A6', self.tender, modules=5, inverters=1,
                         group=self.locked_a, unlinked='Site-specific civil work')
        response = _client(self.scm).get(self.url(self.tender))
        self.assertContains(response, 'Not orderable')
        self.assertContains(response, 'Site-specific civil work')

        # The row carries no input of any kind — it cannot be ticked, priced or posted.
        fragment = re.search(
            r'<li class="list-group-item small d-none js-vo-unlinked"[^>]*>.*?</li>',
            response.content.decode(), re.S).group(0)
        self.assertIn('Site-specific civil work', fragment)
        self.assertNotIn('<input', fragment)

        # And it is in no consolidated quantity: an order raised from the group it sits
        # in has lines for the two catalogue items and nothing else.
        self.post(sites=[(site, self.locked_a)], programs=[self.tender],
                  lines=[(self.opex_module, '5', '50000')])
        self.assertEqual(
            [line.item_description for line in VendorOrderLine.objects.all()],
            ['Module 550Wp'])

    def test_the_raise_page_query_count_does_not_grow_with_site_count(self):
        """The page renders every candidate line once and the browser filters them, so
        the cost is in the number of TENDERS, never the number of sites. Measured at
        three sites in the entering group and again at thirty."""
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
        # PINNED, AND MEASURED RATHER THAN DERIVED. Two tenders in this fixture, so:
        # session and user, the groups, their memberships, one post_qc_pool() per
        # tender, aggregate_group_boq()'s three, the pk-keyed contributions, the
        # catalogue and the vendors. If this number moves, a query was added — say
        # which, and whether it is per tender or per site.
        self.assertEqual(small.count, 15,
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
        self.post(sites=[(self.a1, self.locked_a)], programs=[self.tender],
                  lines=[(self.opex_module, '100', '1000000')], po_number=po)

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
