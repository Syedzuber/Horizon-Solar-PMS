"""
Prompt 1.3b — mirror tasks leave the metrics, through one helper.

`Task.is_mirror` arrived inert in 1.3a. This module is what makes it mean
something: a mirror's status is derived from another object and no human may
write it, so counting one as somebody's work attributes another team's queue to
the wrong person. `utils.human_owned_tasks_q()` / `utils.is_human_owned()` are
the only places that rule is written down, and every task METRIC routes through
one of them.

TWO KINDS OF TEST HERE, AND THE STRUCTURAL ONE IS THE DURABLE ONE.

The behavioural tests pin the counters that exist today. The structural sweep
(`MirrorExclusionSweepTests`) is what survives the next session: it builds a
project holding exactly ONE mirror and ONE otherwise-identical non-mirror, then
walks the WHOLE template context of every dashboard and asserts that no integer
in it equals 2. A counter that forgot the exclusion counts the pair and reads 2,
and the assertion names the context key it came from. A counter ADDED later is
covered automatically — it does not have to be registered anywhere, which is the
failure mode of a hand-maintained list.

WHY "NO INTEGER MAY BE 2" IS SAFE AND NOT A TRICK. The fixture is deliberately
minimal — one project, one phase, one task pair, one user per role — so a
correct counter over it can only be 0 or 1. Anything else is either a mirror
being counted or an unrelated number that happens to be 2, and every one of the
latter is named in ALLOWED_TWOS below with its reason. That list IS the pin:
adding a legitimate 2 is a visible diff, and forgetting the exclusion is not.

The sweep runs over FOUR fixture shapes (open/due-today, blocked, done,
in-progress/overdue) because a single status cannot exercise a status-specific
counter. Each shape re-points the same pair.

WHAT THIS MODULE DOES *NOT* PROVE, stated so nobody reads more into it.
`attach_residential_template()` does not copy `is_mirror` from the template task
onto the Task row — it copies six fields and this is not one of them, despite
models.py:418 calling it "the seventh snapshot". So no attach path can produce a
mirror today, and every fixture below sets `is_mirror` by hand. These tests
prove the COUNTERS are right; they cannot prove the pipeline feeds them, and it
does not yet. See EXECUTION_MODULE_DEFERRED.md §B — it is a hard pre-condition
on 1.3c.
"""
import json
from datetime import date, timedelta

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from .models import Project, ProjectPhase, Task, UserProfile
from .reports import build_user_status_rows
from .utils import human_owned_tasks_q, is_human_owned


def _make_user(username, role):
    # A post_save signal auto-creates the UserProfile — fetch and set the role
    # rather than creating a second one (OneToOne).
    user = User.objects.create_user(username=username, password='pw12345')
    profile = user.profile
    profile.role = role
    profile.save(update_fields=['role'])
    return user, profile


def _make_project(pm_profile, design_profile=None, **kwargs):
    """An ACTIVE, activated, non-deleted project — the state every counter's
    active-project predicate requires. A Draft or un-activated project is
    invisible to most of them and would make a passing test prove nothing."""
    defaults = dict(
        customer_name='Mirror Metrics Co',
        customer_phone='9876500011',
        site_address='1 Derived Way',
        city='Lucknow',
        project_type='Residential',
        status='Active',
        assigned_pm=pm_profile,
        activated_at=timezone.now(),
    )
    defaults.update(kwargs)
    project = Project.objects.create(**defaults)
    if design_profile is not None:
        project.assigned_design = design_profile
        project.save(update_fields=['assigned_design'])
    return project


def _make_pair(phase, owner, **task_kwargs):
    """The heart of every test here: two tasks IDENTICAL in every field a
    counter can filter on, differing only in `is_mirror`.

    That identity is the whole design. If the two differed in status, role,
    type or due date, a counter could return 1 for a reason unrelated to the
    exclusion and the test would pass while proving nothing.
    """
    defaults = dict(
        assigned_role=Task.PM,
        task_type=Task.INTERNAL,
        status=Task.NOT_STARTED,
        assigned_to=owner,
        due_date=date.today(),
    )
    defaults.update(task_kwargs)
    real = Task.objects.create(
        phase=phase, task_name='Net Metering Approval', task_order=1,
        is_mirror=False, **defaults
    )
    mirror = Task.objects.create(
        phase=phase, task_name='COD', task_order=2,
        is_mirror=True, **defaults
    )
    return real, mirror


# ---------------------------------------------------------------------------
# 1 — The helper itself
# ---------------------------------------------------------------------------

class HelperTests(TestCase):
    """The definition, before any consumer of it."""

    def setUp(self):
        self.pm_user, self.pm = _make_user('mm_helper_pm', 'PM')
        self.project = _make_project(self.pm)
        self.phase = ProjectPhase.objects.create(
            project=self.project, phase_name='Closeout', phase_order=1)
        self.real, self.mirror = _make_pair(self.phase, self.pm)

    def test_q_matches_only_the_non_mirror(self):
        matched = Task.objects.filter(human_owned_tasks_q())
        self.assertEqual(list(matched), [self.real])

    def test_q_is_positive_not_a_negation(self):
        """Load-bearing on the CEO dashboard: a negated Q across the
        multi-valued phases__tasks relation takes Django's exclude() subquery
        path instead of a SQL FILTER, changing the join fan-out the two card
        counts deliberately share."""
        self.assertNotIn('NOT ', str(human_owned_tasks_q()))
        self.assertIn('is_mirror', str(human_owned_tasks_q()))

    def test_prefix_reaches_task_through_a_relation(self):
        hit = Project.objects.filter(
            pk=self.project.pk).filter(human_owned_tasks_q('phases__tasks__'))
        # One row per matching task — one non-mirror, so one row.
        self.assertEqual(hit.count(), 1)

    def test_python_predicate_agrees_with_the_q(self):
        self.assertTrue(is_human_owned(self.real))
        self.assertFalse(is_human_owned(self.mirror))


# ---------------------------------------------------------------------------
# 2 — The CEO per-user report, and its row-sum invariant
# ---------------------------------------------------------------------------

class UserStatusReportTests(TestCase):
    """`build_user_status_rows()` — the one counter with a stated arithmetic
    invariant, and (until this module) no test at all. See deferred G5."""

    def setUp(self):
        self.pm_user, self.pm = _make_user('mm_report_pm', 'PM')
        self.project = _make_project(self.pm)
        self.phase = ProjectPhase.objects.create(
            project=self.project, phase_name='Closeout', phase_order=1)

    def _row(self):
        report = build_user_status_rows(date.today())
        rows = [r for r in report['rows'] if r['profile'].pk == self.pm.pk]
        return rows[0] if rows else None

    def test_a_mirror_assigned_to_a_real_user_is_not_their_work(self):
        _make_pair(self.phase, self.pm)
        row = self._row()
        self.assertEqual(row['tasks_assigned'], 1)
        self.assertEqual(row['not_started'], 1)

    def test_a_mirror_alone_gives_a_user_no_task_count(self):
        """With ONLY a mirror, this person has no work.

        The row still EXISTS — they are the project's assigned_pm, which is a
        direct link and nothing to do with tasks — but every task column on it
        must be zero.
        """
        Task.objects.create(
            phase=self.phase, task_name='COD', task_order=1,
            assigned_role=Task.PM, task_type=Task.INTERNAL,
            status=Task.NOT_STARTED, assigned_to=self.pm,
            due_date=date.today(), is_mirror=True,
        )
        row = self._row()
        self.assertEqual(
            (row['tasks_assigned'], row['not_started']), (0, 0),
            'A user holding nothing but mirrors carries a task count on the '
            'per-user status report. Mirrors are nobody\'s work.'
        )

    def test_projects_assigned_task_derived_half_excludes_mirrors(self):
        """A second project reachable ONLY through a mirror must not appear in
        this user's project count — the task-derived half of the union rides
        the same base queryset."""
        se_user, se = _make_user('mm_report_se', 'Site Engineer')
        other = _make_project(self.pm, customer_name='Mirror Only Site')
        other_phase = ProjectPhase.objects.create(
            project=other, phase_name='Closeout', phase_order=1)
        Task.objects.create(
            phase=other_phase, task_name='HOTO', task_order=1,
            assigned_role=Task.SITE_ENGINEER, task_type=Task.INTERNAL,
            status=Task.NOT_STARTED, assigned_to=se,
            due_date=date.today(), is_mirror=True,
        )
        report = build_user_status_rows(date.today())
        rows = [r for r in report['rows'] if r['profile'].pk == se.pk]
        self.assertEqual(
            rows, [],
            'A project reachable only through a mirror counted towards '
            'projects_assigned. Site Engineers reach projects ONLY through '
            'tasks, so this is their entire project list.'
        )

    # THE ROW-SUM INVARIANT, with mirrors present.
    #
    # not_started + in_progress + completed + blocked == tasks_assigned is the
    # existing guard against a join fan-out: the four are a partition of
    # Task.STATUS_CHOICES over one base queryset, so a stray join would break
    # the equality before it broke anything visible. `overdue` and `done_today`
    # OVERLAP the four by design and are not part of the sum.
    #
    # It was NOT previously pinned by any test — the prompt believed it was.
    def test_row_sum_invariant_holds_with_mirrors_present(self):
        statuses = [Task.NOT_STARTED, Task.IN_PROGRESS, Task.DONE, Task.BLOCKED]
        for i, status in enumerate(statuses):
            _make_pair(
                self.phase, self.pm, status=status,
                due_date=date.today() - timedelta(days=1),
            )
            # task_order collides across pairs; harmless, nothing here orders.
        report = build_user_status_rows(date.today())
        for row in report['rows'] + [report['totals']]:
            self.assertEqual(
                row['not_started'] + row['in_progress']
                + row['completed'] + row['blocked'],
                row['tasks_assigned'],
                'The four status columns no longer partition tasks_assigned. '
                'With mirrors present this means the exclusion was applied to '
                'some columns and not others, or a join fanned rows out.'
            )

    def test_row_sum_invariant_counts_only_the_non_mirror_half(self):
        """The invariant holding is necessary but not sufficient — it would
        also hold if mirrors were counted everywhere. Pin the magnitude too."""
        for status in [Task.NOT_STARTED, Task.IN_PROGRESS, Task.DONE, Task.BLOCKED]:
            _make_pair(self.phase, self.pm, status=status)
        row = self._row()
        self.assertEqual(row['tasks_assigned'], 4)   # 4 pairs, 4 non-mirrors


# ---------------------------------------------------------------------------
# 3 — The EOD digest counters, including the send gate
# ---------------------------------------------------------------------------

class EodDigestCounterTests(TestCase):
    """The digest's three task counters. Two of them gate whether an email is
    sent at all, so a mirror here does not merely misreport — it emails
    somebody about work they cannot do."""

    def setUp(self):
        self.pm_user, self.pm = _make_user('mm_eod_pm', 'PM')
        self.coord_user, self.coord = _make_user('mm_eod_coord', 'Project Coordinator')
        self.project = _make_project(self.pm)
        self.project.coordinators.add(self.coord)
        self.phase = ProjectPhase.objects.create(
            project=self.project, phase_name='Closeout', phase_order=1)

    def test_per_user_open_task_count_excludes_mirrors(self):
        _make_pair(self.phase, self.pm)
        from projects.utils import human_owned_tasks_q as _q
        from django.db.models import Count
        assigned = dict(
            Task.objects
            .filter(assigned_to__in=[self.pm.pk])
            .filter(_q())
            .exclude(status=Task.DONE)
            .values_list('assigned_to')
            .annotate(c=Count('id'))
            .values_list('assigned_to', 'c')
        )
        self.assertEqual(assigned.get(self.pm.pk), 1)

    def test_company_totals_excludes_mirrors(self):
        from projects.management.commands.send_eod_digest import Command
        _make_pair(self.phase, self.pm)
        totals = Command()._company_totals(date.today())
        self.assertEqual(totals['assigned'], 1)

    def test_coordinator_open_task_count_excludes_mirrors(self):
        """The worst of the three — it has no assignment term at all, so every
        mirror on every coordinated site counted."""
        from django.db.models import Count
        _make_pair(self.phase, self.pm)
        rows = (
            Task.objects
            .filter(phase__project__coordinators__in=[self.coord.pk],
                    phase__project__is_deleted=False,
                    phase__project__activated_at__isnull=False)
            .filter(human_owned_tasks_q())
            .exclude(phase__project__status='Cancelled')
            .exclude(status=Task.DONE)
            .values_list('phase__project__coordinators')
            .annotate(c=Count('id', distinct=True))
        )
        self.assertEqual(dict(rows).get(self.coord.pk), 1)

    def test_a_coordinator_with_only_mirrors_has_no_open_work(self):
        """The gate, not the number. `has_open_work` rides straight off this
        counter, so a mirror-only coordinator must count zero."""
        from django.db.models import Count
        Task.objects.create(
            phase=self.phase, task_name='COD', task_order=1,
            assigned_role=Task.PM, task_type=Task.INTERNAL,
            status=Task.NOT_STARTED, assigned_to=self.pm,
            due_date=date.today(), is_mirror=True,
        )
        rows = (
            Task.objects
            .filter(phase__project__coordinators__in=[self.coord.pk],
                    phase__project__is_deleted=False,
                    phase__project__activated_at__isnull=False)
            .filter(human_owned_tasks_q())
            .exclude(status=Task.DONE)
            .values_list('phase__project__coordinators')
            .annotate(c=Count('id', distinct=True))
        )
        self.assertEqual(
            dict(rows).get(self.coord.pk, 0), 0,
            'A coordinator whose only open task is a mirror still counts as '
            'having open work, so the EOD digest would email them about it.'
        )


# ---------------------------------------------------------------------------
# 4 — Lists must still SHOW mirrors. This session removed them from metrics,
#     not from views, and that distinction needs a test or it will erode.
# ---------------------------------------------------------------------------

class MirrorsStayVisibleTests(TestCase):
    """The counterweight to everything above."""

    def setUp(self):
        self.client = Client(SERVER_NAME='localhost')
        self.pm_user, self.pm = _make_user('mm_vis_pm', 'PM')
        self.project = _make_project(self.pm)
        self.phase = ProjectPhase.objects.create(
            project=self.project, phase_name='Closeout', phase_order=1)
        self.real, self.mirror = _make_pair(self.phase, self.pm)
        self.client.force_login(self.pm_user)

    def test_project_overview_still_lists_the_mirror(self):
        resp = self.client.get(
            reverse('project_overview', args=[self.project.project_id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(
            resp, 'COD',
            msg_prefix='The mirror vanished from the project overview task '
                       'list. Displaying derived state is the entire reason '
                       'mirrors exist — only METRICS drop them.'
        )

    def test_the_phase_task_list_is_not_filtered(self):
        resp = self.client.get(
            reverse('project_overview', args=[self.project.project_id]))
        phases = resp.context['phases']
        names = {t.task_name for p in phases for t in p.tasks.all()}
        self.assertEqual(names, {'Net Metering Approval', 'COD'})

    def test_and_the_phase_progress_metric_counts_it(self):
        """REVERSED BY PROMPT 1.5, deliberately. Read the reason before restoring it.

        This test used to assert the opposite — `internal_total == 1`, the bar out
        of 1 while the list showed 2 — and called that "the intended asymmetry,
        pinned so it is not 'fixed'". The asymmetry was intended; what it produced
        on screen was not. A phase holding ONLY mirrors rendered "0/0 done" above
        an empty bar while its own card header said "4 tasks", and OPEX has two
        such phases (Design, and Procurement & Delivery's four delivery mirrors).

        The product decision, 1 Sep 2026: one mirror not updated is one task
        pending. So the per-phase bar counts mirrors in BOTH halves.

        THIS IS THE ONLY METRIC THAT COUNTS THEM. Everything else in this module
        still asserts exclusion, and that is the point of the split — R-20 was
        narrowed here, not repealed. The overdue counters, the per-user workload
        counts and R-21's current_phase are all unchanged and still tested above.
        """
        resp = self.client.get(
            reverse('project_overview', args=[self.project.project_id]))
        data = json.loads(resp.context['phase_data_json'])
        row = [d for d in data if d['pk'] == self.phase.pk][0]
        self.assertEqual(
            row['internal_total'], 2,
            'The per-phase progress bar must count the mirror alongside the '
            'entered task (prompt 1.5). If this reads 1, the R-20 exclusion has '
            'been restored to project_overview\'s phase_data_json loop.')
        self.assertEqual(row['pct'], 0)


# ---------------------------------------------------------------------------
# 5 — THE STRUCTURAL SWEEP. The durable half.
# ---------------------------------------------------------------------------

# The fixture holds ONE non-mirror and this many mirrors, all otherwise
# identical. A correct task metric over it reads 0 or 1; one that forgot the
# exclusion reads MIRRORS_PER_FIXTURE + 1.
#
# Six rather than one, purely to make the broken value DISTINCTIVE. With a
# single mirror the tell would be 2, which collides with half the small
# integers a template context legitimately carries (primary keys, loop
# counters, list lengths). Seven collides with almost nothing, so the sweep
# needs barely any allow-list and stays honest.
MIRRORS_PER_FIXTURE = 6
BROKEN_COUNT = MIRRORS_PER_FIXTURE + 1

# Context keys that may legitimately hold BROKEN_COUNT, each with its reason.
# THIS SET IS THE PIN: adding to it is a deliberate, visible diff, and
# forgetting the exclusion on a new counter is not possible without a red test.
ALLOWED_BROKEN_COUNTS = {
    # PROMPT 1.5 — the per-phase progress bar counts mirrors ON PURPOSE, the one
    # carve-out of R-20 in the codebase. A phase holding only mirrors read
    # "0/0 done" above an empty bar while its card header said "4 tasks"; the
    # product decision is that one mirror not updated is one task pending. The
    # sweep's fixture is 1 non-mirror + 6 mirrors in ONE phase, so this metric
    # legitimately reads 7 here — the very number the sweep treats as the tell.
    #
    # THESE TWO KEYS ONLY. Every other counter in every dashboard context is still
    # swept, so a NEW counter that forgets the exclusion still fails without being
    # registered anywhere. If a second phase is ever added to that fixture these
    # keys gain a [1] sibling and must be listed too — the index is part of the key.
    'phase_data_json[0].internal_total',
    # Reached only by the 'done today' shape, where all seven are Done.
    'phase_data_json[0].internal_done',
}

# Key segments that are identities or template machinery, never task metrics.
# A primary key that happens to equal BROKEN_COUNT is noise, not a finding.
_IGNORED_KEY_SEGMENTS = ('pk', 'id', 'forloop')


def _is_ignorable_key(key):
    """True for identity and template-internal keys.

    Matches on the DOTTED SEGMENTS of the path, so `top_assignees[0].assigned_to`
    is ignored (an assignee's profile pk) while `overdue_count` is not.
    """
    for segment in key.replace('[', '.').replace(']', '').split('.'):
        if not segment:
            continue
        if segment in _IGNORED_KEY_SEGMENTS or segment.endswith('_id'):
            return True
        if segment.startswith('assigned_to') or segment.startswith('actor'):
            return True
    return False


class MirrorExclusionSweepTests(TestCase):
    """One project, one phase, one non-mirror + six identical mirrors.

    Every correct counter over this fixture is 0 or 1. The sweep asserts no
    integer anywhere in a dashboard's template context is 7, and names the key
    when one is — so a counter added in a later session is covered without
    being registered anywhere.
    """

    # (label, status, due_date offset in days, completed) — four shapes,
    # because a status-specific counter cannot be exercised by one status.
    SHAPES = [
        ('open, due today',     Task.NOT_STARTED, 0,  False),
        ('blocked',             Task.BLOCKED,     0,  False),
        ('in progress, overdue', Task.IN_PROGRESS, -3, False),
        ('done today',          Task.DONE,        -1, True),
    ]

    def setUp(self):
        self.client = Client(SERVER_NAME='localhost')
        self.ceo_user, self.ceo = _make_user('mm_sweep_ceo', 'CEO')
        self.pm_user, self.pm = _make_user('mm_sweep_pm', 'PM')
        self.se_user, self.se = _make_user('mm_sweep_se', 'Site Engineer')
        self.design_user, self.design = _make_user('mm_sweep_design', 'Design')
        self.scm_user, self.scm = _make_user('mm_sweep_scm', 'SCM')

        self.project = _make_project(self.pm, design_profile=self.design)
        self.phase = ProjectPhase.objects.create(
            project=self.project, phase_name='Closeout', phase_order=1)

    def _build_fixture(self, status, due_offset, completed, owner, role):
        """One non-mirror and MIRRORS_PER_FIXTURE mirrors, identical in every
        field a counter can filter on."""
        Task.objects.filter(phase=self.phase).delete()
        common = dict(
            status=status,
            due_date=date.today() + timedelta(days=due_offset),
            assigned_to=owner,
            assigned_role=role,
            task_type=Task.INTERNAL,
        )
        made = [Task.objects.create(
            phase=self.phase, task_name='Net Metering Approval',
            task_order=1, is_mirror=False, **common)]
        made += [
            Task.objects.create(
                phase=self.phase, task_name=f'COD {i}', task_order=2 + i,
                is_mirror=True, **common)
            for i in range(MIRRORS_PER_FIXTURE)
        ]
        if completed:
            Task.objects.filter(pk__in=[t.pk for t in made]).update(
                completed_at=timezone.now())
        return made

    def _sweep(self, response, label, view_name):
        """Walk every value in the response context; fail on any BROKEN_COUNT.

        Strings that parse as JSON are decoded and walked too — `phase_data_json`
        reaches the template already serialised, and a sweep that stopped at the
        string would silently skip the per-phase progress metric.
        """
        offenders = []

        def _walk(key, value, depth=0):
            if depth > 4 or _is_ignorable_key(key):
                return
            if isinstance(value, bool):
                return
            if isinstance(value, int):
                if value == BROKEN_COUNT and key not in ALLOWED_BROKEN_COUNTS:
                    offenders.append(key)
            elif isinstance(value, dict):
                for k, v in value.items():
                    _walk(f'{key}.{k}', v, depth + 1)
            elif isinstance(value, (list, tuple)):
                for i, v in enumerate(value):
                    _walk(f'{key}[{i}]', v, depth + 1)
            elif isinstance(value, str) and value[:1] in '[{':
                try:
                    decoded = json.loads(value)
                except ValueError:
                    return
                _walk(key, decoded, depth)

        for ctx in (response.context or []):
            for d in ctx.dicts:
                for key, value in d.items():
                    _walk(key, value)

        self.assertEqual(
            sorted(set(offenders)), [],
            f'\n\n{view_name} ({label}): these context values counted ALL '
            f'{BROKEN_COUNT} fixture tasks, i.e. they counted the '
            f'{MIRRORS_PER_FIXTURE} mirrors:\n  '
            + '\n  '.join(sorted(set(offenders)))
            + f'\n\nThe fixture holds {MIRRORS_PER_FIXTURE} mirrors and one '
              'otherwise-identical non-mirror, so a correct task metric reads '
              '0 or 1 here. Route the counter through '
              'utils.human_owned_tasks_q() (or is_human_owned() for a '
              'prefetched list). If the value is not a task count and '
              f'{BROKEN_COUNT} is legitimate, add its key to '
              'ALLOWED_BROKEN_COUNTS in this module with the reason.'
        )

    def _run(self, user, url, view_name, owner, role):
        self.client.force_login(user)
        for label, status, offset, completed in self.SHAPES:
            with self.subTest(view=view_name, shape=label):
                self._build_fixture(status, offset, completed, owner, role)
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 200, view_name)
                self._sweep(resp, label, view_name)

    def test_pm_dashboard(self):
        self._run(self.pm_user, reverse('dashboard_pm'),
                  'dashboard_pm', self.pm, Task.PM)

    def test_ceo_dashboard(self):
        self._run(self.ceo_user, reverse('dashboard_ceo'),
                  'dashboard_ceo', self.pm, Task.PM)

    def test_site_engineer_dashboard(self):
        self._run(self.se_user, reverse('dashboard_site_engineer'),
                  'dashboard_site_engineer', self.se, Task.SITE_ENGINEER)

    def test_design_dashboard(self):
        self._run(self.design_user, reverse('dashboard_design'),
                  'dashboard_design', self.design, Task.DESIGN)

    def test_scm_dashboard(self):
        self._run(self.scm_user, reverse('dashboard_scm'),
                  'dashboard_scm', self.scm, Task.SCM)

    def test_project_overview(self):
        self._run(self.pm_user,
                  reverse('project_overview', args=[self.project.project_id]),
                  'project_overview', self.pm, Task.PM)

    def test_task_drill_downs(self):
        for route in ('tasks_due_today', 'tasks_overdue', 'tasks_due_soon'):
            self._run(self.pm_user, reverse(route), route, self.pm, Task.PM)


class SweepBitesTests(TestCase):
    """Proof the sweep is not vacuous: the same walk, over a context that DOES
    count the mirror, must fail and name the key.

    Without this, a sweep that silently visited nothing would pass forever.
    """

    def _sweep_over(self, payload):
        sweep = MirrorExclusionSweepTests('test_pm_dashboard')
        sweep.setUp()

        class _FakeCtx:
            dicts = [payload]

        class _FakeResponse:
            context = [_FakeCtx()]

        return sweep, _FakeResponse()

    def test_the_sweep_fails_on_an_unexcluded_counter(self):
        sweep, resp = self._sweep_over({'pending_approvals': BROKEN_COUNT})
        with self.assertRaises(AssertionError) as caught:
            sweep._sweep(resp, 'open', 'dashboard_pm')
        self.assertIn('pending_approvals', str(caught.exception))

    def test_the_sweep_descends_into_nested_structures(self):
        sweep, resp = self._sweep_over(
            {'projects': [{'internal_total': BROKEN_COUNT}]})
        with self.assertRaises(AssertionError) as caught:
            sweep._sweep(resp, 'open', 'dashboard_pm')
        self.assertIn('projects[0].internal_total', str(caught.exception))

    def test_the_sweep_decodes_serialised_json(self):
        """`phase_data_json` reaches the template as a string. A sweep that
        stopped there would skip the per-phase progress metric entirely.

        Probes `ext_pending` rather than `internal_total`. Prompt 1.5 put
        `phase_data_json[0].internal_total` into ALLOWED_BROKEN_COUNTS — the
        progress bar counts mirrors on purpose now — so that key can no longer
        demonstrate that the sweep BITES. `ext_pending` is a sibling inside the
        same serialised string, so it still proves the decoding step and would
        still fail if `_walk` stopped at the string.
        """
        sweep, resp = self._sweep_over(
            {'phase_data_json': json.dumps([{'ext_pending': BROKEN_COUNT}])})
        with self.assertRaises(AssertionError) as caught:
            sweep._sweep(resp, 'open', 'dashboard_pm')
        self.assertIn('phase_data_json[0].ext_pending', str(caught.exception))

    def test_the_sweep_passes_a_correct_counter(self):
        """The other half of 'it bites': it must not fire on 0 or 1."""
        sweep, resp = self._sweep_over(
            {'pending_approvals': 1, 'blocked_tasks': 0})
        sweep._sweep(resp, 'open', 'dashboard_pm')   # no raise

    def test_identity_keys_are_ignored_but_metric_keys_are_not(self):
        self.assertTrue(_is_ignorable_key('top_assignees[0].assigned_to'))
        self.assertTrue(_is_ignorable_key('candidates_by_role.PM[0].pk'))
        self.assertTrue(_is_ignorable_key('forloop.counter'))
        self.assertTrue(_is_ignorable_key('rows[0].project_id'))
        self.assertFalse(_is_ignorable_key('pending_approvals'))
        self.assertFalse(_is_ignorable_key('summary.overdue_count'))
        self.assertFalse(_is_ignorable_key('phase_data_json[0].internal_total'))
