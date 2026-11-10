"""
DASH-B1 — the Site Dashboard data layer (site_dashboard.py) and its two permission helpers.

WHAT THIS PINS
    Visibility     each role's site scope; PM handover; coordinator; QA/QC engineer does
                   not widen it; test / deleted / Cancelled never appear, Draft does (D1, D9)
    Payments       the figure for the five portfolio money roles, None for a PM (D8)
    Activity       scheduler rows never win "latest activity" (all four codes, with and
                   without an actor), *_opened skipped, blank and due_date_changed count,
                   money rows follow user_can_see_project_money (D3, ruling 3)
    Parity         overdue = the PM card's rule, progress = the PM card's and overview's
                   rule (D4); plan slip = site_plan_summary(); reason =
                   schedule_site(apply=False).reason (D6)
    Execution      by task, not phase (D5)
    Sort           D7, both orders, drafts last, tie-breaks
    Capacity       None stays None; totals skip blanks and count them (D10)
    Query budget   the same number of queries for 3 sites and for 30

Today is frozen at 9 Nov 2026 by SchedFixture (tests_sched2_scheduler), which also seeds
the real OPEX v1 template and schedule blocks and activates OPEX sites through the view.

Run with:
    python manage.py test projects.tests_site_dashboard --settings=solarpms.test_settings
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.contrib.auth.models import AnonymousUser, User
from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from . import scheduling
from .activity_codes import MONEY_ACTIVITY_CODES, NON_ACTIVITY_CODES
from .management.commands.dashboard_data_check import _reference_pm_card
from .models import (ActivityLog, ContractorAttendance, PaymentRequest, Program, Project,
                     ProjectPhase, SiteQaqcAssignment, Task, Vendor, VendorOrder,
                     VendorOrderSite)
from .permissions import (sites_visible_on_dashboard, user_can_see_project_money,
                          user_sees_money_activity_on_dashboard, user_sees_payment_totals)
from .scheduling import (REASON_EXCLUDED, REASON_NEEDS_ANCHOR, REASON_NEEDS_CAPACITY,
                         REASON_NOT_ELIGIBLE, schedule_site, site_plan_summary)
from .site_dashboard import (EXECUTION_LANE_NAME, LANE_DONE, LANE_EMPTY, LANE_IN_PROGRESS,
                             ORDER_LATEST, ORDER_URGENT, REASON_SCHEDULED, Schedule,
                             SiteCard, build_site_cards, capacity_totals, human_activity_q,
                             is_human_activity, programs_on_cards, sort_cards)
from .site_pm import change_site_pm
from .tests_sched2_scheduler import CIVIL, FROZEN_TODAY, MODULE, SchedFixture
from .tests_two_step_completion import _profile
from .utils import current_phase

SCHEDULER_CODES = ('due_dates_scheduled', 'due_dates_forecast_shift', 'baseline_reset',
                   'auto_schedule_exclusion_changed')


def _fresh(user):
    """A new instance of `user`, so its profile is read again (no cached relation)."""
    return User.objects.get(pk=user.pk)


class DashFixture(SchedFixture):
    """SchedFixture's PM, site engineer, template and blocks, plus every other role."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.other_pm = _profile('dash_pm2', 'PM')
        cls.coord = _profile('dash_coord', 'Project Coordinator')
        cls.ceo = _profile('dash_ceo', 'CEO')
        cls.finance = _profile('dash_fin', 'Finance')
        cls.scm = _profile('dash_scm', 'SCM')
        cls.admin = _profile('dash_admin', 'Admin')
        cls.sysadmin = _profile('dash_sysadmin', 'System Admin')
        cls.head = _profile('dash_head', 'Design', is_design_head=True)
        cls.designer = _profile('dash_designer', 'Design')
        cls.bd = _profile('dash_bd', 'BD')

    _seq = 0

    def _bare(self, project_type='Residential', status='Active', pm=None, **fields):
        """A site with no tasks, made directly (no activation view)."""
        DashFixture._seq += 1
        fields.setdefault('activated_at', timezone.now() if status != 'Draft' else None)
        return Project.objects.create(
            project_id=f'DASH-{DashFixture._seq:03d}', customer_name=f'Dash {DashFixture._seq}',
            customer_phone='9876543210', site_address='1 Dash Road', city='Delhi',
            project_type=project_type, status=status, assigned_pm=pm or self.pm, **fields)

    def _phase(self, site, name, order):
        return ProjectPhase.objects.create(project=site, phase_name=name, phase_order=order)

    def _plain_task(self, phase, order, **fields):
        fields.setdefault('task_name', f'T{order}')
        return Task.objects.create(phase=phase, task_order=order, **fields)

    def _log(self, site, code, when, actor=None, action=None):
        row = ActivityLog.objects.create(project=site, actor=actor, action=action or f'did {code}',
                                         action_code=code)
        ActivityLog.objects.filter(pk=row.pk).update(timestamp=when)
        return row

    def _card(self, viewer, site):
        return next(c for c in build_site_cards(_fresh(viewer.user)) if c.id == site.pk)

    def _ids(self, viewer):
        return {c.id for c in build_site_cards(_fresh(viewer.user))}


# ---------------------------------------------------------------------------
# 1. Visibility (Task 1, D1, D9)
# ---------------------------------------------------------------------------

class VisibilityTests(DashFixture):

    def setUp(self):
        self.mine = self._bare()
        self.coordinated = self._bare(pm=self.other_pm)
        self.coordinated.coordinators.add(self.coord, self.pm)
        self.theirs = self._bare(pm=self.other_pm)
        self.draft = self._bare(status='Draft')
        self.test_site = self._bare(is_test=True)
        self.deleted = self._bare(is_deleted=True)
        self.cancelled = self._bare(status='Cancelled')
        self.on_hold = self._bare(status='On Hold')

    def _visible(self, profile):
        return set(sites_visible_on_dashboard(_fresh(profile.user)).values_list('pk', flat=True))

    def test_pm_sees_assigned_and_coordinated_sites_only(self):
        self.assertEqual(self._visible(self.pm),
                         {self.mine.pk, self.coordinated.pk, self.draft.pk, self.on_hold.pk})

    def test_coordinator_sees_the_sites_they_coordinate(self):
        self.assertEqual(self._visible(self.coord), {self.coordinated.pk})

    def test_pm_scope_equals_user_can_manage_project_on_every_live_site(self):
        from .permissions import user_can_manage_project
        live = Project.objects.filter(is_deleted=False, is_test=False).exclude(status='Cancelled')
        for profile in (self.pm, self.other_pm, self.coord):
            with self.subTest(profile=profile.user.username):
                expected = {p.pk for p in live if user_can_manage_project(profile.user, p)}
                self.assertEqual(self._visible(profile), expected)

    def test_pm_loses_the_site_after_a_pm_handover(self):
        self.assertIn(self.mine.pk, self._visible(self.pm))
        change_site_pm(self.mine, self.other_pm, self.admin,
                       expected_outgoing_pm=self.pm, reason='handover')
        self.assertNotIn(self.mine.pk, self._visible(self.pm))
        self.assertIn(self.mine.pk, self._visible(self.other_pm))

    def test_portfolio_roles_and_design_head_see_every_live_site(self):
        live = {self.mine.pk, self.coordinated.pk, self.theirs.pk, self.draft.pk,
                self.on_hold.pk}
        for profile in (self.ceo, self.finance, self.scm, self.admin, self.sysadmin, self.head):
            with self.subTest(role=profile.role, head=profile.is_design_head):
                self.assertEqual(self._visible(profile), live)

    def test_site_engineer_design_bd_and_no_profile_see_nothing(self):
        for profile in (self.se, self.designer, self.bd):
            with self.subTest(role=profile.role):
                self.assertEqual(self._visible(profile), set())
        self.assertEqual(list(sites_visible_on_dashboard(AnonymousUser())), [])

    def test_test_deleted_and_cancelled_never_appear_draft_does(self):
        for profile in (self.pm, self.ceo):
            with self.subTest(role=profile.role):
                seen = self._ids(profile)
                self.assertIn(self.draft.pk, seen)
                for hidden in (self.test_site, self.deleted, self.cancelled):
                    self.assertNotIn(hidden.pk, seen)

    def test_pm_who_is_qaqc_engineer_on_an_unmanaged_site_does_not_see_it(self):
        SiteQaqcAssignment.objects.create(project=self.theirs, engineer=self.pm,
                                          assigned_by=self.other_pm)
        self.assertNotIn(self.theirs.pk, self._visible(self.pm))

    def test_a_pm_sees_each_site_once_even_as_pm_and_coordinator(self):
        cards = build_site_cards(_fresh(self.pm.user))
        self.assertEqual(len(cards), len({c.id for c in cards}))


# ---------------------------------------------------------------------------
# 2. Payments (D8)
# ---------------------------------------------------------------------------

class PaymentTests(DashFixture):

    def setUp(self):
        self.site = self._bare()
        vendor = Vendor.objects.create(name='Dash Vendor')
        order = VendorOrder.objects.create(vendor=vendor, project_type='Residential',
                                           created_by=self.scm, total_amount=Decimal('200000'))
        VendorOrderSite.objects.create(order=order, project=self.site)

        def pr(amount, status, **fields):
            return PaymentRequest.objects.create(
                project=self.site, vendor=vendor, vendor_order=order, amount=Decimal(amount),
                requested_by=self.scm.user, status=status, **fields)
        pr('50000', PaymentRequest.PENDING_APPROVAL)
        pr('40000', PaymentRequest.APPROVED, approved_amount=Decimal('30000'))  # partial: 30k counts
        pr('10000', PaymentRequest.ON_HOLD)
        pr('70000', PaymentRequest.CONFIRMED, approved_amount=Decimal('70000'))  # paid: not open
        pr('90000', PaymentRequest.REJECTED, decision_reason='no')               # not open

    def test_open_payments_for_the_five_portfolio_money_roles(self):
        for profile in (self.ceo, self.finance, self.scm, self.admin, self.sysadmin):
            with self.subTest(role=profile.role):
                self.assertTrue(user_sees_payment_totals(profile.user))
                self.assertEqual(self._card(profile, self.site).payment_open, Decimal('90000'))

    def test_no_payment_figure_for_pm_coordinator_or_design_head(self):
        self.site.coordinators.add(self.coord)
        for profile in (self.pm, self.coord):
            with self.subTest(role=profile.role):
                self.assertFalse(user_sees_payment_totals(profile.user))
                self.assertIsNone(self._card(profile, self.site).payment_open)
        self.assertFalse(user_sees_payment_totals(self.head.user))
        self.assertFalse(user_sees_payment_totals(AnonymousUser()))

    def test_money_activity_everywhere_only_for_the_five_portfolio_money_roles(self):
        """Ruling 3: the helper is user_can_see_project_money()'s role term. True for the
        five portfolio money roles; False for PM and coordinator (money rows only on sites
        they manage), the Design Head (every site, no money), and everyone else."""
        for profile in (self.ceo, self.finance, self.scm, self.admin, self.sysadmin):
            with self.subTest(role=profile.role):
                self.assertTrue(user_sees_money_activity_on_dashboard(_fresh(profile.user)))
        self.site.coordinators.add(self.coord)
        for profile in (self.pm, self.coord, self.head, self.designer, self.bd, self.se):
            with self.subTest(role=profile.role, head=profile.is_design_head):
                self.assertFalse(user_sees_money_activity_on_dashboard(_fresh(profile.user)))
        # False is not "no money": the site's PM and coordinator still see this site's money
        # through user_can_see_project_money(); the Design Head sees none.
        self.assertTrue(user_can_see_project_money(self.pm.user, self.site))
        self.assertTrue(user_can_see_project_money(self.coord.user, self.site))
        self.assertFalse(user_can_see_project_money(self.head.user, self.site))
        self.assertFalse(user_sees_money_activity_on_dashboard(AnonymousUser()))


# ---------------------------------------------------------------------------
# 3. Latest activity (D3, ruling 3)
# ---------------------------------------------------------------------------

class LatestActivityTests(DashFixture):

    def setUp(self):
        self.site = self._bare()
        self.t0 = timezone.make_aware(datetime(2026, 11, 5, 10, 0))

    def test_non_activity_codes_name_the_scheduler_constants(self):
        self.assertTrue({scheduling.SCHEDULED_ACTION_CODE, scheduling.FORECAST_ACTION_CODE,
                         scheduling.BASELINE_RESET_ACTION_CODE}
                        <= NON_ACTIVITY_CODES)
        self.assertTrue(set(SCHEDULER_CODES) <= NON_ACTIVITY_CODES)
        self.assertTrue({'user_login', 'user_logout'} <= NON_ACTIVITY_CODES)

    def test_a_newer_scheduler_row_never_beats_the_human_row_before_it(self):
        for code in SCHEDULER_CODES:
            for actor in (None, self.pm):
                with self.subTest(code=code, actor=actor and actor.user.username):
                    ActivityLog.objects.filter(project=self.site).delete()
                    human = self._log(self.site, 'task_status_done', self.t0, actor=self.pm,
                                      action='Marked Done')
                    self._log(self.site, code, self.t0 + timedelta(hours=2), actor=actor)
                    card = self._card(self.ceo, self.site)
                    self.assertEqual(card.latest.what, human.action)
                    self.assertEqual(len(card.feed), 1)

    def test_opened_rows_are_skipped(self):
        self._log(self.site, 'task_status_done', self.t0, actor=self.pm, action='human')
        self._log(self.site, 'site_drawing_opened', self.t0 + timedelta(hours=1), actor=self.pm)
        self.assertEqual(self._card(self.ceo, self.site).latest.what, 'human')

    def test_blank_code_and_due_date_changed_count(self):
        self._log(self.site, 'task_status_done', self.t0, actor=self.pm, action='old')
        self._log(self.site, '', self.t0 + timedelta(hours=1), actor=self.pm, action='blank')
        self.assertEqual(self._card(self.ceo, self.site).latest.what, 'blank')
        self._log(self.site, 'due_date_changed', self.t0 + timedelta(hours=2), actor=self.pm,
                  action='date moved')
        self.assertEqual(self._card(self.ceo, self.site).latest.what, 'date moved')

    def test_no_rows_means_no_latest_and_quiet_days_from_activation_else_creation(self):
        """Edge case 2."""
        Project.objects.filter(pk=self.site.pk).update(
            activated_at=timezone.make_aware(datetime(2026, 11, 1, 12, 0)))
        card = self._card(self.ceo, self.site)
        self.assertIsNone(card.latest)
        self.assertEqual(card.feed, ())
        self.assertEqual(card.quiet_days, (FROZEN_TODAY - date(2026, 11, 1)).days)
        draft = self._bare(status='Draft')
        Project.objects.filter(pk=draft.pk).update(
            created_at=timezone.make_aware(datetime(2026, 11, 4, 12, 0)))
        self.assertEqual(self._card(self.ceo, draft).quiet_days, 5)

    def test_older_human_row_found_behind_more_scheduler_rows_than_the_feed_holds(self):
        """Edge case 3: the newest FEED_LEN + 2 rows are all scheduler rows."""
        self._log(self.site, 'issue_created', self.t0, actor=self.pm, action='the human one')
        for i in range(7):
            self._log(self.site, SCHEDULER_CODES[i % 4], self.t0 + timedelta(hours=i + 1))
        card = self._card(self.ceo, self.site)
        self.assertEqual(card.latest.what, 'the human one')
        self.assertEqual([r.what for r in card.feed], ['the human one'])

    def test_feed_holds_the_newest_five_human_rows_newest_first(self):
        for i in range(7):
            self._log(self.site, 'task_status_done', self.t0 + timedelta(hours=i), actor=self.pm,
                      action=f'row {i}')
        card = self._card(self.ceo, self.site)
        self.assertEqual([r.what for r in card.feed], [f'row {i}' for i in (6, 5, 4, 3, 2)])
        self.assertEqual(card.feed[0].who, self.pm.user.get_full_name())
        self._log(self.site, '', self.t0 + timedelta(hours=9), action='no actor')
        self.assertEqual(self._card(self.ceo, self.site).latest.who, 'System')

    def test_money_rows_follow_user_can_see_project_money(self):
        """Ruling 3: the Design Head sees every site but no money row; the site's PM and the
        portfolio money roles see it."""
        self._log(self.site, 'task_status_done', self.t0, actor=self.pm, action='plain')
        self._log(self.site, 'payment_request_raised', self.t0 + timedelta(hours=1),
                  actor=self.scm, action='Raised ₹50,000')
        for profile in (self.pm, self.ceo, self.finance, self.scm, self.head):
            with self.subTest(role=profile.role, head=profile.is_design_head):
                sees_money = user_can_see_project_money(profile.user, self.site)
                latest = self._card(profile, self.site).latest.what
                self.assertEqual(latest, 'Raised ₹50,000' if sees_money else 'plain')
        self.assertFalse(user_can_see_project_money(self.head.user, self.site))
        self.assertIn('payment_request_raised', MONEY_ACTIVITY_CODES)

    def test_predicate_and_q_agree_on_every_kind_of_code(self):
        codes = ['', 'task_status_done', 'due_date_changed', 'site_drawing_opened',
                 'dc_document_opened', *NON_ACTIVITY_CODES]
        for i, code in enumerate(codes):
            self._log(self.site, code, self.t0 + timedelta(minutes=i))
        kept = set(ActivityLog.objects.filter(project=self.site).filter(human_activity_q())
                   .values_list('action_code', flat=True))
        self.assertEqual(kept, {c for c in codes if is_human_activity(c)})
        self.assertEqual(kept, {'', 'task_status_done', 'due_date_changed'})


# ---------------------------------------------------------------------------
# 4. Overdue and progress parity (D4), N/A (edge case 6)
# ---------------------------------------------------------------------------

class OverdueProgressParityTests(DashFixture):

    def setUp(self):
        self.site = self._bare()
        late = FROZEN_TODAY - timedelta(days=3)
        p1 = self._phase(self.site, 'Install', 1)
        p2 = self._phase(self.site, 'Approvals', 2)
        t = Task
        self._plain_task(p1, 1, task_type=t.INTERNAL, status=t.DONE,
                         completed_at=timezone.now())
        self._plain_task(p1, 2, task_type=t.INTERNAL, status=t.NOT_STARTED, due_date=late)  # overdue
        self._plain_task(p1, 3, task_type=t.INTERNAL, status=t.BLOCKED, due_date=late)      # overdue + blocked
        self._plain_task(p1, 4, task_type=t.INTERNAL, status=t.NOT_STARTED, due_date=late,
                         is_not_applicable=True)                                              # N/A: never
        self._plain_task(p1, 5, task_type=t.INTERNAL, status=t.NOT_STARTED, due_date=late,
                         is_mirror=True)                                                     # mirror: % only
        self._plain_task(p2, 1, task_type=t.EXTERNAL, status=t.NOT_STARTED, due_date=late)  # External: not overdue
        self._plain_task(p2, 2, task_type=t.EXTERNAL, status=t.BLOCKED)                     # External blocked counts
        self._plain_task(p2, 3, task_type=t.INTERNAL, status=t.DONE,
                         completed_at=timezone.now())

    def test_card_overdue_blocked_and_progress_equal_the_pm_card(self):
        card = self._card(self.ceo, self.site)
        ref_pct, ref_overdue, ref_blocked = _reference_pm_card(self.site, FROZEN_TODAY)
        self.assertEqual((card.progress, card.overdue, card.blocked),
                         (ref_pct, ref_overdue, ref_blocked))
        # Internal applicable = 2 done of 5 (mirror in, N/A out); overdue Internal human = 2;
        # blocked = the Internal and the External blocked task.
        self.assertEqual((card.progress, card.overdue, card.blocked), (40, 2, 2))

    def test_each_lane_percent_equals_the_overview_phase_bar(self):
        """project_overview's per-phase rule: Internal, applicable, mirrors in, int()."""
        card = self._card(self.ceo, self.site)
        for phase, lane in zip(ProjectPhase.objects.filter(project=self.site)
                               .order_by('phase_order'), card.lanes):
            internal = [t for t in phase.tasks.all()
                        if t.task_type == 'Internal' and not t.is_not_applicable]
            done = sum(1 for t in internal if t.status == 'Done')
            with self.subTest(phase=phase.phase_name):
                self.assertEqual(lane.percent, int(done / len(internal) * 100) if internal else 0)
                self.assertEqual((lane.total, lane.done), (len(internal), done))

    def test_lane_state_counts_every_applicable_task_including_mirrors_and_external(self):
        install, approvals = self._card(self.ceo, self.site).lanes
        self.assertEqual((install.work_total, install.work_done), (4, 1))   # N/A out, mirror in
        self.assertEqual((approvals.work_total, approvals.work_done), (3, 1))
        self.assertEqual((install.overdue, install.blocked), (2, 1))
        self.assertEqual((approvals.overdue, approvals.blocked), (0, 1))

    def test_not_applicable_tasks_are_never_overdue_and_never_in_progress(self):
        """Edge case 6."""
        Task.objects.filter(phase__project=self.site).update(is_not_applicable=True)
        card = self._card(self.ceo, self.site)
        self.assertEqual((card.progress, card.overdue, card.blocked), (0, 0, 0))
        self.assertTrue(all(lane.state == LANE_EMPTY for lane in card.lanes))

    def test_current_phase_equals_utils_current_phase(self):
        card = self._card(self.ceo, self.site)
        self.assertEqual(card.current_phase, current_phase(self.site).phase_name)
        Task.objects.filter(phase__project=self.site, is_mirror=False).update(status=Task.DONE)
        self.assertEqual(self._card(self.ceo, self.site).current_phase, 'Approvals')

    def test_next_tasks_are_the_soonest_dated_open_human_applicable_tasks(self):
        """Open, dated, human-owned, applicable, any type: T2 and T3 of phase 1 and the
        External T1 of phase 2, all due the same day, so phase then task order decides.
        The N/A T4 and the mirror T5 are dated and open but never listed."""
        card = self._card(self.ceo, self.site)
        self.assertEqual([n.name for n in card.next_tasks], ['T2', 'T3', 'T1'])
        Task.objects.filter(phase__project=self.site, task_name='T2').update(
            due_date=FROZEN_TODAY + timedelta(days=30))
        self.assertEqual([n.name for n in self._card(self.ceo, self.site).next_tasks],
                         ['T3', 'T1', 'T2'])

    def test_active_site_with_no_tasks_renders(self):
        """Edge case 1: the taskless-OPEX shape — a card, progress 0, nothing to crash on."""
        empty = self._bare(project_type='CAPEX')
        card = self._card(self.ceo, empty)
        self.assertEqual((card.progress, card.overdue, card.blocked), (0, 0, 0))
        self.assertEqual(card.lanes, ())
        self.assertIsNone(card.current_phase)
        self.assertEqual(card.schedule.reason, REASON_NOT_ELIGIBLE)


# ---------------------------------------------------------------------------
# 5. Plan slip and schedule reason parity (D6), Execution by task (D5)
# ---------------------------------------------------------------------------

class ScheduleParityTests(DashFixture):

    def _ref_plan(self, site):
        summary = site_plan_summary(Project.objects.get(pk=site.pk))
        if summary is None:
            return None
        return (summary.baseline_finish, summary.forecast_finish, summary.days_behind)

    def _got_plan(self, card):
        s = card.schedule
        return None if s.baseline_finish is None else (s.baseline_finish, s.forecast_finish,
                                                      s.days_behind)

    def _ref_reason(self, site):
        return schedule_site(Project.objects.get(pk=site.pk), apply=False).reason or REASON_SCHEDULED

    def test_on_plan_site(self):
        site = self._site(civil_due=date(2026, 11, 10))
        self._apply(site)
        card = self._card(self.ceo, site)
        self.assertEqual(self._got_plan(card), self._ref_plan(site))
        self.assertEqual(card.schedule.days_behind, 0)
        self.assertEqual(card.schedule.reason, REASON_SCHEDULED)
        self.assertEqual(card.schedule.anchor, date(2026, 11, 10))
        self.assertEqual(card.schedule.band, scheduling.capacity_band(Decimal('40.00')))

    def test_three_days_behind_site(self):
        site = self._site(civil_due=date(2026, 11, 10))
        self._apply(site)
        last = (Task.objects.filter(phase__project=site, baseline_due__isnull=False)
                .order_by('-baseline_due').first())
        Task.objects.filter(pk=last.pk).update(due_date=last.baseline_due + timedelta(days=3))
        card = self._card(self.ceo, site)
        self.assertEqual(self._got_plan(card), self._ref_plan(site))
        self.assertEqual(card.schedule.days_behind, 3)
        self.assertEqual(card.urgency[0], 3)

    def test_excluded_site_with_a_leftover_baseline_shows_no_slip(self):
        """Edge case 4: the UKRU001 shape."""
        site = self._site(civil_due=date(2026, 11, 10))
        self._apply(site)
        Project.objects.filter(pk=site.pk).update(exclude_from_auto_schedule=True)
        self.assertTrue(Task.objects.filter(phase__project=site, baseline_due__isnull=False).exists())
        card = self._card(self.ceo, site)
        self.assertIsNone(self._ref_plan(site))
        self.assertIsNone(self._got_plan(card))
        self.assertIsNone(card.schedule.days_behind)
        self.assertEqual(card.schedule.reason, REASON_EXCLUDED)
        # DASH-B2 ruling 2: an excluded OPEX site still has its Execution lane (no plan).
        self.assertEqual(card.execution.name, EXECUTION_LANE_NAME)
        self.assertEqual(card.execution.work_total, self._block_task_count(site))

    def test_unscheduled_site_has_no_plan(self):
        site = self._site()
        card = self._card(self.ceo, site)
        self.assertIsNone(self._ref_plan(site))
        self.assertIsNone(self._got_plan(card))
        self.assertEqual(card.schedule.reason, REASON_NEEDS_ANCHOR)

    def test_reason_equals_schedule_site_for_every_kind_of_site(self):
        cases = {
            REASON_SCHEDULED: self._site(civil_due=date(2026, 11, 10)),
            REASON_NEEDS_ANCHOR: self._site(),
            REASON_NEEDS_CAPACITY: self._site(dc=None, civil_due=date(2026, 11, 10)),
            REASON_EXCLUDED: self._site(civil_due=date(2026, 11, 10),
                                        exclude_from_auto_schedule=True),
            'on hold (not_eligible)': self._site(civil_due=date(2026, 11, 10), status='On Hold'),
            'residential (not_eligible)': self._bare(),
            'capex (not_eligible)': self._bare(project_type='CAPEX'),
            'draft opex (not_eligible)': self._bare(project_type='OPEX', status='Draft'),
        }
        cards = {c.id: c for c in build_site_cards(_fresh(self.ceo.user))}
        for label, site in cases.items():
            with self.subTest(case=label):
                self.assertEqual(cards[site.pk].schedule.reason, self._ref_reason(site))
                if label.endswith('(not_eligible)'):
                    self.assertEqual(cards[site.pk].schedule.reason, REASON_NOT_ELIGIBLE)
                    self.assertIsNone(cards[site.pk].schedule.days_behind)

    def test_execution_lane_holds_the_block_tasks_by_task_not_phase(self):
        """D5: a block task moved into another phase is still Execution; a mirror or a
        location copy in a block phase is not."""
        site = self._site(civil_due=date(2026, 11, 10))
        block_ids = set(scheduling.ScheduleBlockTask.objects.filter(block__is_active=True)
                        .values_list('template_task_id', flat=True))

        def expected_execution():
            return sum(1 for t in Task.objects.filter(phase__project=site)
                       if scheduling._in_scope(t) and t.template_task_id in block_ids
                       and not t.is_not_applicable)

        card = self._card(self.ceo, site)
        self.assertEqual(card.execution.name, EXECUTION_LANE_NAME)
        self.assertEqual(card.execution.work_total, expected_execution())
        before = card.execution.work_total

        module = self._task(site, MODULE)
        closeout = ProjectPhase.objects.filter(project=site).order_by('-phase_order').first()
        closeout_before = next(l for l in card.lanes if l.name == closeout.phase_name).work_total
        Task.objects.filter(pk=module.pk).update(phase=closeout)
        card = self._card(self.ceo, site)
        self.assertEqual(card.execution.work_total, before)
        self.assertEqual(next(l for l in card.lanes if l.name == closeout.phase_name).work_total,
                         closeout_before)

        civil = self._task(site, CIVIL)
        self._plain_task(civil.phase, 90, task_name='Civil copy',
                         template_task=civil.template_task, location_label='Roof B')
        self._plain_task(civil.phase, 91, task_name='Mirror in block phase',
                         template_task=civil.template_task, is_mirror=True)
        lane_before = next(l for l in card.lanes if l.name == civil.phase.phase_name).work_total
        card = self._card(self.ceo, site)
        self.assertEqual(card.execution.work_total, before)
        self.assertEqual(next(l for l in card.lanes if l.name == civil.phase.phase_name).work_total,
                         lane_before + 2)

    def _block_task_count(self, site):
        """The applicable Execution tasks of `site`: the scheduler's scope, in a block."""
        block_ids = set(scheduling.ScheduleBlockTask.objects.filter(block__is_active=True)
                        .values_list('template_task_id', flat=True))
        return sum(1 for t in Task.objects.filter(phase__project=site)
                   if scheduling._in_scope(t) and t.template_task_id in block_ids
                   and not t.is_not_applicable)

    def test_unscheduled_opex_site_has_an_execution_lane_too(self):
        """DASH-B2 ruling 2: every non-draft OPEX site splits Execution out, scheduled or
        not, by the same by-task rule; lanes + Execution still hold every task once."""
        site = self._site()
        card = self._card(self.ceo, site)
        self.assertEqual(card.schedule.reason, REASON_NEEDS_ANCHOR)
        self.assertEqual(card.execution.name, EXECUTION_LANE_NAME)
        self.assertEqual(card.execution.work_total, self._block_task_count(site))
        self.assertEqual(sum(l.work_total for l in card.lanes) + card.execution.work_total,
                         Task.objects.filter(phase__project=site, is_not_applicable=False).count())
        # The template's Execution tasks are all in Installation and Testing & Commissioning,
        # so both lanes are left empty, as on a scheduled site.
        for name in ('Installation', 'Testing & Commissioning'):
            self.assertEqual(next(l for l in card.lanes if l.name == name).state, LANE_EMPTY)

    def test_residential_capex_and_draft_sites_report_phases_as_they_are(self):
        residential = self._bare()
        self._plain_task(self._phase(residential, 'Install', 1), 1)
        capex = self._bare(project_type='CAPEX')
        draft = self._bare(project_type='OPEX', status='Draft')
        cards = {c.id: c for c in build_site_cards(_fresh(self.ceo.user))}
        for site in (residential, capex, draft):
            with self.subTest(site=site.project_type, status=site.status):
                self.assertIsNone(cards[site.pk].execution)
                self.assertIsNone(cards[site.pk].execution_segment)
        self.assertEqual(cards[residential.pk].lanes[0].work_total, 1)

    def test_location_copies_feed_the_execution_segment_state_and_dates_only(self):
        """DASH-B2 ruling 2 (UKRU001): a location copy of a block task is outside the
        Execution lane, but the pipeline's Execution segment takes its state and actual
        dates from it. Every count stays where it was."""
        site = self._site(exclude_from_auto_schedule=True)
        civil = self._task(site, CIVIL)
        before = self._card(self.ceo, site)
        self.assertEqual(before.execution_segment, before.execution)
        copy = self._plain_task(civil.phase, 90, task_name='Civil copy',
                                template_task=civil.template_task, location_label='Roof B')
        when = self._ist(8)
        self._done(copy, when)
        card = self._card(self.ceo, site)
        lane, segment = card.execution, card.execution_segment
        # The lane is untouched: the copy is not an Execution task.
        self.assertEqual((lane.work_total, lane.state, lane.actual_start),
                         (before.execution.work_total, before.execution.state, None))
        # The segment reads the copy for state and dates, and nothing else.
        self.assertEqual(segment.state, LANE_IN_PROGRESS)
        self.assertEqual(segment.actual_start, when)
        self.assertIsNone(segment.actual_end)
        for name in ('total', 'done', 'percent', 'work_total', 'work_done', 'overdue', 'blocked'):
            self.assertEqual(getattr(segment, name), getattr(lane, name), name)
        # The copy stays in its phase lane and in the site's progress.
        self.assertEqual(next(l for l in card.lanes if l.name == civil.phase.phase_name).work_total,
                         next(l for l in before.lanes if l.name == civil.phase.phase_name).work_total + 1)
        Task.objects.filter(phase__project=site, template_task__isnull=False, is_mirror=False,
                            location_label='').update(status=Task.DONE, completed_at=when)
        segment = self._card(self.ceo, site).execution_segment
        self.assertEqual((segment.state, segment.actual_end), (LANE_DONE, when))

    def test_a_manual_date_inside_a_block_is_counted_normally_and_flagged(self):
        """Edge case 7."""
        site = self._site(civil_due=date(2026, 11, 10))
        self._apply(site)
        before = self._card(self.ceo, site)
        self.assertEqual(before.schedule.manual_dates, 0)
        module = self._task(site, MODULE)
        self._set_due(module, date(2026, 11, 30))
        card = self._card(self.ceo, site)
        self.assertEqual(card.schedule.manual_dates, 1)
        # Counted normally: still an Execution task, and its date feeds the plan figures.
        self.assertEqual(card.execution.work_total, before.execution.work_total)
        self.assertEqual(self._got_plan(card), self._ref_plan(site))
        self.assertEqual(card.schedule.forecast_finish,
                         max(card.schedule.forecast_finish, date(2026, 11, 30)))

    def test_execution_lane_state_and_dates(self):
        site = self._site(civil_due=date(2026, 11, 10))
        civil = self._task(site, CIVIL)
        when = self._ist(8)
        self._done(civil, when)
        lane = self._card(self.ceo, site).execution
        self.assertEqual(lane.state, LANE_IN_PROGRESS)
        self.assertEqual(lane.actual_start, when)
        self.assertIsNone(lane.actual_end)
        Task.objects.filter(phase__project=site, template_task__isnull=False, is_mirror=False,
                            location_label='').update(status=Task.DONE, completed_at=when)
        lane = self._card(self.ceo, site).execution
        self.assertEqual(lane.state, LANE_DONE)
        self.assertEqual(lane.actual_end, when)


# ---------------------------------------------------------------------------
# 6. Sort (D7), capacity (D10), programs (edge case 10), today (edge case 9)
# ---------------------------------------------------------------------------

def _card(project_id, *, draft=False, behind=None, load=0, latest=None, created=None, **kw):
    """A SiteCard with only what the sort reads set; everything else neutral."""
    at = timezone.make_aware(latest) if latest else None
    return SiteCard(
        id=hash(project_id) % 100000, project_id=project_id, code=project_id, site_name='',
        city='', project_type='OPEX', status='Draft' if draft else 'Active', is_draft=draft,
        program_id=None, program_code=None, program_name=None, pm_name=None,
        dc_kwp=kw.get('dc'), ac_kw=kw.get('ac'), large_site=False, activated_at=None,
        created_at=timezone.make_aware(created or datetime(2026, 1, 1)), target_date=None,
        current_phase=None, lanes=(), execution=None, progress=0, overdue=load, blocked=0,
        open_issues=0, dc_counts={}, attendance_today=None, payment_open=None, latest=None,
        feed=(), next_tasks=(),
        schedule=Schedule(reason='x', band=None, anchor=None, baseline_finish=None,
                          forecast_finish=None, days_behind=behind, manual_dates=0),
        quiet_days=0, urgency=(behind or 0, load, at))


class SortTests(SimpleTestCase):

    def setUp(self):
        self.cards = [
            _card('C-new', latest=datetime(2026, 11, 9, 9)),
            _card('B-old', latest=datetime(2026, 11, 1, 9), behind=5, load=1),
            _card('A-mid', latest=datetime(2026, 11, 5, 9), behind=None, load=9),
            _card('D-none', latest=None, behind=2),
            _card('Z-draft-old', draft=True, created=datetime(2026, 10, 1), behind=99, load=99,
                  latest=datetime(2026, 11, 9, 10)),
            _card('Y-draft-new', draft=True, created=datetime(2026, 11, 1)),
        ]

    def _order(self, order):
        return [c.project_id for c in sort_cards(self.cards, order)]

    def test_latest_activity_order_with_no_activity_last_and_drafts_at_the_bottom(self):
        self.assertEqual(self._order(ORDER_LATEST),
                         ['C-new', 'A-mid', 'B-old', 'D-none', 'Y-draft-new', 'Z-draft-old'])

    def test_most_urgent_order_days_behind_then_load_then_latest(self):
        # B 5 behind; D 2 behind; then the no-figure sites as 0: A (load 9) before C (load 0).
        self.assertEqual(self._order(ORDER_URGENT),
                         ['B-old', 'D-none', 'A-mid', 'C-new', 'Y-draft-new', 'Z-draft-old'])

    def test_a_site_with_no_days_behind_figure_sorts_as_zero(self):
        cards = [_card('Q', behind=None, load=1), _card('P', behind=0, load=1)]
        self.assertEqual([c.project_id for c in sort_cards(cards, ORDER_URGENT)], ['P', 'Q'])

    def test_identical_figures_and_timestamps_fall_to_project_id(self):
        """Edge case 8."""
        same = datetime(2026, 11, 8, 12)
        cards = [_card(pid, latest=same, behind=1, load=2) for pid in ('S-3', 'S-1', 'S-2')]
        for order in (ORDER_LATEST, ORDER_URGENT):
            with self.subTest(order=order):
                self.assertEqual([c.project_id for c in sort_cards(cards, order)],
                                 ['S-1', 'S-2', 'S-3'])
                self.assertEqual([c.project_id for c in sort_cards(list(reversed(cards)), order)],
                                 ['S-1', 'S-2', 'S-3'])

    def test_unknown_order_is_refused(self):
        with self.assertRaises(ValueError):
            sort_cards(self.cards, 'alphabetical')

    def test_capacity_totals_skip_blanks_and_count_them(self):
        cards = [_card('1', dc=Decimal('40'), ac=None), _card('2', dc=Decimal('60.5'), ac=Decimal('50')),
                 _card('3', dc=None, ac=None)]
        totals = capacity_totals(cards)
        self.assertEqual((totals.dc_kwp, totals.dc_missing), (Decimal('100.5'), 1))
        self.assertEqual((totals.ac_kw, totals.ac_missing), (Decimal('50'), 2))
        none = capacity_totals([_card('4')])
        self.assertEqual((none.dc_kwp, none.dc_missing, none.ac_kw, none.ac_missing),
                         (None, 1, None, 1))


class CardFieldTests(DashFixture):

    def test_blank_capacity_stays_none_and_large_site_flag(self):
        small = self._bare(dc_capacity_kw=Decimal('40'))
        large = self._bare(dc_capacity_kw=Decimal('300.01'), ac_capacity_kw=Decimal('250'))
        blank = self._bare()
        cards = {c.id: c for c in build_site_cards(_fresh(self.ceo.user))}
        self.assertEqual((cards[small.pk].dc_kwp, cards[small.pk].ac_kw), (Decimal('40'), None))
        self.assertIsNone(cards[blank.pk].dc_kwp)
        self.assertFalse(cards[small.pk].large_site)
        self.assertTrue(cards[large.pk].large_site)
        self.assertFalse(cards[blank.pk].large_site)

    def test_programs_with_no_visible_site_never_appear(self):
        """Edge case 10."""
        mine = Program.objects.create(name='Mine', program_type='OPEX', client_name='C',
                                      status='Active', short_tender_code='MINE')
        theirs = Program.objects.create(name='Theirs', program_type='OPEX', client_name='C',
                                        status='Active', short_tender_code='THEIRS')
        self._bare(project_type='OPEX', program=mine)
        self._bare(project_type='OPEX', program=theirs, pm=self.other_pm)
        pm_programs = programs_on_cards(build_site_cards(_fresh(self.pm.user)))
        self.assertEqual(pm_programs, [(mine.pk, 'MINE', 'Mine')])
        ceo_programs = {p[0] for p in programs_on_cards(build_site_cards(_fresh(self.ceo.user)))}
        self.assertEqual(ceo_programs, {mine.pk, theirs.pk})

    def test_today_is_the_ist_day_near_midnight(self):
        """Edge case 9: 00:10 IST on 9 Nov is 8 Nov in UTC; the card counts it as today."""
        site = self._bare()
        self._log(site, '', timezone.make_aware(datetime(2026, 11, 9, 0, 10)), actor=self.pm)
        self.assertEqual(self._card(self.ceo, site).quiet_days, 0)
        ActivityLog.objects.filter(project=site).update(
            timestamp=timezone.make_aware(datetime(2026, 11, 8, 23, 50)))
        self.assertEqual(self._card(self.ceo, site).quiet_days, 1)

    def test_attendance_today_is_the_headcount_logged_today_only(self):
        site = self._bare()
        self.assertIsNone(self._card(self.ceo, site).attendance_today)
        # One crew name per entry: a typed contractor may log one trade once a day
        # (uniq_attendance_unlisted_trade_day).
        for crew, (day, count, voided) in enumerate((
                (FROZEN_TODAY, 6, False), (FROZEN_TODAY, 4, False), (FROZEN_TODAY, 9, True),
                (FROZEN_TODAY - timedelta(days=1), 7, False))):
            void = ({'is_voided': True, 'void_note': 'duplicate', 'voided_by': self.pm,
                     'voided_at': timezone.now()} if voided else {})
            ContractorAttendance.objects.create(project=site, date=day, trade='CIVIL',
                                                headcount=count, time_in='09:00',
                                                recorded_by=self.pm,
                                                unlisted_contractor_name=f'Crew {crew}', **void)
        self.assertEqual(self._card(self.ceo, site).attendance_today, 10)

    def test_next_tasks_flag_external_tasks(self):
        """DASH-B2 E7: the card tags an External task in its "Next due" list."""
        site = self._bare()
        phase = self._phase(site, 'Install', 1)
        self._plain_task(phase, 1, task_name='Ours', task_type=Task.INTERNAL,
                         due_date=FROZEN_TODAY + timedelta(days=1))
        self._plain_task(phase, 2, task_name='Theirs', task_type=Task.EXTERNAL,
                         due_date=FROZEN_TODAY + timedelta(days=2))
        card = self._card(self.ceo, site)
        self.assertEqual([(t.name, t.external) for t in card.next_tasks],
                         [('Ours', False), ('Theirs', True)])


# ---------------------------------------------------------------------------
# 7. Query budget (Task 4)
# ---------------------------------------------------------------------------

class QueryBudgetTests(DashFixture):

    def _populate(self, n):
        t0 = timezone.make_aware(datetime(2026, 11, 2, 9))
        for _ in range(n):
            site = self._bare()
            phase = self._phase(site, 'Install', 1)
            self._plain_task(phase, 1, task_type=Task.INTERNAL, due_date=FROZEN_TODAY)
            self._plain_task(phase, 2, task_type=Task.INTERNAL, status=Task.DONE,
                             completed_at=t0)
            self._log(site, 'task_status_done', t0, actor=self.pm)
            self._log(site, 'due_dates_scheduled', t0 + timedelta(hours=1))

    def _count(self, profile):
        user = _fresh(profile.user)
        with CaptureQueriesContext(connection) as captured:
            cards = build_site_cards(user)
        return len(captured.captured_queries), len(cards)

    def test_the_same_number_of_queries_for_3_sites_and_30_sites(self):
        self._site(civil_due=date(2026, 11, 10))      # one scheduled OPEX site in both runs
        self._populate(2)
        for profile in (self.ceo, self.pm, self.head):
            with self.subTest(role=profile.role, head=profile.is_design_head):
                n3, sites3 = self._count(profile)
                self.assertEqual(sites3, 3)
        counts3 = {p.pk: self._count(p)[0] for p in (self.ceo, self.pm, self.head)}
        self._populate(27)
        for profile in (self.ceo, self.pm, self.head):
            with self.subTest(role=profile.role, head=profile.is_design_head):
                user = _fresh(profile.user)
                with self.assertNumQueries(counts3[profile.pk]):
                    cards = build_site_cards(user)
                self.assertEqual(len(cards), 30)
