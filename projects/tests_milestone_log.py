"""FIN-M-L — every PaymentMilestone change is logged with its old and new values (R15).

WHY THIS FILE EXISTS
--------------------
Ruling R15: PMs and Coordinators may still edit milestone amounts, but every change, by
every writer (PM/Coordinator, BD, Finance, the task sync), is logged with old and new
values. Before FIN-M-L the PM edit logged nothing, BD's line showed only the new amount,
and Finance's lines said "Updated M1" / "Marked M1 as Received" with no values.

Rulings (Zuber, 12 Nov 2026):
* At most one money line and one plain line per save, each listing every changed field
  of its kind, "M1: <label> <old> → <new>; <label> <old> → <new>".
* Money fields: amount (code milestone_amounts_set, as BD's line already was), amount
  received and variance reason (new code milestone_amount_received). Both codes are in
  MONEY_ACTIVITY_CODES, so only the money audience sees them.
* Plain fields: description, due date, status, invoice date, received date, and the
  creation of M1-M3 (new code milestone_updated, not a money code).
* An unchanged field writes nothing; a save that changes nothing writes no line; a
  refused save writes no line. BD's identical amounts write no line.
* The generic lines ("Updated M1", "Marked M1 as Invoiced/Received") are replaced. The
  task-sync plain line keeps its "via completion of Finance task '…'" wording.
* Dates read "5 Dec 2026"; an empty value reads "blank".

The formatter and code tests import inside the test bodies on purpose: run against the
code before FIN-M-L, each test then fails on its own instead of the whole module failing
to import.

Run with:
    python manage.py test projects.tests_milestone_log --settings=solarpms.test_settings
"""
import json
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from .models import ActivityLog, PaymentMilestone, Project, ProjectPhase, Task
from .utils import RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL

MONEY_AMOUNT = 'milestone_amounts_set'
MONEY_RECEIVED = 'milestone_amount_received'
PLAIN = 'milestone_updated'
CODES = (MONEY_AMOUNT, MONEY_RECEIVED, PLAIN)


def _day(value):
    """The log's date format, written out independently of the view's helper."""
    return f'{value.day} {value:%b %Y}'


TODAY = _day(date.today())


def _profile(username, role, email=''):
    """A post_save signal creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x', email=email,
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


class MilestoneLogBase(TestCase):
    """One Active Residential project with a PM, Finance, BD, a Site Engineer holding a
    task and an assigned designer, and an M1 milestone with known stored values."""

    def setUp(self):
        self.pm = _profile('ml_pm', 'PM')
        self.finance = _profile('ml_fin', 'Finance')
        self.bd = _profile('ml_bd', 'BD')
        self.se = _profile('ml_se', 'Site Engineer')
        self.designer = _profile('ml_design', 'Design')
        self.project = Project.objects.create(
            customer_name='Milestone Log', status='Active', project_type='Residential',
            customer_phone='9876543210', site_address='1 Sun Road', city='Lucknow',
            state='Uttar Pradesh', dc_capacity_kw=Decimal('5.00'), assigned_pm=self.pm,
            assigned_design=self.designer, cascade_scheduling=False)
        phase = ProjectPhase.objects.create(project=self.project, phase_name='Install',
                                            phase_order=1)
        Task.objects.create(phase=phase, task_name='Mount structure', task_order=1,
                            assigned_role=Task.SITE_ENGINEER, assigned_to=self.se)
        self.m1 = PaymentMilestone.objects.create(
            project=self.project, milestone_name='M1', milestone_description='Advance',
            amount=Decimal('150000.00'), due_date=date(2026, 10, 15), created_by=self.pm)

    def lines(self):
        """This project's milestone lines, oldest first, as (code, text, actor pk)."""
        return [(r.action_code, r.action, r.actor_id) for r in
                ActivityLog.objects.filter(project=self.project, entity_type='Milestone')
                .order_by('timestamp', 'pk')]

    def coded(self, code):
        return [text for c, text, _actor in self.lines() if c == code]

    def pm_edit(self, amount='150000.00', description='Advance', due_date='2026-10-15'):
        """POST the PM edit form exactly as project_overview.html builds it; the
        defaults are the stored values, so each test changes only what it names."""
        return _client(self.pm).post(
            reverse('project_overview', args=[self.project.project_id]),
            {'action': 'update_milestone', 'milestone_pk': self.m1.pk,
             'milestone_description': description, 'amount': amount,
             'due_date': due_date})

    def finance_edit(self, amount_received='', description='Advance'):
        return _client(self.finance).post(
            reverse('project_overview', args=[self.project.project_id]),
            {'action': 'update_milestone', 'milestone_pk': self.m1.pk,
             'milestone_description': description, 'amount_received': amount_received})


# ---------------------------------------------------------------------------
# PM / Coordinator edit on the project page
# ---------------------------------------------------------------------------

class PMEditLogTests(MilestoneLogBase):

    def test_amount_change_writes_one_money_line_with_old_and_new(self):
        self.pm_edit(amount='135000.50')
        self.assertEqual(self.lines(), [
            (MONEY_AMOUNT, 'M1: amount ₹1,50,000 → ₹1,35,000.50', self.pm.pk)])

    def test_description_only_change_writes_one_plain_line_and_no_amount_line(self):
        self.pm_edit(description='Advance on signing')
        self.assertEqual(self.lines(), [
            (PLAIN, "M1: description 'Advance' → 'Advance on signing'", self.pm.pk)])

    def test_plain_fields_share_one_line(self):
        self.pm_edit(description='Advance on signing', due_date='2026-12-05')
        self.assertEqual(self.coded(PLAIN), [
            "M1: description 'Advance' → 'Advance on signing'; "
            "due date 15 Oct 2026 → 5 Dec 2026"])
        self.assertEqual(self.coded(MONEY_AMOUNT), [])

    def test_amount_and_due_date_write_one_line_of_each_kind(self):
        self.pm_edit(amount='160000', due_date='2026-12-05')
        self.assertEqual(self.coded(MONEY_AMOUNT), ['M1: amount ₹1,50,000 → ₹1,60,000'])
        self.assertEqual(self.coded(PLAIN), ['M1: due date 15 Oct 2026 → 5 Dec 2026'])
        self.assertEqual(len(self.lines()), 2)

    def test_cleared_due_date_reads_blank(self):
        self.pm_edit(due_date='')
        self.assertEqual(self.coded(PLAIN), ['M1: due date 15 Oct 2026 → blank'])

    def test_blank_amount_writes_no_amount_line(self):
        self.pm_edit(amount='')
        self.assertEqual(self.lines(), [])

    def test_blank_amount_with_a_description_change_writes_only_the_plain_line(self):
        self.pm_edit(amount='', description='Advance on signing')
        self.assertEqual([c for c, _t, _a in self.lines()], [PLAIN])

    def test_refused_amount_writes_no_line_at_all(self):
        self.pm_edit(amount='abc', description='Advance on signing',
                     due_date='2026-12-05')
        self.assertEqual(self.lines(), [])

    def test_refused_date_writes_no_line_at_all(self):
        self.pm_edit(amount='160000', due_date='2026-13-45')
        self.assertEqual(self.lines(), [])

    def test_an_unchanged_save_writes_nothing(self):
        # The form posts the stored values back; equal by value, not by spelling.
        self.pm_edit(amount='150000')
        self.assertEqual(self.lines(), [])


# ---------------------------------------------------------------------------
# Finance edit on the project page
# ---------------------------------------------------------------------------

class FinanceEditLogTests(MilestoneLogBase):

    def test_receipt_writes_a_plain_and_a_money_line_and_no_generic_line(self):
        PaymentMilestone.objects.filter(pk=self.m1.pk).update(status='Invoiced')
        self.finance_edit(amount_received='37918')
        self.assertEqual(self.lines(), [
            (PLAIN, f'M1: status Invoiced → Received; received date blank → {TODAY}',
             self.finance.pk),
            (MONEY_RECEIVED, 'M1: amount received blank → ₹37,918', self.finance.pk),
        ])

    def test_description_only_change_writes_only_the_plain_line(self):
        self.finance_edit(description='Advance (cheque)')
        self.assertEqual(self.lines(), [
            (PLAIN, "M1: description 'Advance' → 'Advance (cheque)'", self.finance.pk)])

    def test_a_save_that_changes_nothing_writes_nothing(self):
        # Before FIN-M-L this wrote "Updated M1" on every save.
        self.finance_edit()
        self.assertEqual(self.lines(), [])


# ---------------------------------------------------------------------------
# Finance actions: invoice and receive
# ---------------------------------------------------------------------------

class FinanceActionLogTests(MilestoneLogBase):

    def test_invoice_writes_one_plain_line(self):
        _client(self.finance).post(
            reverse('milestone_invoice', args=[self.project.project_id, self.m1.pk]))
        self.assertEqual(self.lines(), [
            (PLAIN, f'M1: status Pending → Invoiced; invoice date blank → {TODAY}',
             self.finance.pk)])

    def test_receive_writes_a_plain_and_a_money_line_with_the_variance_reason(self):
        PaymentMilestone.objects.filter(pk=self.m1.pk).update(status='Invoiced')
        _client(self.finance).post(
            reverse('milestone_receive', args=[self.project.project_id, self.m1.pk]),
            {'amount_received': '165000'})
        self.assertEqual(self.lines(), [
            (PLAIN, f'M1: status Invoiced → Received; received date blank → {TODAY}',
             self.finance.pk),
            (MONEY_RECEIVED,
             "M1: amount received blank → ₹1,65,000; variance reason blank → 'Overpayment'",
             self.finance.pk),
        ])

    def test_a_refused_receive_writes_nothing(self):
        _client(self.finance).post(
            reverse('milestone_receive', args=[self.project.project_id, self.m1.pk]),
            {'amount_received': ''})
        self.assertEqual(self.lines(), [])


# ---------------------------------------------------------------------------
# BD (and PM) set_milestone_amounts
# ---------------------------------------------------------------------------

class SetAmountsLogTests(MilestoneLogBase):

    def post(self, profile, body):
        return _client(profile).post(
            reverse('set_milestone_amounts', args=[self.project.project_id]),
            data=json.dumps(body), content_type='application/json')

    def test_bd_change_shows_old_and_new_in_one_line(self):
        response = self.post(self.bd, {'m1_amount': 124000, 'm2_amount': 50000})
        self.assertTrue(response.json()['success'], response.content)
        self.assertEqual(self.lines(), [
            (MONEY_AMOUNT,
             'Set milestone amounts: M1 ₹1,50,000 → ₹1,24,000, M2 blank → ₹50,000',
             self.bd.pk)])

    def test_identical_amounts_write_no_line(self):
        response = self.post(self.bd, {'m1_amount': '150000.00'})
        self.assertTrue(response.json()['success'], response.content)
        self.assertEqual(self.lines(), [])

    def test_only_the_changed_milestones_are_listed(self):
        PaymentMilestone.objects.create(project=self.project, milestone_name='M2',
                                        amount=Decimal('50000'), created_by=self.pm)
        self.post(self.pm, {'m1_amount': 150000, 'm2_amount': 60000})
        self.assertEqual(self.coded(MONEY_AMOUNT),
                         ['Set milestone amounts: M2 ₹50,000 → ₹60,000'])

    def test_a_refused_set_writes_nothing(self):
        self.post(self.bd, {'m1_amount': -5})
        self.assertEqual(self.lines(), [])


# ---------------------------------------------------------------------------
# Creation, and the Finance task → milestone sync (activated through the real view)
# ---------------------------------------------------------------------------

class ActivatedProjectLogTests(TestCase):

    def setUp(self):
        # Activation needs the Residential Finance assignee's account to exist.
        self.finance = _profile('ml_act_fin', 'Finance',
                                email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)
        self.pm = _profile('ml_act_pm', 'PM')
        self.designer = _profile('ml_act_design', 'Design')
        self.project = Project.objects.create(
            customer_name='Activated Log', customer_phone='9876543210',
            site_address='1 Sun Road', city='Lucknow', project_type='Residential',
            dc_capacity_kw=Decimal('5.00'), status='Draft', assigned_pm=self.pm,
            target_commissioning_date=date.today() + timedelta(days=90))
        response = _client(self.pm).post(
            reverse('project_activate', args=[self.project.project_id]),
            {'assigned_design_id': self.designer.pk})
        self.assertEqual(response.status_code, 302)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, 'Active')

    def lines(self):
        return [(r.action_code, r.action, r.actor_id) for r in
                ActivityLog.objects.filter(project=self.project, entity_type='Milestone')
                .order_by('timestamp', 'pk')]

    def complete_m3_task(self, **extra):
        task = Task.objects.get(phase__project=self.project,
                                task_name='100% Payment Confirmation')
        Task.objects.filter(pk=task.pk).update(due_date=date.today())
        _client(self.finance).post(
            reverse('task_status_update', args=[self.project.project_id, task.pk]),
            {'status': Task.DONE, **extra})

    def test_activation_writes_one_creation_line(self):
        self.assertEqual(self.lines(), [
            (PLAIN, 'Payment milestones M1–M3 created', self.pm.pk)])

    def test_task_sync_with_an_amount_writes_both_lines_with_the_actor(self):
        self.complete_m3_task(amount_received='100000')
        self.assertEqual(self.lines()[1:], [
            (PLAIN,
             f'M3: status Pending → Received; received date blank → {TODAY} via '
             f"completion of Finance task '100% Payment Confirmation' by "
             f'Ml_Act_Fin Test (Finance)', self.finance.pk),
            (MONEY_RECEIVED, 'M3: amount received blank → ₹1,00,000', self.finance.pk),
        ])

    def test_task_sync_without_an_amount_writes_only_the_plain_line(self):
        self.complete_m3_task()
        self.assertEqual([c for c, _t, _a in self.lines()[1:]], [PLAIN])


class MilestoneCreateLogTests(MilestoneLogBase):

    def test_the_fallback_create_writes_one_creation_line(self):
        PaymentMilestone.objects.filter(project=self.project).delete()
        _client(self.pm).post(reverse('milestone_create', args=[self.project.project_id]))
        self.assertEqual(self.project.milestones.count(), 3)
        self.assertEqual(self.lines(), [
            (PLAIN, 'Payment milestones M1–M3 created', self.pm.pk)])


# ---------------------------------------------------------------------------
# Who sees which line
# ---------------------------------------------------------------------------

class VisibilityTests(MilestoneLogBase):

    def setUp(self):
        super().setUp()
        PaymentMilestone.objects.filter(pk=self.m1.pk).update(status='Invoiced')
        self.pm_edit(amount='160000', due_date='2026-12-05')
        _client(self.finance).post(
            reverse('milestone_receive', args=[self.project.project_id, self.m1.pk]),
            {'amount_received': '37918'})
        self.rows = {r.action_code: r.pk for r in ActivityLog.objects.filter(
            project=self.project, entity_type='Milestone')}
        self.assertEqual(set(self.rows), set(CODES))

    def overview_ids(self, profile):
        response = _client(profile).get(
            reverse('project_overview', args=[self.project.project_id]))
        self.assertEqual(response.status_code, 200)
        return {e.pk for e in response.context['recent_activity']}

    def timeline_ids(self, profile):
        response = _client(profile).get(
            reverse('project_timeline', args=[self.project.project_id]))
        self.assertEqual(response.status_code, 200)
        return {e.pk for e in response.context['page_obj']}

    def test_the_codes_are_classified(self):
        from .activity_codes import MONEY_ACTIVITY_CODES
        self.assertIn(MONEY_AMOUNT, MONEY_ACTIVITY_CODES)
        self.assertIn(MONEY_RECEIVED, MONEY_ACTIVITY_CODES)
        self.assertNotIn(PLAIN, MONEY_ACTIVITY_CODES)

    def test_site_engineer_and_designer_see_no_money_line(self):
        money = {self.rows[MONEY_AMOUNT], self.rows[MONEY_RECEIVED]}
        for who in (self.se, self.designer):
            for seen in (self.overview_ids(who), self.timeline_ids(who)):
                self.assertFalse(money & seen, who.role)
                self.assertIn(self.rows[PLAIN], seen, who.role)

    def test_pm_and_finance_see_every_line(self):
        for who in (self.pm, self.finance):
            for seen in (self.overview_ids(who), self.timeline_ids(who)):
                self.assertTrue(set(self.rows.values()) <= seen, who.role)


# ---------------------------------------------------------------------------
# The value formatting
# ---------------------------------------------------------------------------

class FormatTests(SimpleTestCase):

    def test_rupees(self):
        from .views import _milestone_rupees
        self.assertEqual(_milestone_rupees(None), 'blank')
        self.assertEqual(_milestone_rupees(Decimal('0')), '₹0')
        self.assertEqual(_milestone_rupees(Decimal('37918.00')), '₹37,918')
        self.assertEqual(_milestone_rupees(Decimal('12345678.5')), '₹1,23,45,678.50')
        # Paise are kept: a 40-paise change must not read "₹X → ₹X".
        self.assertEqual(_milestone_rupees(Decimal('100000.40')), '₹1,00,000.40')
        # milestone_receive accepts a negative today (a secondary finding); the log
        # still reads it correctly rather than mis-grouping the sign.
        self.assertEqual(_milestone_rupees(Decimal('-50000')), '-₹50,000')
