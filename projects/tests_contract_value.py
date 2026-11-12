"""FIN-M-B2 — contract value on the project page's milestone card (ruling R12).

WHY THIS FILE EXISTS
--------------------
Ruling R12: "Contract value: CEO + Finance everywhere. The PM keeps it on the create/edit
form they enter it on; BD keeps it in the amount-check error message. Not SCM, not the
site PM elsewhere. New dedicated helper in permissions.py (the money-portfolio audience is
too wide)." FIN-M-B2 rulings (Zuber, 12 Nov 2026):
* The helper, user_can_see_contract_value(), admits CEO, Finance, Admin and System Admin;
  role first, then a missing or deleted project is refused; no project-type term.
* The milestone card (Residential only, R2) gets a footer for those roles: "Contract value
  ₹X" and "M1 + M2 + M3 = ₹Y · matches" / "· ₹Z short of contract" / "· ₹Z over contract",
  or "M1–M3 amounts not set", or "Contract value not set" (followed by "M1 + M2 + M3 = ₹Y"
  with no verdict when all three are set). "matches" is green, short/over red. Shown in
  the "No milestones yet" state too.
* The card's own role list is NOT changed: System Admin still does not see the card, so
  the footer cannot show for them on this page even though the helper admits them.
* A milestone amount of 0 reads ₹0 on the card, "Amount TBD" only when blank — otherwise
  the new sum line (which counts 0) would contradict the row.

The helper tests import inside the test bodies on purpose: run against the code before
FIN-M-B2, each fails on its own instead of the whole module failing to import.

Run with:
    python manage.py test projects.tests_contract_value --settings=solarpms.test_settings
"""
import re
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser, User
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .models import PaymentMilestone, Project, ProjectPhase, Task

FOOTER = 'id="contract-value-footer"'


def _profile(username, role):
    """A post_save signal creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title(), last_name='Test')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.save()
    return profile


def _client(profile):
    client = Client(SERVER_NAME='localhost')
    client.force_login(profile.user)
    return client


def _text(html):
    """Visible text with tags dropped and whitespace collapsed."""
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html)).strip()


def _card(html):
    """The milestone card's markup: from its title to the next card's title. Empty when
    the card is not rendered."""
    start = html.find('Payment Milestones')
    if start == -1:
        return ''
    return html[start:html.find('Recent Activity', start)]


class ContractValueBase(TestCase):
    """One Active Residential project, contract ₹3,00,000, M1-M3 at ₹1,00,000 each, and
    one person in every role that can see the project."""

    def setUp(self):
        self.ceo = _profile('cv_ceo', 'CEO')
        self.finance = _profile('cv_fin', 'Finance')
        self.admin = _profile('cv_admin', 'Admin')
        self.sysadmin = _profile('cv_sysadmin', 'System Admin')
        self.scm = _profile('cv_scm', 'SCM')
        self.bd = _profile('cv_bd', 'BD')
        self.pm = _profile('cv_pm', 'PM')
        self.pc = _profile('cv_pc', 'Project Coordinator')
        self.se = _profile('cv_se', 'Site Engineer')
        self.designer = _profile('cv_design', 'Design')
        self.project = Project.objects.create(
            customer_name='Contract Value', status='Active', project_type='Residential',
            customer_phone='9876543210', site_address='1 Sun Road', city='Lucknow',
            state='Uttar Pradesh', dc_capacity_kw=Decimal('5.00'),
            contract_value=Decimal('300000.00'), assigned_pm=self.pm,
            assigned_design=self.designer, cascade_scheduling=False)
        self.project.coordinators.add(self.pc)
        phase = ProjectPhase.objects.create(project=self.project, phase_name='Install',
                                            phase_order=1)
        Task.objects.create(phase=phase, task_name='Mount structure', task_order=1,
                            assigned_role=Task.SITE_ENGINEER, assigned_to=self.se)
        self.ms = {}
        for name in ('M1', 'M2', 'M3'):
            self.ms[name] = PaymentMilestone.objects.create(
                project=self.project, milestone_name=name, amount=Decimal('100000.00'),
                created_by=self.pm)

    def page(self, profile, project=None):
        project = project or self.project
        response = _client(profile).get(reverse('project_overview',
                                                args=[project.project_id]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def footer(self, profile=None):
        """The footer's visible text for `profile` (CEO by default); '' when absent."""
        html = self.page(profile or self.ceo)
        start = html.find(FOOTER)
        if start == -1:
            return ''
        # From the footer's opening tag; it is the last thing in the card, so its text runs
        # to the next card's title.
        start = html.rfind('<', 0, start)
        return _text(html[start:html.find('Recent Activity', start)])

    def set_amounts(self, m1, m2, m3):
        for name, amount in (('M1', m1), ('M2', m2), ('M3', m3)):
            PaymentMilestone.objects.filter(pk=self.ms[name].pk).update(amount=amount)


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------

class HelperTests(ContractValueBase):

    def test_ceo_finance_admin_and_system_admin_are_admitted(self):
        from .permissions import user_can_see_contract_value
        for profile in (self.ceo, self.finance, self.admin, self.sysadmin):
            with self.subTest(role=profile.role):
                self.assertTrue(user_can_see_contract_value(profile.user, self.project))

    def test_every_other_role_is_refused_including_the_sites_own_pm(self):
        from .permissions import user_can_see_contract_value
        for profile in (self.pm, self.pc, self.scm, self.se, self.designer, self.bd):
            with self.subTest(role=profile.role):
                self.assertFalse(user_can_see_contract_value(profile.user, self.project))

    def test_a_deleted_or_missing_project_is_refused(self):
        from .permissions import user_can_see_contract_value
        self.project.is_deleted = True
        self.assertFalse(user_can_see_contract_value(self.ceo.user, self.project))
        self.assertFalse(user_can_see_contract_value(self.ceo.user, None))

    def test_a_user_with_no_profile_is_refused(self):
        from .permissions import user_can_see_contract_value
        self.assertFalse(user_can_see_contract_value(AnonymousUser(), self.project))

    def test_no_project_type_term(self):
        """Ruling: the helper does not gate on type; the card's template does."""
        from .permissions import user_can_see_contract_value
        self.project.project_type = 'OPEX'
        self.assertTrue(user_can_see_contract_value(self.finance.user, self.project))

    def test_costs_no_query(self):
        from .permissions import user_can_see_contract_value
        user = User.objects.get(pk=self.finance.user.pk)
        user.profile   # cached, as request.user.profile is by the time the view asks
        with self.assertNumQueries(0):
            user_can_see_contract_value(user, self.project)


# ---------------------------------------------------------------------------
# Who sees the footer on the card
# ---------------------------------------------------------------------------

class CardAudienceTests(ContractValueBase):

    def test_ceo_and_finance_see_contract_value_on_the_card(self):
        for profile in (self.ceo, self.finance):
            with self.subTest(role=profile.role):
                card = _text(_card(self.page(profile)))
                self.assertIn('Contract value ₹3,00,000', card)
                self.assertIn('M1 + M2 + M3 = ₹3,00,000 · matches', card)

    def test_admin_sees_it_too(self):
        card = _text(_card(self.page(self.admin)))
        self.assertIn('Contract value ₹3,00,000', card)

    def test_pm_and_bd_see_the_card_but_not_the_contract_value(self):
        """R12 keeps the figure for them only on their own form and in BD's error."""
        for profile in (self.pm, self.bd):
            with self.subTest(role=profile.role):
                html = self.page(profile)
                self.assertIn('Payment Milestones', html)   # the card is still theirs
                self.assertNotIn(FOOTER, html)
                self.assertNotIn('Contract value', _text(_card(html)))
                self.assertNotIn('3,00,000', html)

    def test_roles_without_the_card_never_see_the_figure(self):
        for profile in (self.scm, self.pc, self.se, self.designer):
            with self.subTest(role=profile.role):
                html = self.page(profile)
                self.assertNotIn(FOOTER, html)
                self.assertNotIn('3,00,000', html)

    def test_system_admin_sees_no_footer_because_the_card_role_list_is_unchanged(self):
        """The helper admits System Admin, but the card is not shown to them (ruling:
        the card's role list is not touched), so neither is the footer."""
        html = self.page(self.sysadmin)
        self.assertNotIn('Payment Milestones', html)
        self.assertNotIn(FOOTER, html)

    def test_no_footer_on_a_non_residential_project(self):
        """R2: the card is Residential-only; the footer goes with it."""
        Project.objects.filter(pk=self.project.pk).update(project_type='CAPEX')
        html = self.page(self.ceo)
        self.assertNotIn(FOOTER, html)
        self.assertNotIn('3,00,000', html)


# ---------------------------------------------------------------------------
# The sum line
# ---------------------------------------------------------------------------

class SumLineTests(ContractValueBase):

    def test_amounts_equal_to_contract_read_matches(self):
        self.assertEqual(self.footer(),
                         'Contract value ₹3,00,000 M1 + M2 + M3 = ₹3,00,000 · matches')

    def test_amounts_below_contract_read_short(self):
        self.set_amounts(Decimal('100000'), Decimal('100000'), Decimal('90000'))
        self.assertIn('M1 + M2 + M3 = ₹2,90,000 · ₹10,000 short of contract', self.footer())

    def test_amounts_above_contract_read_over(self):
        self.set_amounts(Decimal('100000'), Decimal('100000'), Decimal('110000'))
        self.assertIn('M1 + M2 + M3 = ₹3,10,000 · ₹10,000 over contract', self.footer())

    def test_a_paise_difference_is_shown_not_rounded_to_zero(self):
        self.set_amounts(Decimal('100000'), Decimal('100000'), Decimal('99999.60'))
        self.assertIn('₹0.40 short of contract', self.footer())

    def test_a_blank_amount_reads_amounts_not_set(self):
        self.set_amounts(Decimal('100000'), None, Decimal('100000'))
        self.assertEqual(self.footer(), 'Contract value ₹3,00,000 M1–M3 amounts not set')

    def test_a_missing_milestone_row_reads_amounts_not_set(self):
        self.ms['M3'].delete()
        self.assertEqual(self.footer(), 'Contract value ₹3,00,000 M1–M3 amounts not set')

    def test_zero_counts_as_a_set_amount(self):
        """BD enters 0 for a milestone that does not apply."""
        Project.objects.filter(pk=self.project.pk).update(contract_value=Decimal('200000'))
        self.set_amounts(Decimal('100000'), Decimal('100000'), Decimal('0'))
        self.assertIn('M1 + M2 + M3 = ₹2,00,000 · matches', self.footer())

    def test_shown_in_the_no_milestones_yet_state(self):
        PaymentMilestone.objects.filter(project=self.project).delete()
        html = self.page(self.ceo)
        self.assertIn('No milestones yet', html)
        self.assertIn('Contract value ₹3,00,000 M1–M3 amounts not set',
                      _text(_card(html)))

    def test_matches_is_green_and_a_mismatch_is_red(self):
        """Ruling: "matches" in green; short and over in red."""
        html = self.page(self.ceo)
        self.assertIn('<div class="text-success">M1 + M2 + M3 = ₹3,00,000 · matches</div>',
                      html)
        self.set_amounts(Decimal('100000'), Decimal('100000'), Decimal('90000'))
        self.assertIn('<div class="text-danger">M1 + M2 + M3 = ₹2,90,000 ·',
                      self.page(self.ceo))

    def test_a_blank_contract_value_with_all_amounts_set_shows_the_total_no_verdict(self):
        """Ruling: the total is still shown, with nothing to compare it against."""
        Project.objects.filter(pk=self.project.pk).update(contract_value=None)
        self.assertEqual(self.footer(), 'Contract value not set M1 + M2 + M3 = ₹3,00,000')

    def test_a_blank_contract_value_and_a_blank_amount_reads_not_set_only(self):
        Project.objects.filter(pk=self.project.pk).update(contract_value=None)
        self.set_amounts(Decimal('100000'), None, Decimal('100000'))
        self.assertEqual(self.footer(), 'Contract value not set')


# ---------------------------------------------------------------------------
# The zero display on the card's rows
# ---------------------------------------------------------------------------

class ZeroAmountDisplayTests(ContractValueBase):

    def setUp(self):
        super().setUp()
        PaymentMilestone.objects.filter(project=self.project,
                                        milestone_name__in=('M2', 'M3')).delete()

    def test_amount_zero_reads_rupees_zero(self):
        PaymentMilestone.objects.filter(pk=self.ms['M1'].pk).update(amount=Decimal('0'))
        card = _text(_card(self.page(self.pm)))
        self.assertIn('M1 ₹0 Pending', card)
        self.assertNotIn('Amount TBD', card)

    def test_blank_amount_reads_amount_tbd(self):
        PaymentMilestone.objects.filter(pk=self.ms['M1'].pk).update(amount=None)
        card = _text(_card(self.page(self.pm)))
        self.assertIn('M1 Amount TBD Pending', card)


# ---------------------------------------------------------------------------
# Query count
# ---------------------------------------------------------------------------

class QueryCountTests(ContractValueBase):
    """The footer is built from rows the page already loads: showing it adds no query."""

    def _queries(self, profile, show_footer):
        client = _client(profile)
        url = reverse('project_overview', args=[self.project.project_id])
        client.get(url)   # warm the session
        if show_footer:
            with CaptureQueriesContext(connection) as ctx:
                response = client.get(url)
        else:
            with patch('projects.permissions.user_can_see_contract_value',
                       return_value=False), CaptureQueriesContext(connection) as ctx:
                response = client.get(url)
        self.assertEqual(FOOTER in response.content.decode(), show_footer)
        return len(ctx.captured_queries)

    def test_footer_adds_no_query(self):
        for profile in (self.ceo, self.finance):
            with self.subTest(role=profile.role):
                self.assertEqual(self._queries(profile, True),
                                 self._queries(profile, False))
