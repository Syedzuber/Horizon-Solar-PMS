"""
A mirror task refuses a human status write. Prompt B22.

WHY THIS FILE EXISTS
--------------------
`Task.is_mirror` marks a task whose status is DERIVED from another object — the
Design mirror follows its `DesignAssignment`, Material Delivery follows accepted
delivery quantities, COD and HOTO follow the commissioning and handover records.
Nobody types into a mirror; a mirror that a human can move can disagree with its
source, and then neither number means anything. The OPEX spec calls the refusal
"the single most important line in the feature" (§2.2).

Three documents said 1.3c would build it. It did not — 1.3c owned the opening
transition, and the refusal belongs on the status path. Until this session, a
mirror was unwritable **by accident**: mirrors are seeded with `assigned_to = NULL`
and BOTH status views refuse an unassigned task before `_apply_task_status_change()`
is ever reached. Nothing in the code said mirrors were read-only. Assign COD to a PM
through `task_assign` — an entirely reasonable thing to do, to get it onto a
dashboard — and the protection evaporated silently.

THE TRAP THIS FILE IS WRITTEN AROUND
------------------------------------
A refusal test written against a mirror AS SEEDED passes without proving anything,
because the unassigned gate refuses it first and never reaches the rule under test.

**EVERY TEST BELOW ASSIGNS THE MIRROR TO A REAL USER FIRST**, and
`_assign_mirror()` asserts that the assignment actually landed, so the fixture
cannot quietly regress into testing the wrong branch. `TheTrapTests` pins the
distinction from the other direction: it shows what an UNASSIGNED mirror does on
each screen, which is visibly not what an assigned one does.

WHAT IS ASSERTED, AND WHERE
---------------------------
The refusal lives in `_apply_task_status_change()` and nowhere else (R-18), so the
contract runs through BOTH entry points as two concrete subclasses of one mixin — a
rule that stops holding on one screen fails a named test rather than becoming a
support ticket. Same shape as `tests_task_status_path.py`, for the same reason.

Every fixture here is a REALLY ACTIVATED OPEX site: the mirrors under test are the
rows `attach_opex_template()` produced, not hand-made `Task`s with the flag set. A
hand-made row would prove the `if` works and nothing about whether production
carries the flag.

Run with:
    python manage.py test projects --settings=solarpms.test_settings
"""
from decimal import Decimal
from importlib import import_module

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import Client, TestCase
from django.urls import reverse

from .models import (
    ActivityLog, NotificationLog, Project, StatusTransition, Task,
    TaskTemplate, TaskTemplatePhase, TaskTemplateTask, UserProfile,
    SUBJECT_TASK,
)
from .utils import (
    RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL, assign_task_to,
    resolve_residential_template,
)


# The five OPEX mirrors, transcribed from docs/OPEX_task_template_spec.md §3 rather
# than read out of migration 0075. Two transcriptions on purpose (same argument as
# tests_opex_template.py): a test that imports the seed's own data agrees with any
# typo the seed contains.
EXPECTED_MIRROR_NAMES = {
    'Design',
    'Material Delivery',
    'COD',
    'As-Built Drawings',
    'HOTO',
}

# A non-mirror PM task on the same OPEX site, used as the control. Entered, not
# derived — a human is supposed to move this one.
CONTROL_TASK_NAME = 'Net Metering Approval'

# The transition table from `_apply_task_status_change()`, transcribed. It is a local
# inside that function and cannot be imported; transcribing it is also what makes the
# "refused above the table" claim testable — if the table gains a transition and this
# copy does not, the OTHER direction (a mirror silently gaining a legal move) is still
# covered, because the mirror check does not consult the table at all.
TABLE_ALLOWS = [
    (Task.NOT_STARTED, Task.IN_PROGRESS),
    (Task.NOT_STARTED, Task.BLOCKED),
    (Task.NOT_STARTED, Task.DONE),
    (Task.IN_PROGRESS, Task.DONE),
    (Task.IN_PROGRESS, Task.BLOCKED),
    (Task.BLOCKED,     Task.IN_PROGRESS),
    (Task.BLOCKED,     Task.BLOCKED),
    (Task.DONE,        Task.BLOCKED),
]


class _ConcreteApps:
    """Stands in for the `apps` registry a RunPython function is handed.

    Copied in shape from tests_opex_activation.py, for the reason given there: the
    seed only ever calls apps.get_model(), and the concrete classes carry the save()
    overrides that make the R-7 draft guard real.
    """

    _MODELS = {
        'TaskTemplate':      TaskTemplate,
        'TaskTemplatePhase': TaskTemplatePhase,
        'TaskTemplateTask':  TaskTemplateTask,
        'Task':              Task,
    }

    def get_model(self, app_label, model_name):
        assert app_label == 'projects'
        return self._MODELS[model_name]


def _seed_opex():
    module = import_module('projects.migrations.0075_seed_opex_template_v1')
    module.seed_opex_v1(_ConcreteApps(), None)


def _profile(username, role, email=''):
    """Create a user and give their profile `role`.

    signals.py creates the profile on post_save with the model's DEFAULT role, so
    this UPDATES rather than creates — a get_or_create here returns the signal's
    profile with the wrong role and every role gate then refuses.
    """
    user = User.objects.create_user(
        username=username, password='x', email=email,
        first_name=username.title(), last_name='Test',
    )
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.save(update_fields=['role'])
    return profile


def _client_for(profile):
    client = Client()
    client.force_login(profile.user)
    return client


class MirrorReadOnlyFixture(TestCase):
    """One REALLY ACTIVATED OPEX site, and a PM who can drive both screens.

    The actor matters. The two entry points do not admit the same people — the
    overview row is role-or-PM, the detail block is assignee-only — so a test that
    runs through both needs someone who satisfies both at once. The site's own PM,
    holding the task as `assigned_to`, is exactly that person for a task of ANY role:
    `is_pm` short-circuits the overview's role match, and the detail screen asks only
    about `assigned_to`. That is also the realistic shape of the B22 scenario — a PM
    assigning a mirror to themselves to get it onto a dashboard.
    """

    @classmethod
    def setUpTestData(cls):
        resolve_residential_template()   # bootstraps RESIDENTIAL v1 on a virgin DB
        _seed_opex()

        cls.pm = _profile('b22_pm', 'PM')
        # Required data for the RESIDENTIAL path only; harmless here, and present so
        # a later test in this module can activate a house without a surprise.
        cls.finance = _profile('b22_fin', 'Finance',
                               email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)

    def setUp(self):
        self.site = Project.objects.create(
            customer_name='B22 Tender Site',
            customer_phone='9876543210',
            site_address='1 Mirror Road',
            city='Lucknow',
            project_type='OPEX',
            capacity_kw=Decimal('100.00'),
            status='Draft',
            assigned_pm=self.pm,
        )
        response = _client_for(self.pm).post(
            reverse('opex_site_activate', args=[self.site.project_id]))
        self.assertEqual(response.status_code, 302, 'OPEX activation did not redirect')
        self.site.refresh_from_db()
        self.assertEqual(self.site.status, 'Active')

    # -- helpers -------------------------------------------------------------

    def _mirrors(self):
        return Task.objects.filter(
            phase__project=self.site, is_mirror=True).order_by('pk')

    def _assign_mirror(self, name='COD'):
        """A mirror from the real attach, ASSIGNED to the acting PM.

        The assignment is the whole point. Assigned through the chokepoint
        (utils.assign_task_to) rather than by hand, because that is the path a PM
        actually takes; `notify=False` keeps the notification counters in these
        tests measuring only what the status POST did.

        The two asserts are the guard against B22's trap: a mirror that is not a
        mirror, or a mirror that is not assigned, would make every refusal below
        pass for the wrong reason.
        """
        task = Task.objects.filter(
            phase__project=self.site, task_name=name).first()
        self.assertIsNotNone(task, f'the OPEX attach produced no task named {name!r}')
        self.assertTrue(task.is_mirror, f'{name!r} is not a mirror — fixture is wrong')
        self.assertIsNone(task.assigned_to,
                          'the attach is supposed to leave mirrors unassigned')

        assign_task_to(task, self.pm, notify=False)
        task.refresh_from_db()
        self.assertEqual(
            task.assigned_to, self.pm,
            'THE TRAP: the mirror is still unassigned, so the unassigned gate would '
            'refuse it before the mirror rule is ever reached and this test would '
            'prove nothing. See EXECUTION_MODULE_DEFERRED.md B22.'
        )
        return task

    def _control_task(self):
        """A NON-mirror task on the same project, already the PM's by the attach."""
        task = Task.objects.filter(
            phase__project=self.site, task_name=CONTROL_TASK_NAME).first()
        self.assertIsNotNone(task, 'the OPEX attach produced no control task')
        self.assertFalse(task.is_mirror, 'the control task must not be a mirror')
        self.assertEqual(task.assigned_to, self.pm,
                         'the attach pre-assigns non-mirror PM tasks to the site PM')
        return task

    def _ledger(self, task):
        return list(StatusTransition.objects.filter(
            subject_type=SUBJECT_TASK, subject_id=task.pk))

    def _task_logs(self, task):
        return list(ActivityLog.objects.filter(
            entity_type='Task', entity_id=task.pk))

    def _messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]


class MirrorReadOnlyContract:
    """Every assertion here runs through BOTH entry points.

    Anything asserted in this mixin is a property of `_apply_task_status_change()`
    and therefore of the system, not of one screen. R-18: the rule is in the helper,
    so it cannot hold on the overview row and not on the task-detail block.
    """

    ENTRY = None  # set by the concrete subclasses below

    def _post(self, task, data):
        return _client_for(self.pm).post(
            reverse(self.ENTRY, args=[self.site.project_id, task.pk]), data)

    # -- 1. the refusal ------------------------------------------------------

    def test_an_assigned_mirror_is_refused_a_status_change(self):
        task = self._assign_mirror()
        self._post(task, {'status': Task.DONE})
        task.refresh_from_db()
        self.assertEqual(
            task.status, Task.NOT_STARTED,
            'an assigned mirror was moved — the read-only rule did not fire')
        self.assertIsNone(task.completed_at)

    # -- 2. the refusal is the MIRROR rule, not the unassigned gate ----------

    def test_the_refusal_names_the_mirror_rule_and_not_the_assignment(self):
        """THE TEST THAT WOULD HAVE CAUGHT B22.

        Before this session the same POST was also refused — by the unassigned gate,
        one layer up, for an unrelated reason. The status field alone cannot tell the
        two apart. The MESSAGE can, and it is the helper's own, so it is identical on
        both screens by construction.
        """
        task = self._assign_mirror()
        response = self._post(task, {'status': Task.DONE})
        text = ' '.join(self._messages(response))

        self.assertIn('mirror task', text,
                      'the refusal did not name the mirror rule')
        self.assertIn('derived from', text,
                      'the refusal must say WHY, or the user asks for permission')
        self.assertNotIn('unassigned', text.lower(),
                         'refused by the assignment gate, not by the mirror rule — '
                         'this test is proving nothing (B22)')
        self.assertNotIn('permission', text.lower(),
                         'a permission refusal invites a request for permission; '
                         'no permission exists that makes a mirror writable')

    # -- 3. and 4. a refused move is not an event ----------------------------

    def test_a_refused_mirror_move_writes_no_status_transition(self):
        task = self._assign_mirror()
        before = len(self._ledger(task))
        self._post(task, {'status': Task.DONE})
        self.assertEqual(len(self._ledger(task)), before,
                         'a refused move wrote a ledger row — a refusal is not an event')

    def test_a_refused_mirror_move_writes_no_activity_log(self):
        task = self._assign_mirror()
        before = len(self._task_logs(task))
        self._post(task, {'status': Task.DONE})
        self.assertEqual(len(self._task_logs(task)), before,
                         'a refused move wrote an ActivityLog row')

    def test_a_refused_mirror_move_sends_no_notification(self):
        task = self._assign_mirror()
        before = NotificationLog.objects.count()
        self._post(task, {'status': Task.DONE})
        self.assertEqual(NotificationLog.objects.count(), before,
                         'a refused move sent a notification')

    def test_a_refused_mirror_move_does_not_write_the_inline_due_date(self):
        """Rung 0 is above the inline due_date write, and this is why it must be.

        `_apply_task_status_change()` saves a POSTed due_date BEFORE the In-Progress
        guard below it can refuse anything. A mirror check placed after the table
        would still let this POST write a column on a read-only row.
        """
        task = self._assign_mirror()
        self.assertIsNone(task.due_date, 'fixture assumption: mirror has no due date')
        self._post(task, {'status': Task.IN_PROGRESS, 'due_date': '2026-12-31'})
        task.refresh_from_db()
        self.assertIsNone(task.due_date,
                          'a refused mirror move wrote due_date — the check is too low '
                          'in the ladder')
        self.assertEqual(task.status, Task.NOT_STARTED)

    # -- 5. the refusal is scoped to the FLAG --------------------------------

    def test_a_non_mirror_task_on_the_same_project_still_moves(self):
        """Same project, same user, same screen — only the flag differs.

        Without this, "mirrors are refused" and "this OPEX site is frozen" look
        identical from the outside.
        """
        task = self._control_task()
        self._post(task, {'status': Task.DONE})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE,
                         'the refusal leaked onto a non-mirror task')
        self.assertIsNotNone(task.completed_at)

    def test_a_non_mirror_task_still_writes_its_ledger_row(self):
        task = self._control_task()
        self._post(task, {'status': Task.DONE})
        rows = self._ledger(task)
        self.assertEqual(len(rows), 1,
                         'the ordinary path stopped writing its ledger row')
        self.assertEqual(rows[0].to_status, Task.DONE)

    # -- 6. above the table, not inside it -----------------------------------

    def test_every_transition_the_table_allows_is_refused_on_a_mirror(self):
        """The check sits ABOVE the transition table, so the table is irrelevant.

        Each pair below is a move `_apply_task_status_change()` would otherwise
        apply. If the refusal had been written into the table — as one more entry, or
        as a per-transition guard — some subset of these would still go through.
        """
        task = self._assign_mirror()
        for from_status, to_status in TABLE_ALLOWS:
            with self.subTest(entry=self.ENTRY, frm=from_status, to=to_status):
                Task.objects.filter(pk=task.pk).update(status=from_status)
                task.refresh_from_db()
                self._post(task, {
                    'status': to_status,
                    # Supplied so that, on a non-mirror, the Blocked branch would
                    # have proceeded rather than asking for a reason.
                    'block_issue_title': 'would have blocked it',
                })
                task.refresh_from_db()
                self.assertEqual(
                    task.status, from_status,
                    f'a mirror moved {from_status} -> {to_status}')
                self.assertEqual(
                    self._ledger(task), [],
                    f'a refused {from_status} -> {to_status} wrote a ledger row')


class OverviewRowPathTests(MirrorReadOnlyContract, MirrorReadOnlyFixture):
    """The contract, driven through the project-overview row control."""

    ENTRY = 'task_status_update'


class TaskDetailPathTests(MirrorReadOnlyContract, MirrorReadOnlyFixture):
    """The contract, driven through the task-detail status block."""

    ENTRY = 'task_detail_status_update'


# ---------------------------------------------------------------------------
# 7. All five mirrors, on the site the pipeline built
# ---------------------------------------------------------------------------

class EveryMirrorOnARealSiteTests(MirrorReadOnlyFixture):
    """Not one hand-made row with the flag set — all five, from the real attach.

    A test that builds `Task(is_mirror=True)` itself proves the `if` works and
    nothing about whether production rows carry the flag. That distinction is not
    academic here: until 1.3c added `is_mirror` as the seventh snapshot in
    `_attach_task_template()`, NO row in production could carry it and every
    mirror-aware queryset in the codebase was correct and firing on nothing (B19).
    """

    def test_the_attach_produced_exactly_the_five_expected_mirrors(self):
        names = set(self._mirrors().values_list('task_name', flat=True))
        self.assertEqual(names, EXPECTED_MIRROR_NAMES)
        self.assertEqual(self._mirrors().count(), 5)

    def test_all_five_mirrors_are_refused_through_both_entry_points(self):
        for mirror in self._mirrors():
            assign_task_to(mirror, self.pm, notify=False)
            mirror.refresh_from_db()
            self.assertEqual(mirror.assigned_to, self.pm,
                             f'{mirror.task_name}: not assigned, so a refusal below '
                             f'would come from the unassigned gate (B22)')

        for entry in ('task_status_update', 'task_detail_status_update'):
            for mirror in self._mirrors():
                with self.subTest(entry=entry, task=mirror.task_name):
                    response = _client_for(self.pm).post(
                        reverse(entry, args=[self.site.project_id, mirror.pk]),
                        {'status': Task.DONE},
                    )
                    mirror.refresh_from_db()
                    self.assertEqual(
                        mirror.status, Task.NOT_STARTED,
                        f'{mirror.task_name} was moved through {entry}')
                    self.assertIn(
                        'mirror task', ' '.join(self._messages(response)),
                        f'{mirror.task_name}: refused for the wrong reason')
                    self.assertEqual(self._ledger(mirror), [])

    def test_the_seventeen_entered_tasks_are_not_caught_by_the_flag(self):
        """The other side of the same claim: 22 tasks, 5 mirrors, 17 writable rows."""
        entered = Task.objects.filter(phase__project=self.site, is_mirror=False)
        self.assertEqual(entered.count(), 17)
        self.assertEqual(Task.objects.filter(phase__project=self.site).count(), 22)


# ---------------------------------------------------------------------------
# The trap, pinned from the other direction
# ---------------------------------------------------------------------------

class TheTrapTests(MirrorReadOnlyFixture):
    """What an UNASSIGNED mirror does, so the two refusals are visibly different.

    This class exists to make the contract above meaningful. The mirrors ship
    unassigned, and each screen answers an unassigned task in its own way (B13) —
    neither of which is the mirror message. If a later change makes these two
    refusals indistinguishable, the contract's message assertions stop proving which
    rule fired, and these tests are where that shows up.
    """

    def _unassigned_mirror(self):
        task = Task.objects.filter(
            phase__project=self.site, task_name='COD').first()
        self.assertTrue(task.is_mirror)
        self.assertIsNone(task.assigned_to,
                          'the attach must leave mirrors unassigned')
        return task

    def test_the_overview_screen_refuses_an_unassigned_mirror_for_the_other_reason(self):
        task = self._unassigned_mirror()
        response = _client_for(self.pm).post(
            reverse('task_status_update', args=[self.site.project_id, task.pk]),
            {'status': Task.DONE},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('unassigned', response.content.decode().lower())
        self.assertNotIn('mirror', response.content.decode().lower(),
                         'the unassigned gate fires FIRST — that is exactly why a '
                         'refusal test must assign the mirror before posting')
        task.refresh_from_db()
        self.assertEqual(task.status, Task.NOT_STARTED)

    def test_the_detail_screen_refuses_an_unassigned_mirror_for_the_other_reason(self):
        task = self._unassigned_mirror()
        response = _client_for(self.pm).post(
            reverse('task_detail_status_update',
                    args=[self.site.project_id, task.pk]),
            {'status': Task.DONE},
        )
        self.assertEqual(response.status_code, 403)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.NOT_STARTED)

    def test_assigning_a_mirror_is_still_permitted_and_still_pointless(self):
        """Recorded, not endorsed.

        `task_assign` filters candidates by role and never looks at `is_mirror`, so a
        mirror can be handed to a person who then cannot act on it. That is now a
        harmless inconsistency rather than a hole, because the status write is refused
        either way — but it is an inconsistency, and it is open in
        EXECUTION_MODULE_DEFERRED.md §B. If a later prompt makes the chokepoint refuse
        a mirror, THIS TEST SHOULD FAIL and be replaced by its opposite.
        """
        task = self._assign_mirror('HOTO')
        self.assertEqual(task.assigned_to, self.pm)
