"""Not Applicable — a flag beside the status, and every metric that must drop it.

WHY A FLAG AND NOT A FIFTH `STATUS_CHOICES` VALUE. This is the same question 2.1
answered for "submitted" and it is answered the same way. A fifth status would have
been ABSORBED AS OPEN WORK by nine readers that ask "not Done" rather than naming the
statuses they mean — the overdue counts on four dashboards, `current_phase()`, the EOD
digest, `ext_pending`, the drill-down — rendered as the literal words "Not Started"
through the bare `{% else %}` ending three badge chains, appeared in every status
dropdown on every project type the moment it was added, and had no `VALID_TRANSITIONS`
key, which would have frozen the task permanently because
`VALID_TRANSITIONS.get(status, set())` refuses everything for an unknown from-state.
`DailyReportInvariantTests` in tests_two_step_completion.py exists to fail if anyone
folds a state into the status vocabulary. Nothing here touches it.

THE PRODUCT RULE, and it is the one thing that makes N/A different from `is_mirror`:

    N/A leaves BOTH halves of every ratio. A mirror stays IN the progress denominator
    because an undelivered consignment really is outstanding work (R-20's PROGRESS
    half). An N/A task is neither done nor outstanding, so a phase of 8 Done + 2 N/A
    reads 100%, not 80%.

That is why `applicable_tasks_q()` COMPOSES with both R-20 helpers rather than being
folded into either — see its docstring in utils.py.

HOW THIS MODULE IS BUILT, following tests_mirror_metrics.py rather than
tests_progress_vs_workload.py: the fixtures are built BY HAND, not by activating a
template. These are guards over counters, and a fixture with no other moving parts is
what lets a failure name one reader. Where a test needs a pair it uses two tasks
IDENTICAL in every field a counter can filter on, differing only in
`is_not_applicable` — if they differed in status, role, type or due date a counter
could return the right number for the wrong reason.

ONE TEST PER READER, DELIBERATELY NOT COLLAPSED. Every reader listed here was changed
by hand and can regress on its own; a single test over one dashboard would go green
while the other eight rotted.
"""
import json
from datetime import date, timedelta

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from .models import ActivityLog, Project, ProjectPhase, Task, UserProfile
from .reports import build_user_status_rows
from .task_dependencies import incomplete_predecessors
from .utils import applicable_tasks_q, is_applicable, current_phase


# ---------------------------------------------------------------------------
# Fixture helpers — the same shape tests_mirror_metrics.py uses.
# ---------------------------------------------------------------------------

def _make_user(username, role, **profile_fields):
    """A post_save signal auto-creates the UserProfile — fetch and set the role
    rather than creating a second one (OneToOne)."""
    user = User.objects.create_user(username=username, password='pw12345',
                                    email=f'{username}@example.com')
    profile = user.profile
    profile.role = role
    for field, value in profile_fields.items():
        setattr(profile, field, value)
    profile.save()
    return user, profile


def _client_for(user):
    client = Client(SERVER_NAME='localhost')
    client.force_login(user)
    return client


def _make_project(pm_profile, **kwargs):
    """An ACTIVE, activated, non-deleted project — the state every counter's
    active-project predicate requires. A Draft or un-activated project is invisible
    to most of them and would make a passing test prove nothing."""
    defaults = dict(
        customer_name='Not Applicable Co',
        customer_phone='9876500022',
        site_address='2 Out Of Scope Lane',
        city='Lucknow',
        project_type='Residential',
        status='Active',
        assigned_pm=pm_profile,
        activated_at=timezone.now(),
    )
    defaults.update(kwargs)
    return Project.objects.create(**defaults)


def _task(phase, order, owner=None, **kwargs):
    defaults = dict(
        task_name=f'Task {order}',
        task_order=order,
        assigned_role=Task.PM,
        task_type=Task.INTERNAL,
        status=Task.NOT_STARTED,
        assigned_to=owner,
        due_date=date.today(),
    )
    defaults.update(kwargs)
    return Task.objects.create(phase=phase, **defaults)


class NotApplicableBase(TestCase):
    """One active Residential project, one phase, one PM who owns it."""

    def setUp(self):
        self.pm_user, self.pm = _make_user('na_pm', 'PM')
        self.project = _make_project(self.pm)
        self.phase = ProjectPhase.objects.create(
            project=self.project, phase_name='Execution', phase_order=1)
        self.client_pm = _client_for(self.pm_user)

    def _phase_bar(self, index=0):
        """`phase_data_json` reaches the template as a JSON STRING (json.dumps at the
        end of project_overview), not a list — parsed here so a reader of these tests
        sees the same dict the bar is drawn from."""
        response = self.client_pm.get(
            reverse('project_overview', args=[self.project.project_id]))
        return json.loads(response.context['phase_data_json'])[index]

    def _post_na(self, task, marking=True, reason='Site already compliant', client=None):
        return (client or self.client_pm).post(
            reverse('task_set_not_applicable',
                    args=[self.project.project_id, task.pk]),
            {'not_applicable': '1' if marking else '0',
             'not_applicable_reason': reason},
            follow=False,
        )

    def _mark(self, task, reason='Site already compliant'):
        """Mark via the VIEW, never by setting the column — every test that needs an
        N/A task gets one the only way production can produce one."""
        response = self._post_na(task, marking=True, reason=reason)
        task.refresh_from_db()
        assert task.is_not_applicable, f'fixture failed to mark N/A: {response.status_code}'
        return task


# ---------------------------------------------------------------------------
# a — marking works, and the status does NOT move
# ---------------------------------------------------------------------------

class MarkingTests(NotApplicableBase):

    def test_pm_marks_with_a_reason_and_the_status_is_unchanged(self):
        task = _task(self.phase, 1, self.pm, status=Task.IN_PROGRESS)

        self._post_na(task, marking=True, reason='Existing earthing pit is compliant')
        task.refresh_from_db()

        self.assertTrue(task.is_not_applicable)
        # THE POINT OF THE WHOLE DESIGN: the stored status is untouched, which is what
        # makes un-marking a one-field write with nothing to reconstruct.
        self.assertEqual(task.status, Task.IN_PROGRESS)
        self.assertEqual(task.not_applicable_marked_by, self.pm)
        self.assertIsNotNone(task.not_applicable_marked_at)
        self.assertIn('Existing earthing pit is compliant', task.not_applicable_reason)

    def test_marking_writes_an_activity_log_row_with_its_own_action_code(self):
        task = _task(self.phase, 1, self.pm)
        self._post_na(task, marking=True, reason='Not in scope for this site')

        row = ActivityLog.objects.filter(
            project=self.project, action_code='task_marked_not_applicable').first()
        self.assertIsNotNone(row, 'no ActivityLog row for the mark')
        self.assertEqual(row.actor, self.pm)
        self.assertEqual(row.entity_type, 'Task')
        self.assertEqual(row.entity_id, task.pk)

    def test_unmarking_writes_its_own_distinct_action_code(self):
        task = self._mark(_task(self.phase, 1, self.pm))
        self._post_na(task, marking=False, reason='Scope restored by variation order')

        self.assertTrue(ActivityLog.objects.filter(
            project=self.project, action_code='task_unmarked_not_applicable').exists())

    def test_the_status_choices_vocabulary_is_untouched(self):
        """The guard on the whole approach: four statuses, exactly as before."""
        self.assertEqual(
            [value for value, _label in Task.STATUS_CHOICES],
            [Task.NOT_STARTED, Task.IN_PROGRESS, Task.DONE, Task.BLOCKED],
            'a fifth status appeared — this feature exists to avoid exactly that',
        )


# ---------------------------------------------------------------------------
# b — a reason is required in BOTH directions
# ---------------------------------------------------------------------------

class ReasonRequiredTests(NotApplicableBase):

    def test_marking_with_a_blank_reason_is_refused(self):
        task = _task(self.phase, 1, self.pm)
        self._post_na(task, marking=True, reason='   ')
        task.refresh_from_db()
        self.assertFalse(task.is_not_applicable)

    def test_unmarking_with_a_blank_reason_is_refused(self):
        task = self._mark(_task(self.phase, 1, self.pm))
        self._post_na(task, marking=False, reason='')
        task.refresh_from_db()
        self.assertTrue(task.is_not_applicable, 'un-marked without stating why')

    def test_get_is_refused(self):
        task = _task(self.phase, 1, self.pm)
        self.client_pm.get(reverse('task_set_not_applicable',
                                   args=[self.project.project_id, task.pk]))
        task.refresh_from_db()
        self.assertFalse(task.is_not_applicable)


# ---------------------------------------------------------------------------
# c — reversible, and the reason history is APPENDED not overwritten
# ---------------------------------------------------------------------------

class ReversibilityTests(NotApplicableBase):

    def test_unmarking_restores_it_to_open_work_and_keeps_both_reasons(self):
        task = self._mark(_task(self.phase, 1, self.pm),
                          reason='Out of scope per site survey')
        self._post_na(task, marking=False, reason='Client added it back by variation')
        task.refresh_from_db()

        self.assertFalse(task.is_not_applicable)
        self.assertEqual(task.status, Task.NOT_STARTED, 'status moved on un-mark')
        # BOTH reasons survive. A task ruled out in March and pulled back in June has
        # two reasons and the second is worthless without the first to contradict.
        self.assertIn('Out of scope per site survey', task.not_applicable_reason)
        self.assertIn('Client added it back by variation', task.not_applicable_reason)
        self.assertIn('Marked not applicable', task.not_applicable_reason)
        self.assertIn('Un-marked, now applicable', task.not_applicable_reason)

    def test_an_unmarked_task_is_counted_again_everywhere(self):
        """The round trip, asserted on a live reader rather than on the column."""
        task = _task(self.phase, 1, self.pm, due_date=date.today() - timedelta(days=5))
        before = self.client_pm.get(reverse('dashboard_pm')).context['summary']['tasks_overdue']

        self._mark(task)
        during = self.client_pm.get(reverse('dashboard_pm')).context['summary']['tasks_overdue']

        self._post_na(task, marking=False, reason='Applies after all')
        after = self.client_pm.get(reverse('dashboard_pm')).context['summary']['tasks_overdue']

        self.assertEqual(before, 1)
        self.assertEqual(during, 0)
        self.assertEqual(after, 1, 'un-marking did not restore the task to overdue')

    def test_marking_an_already_na_task_again_is_a_no_op(self):
        task = self._mark(_task(self.phase, 1, self.pm), reason='First')
        first_reason = Task.objects.get(pk=task.pk).not_applicable_reason
        self._post_na(task, marking=True, reason='Second')
        self.assertEqual(Task.objects.get(pk=task.pk).not_applicable_reason, first_reason)


# ---------------------------------------------------------------------------
# d, e, f — who may do it, and to what
# ---------------------------------------------------------------------------

class AuthorityTests(NotApplicableBase):
    """The assigned PM alone. This is NARROWER than user_can_manage_project()."""

    def _refused(self, user):
        task = _task(self.phase, 1, self.pm)
        response = self._post_na(task, client=_client_for(user))
        task.refresh_from_db()
        self.assertFalse(task.is_not_applicable)
        return response

    def test_the_assigned_pm_may(self):
        task = _task(self.phase, 1, self.pm)
        self._post_na(task)
        task.refresh_from_db()
        self.assertTrue(task.is_not_applicable)

    def test_a_project_coordinator_is_refused(self):
        """Coordinator authority is additive PM authority everywhere else in the
        portal. Here it is not: marking work out of scope edits what the plan WAS."""
        coord_user, coord = _make_user('na_coord', 'Project Coordinator')
        self.project.coordinators.add(coord)
        response = self._refused(coord_user)
        self.assertEqual(response.status_code, 403)

    def test_a_site_engineer_is_refused(self):
        se_user, _se = _make_user('na_se', 'Site Engineer')
        self._refused(se_user)

    def test_an_admin_is_refused(self):
        """Admin is not the project's PM. Authority here is ownership, not seniority."""
        admin_user, _admin = _make_user('na_admin', 'Admin')
        self._refused(admin_user)

    def test_another_projects_pm_is_refused(self):
        other_user, other_pm = _make_user('na_other_pm', 'PM')
        _make_project(other_pm, customer_phone='9876500099')
        self._refused(other_user)

    def test_a_mirror_task_is_refused(self):
        """A mirror's state is derived; the next sync run would recompute the row and
        sail straight past the flag, leaving a task that reads N/A and is still counted
        as live by the derivation that owns it."""
        mirror = _task(self.phase, 1, self.pm, is_mirror=True, task_name='COD')
        response = self._post_na(mirror)
        mirror.refresh_from_db()
        self.assertEqual(response.status_code, 403)
        self.assertFalse(mirror.is_not_applicable)

    def test_a_done_task_is_refused(self):
        """"Not applicable" and "finished" are contradictory claims about one row, and
        allowing it would move the progress bar DOWN on marking a completed task."""
        done = _task(self.phase, 1, self.pm, status=Task.DONE,
                     completed_at=timezone.now())
        self._post_na(done)
        done.refresh_from_db()
        self.assertFalse(done.is_not_applicable)
        self.assertEqual(done.status, Task.DONE)


# ---------------------------------------------------------------------------
# The predicate itself, before any consumer of it
# ---------------------------------------------------------------------------

class PredicateTests(NotApplicableBase):

    def test_q_matches_only_the_applicable_one_of_an_identical_pair(self):
        live = _task(self.phase, 1, self.pm)
        na   = self._mark(_task(self.phase, 2, self.pm))
        self.assertEqual(list(Task.objects.filter(applicable_tasks_q())), [live])
        self.assertTrue(is_applicable(live))
        self.assertFalse(is_applicable(Task.objects.get(pk=na.pk)))

    def test_the_predicate_is_independent_of_the_mirror_predicate(self):
        """The two flags drop a row from DIFFERENT metrics and must not be merged."""
        mirror = _task(self.phase, 1, self.pm, is_mirror=True)
        self.assertTrue(is_applicable(mirror),
                        'a mirror was treated as not-applicable — the two are separate')


# ---------------------------------------------------------------------------
# g — ONE TEST PER READER. Open-work / overdue readers first.
# ---------------------------------------------------------------------------

class OverdueReaderTests(NotApplicableBase):
    """Every reader that asks "not Done" and would otherwise absorb an N/A task.

    Each builds ONE overdue task, reads the number, marks it N/A and reads again.
    Asserting 1 -> 0 rather than just 0 is what stops a test passing because the
    fixture never reached the reader at all.
    """

    def setUp(self):
        super().setUp()
        self.overdue_day = date.today() - timedelta(days=3)

    def test_reports_build_user_status_rows_overdue(self):
        task = _task(self.phase, 1, self.pm, due_date=self.overdue_day)

        def overdue_for_pm():
            for row in build_user_status_rows(date.today())['rows']:
                if row['profile'].pk == self.pm.pk:
                    return row['overdue']
            return None

        self.assertEqual(overdue_for_pm(), 1)
        self._mark(task)
        self.assertEqual(overdue_for_pm(), 0)

    def test_dashboard_pm_overdue(self):
        task = _task(self.phase, 1, self.pm, due_date=self.overdue_day)
        read = lambda: self.client_pm.get(
            reverse('dashboard_pm')).context['summary']['tasks_overdue']
        self.assertEqual(read(), 1)
        self._mark(task)
        self.assertEqual(read(), 0)

    def test_dashboard_pm_per_project_overdue_counter(self):
        task = _task(self.phase, 1, self.pm, due_date=self.overdue_day)
        read = lambda: self.client_pm.get(
            reverse('dashboard_pm')).context['projects_with_progress'][0]['overdue_count']
        self.assertEqual(read(), 1)
        self._mark(task)
        self.assertEqual(read(), 0)

    def test_dashboard_pm_external_pending(self):
        task = _task(self.phase, 1, self.pm, task_type=Task.EXTERNAL)
        read = lambda: self.client_pm.get(
            reverse('dashboard_pm')).context['summary']['external_pending']
        self.assertEqual(read(), 1)
        self._mark(task)
        self.assertEqual(read(), 0)

    def test_dashboard_site_engineer_overdue(self):
        # The SE dashboard scopes purely by `assigned_to`; there is no
        # Project.assigned_site_engineer column to set.
        se_user, se = _make_user('na_se_dash', 'Site Engineer')
        task = _task(self.phase, 1, se, assigned_role=Task.SITE_ENGINEER,
                     due_date=self.overdue_day)
        client = _client_for(se_user)
        read = lambda: client.get(
            reverse('dashboard_site_engineer')).context['tasks_overdue']
        self.assertEqual(read(), 1)
        self._mark(task)
        self.assertEqual(read(), 0)

    def test_dashboard_design_overdue(self):
        design_user, design = _make_user('na_design', 'Design')
        self.project.assigned_design = design
        self.project.save(update_fields=['assigned_design'])
        task = _task(self.phase, 1, design, assigned_role=Task.DESIGN,
                     due_date=self.overdue_day)
        client = _client_for(design_user)
        read = lambda: client.get(reverse('dashboard_design')).context['tasks_overdue']
        self.assertEqual(read(), 1)
        self._mark(task)
        self.assertEqual(read(), 0)

    def test_dashboard_scm_overdue(self):
        scm_user, scm = _make_user('na_scm', 'SCM')
        task = _task(self.phase, 1, scm, assigned_role=Task.SCM,
                     due_date=self.overdue_day)
        client = _client_for(scm_user)
        read = lambda: client.get(
            reverse('dashboard_scm')).context['summary']['tasks_overdue']
        self.assertEqual(read(), 1)
        self._mark(task)
        self.assertEqual(read(), 0)

    def test_tasks_drill_down_overdue_list(self):
        task = _task(self.phase, 1, self.pm, due_date=self.overdue_day)
        read = lambda: self.client_pm.get(reverse('tasks_overdue')).context['total_count']
        self.assertEqual(read(), 1)
        self._mark(task)
        self.assertEqual(read(), 0,
                         'the drill-down list disagrees with the card it hangs off')

    def test_project_overview_ext_pending(self):
        task = _task(self.phase, 1, self.pm, task_type=Task.EXTERNAL)
        read = lambda: self._phase_bar()['ext_pending']
        self.assertEqual(read(), 1)
        self._mark(task)
        self.assertEqual(read(), 0)

    def test_utils_current_phase_r21(self):
        """R-21: a phase is "current" as the answer to "what is this site waiting on",
        and nobody is waiting on a task ruled out of scope. An N/A task pinning a phase
        open forever is the exact defect B21 was written to fix for mirrors."""
        second = ProjectPhase.objects.create(
            project=self.project, phase_name='Closeout', phase_order=2)
        open_task = _task(self.phase, 1, self.pm)
        _task(second, 1, self.pm, status=Task.DONE, completed_at=timezone.now())

        self.assertEqual(current_phase(self.project), self.phase)
        self._mark(open_task)
        self.assertEqual(
            current_phase(self.project), second,
            'an N/A task held its phase open — R-21 regressed',
        )

    def test_incomplete_predecessors(self):
        """NOT NAMED IN THE BUILD PROMPT — found during the sweep and fixed. Inert
        today (materialise_task_dependencies is unwired, so TaskDependency holds no
        rows), so this asserts the PREDICATE rather than a populated graph: an N/A
        predecessor can never reach Done and would warn about its successor forever."""
        from .models import TaskDependency
        pred = _task(self.phase, 1, self.pm)
        succ = _task(self.phase, 2, self.pm)
        TaskDependency.objects.create(predecessor=pred, successor=succ)

        self.assertEqual(incomplete_predecessors(succ), [pred])
        self._mark(pred)
        self.assertEqual(incomplete_predecessors(succ), [],
                         'an N/A predecessor still blocks its successor')

    def test_send_eod_digest_open_work_gate(self):
        """The digest's open-task snapshot GATES THE SEND (`has_open_work`), so this
        is read through the gate rather than through the count — an N/A task must not
        be the thing that earns somebody a digest email about work they are not doing.

        The command's three open-work querysets share one shape (`human_owned_tasks_q`
        then `exclude(status=Done)`), and this exercises the recipient-facing one.
        """
        from io import StringIO
        from django.core.management import call_command

        task = _task(self.phase, 1, self.pm, due_date=self.overdue_day)

        def skipped_for_no_work():
            out = StringIO()
            call_command('send_eod_digest', '--dry-run',
                         '--date', date.today().isoformat(),
                         '--user', self.pm_user.email,
                         stdout=out)
            return 'skipped: no open tasks/issues' in out.getvalue()

        self.assertFalse(skipped_for_no_work(), 'fixture never reached the digest gate')
        self._mark(task)
        self.assertTrue(skipped_for_no_work(),
                        'an N/A task still counts as open work for the EOD digest')


# ---------------------------------------------------------------------------
# g — DENOMINATORS. 8 Done + 2 N/A must read 100%.
# ---------------------------------------------------------------------------

class DenominatorTests(NotApplicableBase):
    """N/A leaves the denominator as well as the numerator.

    This is the half that distinguishes N/A from `is_mirror`, which stays IN these
    denominators on purpose. Eight Done and two N/A is the prompt's own case and is
    used verbatim so the expected number is 100 and not an arithmetic accident.
    """

    def setUp(self):
        super().setUp()
        for order in range(1, 9):
            _task(self.phase, order, self.pm,
                  status=Task.DONE, completed_at=timezone.now())
        self.na_tasks = [_task(self.phase, 9, self.pm), _task(self.phase, 10, self.pm)]

    def _mark_both(self):
        for task in self.na_tasks:
            self._mark(task)

    def test_dashboard_pm_progress_bar_reads_100(self):
        read = lambda: self.client_pm.get(
            reverse('dashboard_pm')).context['projects_with_progress'][0]['internal_percent']
        self.assertEqual(read(), 80, 'fixture is not 8 Done out of 10')
        self._mark_both()
        self.assertEqual(read(), 100)

    def test_dashboard_pm_denominator_drops_the_na_rows(self):
        card = lambda: self.client_pm.get(
            reverse('dashboard_pm')).context['projects_with_progress'][0]
        self.assertEqual(card()['internal_total'], 10)
        self._mark_both()
        after = card()
        self.assertEqual(after['internal_total'], 8, 'N/A stayed in the denominator')
        self.assertEqual(after['internal_done'], 8)
        self.assertEqual(after['total_tasks'], 8)

    def test_project_overview_phase_bar_reads_100(self):
        read = lambda: self._phase_bar()
        self.assertEqual(read()['pct'], 80)
        self._mark_both()
        after = read()
        self.assertEqual(after['pct'], 100)
        self.assertEqual(after['internal_total'], 8)
        self.assertEqual(after['internal_done'], 8)

    def test_dashboard_ceo_project_card_counts(self):
        ceo_user, _ceo = _make_user('na_ceo', 'CEO')
        client = _client_for(ceo_user)

        def card():
            cards = client.get(reverse('dashboard_ceo')).context['project_cards']
            return next(c for c in cards if c['project'].pk == self.project.pk)

        before = card()
        self.assertEqual(before['completed'], 8)
        self.assertEqual(before['pending'], 2)

        self._mark_both()
        after = card()
        self.assertEqual(after['completed'], 8)
        # THE SUBTRACTIVE `pending` (task_total_count - task_done_count). It has no
        # filter of its own, so the only way to exclude an N/A task from it is to
        # exclude it from BOTH operands — which is what the annotations now do.
        self.assertEqual(after['pending'], 0,
                         'the subtractive pending still counts N/A tasks')

    def test_pm_and_overview_still_agree_with_each_other(self):
        """The 1.6 invariant: two screens, one project, one denominator. N/A must not
        reopen the split that prompts 1.5 and 1.6 were spent closing."""
        self._mark_both()
        pm_pct = self.client_pm.get(
            reverse('dashboard_pm')).context['projects_with_progress'][0]['internal_percent']
        overview_pct = self._phase_bar()['pct']
        self.assertEqual(pm_pct, overview_pct)


# ---------------------------------------------------------------------------
# h — the CEO report's four columns still partition tasks_assigned
# ---------------------------------------------------------------------------

class DailyReportPartitionTests(NotApplicableBase):
    """`not_started + in_progress + completed + blocked == tasks_assigned`.

    The four columns partition rows BY STATUS, and an N/A task still holds one of the
    four stored values. Excluding it PER-COUNT would drop it from the four while
    `tasks_assigned=Count('id')` went on counting it, and the invariant would break by
    exactly the number of N/A tasks. It is excluded on the BASE queryset instead, so
    the row is simply not there and all six counts agree.

    tests_two_step_completion.DailyReportInvariantTests asserts the same property for
    a submitted task and is deliberately left untouched.
    """

    def _row(self):
        for row in build_user_status_rows(date.today())['rows']:
            if row['profile'].pk == self.pm.pk:
                return row
        return None

    def test_the_four_columns_still_sum_with_an_na_task_present(self):
        _task(self.phase, 1, self.pm, status=Task.NOT_STARTED)
        _task(self.phase, 2, self.pm, status=Task.IN_PROGRESS)
        self._mark(_task(self.phase, 3, self.pm, status=Task.IN_PROGRESS))

        row = self._row()
        self.assertEqual(
            row['not_started'] + row['in_progress'] + row['completed'] + row['blocked'],
            row['tasks_assigned'],
            'the four status columns stopped partitioning Tasks Assigned',
        )

    def test_the_na_task_is_out_of_tasks_assigned_entirely(self):
        _task(self.phase, 1, self.pm)
        task = _task(self.phase, 2, self.pm)
        self.assertEqual(self._row()['tasks_assigned'], 2)
        self._mark(task)
        self.assertEqual(self._row()['tasks_assigned'], 1)

    def test_the_totals_row_sums_too(self):
        self._mark(_task(self.phase, 1, self.pm))
        _task(self.phase, 2, self.pm)
        totals = build_user_status_rows(date.today())['totals']
        self.assertEqual(
            totals['not_started'] + totals['in_progress']
            + totals['completed'] + totals['blocked'],
            totals['tasks_assigned'],
        )
