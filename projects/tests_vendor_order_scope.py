"""O2d — what an order was sized against, and a payment with no site to be filed under.

What this file pins, and why each matters:

  * SITES ARE A REQUIREMENT BASIS, NOT A DESTINATION. An order may name ZERO sites. Such
    an order is still readable by the five portfolio roles and still unreadable by a PM
    with no claim on it — the site clause in user_can_view_vendor_order() ADDS readers
    and never gates them.
  * A PAYMENT MAY HAVE NO ANCHOR. PaymentRequest.project is nullable, and a payment on a
    site-less order saves with it NULL, together with its feed line and its ledger row.
  * SCOPE IS SAID IN ONE PLACE. scope_label has four forms and one definition, on
    VendorOrder, which PaymentRequest delegates to.
  * THE RESIDENTIAL RAISE DID NOT MOVE. One site, the anchor set, and now the site's
    tender recorded when it has one.
  * THE READERS THIS PROMPT MAY NOT CHANGE DO NOT CRASH. Every screen named in O2d's
    pre-flight is asked to render with a NULL-anchor payment present.

Run with:
    python manage.py test projects.tests_vendor_order_scope --settings=solarpms.test_settings
"""
import uuid
from datetime import date
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from .models import (
    ActivityLog, PaymentRequest, Program, Project, StatusTransition,
    SUBJECT_PAYMENT_REQUEST, VendorOrder, VendorOrderLine, VendorOrderProgram,
    VendorOrderSite,
)
from .tests_vendor_order_payments import _make_order, _pay
from .tests_vendor_order_raise import RaiseFixture, _client, _profile


def _bare_order(fixture, total=Decimal('90000'), po='PO-CENTRAL'):
    """An order with NO sites — a central purchase. Written directly, because no view
    raises one yet: O2d is the structure, and the screen that uses it is O3's."""
    order = VendorOrder.objects.create(
        vendor=fixture.vendor, project_type='OPEX', po_number=po,
        total_amount=total, created_by=fixture.scm, client_uuid=uuid.uuid4())
    VendorOrderLine.objects.create(order=order, item_description='Modules - stock',
                                   quantity=Decimal('100'), amount=total)
    return order


def _pay_siteless(fixture, amount):
    """A payment written directly against a site-less order, with no anchor."""
    return PaymentRequest.objects.create(
        vendor_order=fixture.order, project=None, vendor=fixture.vendor,
        amount=Decimal(amount), requested_by=fixture.scm.user,
        status=PaymentRequest.APPROVED)


class ScopeFixture(RaiseFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ceo    = _profile('o2d_ceo', 'CEO')
        cls.sysadm = _profile('o2d_sysadmin', 'System Admin')
        cls.tender = Program.objects.create(
            program_type='OPEX', name='IPGCL Delhi Tender', client_name='IPGCL',
            short_tender_code='IPGCL26', status='Active')
        cls.tender_b = Program.objects.create(
            program_type='OPEX', name='NDMC Phase 2', client_name='NDMC',
            short_tender_code='NDMC26', status='Active')


# ---------------------------------------------------------------------------
# An order with no sites
# ---------------------------------------------------------------------------

class SitelessOrderTests(ScopeFixture):

    def setUp(self):
        self.order = _bare_order(self)

    def detail_url(self):
        return reverse('vendor_order_detail', args=[self.order.pk])

    def test_an_order_can_be_created_with_no_sites(self):
        self.assertEqual(self.order.sites.count(), 0)
        self.assertEqual(self.order.total, Decimal('90000'))

    def test_the_portfolio_roles_read_it(self):
        # The site clause admits nobody here; the role clause still decides on its own.
        for profile in (self.ceo, self.admin, self.sysadm, self.finance, self.scm):
            with self.subTest(role=profile.role):
                response = _client(profile).get(self.detail_url())
                self.assertEqual(response.status_code, 200)

    def test_an_unrelated_pm_is_refused(self):
        self.assertEqual(_client(self.pm).get(self.detail_url()).status_code, 403)
        self.assertEqual(_client(self.pm_b).get(self.detail_url()).status_code, 403)

    def test_the_page_says_no_sites_and_names_its_tenders(self):
        VendorOrderProgram.objects.create(order=self.order, program=self.tender)
        response = _client(self.scm).get(self.detail_url())
        self.assertContains(response, 'Sites this order was sized against')
        self.assertContains(response,
                            'No sites recorded — sized against IPGCL Delhi Tender')
        self.assertContains(response, 'held centrally and issued to sites later')

    def test_the_page_says_no_sites_when_there_are_no_tenders_either(self):
        response = _client(self.scm).get(self.detail_url())
        self.assertContains(response, 'No sites recorded')
        self.assertNotContains(response, 'sized against IPGCL')

    def test_the_explanatory_note_is_drawn_and_its_comment_is_not(self):
        # A multi-line {# #} renders as visible text; only {% comment %} is stripped.
        # The same trap the documents page's test guards against.
        response = _client(self.scm).get(self.detail_url())
        self.assertContains(response, 'held centrally and issued to sites later')
        self.assertNotContains(response, 'reads as a delivery list')

    def test_a_residential_order_still_shows_its_one_site(self):
        order = _make_order(self)
        response = _client(self.scm).get(reverse('vendor_order_detail', args=[order.pk]))
        self.assertContains(response, 'Sites this order was sized against')
        self.assertContains(response, self.project.project_id)
        self.assertNotContains(response, 'No sites recorded')


class SitelessPaymentTests(ScopeFixture):

    def setUp(self):
        self.order = _bare_order(self)

    def test_a_payment_on_a_siteless_order_saves_with_a_null_anchor(self):
        response = _client(self.scm).post(
            reverse('vendor_order_add_payment', args=[self.order.pk]),
            {'client_uuid': str(uuid.uuid4()), 'payment_amount': '40000',
             'payment_note': 'advance'})
        self.assertRedirects(response,
                             reverse('vendor_order_detail', args=[self.order.pk]),
                             fetch_redirect_response=False)
        pr = PaymentRequest.objects.get()
        self.assertIsNone(pr.project)
        self.assertEqual(pr.vendor_order, self.order)
        self.assertEqual(pr.amount, Decimal('40000'))
        # The feed line and the ledger row are written, both with a null project.
        log = ActivityLog.objects.get(action_code='payment_request_raised')
        self.assertIsNone(log.project)
        transition = StatusTransition.objects.get(subject_type=SUBJECT_PAYMENT_REQUEST,
                                                  subject_id=pr.pk)
        self.assertIsNone(transition.project)

    def test_the_balance_rule_is_unchanged_on_a_siteless_order(self):
        _pay_siteless(self, '90000')
        response = _client(self.scm).post(
            reverse('vendor_order_add_payment', args=[self.order.pk]),
            {'client_uuid': str(uuid.uuid4()), 'payment_amount': '0.01'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(PaymentRequest.objects.count(), 1)

    def test_str_does_not_raise_on_a_null_anchor(self):
        pr = _pay_siteless(self, '40000')
        self.assertIn(f'order #{self.order.pk}', str(pr))


# ---------------------------------------------------------------------------
# scope_label — one definition, four forms
# ---------------------------------------------------------------------------

class ScopeLabelTests(ScopeFixture):

    def test_one_site_is_that_sites_project_id(self):
        order = _make_order(self)
        self.assertEqual(order.scope_label, self.project.project_id)

    def test_several_sites_is_a_count(self):
        order = _make_order(self)
        VendorOrderSite.objects.create(order=order, project=self.other_project)
        self.assertEqual(order.scope_label, '2 sites')

    def test_no_sites_with_tenders_is_the_tender_names(self):
        order = _bare_order(self)
        VendorOrderProgram.objects.create(order=order, program=self.tender)
        VendorOrderProgram.objects.create(order=order, program=self.tender_b)
        self.assertEqual(order.scope_label, 'IPGCL Delhi Tender, NDMC Phase 2')

    def test_neither_is_the_order_itself(self):
        order = _bare_order(self)
        self.assertEqual(order.scope_label, f'Order #{order.pk}')

    def test_a_payment_delegates_to_its_order(self):
        order = _make_order(self)
        pr = _pay(self, order, '1000')
        self.assertEqual(pr.scope_label, order.scope_label)
        self.assertEqual(pr.scope_label, self.project.project_id)


class VendorOrderProgramTests(ScopeFixture):

    def test_the_same_tender_cannot_be_recorded_twice(self):
        order = _bare_order(self)
        VendorOrderProgram.objects.create(order=order, program=self.tender)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                VendorOrderProgram.objects.create(order=order, program=self.tender)

    def test_two_orders_may_name_the_same_tender(self):
        first, second = _bare_order(self, po='PO-1'), _bare_order(self, po='PO-2')
        VendorOrderProgram.objects.create(order=first, program=self.tender)
        VendorOrderProgram.objects.create(order=second, program=self.tender)
        self.assertEqual(VendorOrderProgram.objects.count(), 2)


# ---------------------------------------------------------------------------
# The Residential raise did not move
# ---------------------------------------------------------------------------

class ResidentialRaiseUnchangedTests(RaiseFixture):

    def test_the_raise_records_exactly_one_site_and_sets_the_anchor(self):
        response, _ = self.post()
        self.assertEqual(response.status_code, 302)
        order = VendorOrder.objects.get()
        self.assertEqual([site.project for site in order.sites.all()], [self.project])
        self.assertEqual(PaymentRequest.objects.get().project, self.project)
        self.assertEqual(order.scope_label, self.project.project_id)

    def test_a_residential_site_has_no_tender_so_none_is_recorded(self):
        # Project._validate_program_link() forbids a Residential site from belonging to a
        # Program, so this is the only shape the raise can ever see today. The branch
        # must skip silently, not refuse.
        self.post()
        self.assertEqual(VendorOrderProgram.objects.count(), 0)

    def test_the_tender_is_recorded_when_the_site_has_one(self):
        # .update() to bypass _validate_program_link(): no Residential site may carry a
        # Program through save(), and this is the only way to reach the derivation
        # branch until O3's group raise makes it ordinary.
        program = Program.objects.create(
            program_type='OPEX', name='Mixed Tender', client_name='X',
            short_tender_code='MIX26', status='Active')
        Project.objects.filter(pk=self.project.pk).update(program=program)
        self.post()
        order = VendorOrder.objects.get()
        self.assertEqual([link.program for link in order.programs.all()], [program])
        self.assertEqual(order.sites.count(), 1)


# ---------------------------------------------------------------------------
# The readers O2d may not change — none of them may 500
# ---------------------------------------------------------------------------

class NullAnchorReaderTests(ScopeFixture):
    """Every reader of PaymentRequest.project named in O2d's pre-flight that this prompt
    is forbidden from changing. Each filters on `project`, so a NULL-anchor payment is
    silently EXCLUDED rather than rendered — these assert that the exclusion is clean and
    that no page breaks. Making such a payment VISIBLE on them is O5's and O6's work."""

    def setUp(self):
        self.order    = _bare_order(self)
        self.anchored = _make_order(self)
        _pay(self, self.anchored, '10000')
        self.siteless = _pay_siteless(self, '40000')

    def test_the_finance_dashboard_renders_and_counts_only_anchored_payments(self):
        response = _client(self.finance).get(reverse('dashboard_finance'))
        self.assertEqual(response.status_code, 200)
        # One anchored payment on the portfolio; the site-less one joins no project.
        self.assertEqual(response.context['total_payment_requests'], 1)
        self.assertEqual(response.context['total_payment_request_value'], Decimal('10000'))

    def test_the_ceo_dashboard_renders_and_counts_only_anchored_payments(self):
        response = _client(self.ceo).get(reverse('dashboard_ceo'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['fin_payment_requests_pending'], 1)

    def test_project_overview_renders_and_lists_only_its_own_payments(self):
        response = _client(self.pm).get(
            reverse('project_overview', args=[self.project.project_id]))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.siteless, list(response.context['payment_requests']))

    def test_my_documents_renders_for_the_scm_who_raised_it(self):
        response = _client(self.scm).get(reverse('my_documents'))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.siteless, list(response.context['pr_list']))

    def test_the_payment_detail_page_has_no_url_for_a_siteless_payment(self):
        # Its URL is /projects/<project_id>/payment-request/<pk>/. There is no project_id
        # to build it with, so the page is unreachable rather than broken. O6 owns this.
        response = _client(self.scm).get(reverse(
            'payment_request_detail', args=[self.project.project_id, self.siteless.pk]))
        self.assertEqual(response.status_code, 404)

    def test_confirm_refuses_a_siteless_payment_under_any_projects_url(self):
        response = _client(self.finance).post(reverse(
            'confirm_payment_request', args=[self.project.project_id, self.siteless.pk]),
            {'payment_date': '2026-09-23', 'payment_reference': 'UTR-1'})
        self.assertEqual(response.status_code, 404)
        self.siteless.refresh_from_db()
        self.assertEqual(self.siteless.status, PaymentRequest.APPROVED)

    def test_list_implausible_dates_does_not_raise_on_a_null_anchor(self):
        self.siteless.payment_date = date(1999, 1, 1)
        self.siteless.save(update_fields=['payment_date'])
        out = StringIO()
        call_command('list_implausible_dates', stdout=out)
        self.assertIn('PaymentRequest', out.getvalue())
