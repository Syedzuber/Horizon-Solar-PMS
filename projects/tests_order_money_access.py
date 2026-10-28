"""AF-2 — the order pages follow AF-1's money audience.

What this file pins, and why each matters:

  * THE ORDER PAGES' READERS ARE THE MONEY AUDIENCE. vendor_order_list,
    vendor_order_detail and program_vendor_order_list admit CEO, Admin, System Admin,
    Finance, SCM and whoever MANAGES a site on the order (user_can_see_project_money()).
    Seeing the site is not enough: a site engineer holding a task, BD, a Design Head flag
    holder, the designer assigned to the site, the site's QA/QC engineer and a
    coordinator who does not manage the site are all refused — before AF-2 the first
    five of them read every price, payment and document on the order by URL.
  * A MANAGER OF ONE SITE READS THE WHOLE ORDER. A coordinator of one site of a
    two-site order opens the order, with the other site's payment on it, and the
    tender's list.
  * THE APPROVAL PAGE STOPS LINKING, IT DOES NOT START REFUSING. A Design Head on a
    pre-dispatch request loses "Open the PO/PI record"; the PO and PI numbers stay,
    because they come from the request's snapshot, not the order page.
  * THE OLD PAYMENT URL STILL HAS NO GATE OF ITS OWN. payment_request_detail redirects,
    and the order page it lands on is what refuses a non-money user.
  * NOTHING MOVES FOR THE PEOPLE WHO PAY. The payments queue's buttons, the duplicate-
    submission redirect and the approve / hold / answer actions behave as before for a
    Finance flag holder, a CEO flag holder and SCM.

Run with:
    python manage.py test projects.tests_order_money_access --settings=solarpms.test_settings
"""
from decimal import Decimal

from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory
from django.urls import reverse

from .approvals import APPROVAL_KIND_MATERIAL_PRE_DISPATCH, create_approval_request
from .models import (
    PaymentRequest, PaymentRequestHold, Program, Project, ProjectPhase, SiteQaqcAssignment,
    Task, VendorOrderLine, VendorOrderSite,
)
from .submission_guard import already_submitted
from .tests_approval_views import MODULE_LINE
from .tests_payment_approval import _approver
from .tests_payment_queue import _make_order, _make_payment
from .tests_vendor_order_raise import RaiseFixture, _client, _profile


def _site(project_id, program, pm):
    return Project.objects.create(
        project_id=project_id, customer_name='AF2Client', customer_phone='9876543210',
        site_address='1 Sun Rd', city='Delhi', project_type='OPEX', program=program,
        site_code=project_id, dc_capacity_kw=Decimal('100.00'), status='Active',
        assigned_pm=pm)


def _hold_task(site, profile, role=Task.SITE_ENGINEER):
    phase = ProjectPhase.objects.create(project=site, phase_name='Install', phase_order=1)
    Task.objects.create(phase=phase, task_name='Mount structure', task_order=1,
                        assigned_role=role, assigned_to=profile)


class OrderAccessFixture(RaiseFixture):
    """One tender with two sites, A and B, and ONE order sized against both.

    A: pm (assigned), se (holds a task), designer (assigned_design), qaqc_pm (PM role,
       the site's QA/QC engineer — sees A, manages nothing). A has the lower pk, so it is
       the order's anchor and the detail page's "Back to orders" target.
    B: pm_b (assigned), coord_b (a coordinator of B only — manages one site of the two).
    Elsewhere: coord_far, a coordinator of a site in ANOTHER tender who holds a task on
       A — a coordinator who does not manage A or any site in A's tender.
    Portfolio-wide viewers of every project with no money: bd, head (Design Head flag).
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ceo       = _profile('af2_ceo', 'CEO')
        cls.sysadmin  = _profile('af2_sysadmin', 'System Admin')
        cls.bd        = _profile('af2_bd', 'BD')
        cls.head      = _profile('af2_head', 'Design')
        cls.head.is_design_head = True
        cls.head.save(update_fields=['is_design_head'])
        cls.designer  = _profile('af2_designer', 'Design')
        cls.qaqc_pm   = _profile('af2_qaqc_pm', 'PM')
        cls.coord_b   = _profile('af2_coord_b', 'Project Coordinator')
        cls.coord_far = _profile('af2_coord_far', 'Project Coordinator')
        cls.fin_approver = _approver('af2_fin_approver')                # Finance, flag
        cls.ceo_approver = _approver('af2_ceo_approver', role='CEO')

        cls.tender = Program.objects.create(
            name='AF2 Tender', program_type='OPEX', client_name='AF2Client',
            status='Active', short_tender_code='AF2')
        far_tender = Program.objects.create(
            name='AF2 Far Tender', program_type='OPEX', client_name='AF2Far',
            status='Active', short_tender_code='AF2F')
        cls.site_a = _site('AF2-A', cls.tender, cls.pm)
        cls.site_b = _site('AF2-B', cls.tender, cls.pm_b)
        far_site = _site('AF2-FAR', far_tender, cls.pm_b)
        Project.objects.filter(pk=cls.site_a.pk).update(assigned_design=cls.designer)
        cls.site_b.coordinators.add(cls.coord_b)
        far_site.coordinators.add(cls.coord_far)
        _hold_task(cls.site_a, cls.se)
        _hold_task(cls.site_a, cls.coord_far, role=Task.PROJECT_COORDINATOR)
        SiteQaqcAssignment.objects.create(project=cls.site_a, engineer=cls.qaqc_pm,
                                          assigned_by=cls.pm)

    def setUp(self):
        self.order = _make_order(self, 'OPEX', site=self.site_a, po='PO-AF2', pi='PI-AF2')
        VendorOrderSite.objects.create(order=self.order, project=self.site_b)
        VendorOrderLine.objects.create(order=self.order, item_description='Site B cables',
                                       quantity=Decimal('5'), amount=Decimal('7000'))
        # Anchored to A: a manager of B only must still see it — the WHOLE order.
        self.payment = _make_payment(self, self.order, amount='12345')

    def list_url(self, site=None):
        return reverse('vendor_order_list', args=[(site or self.site_a).pk])

    def detail_url(self):
        return reverse('vendor_order_detail', args=[self.order.pk])

    def tender_url(self):
        return reverse('program_vendor_order_list', args=[self.tender.pk])

    def three(self, profile):
        client = _client(profile)
        return tuple(client.get(url).status_code
                     for url in (self.list_url(), self.detail_url(), self.tender_url()))


# ---------------------------------------------------------------------------
# Who opens the three order pages
# ---------------------------------------------------------------------------

class ReaderTests(OrderAccessFixture):

    def test_viewers_outside_the_money_audience_are_refused_all_three(self):
        for name in ('se', 'bd', 'head', 'designer', 'qaqc_pm', 'coord_far'):
            with self.subTest(who=name):
                self.assertEqual(self.three(getattr(self, name)), (403, 403, 403))

    def test_the_refused_viewers_can_still_see_the_site(self):
        # The refusal is AF-2's money line, not a visibility gap: all but the far
        # coordinator open the site's own page.
        url = reverse('project_overview', args=[self.site_a.project_id])
        for name in ('se', 'bd', 'head', 'designer', 'qaqc_pm'):
            with self.subTest(who=name):
                self.assertEqual(_client(getattr(self, name)).get(url).status_code, 200)

    def test_the_money_audience_opens_all_three(self):
        for name in ('ceo', 'admin', 'sysadmin', 'finance', 'scm', 'pm'):
            with self.subTest(who=name):
                self.assertEqual(self.three(getattr(self, name)), (200, 200, 200))

    def test_a_manager_of_one_site_reads_the_whole_order(self):
        client = _client(self.coord_b)
        detail = client.get(self.detail_url())
        self.assertEqual(detail.status_code, 200)
        # Both sites, both lines, and the payment anchored to the OTHER site.
        self.assertContains(detail, 'AF2-A')
        self.assertContains(detail, 'AF2-B')
        self.assertContains(detail, 'Site B cables')
        self.assertIn(self.payment.pk, [row['payment'].pk
                                        for row in detail.context['payments']])
        self.assertEqual(client.get(self.list_url(self.site_b)).status_code, 200)
        self.assertEqual(client.get(self.tender_url()).status_code, 200)

    def test_the_old_payment_url_ends_at_403_for_a_non_money_user(self):
        response = _client(self.se).get(
            reverse('payment_request_detail', args=[self.site_a.project_id, self.payment.pk]),
            follow=True)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.redirect_chain[-1][0],
                         self.detail_url() + f'#payment-{self.payment.pk}')


# ---------------------------------------------------------------------------
# The detail page's "Back to orders" link
# ---------------------------------------------------------------------------

class BackLinkTests(OrderAccessFixture):

    def test_present_for_a_reader_who_can_open_the_list(self):
        # The anchor site's PM and SCM (and Finance, portfolio-wide) open AF2-A's list.
        for name in ('pm', 'scm', 'finance'):
            with self.subTest(who=name):
                response = _client(getattr(self, name)).get(self.detail_url())
                self.assertTrue(response.context['can_open_order_list'])
                self.assertContains(response, 'Back to orders')

    def test_absent_for_a_manager_of_the_non_anchor_site(self):
        # U2: pm_b and coord_b manage AF2-B only. They read the whole order (AF-2), but
        # the link leads to the ANCHOR's (AF2-A's) list, which refuses them.
        for name in ('pm_b', 'coord_b'):
            with self.subTest(who=name):
                client = _client(getattr(self, name))
                response = client.get(self.detail_url())
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.context['can_open_order_list'])
                self.assertNotContains(response, 'Back to orders')
                self.assertNotContains(response, self.list_url())
                self.assertEqual(client.get(self.list_url()).status_code, 403)


# ---------------------------------------------------------------------------
# Approvals: the Design Head on a pre-dispatch request
# ---------------------------------------------------------------------------

class PreDispatchLinkTests(OrderAccessFixture):

    def setUp(self):
        super().setUp()
        self.approval = create_approval_request(
            kind=APPROVAL_KIND_MATERIAL_PRE_DISPATCH, raised_by=self.scm,
            title='Modules, lot 1', description='First truck.', pm_assignee=self.pm,
            design_signoff_required=True, design_assignee=self.head,
            vendor=self.order.vendor, material={'vendor_order': self.order},
            lines=[dict(MODULE_LINE)])

    def detail(self, profile):
        return _client(profile).get(reverse('approval_detail', args=[self.approval.pk]))

    def test_the_design_head_sees_the_numbers_but_no_link(self):
        page = self.detail(self.head)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'PO/PI record · PO PO-AF2 · PI PI-AF2')
        self.assertNotContains(page, self.detail_url())
        self.assertNotContains(page, 'Open the PO/PI record')

    def test_scm_and_the_sites_pm_keep_the_link(self):
        for name in ('scm', 'pm'):
            with self.subTest(who=name):
                self.assertContains(self.detail(getattr(self, name)), self.detail_url())


# ---------------------------------------------------------------------------
# The people who pay: queue buttons, the duplicate redirect, the actions
# ---------------------------------------------------------------------------

class PayersUnchangedTests(OrderAccessFixture):

    def queue_row(self, profile):
        response = _client(profile).get(reverse('payment_queue'), {'tab': 'OPEX'})
        self.assertEqual(response.status_code, 200)
        return {row['payment'].pk: row for row in response.context['rows']}[self.payment.pk]

    def test_queue_buttons_for_both_flag_holders(self):
        for profile in (self.fin_approver, self.ceo_approver):
            with self.subTest(role=profile.role):
                row = self.queue_row(profile)
                self.assertTrue(row['can_approve'])
                self.assertTrue(row['can_hold'])
                # Reject is reachable only from hold (user_can_reject_payment), so a
                # pending row draws none — the same answer as before AF-2.
                self.assertFalse(row['can_reject'])
        PaymentRequest.objects.filter(pk=self.payment.pk).update(
            status=PaymentRequest.ON_HOLD)
        for profile in (self.fin_approver, self.ceo_approver):
            with self.subTest(role=profile.role, status='on_hold'):
                self.assertTrue(self.queue_row(profile)['can_reject'])

    def test_scm_is_still_refused_the_queue(self):
        self.assertEqual(_client(self.scm).get(reverse('payment_queue')).status_code, 403)

    def test_the_duplicate_redirect_still_lands_on_the_order(self):
        for profile in (self.fin_approver, self.ceo_approver, self.scm):
            with self.subTest(role=profile.role):
                request = RequestFactory(SERVER_NAME='localhost').get('/orders/x/?key=1')
                request.user, request.session = profile.user, {}
                request._messages = FallbackStorage(request)
                response = already_submitted(request, self.order)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response['Location'], self.detail_url())

    def test_the_duplicate_answer_is_a_403_for_a_non_money_user(self):
        request = RequestFactory(SERVER_NAME='localhost').get('/orders/x/?key=1')
        request.user, request.session = self.se.user, {}
        request._messages = FallbackStorage(request)
        self.assertEqual(already_submitted(request, self.order).status_code, 403)

    def test_hold_then_answer_then_approve(self):
        # CEO flag holder holds, SCM answers, Finance flag holder approves — each lands
        # on the order page and moves the row, as before AF-2.
        held = _client(self.ceo_approver).post(
            reverse('payment_hold', args=[self.payment.pk]), {'reason': 'Rate check'})
        self.assertRedirects(held, self.detail_url(), fetch_redirect_response=False)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentRequest.ON_HOLD)
        self.assertTrue(PaymentRequestHold.objects.filter(payment_request=self.payment)
                        .exists())

        answered = _client(self.scm).post(
            reverse('payment_hold_respond', args=[self.payment.pk]),
            {'response': 'Rate is the tender rate'})
        self.assertRedirects(answered, self.detail_url(), fetch_redirect_response=False)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentRequest.PENDING_APPROVAL)

        approved = _client(self.fin_approver).post(
            reverse('payment_approve', args=[self.payment.pk]),
            {'approved_amount': '12345'})
        self.assertRedirects(approved, self.detail_url(), fetch_redirect_response=False)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentRequest.APPROVED)

    def test_a_non_money_user_is_refused_the_actions_before_any_check(self):
        response = _client(self.head).post(
            reverse('payment_hold', args=[self.payment.pk]), {'reason': 'x'})
        self.assertEqual(response.status_code, 403)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentRequest.PENDING_APPROVAL)
