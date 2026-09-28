"""
S2 — the CEO dashboard's Tenders view, top of page: the four-bucket payment strip,
the kWp coverage and "Not shown: CAPEX" header lines, the no-context view hiding test
data, and a Refresh link that keeps the context.

THE RULES PINNED HERE:
  * payments.ceo_payment_strip() scopes like payment_counts() and ALSO drops a request
    whose order names only test sites. A request with one real site, or with no site,
    is kept.
  * awaiting / on hold sum what was asked (`amount`); approved / paid sum the approved
    figure (effective_amount). REJECTED is on no card. "Paid this month" is the IST
    month of `payment_date`.
  * payment_counts() is unchanged: it still counts every row, test sites included.

Real fixtures throughout: every assertion reads rows written to the test database.

    python manage.py test projects.tests_ceo_tenders_strip --settings=solarpms.test_settings
"""
import re
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    PaymentRequest, PaymentRequestHold, Program, Project, Vendor, VendorOrder,
    VendorOrderProgram, VendorOrderSite,
)
from .payments import PaymentTotals, StripBucket, ceo_payment_strip, payment_counts
from .tests_ceo_tenders_scope import _profile, _project
from .tests_vendor_order_raise import _client
from .views import _format_inr, _get_ceo_dashboard_context


def _text(response):
    """The page's visible text, tags stripped and whitespace collapsed."""
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', response.content.decode()))


class StripFixture(TestCase):
    """Real OPEX sites R1/R2, test sites T1/T2, one tender, a vendor, SCM and an
    approver. Payments are added by each subclass through _pay()."""

    @classmethod
    def setUpTestData(cls):
        cls.ceo      = _profile('s2_ceo', 'CEO')
        cls.scm      = _profile('s2_scm', 'SCM')
        cls.approver = _profile('s2_approver', 'Finance')
        cls.vendor   = Vendor.objects.create(name='Surya Modules', contact_person='S',
                                             phone='9000000001')
        cls.tender   = Program.objects.create(
            name='S2 Tender', program_type='OPEX', client_name='C',
            status='Active', short_tender_code='S2T')
        cls.r1 = _project('S2-R1', 'OPEX', program=cls.tender)
        cls.r2 = _project('S2-R2', 'OPEX', program=cls.tender)
        cls.t1 = _project('S2-T1', 'OPEX', program=cls.tender, is_test=True)
        cls.t2 = _project('S2-T2', 'OPEX', program=cls.tender, is_test=True)
        cls.today = timezone.localdate()

    @classmethod
    def _pay(cls, sites, amount, status=PaymentRequest.PENDING_APPROVAL, *,
             approved=None, paid_on=None, raised_days_ago=0, project_type='OPEX',
             program=None, reason=''):
        """One order sized against `sites` (and `program`), one payment on it."""
        order = VendorOrder.objects.create(
            vendor=cls.vendor, project_type=project_type,
            total_amount=Decimal('10000000'), created_by=cls.scm)
        for site in sites:
            VendorOrderSite.objects.create(order=order, project=site)
        if program is not None:
            VendorOrderProgram.objects.create(order=order, program=program)
        payment = PaymentRequest.objects.create(
            vendor_order=order, project=sites[0] if sites else None, vendor=cls.vendor,
            amount=Decimal(amount), requested_by=cls.scm.user, status=status,
            approved_amount=Decimal(approved) if approved else None,
            payment_date=paid_on, decision_reason=reason)
        # requested_date is auto_now_add, so the age is set after the insert.
        PaymentRequest.objects.filter(pk=payment.pk).update(
            requested_date=timezone.now() - timedelta(days=raised_days_ago))
        return payment

    @classmethod
    def _hold(cls, payment, held_days_ago, answered=False):
        hold = PaymentRequestHold.objects.create(
            payment_request=payment, reason='Justify the rate', held_by=cls.approver,
            **({'response': 'Rate is the tender rate', 'responded_by': cls.scm,
                'responded_at': timezone.now()} if answered else {}))
        PaymentRequestHold.objects.filter(pk=hold.pk).update(
            held_at=timezone.now() - timedelta(days=held_days_ago))
        return hold


# ---------------------------------------------------------------------------
# a) each bucket's count and sum; h) payment_counts unchanged
# ---------------------------------------------------------------------------

class BucketTests(StripFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        month_start = cls.today.replace(day=1)
        # Awaiting: 1,50,000 raised 5 days ago; 50,000 raised 2 days ago that carries an
        # approved ceiling of 30,000 from before a hold (O4c) — awaiting sums the 50,000.
        cls._pay([cls.r1], '150000', raised_days_ago=5)
        cls._pay([cls.r2], '50000', approved='30000', raised_days_ago=2)
        # On hold: 40,000. Its answered hold is 10 days old, the open one 3 — the card
        # reads the open one, and the two holds must not count the 40,000 twice.
        held = cls._pay([cls.r1], '40000', PaymentRequest.ON_HOLD)
        cls._hold(held, 10, answered=True)
        cls._hold(held, 3)
        # Approved: a partial (50,000 of 1,00,000) and a full 20,000.
        cls._pay([cls.r1], '100000', PaymentRequest.APPROVED, approved='50000')
        cls._pay([cls.r2], '20000', PaymentRequest.APPROVED, approved='20000')
        # Paid this month: a partial (60,000 of 80,000) on the 1st, a full 30,000 today.
        cls._pay([cls.r1], '80000', PaymentRequest.CONFIRMED, approved='60000',
                 paid_on=month_start)
        cls._pay([cls.r2], '30000', PaymentRequest.CONFIRMED, approved='30000',
                 paid_on=cls.today)
        # On no card: paid last month, rejected, and a CAPEX request outside the scope.
        cls._pay([cls.r1], '70000', PaymentRequest.CONFIRMED, approved='70000',
                 paid_on=month_start - timedelta(days=1))
        cls._pay([cls.r1], '999', PaymentRequest.REJECTED, reason='Duplicate')
        cls._pay([], '5000', project_type='CAPEX')
        # Test data only — counted by payment_counts, never by the strip.
        cls._pay([cls.t1], '700000')

    def test_each_bucket(self):
        strip = ceo_payment_strip(['OPEX'], self.today)
        self.assertEqual(strip['awaiting'],   StripBucket(2, Decimal('200000'), 5, None))
        self.assertEqual(strip['on_hold'],    StripBucket(1, Decimal('40000'), 3, None))
        self.assertEqual(strip['approved'],   StripBucket(2, Decimal('70000'), None, None))
        self.assertEqual(strip['paid_month'], StripBucket(2, Decimal('90000'), None, 1))

    def test_tenders_page_renders_the_four_cards(self):
        text = _text(_client(self.ceo).get(reverse('dashboard_ceo'), {'context': 'tenders'}))
        self.assertIn('₹2,00,000 Awaiting approval 2 requests · oldest raised 5 d ago', text)
        self.assertIn('₹40,000 On hold — SCM to justify 1 request · held 3 d', text)
        self.assertIn('₹70,000 Approved, not yet paid 2 requests', text)
        self.assertIn('₹90,000 Paid this month 2 requests · 1 partial', text)
        # D3: no contract or client money on Tenders.
        self.assertNotIn('Total Client Contract Value', text)
        self.assertNotIn('Payment Pending from Client', text)

    def test_payment_counts_is_unchanged(self):
        """h) The full dict, test-site and rejected rows included, exactly as O6 counts."""
        z = Decimal('0')
        self.assertEqual(payment_counts(['OPEX']), {
            PaymentRequest.PENDING_APPROVAL: PaymentTotals(
                3, Decimal('880000'), Decimal('900000')),   # 1,50,000 + 30,000 ceiling + 7,00,000
            PaymentRequest.ON_HOLD:   PaymentTotals(1, Decimal('40000'), Decimal('40000')),
            PaymentRequest.APPROVED:  PaymentTotals(2, Decimal('70000'), Decimal('120000')),
            PaymentRequest.CONFIRMED: PaymentTotals(3, Decimal('160000'), Decimal('180000')),
            PaymentRequest.REJECTED:  PaymentTotals(1, Decimal('999'), Decimal('999')),
        })
        self.assertEqual(payment_counts(['CAPEX'])[PaymentRequest.PENDING_APPROVAL],
                         PaymentTotals(1, Decimal('5000'), Decimal('5000')))
        self.assertEqual(payment_counts(['CAPEX'])[PaymentRequest.APPROVED],
                         PaymentTotals(0, z, z))


# ---------------------------------------------------------------------------
# b) which requests are test data
# ---------------------------------------------------------------------------

class TestDataExclusionTests(StripFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls._pay([cls.t1, cls.t2], '1000')         # only test sites: dropped
        cls._pay([cls.t1, cls.r1], '200')          # a test AND a real site: kept
        cls._pay([], '30', program=cls.tender)     # no site at all: kept

    def test_only_all_test_requests_are_dropped(self):
        awaiting = ceo_payment_strip(['OPEX'], self.today)['awaiting']
        self.assertEqual((awaiting.count, awaiting.amount), (2, Decimal('230')))

    def test_a_zero_card_says_so(self):
        text = _text(_client(self.ceo).get(reverse('dashboard_ceo'), {'context': 'tenders'}))
        self.assertIn('₹0 On hold — SCM to justify No requests', text)
        self.assertIn('₹0 Paid this month No requests', text)


# ---------------------------------------------------------------------------
# c) the month boundary
# ---------------------------------------------------------------------------

class PaidMonthBoundaryTests(StripFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # `today` is passed in fixed, so the boundary is tested on a known month.
        cls._pay([cls.r1], '11000', PaymentRequest.CONFIRMED, approved='11000',
                 paid_on=date(2026, 9, 30))
        cls._pay([cls.r1], '22000', PaymentRequest.CONFIRMED, approved='22000',
                 paid_on=date(2026, 10, 1))

    def test_last_day_of_the_previous_month_is_excluded(self):
        paid = ceo_payment_strip(['OPEX'], date(2026, 10, 1))['paid_month']
        self.assertEqual((paid.count, paid.amount), (1, Decimal('22000')))

    def test_the_previous_month_counts_only_in_its_own_month(self):
        paid = ceo_payment_strip(['OPEX'], date(2026, 9, 15))['paid_month']
        self.assertEqual((paid.count, paid.amount), (1, Decimal('11000')))

    def test_december_ends_at_the_new_year(self):
        self._pay([self.r1], '33000', PaymentRequest.CONFIRMED, approved='33000',
                  paid_on=date(2026, 12, 31))
        self._pay([self.r1], '44000', PaymentRequest.CONFIRMED, approved='44000',
                  paid_on=date(2027, 1, 1))
        paid = ceo_payment_strip(['OPEX'], date(2026, 12, 15))['paid_month']
        self.assertEqual((paid.count, paid.amount), (1, Decimal('33000')))


# ---------------------------------------------------------------------------
# d) Residential keeps its cards; the Refresh link (T6)
# ---------------------------------------------------------------------------

class ResidentialAndRefreshTests(StripFixture):

    def test_residential_still_renders_the_old_four_cards(self):
        response = _client(self.ceo).get(reverse('dashboard_ceo'), {'context': 'residential'})
        self.assertIsNone(response.context['payment_strip'])
        text = _text(response)
        for label in ('Payment Requests Pending', 'Vendor Payments Outstanding',
                      'Total Client Contract Value', 'Payment Pending from Client'):
            self.assertIn(label, text)
        self.assertNotIn('Awaiting approval', text)

    @staticmethod
    def _refresh_href(response):
        """The Refresh button's href alone. The context switcher links to
        ?context=tenders as well, so a page-wide search would pass without the fix."""
        match = re.search(r'<a href="([^"]*)"\s+class="[^"]*"\s+title="Reload the dashboard',
                          response.content.decode())
        return match.group(1)

    def test_refresh_keeps_the_context(self):
        url = reverse('dashboard_ceo')
        tenders = _client(self.ceo).get(url, {'context': 'tenders'})
        self.assertEqual(self._refresh_href(tenders), f'{url}?context=tenders')
        plain = _client(self.ceo).get(url)
        self.assertEqual(self._refresh_href(plain), url)


# ---------------------------------------------------------------------------
# e) kWp coverage; f) the CAPEX line
# ---------------------------------------------------------------------------

def _site(project_id, kwp, project_type='OPEX', status='Active', **extra):
    """A site whose dc_capacity_kw may be None — _project() cannot make one."""
    return Project.objects.create(
        project_id=project_id, customer_name=project_id, customer_phone='9876543210',
        site_address='1 Sun Road', city='Lucknow', project_type=project_type,
        dc_capacity_kw=kwp, status=status, **extra)


class HeaderTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.ceo = _profile('s2_head_ceo', 'CEO')
        _site('S2-K1', Decimal('100.00'), activated_at=timezone.now())
        _site('S2-K2', Decimal('25.50'), status='Draft')

    def _tenders_text(self):
        return _text(_client(self.ceo).get(reverse('dashboard_ceo'), {'context': 'tenders'}))

    def test_no_coverage_note_when_every_site_has_kwp(self):
        text = self._tenders_text()
        self.assertIn('2 sites · 125.5 kWp · 1 activated', text)
        self.assertNotIn(' of 2 sites)', text)

    def test_coverage_note_when_some_sites_have_none(self):
        _site('S2-K3', None)                     # null is unknown
        _site('S2-K4', Decimal('0'))             # so is zero
        self.assertIn('4 sites · 125.5 kWp (2 of 4 sites) · 1 activated', self._tenders_text())

    def test_capex_line_follows_live_capex_projects(self):
        self.assertNotIn('Not shown: CAPEX', self._tenders_text())
        capex = _site('S2-CPX', Decimal('500'), project_type='CAPEX')
        _site('S2-CPX-T', Decimal('500'), project_type='CAPEX', is_test=True)
        _site('S2-CPX-X', Decimal('500'), project_type='CAPEX', status='Cancelled')
        self.assertIn('Not shown: CAPEX · 1 project', self._tenders_text())
        Project.objects.filter(pk=capex.pk).update(status='Cancelled')
        self.assertNotIn('Not shown: CAPEX', self._tenders_text())


# ---------------------------------------------------------------------------
# g) the no-context view
# ---------------------------------------------------------------------------

class NoContextTests(TestCase):

    def test_no_context_hides_test_projects_and_keeps_capex(self):
        _site('S2-NC-REAL', Decimal('5'))
        _site('S2-NC-TEST', Decimal('5'), is_test=True)
        _site('S2-NC-CPX', Decimal('5'), project_type='CAPEX')
        ids = {c['project'].project_id for c in _get_ceo_dashboard_context(None)['project_cards']}
        self.assertEqual(ids, {'S2-NC-REAL', 'S2-NC-CPX'})


class InrFormatTests(TestCase):

    def test_indian_grouping(self):
        self.assertEqual(_format_inr(Decimal('0')), '0')
        self.assertEqual(_format_inr(Decimal('999')), '999')
        self.assertEqual(_format_inr(Decimal('50000.00')), '50,000')
        self.assertEqual(_format_inr(Decimal('200000')), '2,00,000')
        self.assertEqual(_format_inr(Decimal('12345678.50')), '1,23,45,679')
        self.assertEqual(_format_inr(None), '0')
