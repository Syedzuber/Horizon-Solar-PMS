"""
FIN-H-B3 — the Finance Head page (/finance/head/), its landing and its More-row link.

THE RULES PINNED HERE (rulings H2, H4, H6, H9, H12, H13, H17, H19, H21, H37; B3 answers
1-7):
  * Access (H12): Finance Head, CEO, Admin, System Admin; everyone else the 403 page;
    inactive refused; anonymous to the login page.
  * Landing (H13): the Finance Head lands here; plain Finance keeps Finance work; the CEO
    keeps the Site Dashboard.
  * More row: "Finance Head" for exactly the four allowed kinds of user.
  * Period: ?period=month|30d|fy, anything else -> month.
  * H21 / H37: no payment request -> "No payment requests in PMS yet.", no Residential net.
  * H4: a sum with blank amounts says "amount not set on N".
  * H6: sd_base.css, then the inline <style>, then sd_queue.css.
  * H2: no form on the page.
  * A fixed number of queries whatever the data size.
  * The tiles' short Indian figure (answer 6).

Real fixtures throughout (the FIN-H-B2 builders).

    python manage.py test projects.tests_finance_head_page --settings=solarpms.test_settings
"""
import re
from decimal import Decimal

from django.db import connection
from django.test import Client, SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .decorators import SITE_DASHBOARD_URL, get_post_login_url, get_user_dashboard
from .models import PaymentMilestone, PaymentRequest
from .tests_finance_head_data import FinanceHeadFixture
from .tests_finance_work import _profile
from .utils import INVOICE_DUE_TRIGGERS
from .views_finance_head import NO_MONEY_OUT, NO_PAYMENTS, _short_inr

URL = '/finance/head/'
FINANCE_WORK = '/finance/work/'
D = Decimal


class PageFixture(FinanceHeadFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.head = _profile('fh_head', 'Finance', is_finance_head=True)

    def client_for(self, profile):
        client = Client()
        client.force_login(profile.user)
        return client

    def get(self, profile, query=''):
        return self.client_for(profile).get(URL + query)

    def page(self, profile=None, query=''):
        """The page's own markup (between its markers), without base.html around it."""
        html = self.get(profile or self.head, query).content.decode()
        return html.split('<!-- fh:start -->', 1)[1].split('<!-- fh:end -->', 1)[0]


# ---------------------------------------------------------------------------
# Access (H12)
# ---------------------------------------------------------------------------

class AccessTests(PageFixture):

    def test_the_url_constant_is_the_route(self):
        from . import decorators
        self.assertEqual(reverse('finance_head'), URL)
        self.assertEqual(decorators.FINANCE_HEAD_URL, URL)

    def test_finance_head_ceo_admin_and_system_admin_open_it(self):
        for profile in (self.head, self.ceo, self.admin, self.sysadmin):
            with self.subTest(role=profile.role):
                self.assertEqual(self.get(profile).status_code, 200)

    def test_plain_finance_pm_scm_coordinator_and_bd_are_refused(self):
        for profile in (self.finance, self.pm, self.scm, self.coord, self.bd):
            with self.subTest(role=profile.role):
                response = self.get(profile)
                self.assertEqual(response.status_code, 403)
                self.assertContains(response, 'Return to your dashboard', status_code=403)

    def test_an_inactive_finance_head_is_refused(self):
        self.head.is_active = False
        self.head.save()
        self.assertEqual(self.get(self.head).status_code, 403)

    def test_a_finance_head_with_an_inactive_login_is_refused(self):
        client = self.client_for(self.head)
        self.head.user.is_active = False
        self.head.user.save()
        response = client.get(URL)
        self.assertNotEqual(response.status_code, 200)

    def test_anonymous_goes_to_the_login_page(self):
        response = Client().get(URL)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/login/')

    def test_get_only(self):
        self.assertEqual(self.client_for(self.head).post(URL).status_code, 405)


# ---------------------------------------------------------------------------
# Landing (H13)
# ---------------------------------------------------------------------------

class LandingTests(PageFixture):

    def login(self, profile):
        return Client().post('/login/', {'username': profile.user.username,
                                         'password': 'pw12345'})

    def test_the_finance_head_lands_here_after_login(self):
        response = self.login(self.head)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], URL)
        self.assertEqual(get_post_login_url(self.head.user), URL)

    def test_home_and_root_send_the_finance_head_here(self):
        client = self.client_for(self.head)
        response = client.get('/', follow=True)
        self.assertEqual(response.redirect_chain[-1][0], URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'<a href="{URL}" class="nav-link text-white small" title="Home">')

    def test_the_403_page_returns_the_finance_head_here(self):
        response = self.client_for(self.head).get('/dashboard/bd/')
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, f'href="{URL}"', status_code=403)

    def test_plain_finance_keeps_finance_work(self):
        self.assertEqual(get_user_dashboard(self.finance.user), FINANCE_WORK)
        self.assertEqual(self.login(self.finance)['Location'], FINANCE_WORK)

    def test_ceo_admin_and_system_admin_keep_the_site_dashboard(self):
        for profile in (self.ceo, self.admin, self.sysadmin):
            with self.subTest(role=profile.role):
                self.assertEqual(get_user_dashboard(profile.user), SITE_DASHBOARD_URL)

    def test_a_flag_left_on_a_non_finance_role_moves_nobody(self):
        # The Departments screens can change a role without clearing the flag.
        self.pm.is_finance_head = True
        self.pm.save()
        self.assertEqual(get_user_dashboard(self.pm.user), SITE_DASHBOARD_URL)


# ---------------------------------------------------------------------------
# More row
# ---------------------------------------------------------------------------

class MoreRowTests(PageFixture):

    def finance_links(self, profile):
        response = self.client_for(profile).get(SITE_DASHBOARD_URL)
        self.assertEqual(response.status_code, 200)
        groups = {g['label']: g['links'] for g in response.context['more_groups']}
        return {link['label']: link['href'] for link in groups.get('Finance', [])}

    def test_shown_to_the_four_allowed_users(self):
        for profile in (self.head, self.ceo, self.admin, self.sysadmin):
            with self.subTest(role=profile.role):
                self.assertEqual(self.finance_links(profile).get('Finance Head'), URL)

    def test_absent_for_plain_finance_and_pm(self):
        for profile in (self.finance, self.pm):
            with self.subTest(role=profile.role):
                self.assertNotIn('Finance Head', self.finance_links(profile))

    def test_the_page_links_to_finance_work_and_the_site_dashboard(self):
        html = self.page()
        self.assertIn(f'href="{FINANCE_WORK}" data-finance-work', html)
        self.assertIn(f'href="{SITE_DASHBOARD_URL}" data-site-list', html)


# ---------------------------------------------------------------------------
# Period
# ---------------------------------------------------------------------------

class PeriodTests(PageFixture):

    def active(self, query):
        response = self.get(self.head, query)
        return response.context['period'], response.content.decode()

    def test_fy_and_30d_render_their_labels(self):
        for query, value, label in (('?period=fy', 'fy', 'FY to date'),
                                    ('?period=30d', '30d', 'Last 30 days')):
            with self.subTest(period=value):
                period, html = self.active(query)
                self.assertEqual(period, value)
                self.assertIn(f'aria-current="true" data-period="{value}">{label}</a>', html)
                self.assertIn(f'<p class="sub">{label}:', html)

    def test_bogus_and_missing_fall_back_to_month(self):
        for query in ('?period=bogus', ''):
            with self.subTest(query=query):
                period, html = self.active(query)
                self.assertEqual(period, 'month')
                self.assertIn('aria-current="true" data-period="month">This month</a>', html)

    def test_money_in_is_labelled_recorded(self):
        html = self.page()
        self.assertIn('Recorded ', html)
        self.assertNotIn('Received on', html)


# ---------------------------------------------------------------------------
# Empty outgoing (H21, H37) and with payments
# ---------------------------------------------------------------------------

class OutgoingTests(PageFixture):

    def test_no_payment_requests_reads_the_empty_message_and_hides_the_net(self):
        site = self.site()
        self.received(site, 'M1', '50000', self.today)
        html = self.page()
        self.assertIn(NO_PAYMENTS, html)
        self.assertIn('data-outgoing-empty', html)
        self.assertIn(NO_MONEY_OUT, html)
        self.assertNotIn('data-net', html)
        self.assertNotIn('data-out-bar', html)
        # No bill waiting either: the bills row stays inside the empty message.
        self.assertNotIn('data-row="bills"', html)
        # Money out, To pay and Awaiting tiles all read the message, never ₹0.
        self.assertEqual(len(re.findall(r'<span class="v msg" data-empty>' + re.escape(NO_PAYMENTS),
                                        html)), 3)
        # RESCO and CAPEX cards' money-out lines read it too (answer 7).
        for key in ('OPEX', 'CAPEX'):
            card = html.split(f'data-card="{key}"', 1)[1].split('</div>', 1)[0]
            self.assertIn(f'<dt>Paid</dt><dd>{NO_PAYMENTS}</dd>', card)

    def test_an_unforwarded_bill_shows_even_with_no_payment_requests(self):
        # B3 follow-up answer 2: the bill comes from a bill approval, not a payment, so
        # H21's empty message does not hide it; everything else in the panel still does.
        self.bill(self.site(), '210000')
        html = self.page()
        outgoing = html.split('id="outgoing"', 1)[1].split('</section>', 1)[0]
        self.assertIn(f'data-outgoing-empty>{NO_PAYMENTS}', outgoing)
        bills = outgoing.split('data-row="bills"', 1)[1].split('</li>', 1)[0]
        self.assertIn('Approved bills not yet sent to Finance', bills)
        self.assertIn('<span class="amt">₹2,10,000</span>', bills)
        self.assertIn('1 bill', bills)
        for row in ('awaiting', 'on_hold', 'to_pay', 'paid'):
            self.assertNotIn(f'data-row="{row}"', outgoing)
        self.assertNotIn('payables-ageing', outgoing)

    def test_with_payments_the_panel_shows_amounts_and_the_net(self):
        site = self.site()
        self.received(site, 'M1', '300000', self.today)
        self.pay(sites=[site], amount='120000')
        self.pay(sites=[site], status=PaymentRequest.CONFIRMED, amount='100000',
                 approved='100000', payment_date=self.today)
        html = self.page()
        self.assertNotIn('data-outgoing-empty', html)
        self.assertNotIn(NO_MONEY_OUT, html)
        outgoing = html.split('id="outgoing"', 1)[1].split('</section>', 1)[0]
        self.assertIn('₹1,20,000', outgoing)
        self.assertIn('₹1,00,000', outgoing)
        residential = html.split('data-card="Residential"', 1)[1].split('</div>', 1)[0]
        self.assertIn('data-net>₹2,00,000</dd>', residential)
        resco = html.split('data-card="OPEX"', 1)[1].split('</div>', 1)[0]
        self.assertIn('<h3>RESCO</h3>', resco)
        self.assertIn('data-in>Not in PMS yet</dd>', resco)
        self.assertIn('Not comparable', resco)
        self.assertIn('data-out-bar', html)

    def test_the_bills_row_is_a_link_only_for_a_viewer_of_the_approvals_list(self):
        site = self.site()
        self.bill(site, '5000')
        self.pay(sites=[site])
        self.assertIn('data-bills-link', self.page(self.ceo))
        head_html = self.page(self.head)
        self.assertIn('Approved bills not yet sent to Finance', head_html)
        self.assertNotIn('data-bills-link', head_html)

    def test_capex_type_link_only_when_capex_has_a_payment(self):
        site = self.site()
        self.pay(sites=[site])
        self.assertNotIn('tab=CAPEX', self.page())
        self.pay(sites=[self.site(project_type='CAPEX')], ptype='CAPEX')
        self.assertIn('tab=CAPEX&amp;status=pending_approval', self.page())


# ---------------------------------------------------------------------------
# Null amounts (H4)
# ---------------------------------------------------------------------------

class NullAmountTests(PageFixture):

    def test_two_due_milestones_without_amounts_say_so(self):
        for _ in range(2):
            self.milestone(self.site(), 'M1')          # M1 Pending on an active site: due
        html = self.page()
        due = html.split('data-row="due"', 1)[1].split('</li>', 1)[0]
        self.assertIn('₹0', due)
        self.assertIn('amount not set on 2', due)
        tile = html.split('data-tile="due"', 1)[1].split('</a>', 1)[0]
        self.assertIn('2 due', tile)
        self.assertIn('amount not set on 2', tile)


# ---------------------------------------------------------------------------
# Look (H6) and read only (H2)
# ---------------------------------------------------------------------------

class MarkupTests(PageFixture):

    def test_css_load_order_base_then_inline_then_queue(self):
        html = self.get(self.head).content.decode()
        base = html.index('projects/css/sd_base.css?v=20261116')
        inline = html.index('<style>', base)
        queue = html.index('projects/css/sd_queue.css?v=20261116')
        self.assertLess(base, inline)
        self.assertLess(inline, queue)
        self.assertNotIn('<link', html[base + 1:inline])

    def test_the_page_has_no_form(self):
        html = self.page()
        self.assertNotIn('<form', html)
        self.assertNotIn('method="post"', html.lower())

    def test_a_tile_shows_the_short_figure_with_the_full_one_as_title(self):
        site = self.site()
        self.received(site, 'M1', '1984000', self.today)
        tile = self.page().split('data-tile="in"', 1)[1].split('</div>', 1)[0]
        self.assertIn('title="₹19,84,000">₹19.84 L</span>', tile)


# ---------------------------------------------------------------------------
# Query count
# ---------------------------------------------------------------------------

class QueryCountTests(PageFixture):

    def add_sites(self, n):
        for _ in range(n):
            s = self.site(contract_value=D('500000'))
            self.milestone(s, 'M1')
            self.milestone(s, 'M2')
            self.trigger(s, INVOICE_DUE_TRIGGERS['M2'])
            self.milestone(s, 'M3', status=PaymentMilestone.INVOICED, invoice_amount=D('1000'),
                           invoice_date=self.today)

    def count(self):
        client = self.client_for(self.head)
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(URL)
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries)

    def test_three_and_thirty_sites_zero_and_ten_payments_cost_the_same(self):
        self.add_sites(3)
        small = self.count()
        self.add_sites(27)
        large = self.count()
        sites = list(self.site() for _ in range(2))
        for i in range(10):
            status = (PaymentRequest.CONFIRMED if i % 3 == 0 else
                      PaymentRequest.APPROVED if i % 3 == 1 else
                      PaymentRequest.PENDING_APPROVAL)
            self.pay(sites=sites, ptype='OPEX' if i % 2 else 'Residential', status=status,
                     approved='100' if status != PaymentRequest.PENDING_APPROVAL else None,
                     payment_date=self.today if status == PaymentRequest.CONFIRMED else None)
        with_payments = self.count()
        print(f'\nFIN-H-B3 queries: 3 sites/0 payments={small}, 30 sites/0 payments={large}, '
              f'30 sites/10 payments={with_payments}')
        self.assertEqual(small, large)
        self.assertEqual(large, with_payments)


# ---------------------------------------------------------------------------
# The tiles' short figure (answer 6)
# ---------------------------------------------------------------------------

class ShortInrTests(SimpleTestCase):

    def test_cases(self):
        for value, expected in (
                (D('0'), '₹0'),
                (D('45000'), '₹45,000'),
                (D('99999.40'), '₹99,999'),
                (D('100000'), '₹1.00 L'),
                (D('1984000'), '₹19.84 L'),
                (D('9999999.99'), '₹1.00 Cr'),
                (D('10000000'), '₹1.00 Cr'),
                (D('122000000'), '₹12.20 Cr'),
                (D('12200000'), '₹1.22 Cr'),
                (D('-1984000'), '−₹19.84 L'),
                (D('-45000'), '−₹45,000'),
                (None, None)):
            with self.subTest(value=value):
                self.assertEqual(_short_inr(value), expected)
