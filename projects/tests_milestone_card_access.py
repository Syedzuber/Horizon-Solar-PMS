"""FIN-M-C — who sees the payment milestone card on the project page, and which edit
pencil they get.

WHY THIS FILE EXISTS
--------------------
The card was gated by a role list in the template (PM, Finance, CEO, Admin, BD). That
showed every amount to any PM who could open the site, whether or not they managed it
(CL-I: a PM who sees a site only as its QA/QC engineer gets no more than a Site
Engineer), and hid the card from Project Coordinators, whom update_milestone already
lets save (R15). System Admin, admitted by user_can_see_contract_value(), had no card.

Rulings (Zuber, 12 Nov 2026):
* One helper, permissions.milestone_card_mode(), returning None / 'read' / 'manager' /
  'finance' / 'bd'. Finance first, then the site's managers, then BD, then read-only CEO,
  Admin and System Admin; everyone else None.
* BD keeps the card and the BD editor (R6: BD owns M1-M3 amounts).
* The two neighbouring columns are a third of the row only when the card is actually
  drawn (audience AND Residential): no empty milestone column on an OPEX page.
* Server-side save rules are unchanged.

The helper tests import inside the test bodies on purpose: run against the code before
FIN-M-C, each fails on its own instead of the whole module failing to import.

Run with:
    python manage.py test projects.tests_milestone_card_access --settings=solarpms.test_settings
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser, User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .models import (ActivityLog, PaymentMilestone, Program, Project, ProjectPhase,
                     SiteQaqcAssignment, Task)
from .tests_contract_value import FOOTER, ContractValueBase, _card, _client, _profile

CARD = 'Payment Milestones'
# The pencil BUTTONS, not the CSS rule for .milestone-edit-btn in the page's <style>.
PENCIL = 'class="btn btn-outline-secondary milestone-edit-btn"'
MANAGER_FORM = 'name="amount" step="0.01"'      # PM/Coordinator form's amount box
FINANCE_FORM = 'placeholder="₹ Amount Received"'  # Finance form's amount-received box
BD_EDITOR = 'id="bdAmtInput'
AMOUNT = '₹100000'   # a milestone row's amount as the card prints it (floatformat:0)


class MilestoneCardBase(ContractValueBase):
    """ContractValueBase's Residential project (pm assigned, pc a coordinator, M1-M3 at
    ₹1,00,000 each, a viewer in every role) plus:
      * head_pm — PM role with the Design Head flag: sees the Residential site without
        managing it;
      * an OPEX site where qaqc_pm (PM role) is only the QA/QC engineer, pm is assigned."""

    def setUp(self):
        super().setUp()
        self.head_pm = _profile('mc_head_pm', 'PM')
        self.head_pm.is_design_head = True
        self.head_pm.save()
        self.qaqc_pm = _profile('mc_qaqc_pm', 'PM')
        program = Program.objects.create(name='Test-MC', program_type='OPEX',
                                         client_name='MCClient', status='Active',
                                         short_tender_code='MC1')
        self.opex = Project.objects.create(
            project_id='MC1-A', customer_name='MCClient', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX', program=program,
            site_code='MC1-A', dc_capacity_kw=Decimal('100.00'), status='Active',
            assigned_pm=self.pm)
        phase = ProjectPhase.objects.create(project=self.opex, phase_name='Install',
                                            phase_order=1)
        Task.objects.create(phase=phase, task_name='Mount structure', task_order=1,
                            assigned_role=Task.SITE_ENGINEER, assigned_to=self.se)
        SiteQaqcAssignment.objects.create(project=self.opex, engineer=self.qaqc_pm,
                                          assigned_by=self.pm)

    def mode(self, profile, project=None):
        from .permissions import milestone_card_mode
        return milestone_card_mode(profile.user, project or self.project)

    def assert_no_card(self, html):
        self.assertNotIn(CARD, html)
        self.assertNotIn(PENCIL, html)
        self.assertNotIn(AMOUNT, html)

    def column_class(self, html, heading):
        """The class of the column <div> holding the card titled `heading`."""
        at = html.find(heading)
        self.assertNotEqual(at, -1, heading)
        start = html.rfind('<div class="col-12 ', 0, at)
        return html[start:html.find('>', start)]


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------

class HelperTests(MilestoneCardBase):

    def test_site_managers_get_the_manager_form(self):
        for profile in (self.pm, self.pc):
            with self.subTest(role=profile.role):
                self.assertEqual(self.mode(profile), 'manager')

    def test_finance_bd_and_read_only_roles(self):
        self.assertEqual(self.mode(self.finance), 'finance')
        self.assertEqual(self.mode(self.bd), 'bd')
        for profile in (self.ceo, self.admin, self.sysadmin):
            with self.subTest(role=profile.role):
                self.assertEqual(self.mode(profile), 'read')

    def test_everyone_else_gets_no_card(self):
        for profile in (self.scm, self.se, self.designer, self.head_pm):
            with self.subTest(role=profile.role, user=profile.user.username):
                self.assertIsNone(self.mode(profile))

    def test_a_pm_who_is_only_the_qaqc_engineer_gets_no_card(self):
        """CL-I: QA/QC grants view, never management. The helper has no type term, so it
        answers on the OPEX site itself — where the assigned PM still manages."""
        self.assertIsNone(self.mode(self.qaqc_pm, self.opex))
        self.assertEqual(self.mode(self.pm, self.opex), 'manager')

    def test_finance_who_also_manages_keeps_the_finance_form(self):
        """update_milestone picks its Finance branch by role, before management."""
        Project.objects.filter(pk=self.project.pk).update(assigned_pm=self.finance)
        self.project.refresh_from_db()
        self.assertEqual(self.mode(self.finance), 'finance')

    def test_bd_who_manages_gets_the_manager_form(self):
        """Management is asked before BD, as the template's if/elif did."""
        Project.objects.filter(pk=self.project.pk).update(assigned_pm=self.bd)
        self.project.refresh_from_db()
        self.assertEqual(self.mode(self.bd), 'manager')

    def test_a_deleted_or_missing_project_or_no_profile_gets_nothing(self):
        from .permissions import milestone_card_mode
        self.assertIsNone(milestone_card_mode(self.ceo.user, None))
        self.assertIsNone(milestone_card_mode(AnonymousUser(), self.project))
        self.project.is_deleted = True
        self.assertIsNone(milestone_card_mode(self.ceo.user, self.project))

    def test_costs_no_query_when_management_is_passed_in(self):
        from .permissions import milestone_card_mode
        for profile in (self.pc, self.se, self.ceo, self.finance, self.bd):
            user = User.objects.get(pk=profile.user.pk)
            user.profile   # cached, as request.user.profile is by the time the view asks
            with self.subTest(role=profile.role), self.assertNumQueries(0):
                milestone_card_mode(user, self.project, can_manage=False)


# ---------------------------------------------------------------------------
# The card on the page
# ---------------------------------------------------------------------------

class CardAudienceTests(MilestoneCardBase):

    def test_assigned_pm_sees_card_and_manager_pencil(self):
        card = _card(self.page(self.pm))
        self.assertIn(PENCIL, card)
        self.assertIn(f'data-bs-target="#editMilestone{self.ms["M1"].pk}"', card)
        self.assertIn(MANAGER_FORM, card)
        self.assertNotIn(BD_EDITOR, card)

    def test_coordinator_sees_card_and_manager_pencil(self):
        """New: the Coordinator could save but never saw the card."""
        html = self.page(self.pc)
        card = _card(html)
        self.assertIn(CARD, html)
        self.assertIn(AMOUNT, card)
        self.assertIn(f'data-bs-target="#editMilestone{self.ms["M1"].pk}"', card)
        self.assertIn(MANAGER_FORM, card)
        self.assertIn('col-md-4', self.column_class(html, 'Recent Activity'))

    def test_pm_who_does_not_manage_the_site_sees_no_card(self):
        """New: a Design Head PM can open the Residential site but does not manage it."""
        self.assert_no_card(self.page(self.head_pm))

    def test_pm_who_is_only_the_qaqc_engineer_sees_no_card(self):
        self.assert_no_card(self.page(self.qaqc_pm, self.opex))

    def test_finance_sees_card_and_finance_form(self):
        card = _card(self.page(self.finance))
        self.assertIn(PENCIL, card)
        self.assertIn(FINANCE_FORM, card)
        self.assertNotIn(MANAGER_FORM, card)
        self.assertNotIn(BD_EDITOR, card)

    def test_bd_sees_card_and_bd_editor(self):
        card = _card(self.page(self.bd))
        self.assertIn(f'data-bs-target="#editMilestoneBD{self.ms["M1"].pk}"', card)
        self.assertIn(BD_EDITOR, card)
        self.assertNotIn(MANAGER_FORM, card)
        self.assertNotIn(FINANCE_FORM, card)

    def test_ceo_admin_and_system_admin_see_card_without_pencil(self):
        """System Admin is new."""
        for profile in (self.ceo, self.admin, self.sysadmin):
            with self.subTest(role=profile.role):
                html = self.page(profile)
                card = _card(html)
                self.assertIn(CARD, html)
                self.assertIn(AMOUNT, card)
                self.assertNotIn(PENCIL, card)
                self.assertNotIn(MANAGER_FORM, card)
                self.assertNotIn(BD_EDITOR, card)

    def test_scm_site_engineer_and_design_see_no_card(self):
        for profile in (self.scm, self.se, self.designer):
            with self.subTest(role=profile.role):
                self.assert_no_card(self.page(profile))

    def test_contract_footer_still_only_for_the_contract_value_roles(self):
        for profile in (self.ceo, self.finance, self.admin, self.sysadmin):
            with self.subTest(role=profile.role, shown=True):
                self.assertIn(FOOTER, self.page(profile))
        for profile in (self.pm, self.pc, self.bd):
            with self.subTest(role=profile.role, shown=False):
                html = self.page(profile)
                self.assertIn(CARD, html)
                self.assertNotIn(FOOTER, html)
                self.assertNotIn('3,00,000', html)


# ---------------------------------------------------------------------------
# Column widths
# ---------------------------------------------------------------------------

class ColumnWidthTests(MilestoneCardBase):

    def test_opex_site_has_no_empty_milestone_column(self):
        """Ruling (b): on OPEX there is no card, so the phase and activity columns take
        half the row each for every role, the old card roles included."""
        for profile in (self.pm, self.finance, self.ceo):
            with self.subTest(role=profile.role):
                html = self.page(profile, self.opex)
                self.assertNotIn(CARD, html)
                for heading in ('Task Progress by Phase', 'Recent Activity'):
                    cls = self.column_class(html, heading)
                    self.assertIn('col-md-6', cls, heading)
                    self.assertNotIn('col-md-4', cls, heading)

    def test_residential_columns_are_a_third_when_the_card_is_drawn(self):
        for profile, width in ((self.pm, 'col-md-4'), (self.sysadmin, 'col-md-4'),
                               (self.scm, 'col-md-6')):
            with self.subTest(role=profile.role):
                html = self.page(profile)
                self.assertIn(width, self.column_class(html, 'Recent Activity'))


# ---------------------------------------------------------------------------
# The Coordinator's save (server unchanged; the path the card now offers them)
# ---------------------------------------------------------------------------

class CoordinatorSaveTests(MilestoneCardBase):

    def test_coordinator_amount_edit_saves_and_is_logged(self):
        url = reverse('project_overview', args=[self.project.project_id])
        response = _client(self.pc).post(url, {
            'action': 'update_milestone', 'milestone_pk': self.ms['M1'].pk,
            'milestone_description': '', 'amount': '120000', 'due_date': ''})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PaymentMilestone.objects.get(pk=self.ms['M1'].pk).amount,
                         Decimal('120000'))
        self.assertTrue(ActivityLog.objects.filter(
            project=self.project, actor=self.pc,
            action_code='milestone_amounts_set').exists())


# ---------------------------------------------------------------------------
# Query count
# ---------------------------------------------------------------------------

class QueryCountTests(MilestoneCardBase):
    """The helper adds no query to the page: the same page with the helper replaced by a
    stub that returns the same answer and does no work costs exactly as much."""

    def _count(self, profile, stub=None):
        client = _client(profile)
        url = reverse('project_overview', args=[self.project.project_id])
        client.get(url)   # warm the session
        if stub is None:
            with CaptureQueriesContext(connection) as ctx:
                client.get(url)
        else:
            with patch('projects.permissions.milestone_card_mode',
                       lambda *a, **k: stub), CaptureQueriesContext(connection) as ctx:
                client.get(url)
        return len(ctx.captured_queries)

    def test_query_count_per_role_unchanged(self):
        for profile in (self.ceo, self.finance, self.admin, self.sysadmin, self.scm,
                        self.bd, self.pm, self.pc, self.se, self.designer):
            with self.subTest(role=profile.role):
                stub = self.mode(profile)
                self.assertEqual(self._count(profile), self._count(profile, stub))
