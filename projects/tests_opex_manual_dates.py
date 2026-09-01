"""OPEX dates are manual, and the PM dashboard can activate a tender site.
Prompts B18 and B23.

WHY THIS FILE EXISTS
--------------------
**B18 — auto-scheduling is not in OPEX v1.** Every one of the 22 OPEX v1 template
tasks carries `duration_days`'s field DEFAULT of 1, because the durations are the
Tenders team's to decide and they have not decided them. All 22 are also `Internal`,
and `calculate_due_dates()` chains Internal tasks strictly one calendar day apart off
`activated_at`. So any bulk recalculation puts HOTO at `activated_at + 22 days` and
reads the whole tender site as a three-week project. That is not a slightly-wrong
schedule; it is a specific false claim, made 95 times, and the likely reaction to a
portfolio that goes fully overdue within a month is to stop trusting the overdue
number rather than to fix the durations. A NULL due date says "not scheduled", which
is true.

1.3c took the first half: `opex_site_activate` does not call `calculate_due_dates()`,
so an activated tender site starts with 22 nulls. This session takes the second half —
the two paths that could still write the chain onto an already-activated site.

**What the B18 entry got wrong, and this file pins instead.** The entry said the
*Recalculate dates* button "is on `project_overview`". It is not, and never was: no
template in the tree renders `project_recalculate_dates`. The exposure was the VIEW,
reachable by a direct POST, gated only on PM ownership and `status != 'Draft'`.
`test_no_template_renders_the_recalculate_control` pins that as the standing fact so
the correction cannot quietly reverse — if someone adds the button, that test fails
and they are made to decide about OPEX deliberately.

**The second door, which the entry did not name at all, is the worse one.**
`enable_cascade_scheduling` calls the same `calculate_due_dates()`, IS rendered on
`project_overview`, and is irreversible by design. Worse still, once
`cascade_scheduling` is on, `task_set_due_date` refuses every non-PM role owner
outright. Nine of the 22 OPEX tasks belong to Site Engineer. Turning it on would
permanently delete the only scheduling a tender site has. It is latent today only
because `SystemSettings.cascade_scheduling_enabled` defaults False — and a switch an
Admin can flip is not a guarantee. So the tests below turn that switch ON and then
assert the refusal, which is the only arrangement that tests the guard rather than
the default.

**B23 — the PM dashboard's draft card.** `dashboard/pm.html` still rendered one
unbranched Activate button wired to `#activateDesignerModal`, whose designer `<select>`
is `required`. OPEX sites are created Draft and stay Draft, so that strip is the
surface where a PM actually meets them, and the modal made every one of them
unactivatable from the one place they are all visible. `project_overview.html` was
fixed by 1.3c; this is the same four-line branch.

WHAT IS ASSERTED, AND HOW
-------------------------
Every OPEX fixture here is a REALLY ACTIVATED site — `opex_site_activate` POSTed by a
real PM, producing the rows `attach_opex_template()` actually creates. A `Project` with
`project_type='OPEX'` and hand-made `Task`s would prove the `if` works and nothing
about whether production reaches it. Same argument as `tests_mirror_readonly.py`.

Both halves of each refusal are asserted separately, because they are separate
failures: a hidden control with a live view is not disabled, and a refusing view with
a visible button sends PMs into an error message.

Residential is asserted UNCHANGED throughout — not merely "still allowed", but the
same 53-task chain landing on the same dates. B18 is a carve-out for one project type,
and a carve-out that quietly narrows the other side is a regression.

Run with:
    python manage.py test projects --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal
from importlib import import_module

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from .models import (
    ActivityLog, Project, SystemSettings, Task, TaskTemplate,
    TaskTemplatePhase, TaskTemplateTask, UserProfile,
)
from .utils import RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL, resolve_residential_template


# The roles the OPEX v1 template actually uses, transcribed from
# docs/OPEX_task_template_spec.md rather than read out of migration 0075, and paired
# with one named non-mirror task each. Non-mirror on purpose: a mirror's date is
# nobody's to set and is a separate question (recorded under B18 in
# EXECUTION_MODULE_DEFERRED.md). These are the rows a PM schedules by hand.
#
# 'Design' has no non-mirror task in the OPEX template — both its tasks are mirrors —
# so the Design row is covered by the mirror case in ManualDueDatesOnMirrorsTests
# instead, and its absence here is deliberate rather than an oversight.
#
# 'SCM' JOINED DESIGN IN THAT CATEGORY, and it is worth stating why rather than just
# deleting the row. SCM's entry here was 'Inspection — Factory / Vendor', one of the two
# inspections spec v1.4 REMOVED (an inspection at a vendor's works covers a consignment,
# not a site — phase 4.5 owns it). The same revision split Material Delivery into four
# mirrors. So SCM now owns four mirrors and no entered task at all, and there is no SCM
# row a PM can hand-schedule. That is a real consequence of the template change, not a
# gap in this test: see EXECUTION_MODULE_DEFERRED.md §B27.
MANUAL_DATE_TASKS = {
    'PM':                 'Net Metering Approval',
    'Site Engineer':      'Civil Work and MMS Installation',
    'Project Coordinator': 'Completion Certificates (Paperwork)',
}


class _ConcreteApps:
    """Minimal `apps` stand-in so migration 0075's seeder can run against the real
    models. Same shape as the one in tests_mirror_readonly.py, for the same reason:
    the seed function is the production seed, and calling it is what makes these
    fixtures the rows production has."""

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

    signals.py creates the profile on User post_save with the model's DEFAULT role,
    so this UPDATES rather than creates — a get_or_create(defaults=...) here returns
    the signal's profile with the wrong role and every role gate then refuses.
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
    # ALLOWED_HOSTS has no 'testserver', so the default Client 400s.
    client = Client(SERVER_NAME='localhost')
    client.force_login(profile.user)
    return client


class ManualDatesFixture(TestCase):
    """One PM, one really-activated OPEX site, one really-activated Residential
    project, and both templates seeded the way production got them.

    The Residential project is not decoration. Every OPEX refusal below is a
    carve-out, and a carve-out is only correct if the other side still works — so
    each refusal test has a Residential twin built from the same fixture in the same
    way, and the two are compared rather than asserted in isolation.
    """

    @classmethod
    def setUpTestData(cls):
        resolve_residential_template()   # bootstraps RESIDENTIAL v1 on a virgin DB
        _seed_opex()

        cls.pm       = _profile('b18_pm',  'PM')
        cls.other_pm = _profile('b18_pm2', 'PM')
        cls.designer = _profile('b18_des', 'Design')
        cls.engineer = _profile('b18_se',  'Site Engineer')
        # attach_residential_template() raises and rolls the whole activation back
        # without a Finance user at this address.
        cls.finance  = _profile('b18_fin', 'Finance',
                                email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)

    def setUp(self):
        self.site = self._make_opex_site()
        self._activate_opex(self.site)

    # -- fixtures ------------------------------------------------------------

    def _make_opex_site(self, customer_name='B18 Tender Site', pm=None):
        return Project.objects.create(
            customer_name=customer_name,
            customer_phone='9876543210',
            site_address='1 Tender Road',
            city='Lucknow',
            project_type='OPEX',
            capacity_kw=Decimal('100.00'),
            status='Draft',
            assigned_pm=pm or self.pm,
        )

    def _make_residential(self, customer_name='B18 House'):
        return Project.objects.create(
            customer_name=customer_name,
            customer_phone='9876543211',
            site_address='1 Sun Road',
            city='Lucknow',
            project_type='Residential',
            capacity_kw=Decimal('5.00'),
            status='Draft',
            assigned_pm=self.pm,
            target_commissioning_date=date.today() + timedelta(days=90),
        )

    def _activate_opex(self, project):
        response = _client_for(self.pm).post(
            reverse('opex_site_activate', args=[project.project_id]))
        self.assertEqual(response.status_code, 302,
                         'OPEX activation did not redirect — fixture is wrong')
        project.refresh_from_db()
        self.assertEqual(project.status, 'Active')
        return project

    def _activate_residential(self, project):
        response = _client_for(self.pm).post(
            reverse('project_activate', args=[project.project_id]),
            {'assigned_design_id': self.designer.pk},
        )
        self.assertEqual(response.status_code, 302,
                         'Residential activation did not redirect — fixture is wrong')
        project.refresh_from_db()
        self.assertEqual(project.status, 'Active')
        return project

    def _activated_residential(self):
        return self._activate_residential(self._make_residential())

    def _activated_capex_site(self, customer_name='B18 CAPEX Site'):
        """An activated CAPEX site, made by activating as OPEX and then flipping the
        type.

        Not a shortcut. `attach_opex_template()` raises `TaskTemplate.DoesNotExist`
        for CAPEX — no ACTIVE CAPEX template has been seeded, deliberately, and a
        version is not created automatically to cover it. So a CAPEX site cannot be
        activated through the front door today, and the only way to get one carrying
        the 22 placeholder-duration tasks is this. The resulting row is exactly what
        the guard would meet the day a CAPEX template is seeded, which is what the
        guard has to survive: the rule is "not Residential", not "OPEX".
        """
        site = self._make_opex_site(customer_name=customer_name)
        self._activate_opex(site)
        site.project_type = 'CAPEX'
        site.save(update_fields=['project_type'])
        return site

    # -- helpers -------------------------------------------------------------

    def _tasks(self, project):
        return Task.objects.filter(phase__project=project)

    def _due_dates(self, project):
        return list(self._tasks(project).values_list('due_date', flat=True))

    def _task_named(self, project, name):
        task = self._tasks(project).filter(task_name=name).first()
        self.assertIsNotNone(
            task, f'the attach produced no task named {name!r} — fixture is wrong')
        return task

    def _enable_cascade_gate(self):
        """Turn the system-wide feature switch ON.

        Without this, `enable_cascade_scheduling` refuses on the gate and every
        assertion below would pass for the wrong reason — the default, not the guard.
        """
        settings_obj = SystemSettings.get()
        settings_obj.cascade_scheduling_enabled = True
        settings_obj.save(update_fields=['cascade_scheduling_enabled'])
        return settings_obj

    def _recalc(self, project, actor=None):
        return _client_for(actor or self.pm).post(
            reverse('project_recalculate_dates', args=[project.project_id]))

    def _enable_cascade(self, project, actor=None):
        return _client_for(actor or self.pm).post(
            reverse('enable_cascade_scheduling', args=[project.project_id]))


# ---------------------------------------------------------------------------
# 1 — the fixture itself: an activated OPEX site is unscheduled (1.3c's half)
# ---------------------------------------------------------------------------

class ActivatedOpexSiteStartsUnscheduledTests(ManualDatesFixture):
    """The precondition every refusal below is protecting.

    If activation ever starts writing dates, these tests fail first and name the
    real cause, instead of the refusal tests failing and pointing at the guard.
    """

    def test_all_twenty_three_tasks_are_created_with_a_null_due_date(self):
        due_dates = self._due_dates(self.site)
        self.assertEqual(len(due_dates), 23,
                         'the OPEX attach no longer produces 23 tasks')
        self.assertEqual(
            [d for d in due_dates if d is not None], [],
            'an activated OPEX site now carries due dates — activation is calling '
            'calculate_due_dates() again, which is what B18 exists to prevent')

    def test_every_task_still_carries_the_placeholder_duration(self):
        """The reason the refusals exist, asserted rather than assumed.

        The day real durations arrive as an OPEX v2 template, this test fails —
        which is the correct moment to revisit whether the refusals should stay.
        """
        durations = set(self._tasks(self.site).values_list('duration_days', flat=True))
        self.assertEqual(
            durations, {1},
            'OPEX task durations are no longer all the placeholder 1. If the '
            'Tenders team has supplied real durations as a template version bump '
            '(R-7), B18 is ready to be reconsidered — see docs/execution-model.md.')


# ---------------------------------------------------------------------------
# 2 — the Recalculate view refuses OPEX (B18, first door)
# ---------------------------------------------------------------------------

class RecalculateRefusesOpexTests(ManualDatesFixture):

    def test_the_recalculate_view_refuses_an_activated_opex_site(self):
        response = self._recalc(self.site)
        self.assertEqual(response.status_code, 302)

        self.assertEqual(
            [d for d in self._due_dates(self.site) if d is not None], [],
            'the recalculate view wrote due dates onto an OPEX site — every one of '
            'the 23 tasks carries duration_days=1, so this is the 23-day chain B18 '
            'exists to prevent')

    def test_the_refusal_writes_no_recalculation_activity_row(self):
        """`calculate_due_dates()` logs one summary row with
        action_code='due_dates_recalculated'. Its absence is the machine-readable
        proof that the function was never entered, not merely that the dates
        happened to come out null."""
        self._recalc(self.site)
        self.assertFalse(
            ActivityLog.objects.filter(
                project=self.site, action_code='due_dates_recalculated').exists(),
            'calculate_due_dates() ran on an OPEX site — the refusal is placed '
            'after the call, or has been removed')

    def test_the_refusal_tells_the_pm_dates_are_manual(self):
        response = self._recalc(self.site, )
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(
            any('manually' in m for m in messages),
            f'the refusal gave no message explaining that OPEX dates are set by '
            f'hand; a silent redirect reads as a broken button. Got: {messages}')

    def test_the_refusal_survives_a_site_that_passes_every_other_gate(self):
        """The guard must not be doing its work by accident.

        This site is Active, has an `activated_at`, and is POSTed by its own assigned
        PM — it clears status, activation-date and ownership. `project_type` is the
        only thing standing between it and the 22-day chain.
        """
        self.assertEqual(self.site.status, 'Active')
        self.assertIsNotNone(self.site.activated_at)
        self.assertEqual(self.site.assigned_pm_id, self.pm.pk)

        self._recalc(self.site)
        self.assertEqual([d for d in self._due_dates(self.site) if d is not None], [])

    def test_a_capex_site_is_refused_too(self):
        """The rule is "not Residential", not "OPEX". CAPEX sites use the same
        template and the same placeholder durations."""
        capex = self._activated_capex_site()

        self._recalc(capex)
        self.assertEqual(
            [d for d in self._due_dates(capex) if d is not None], [],
            'a CAPEX site was scheduled — the guard is testing for the string '
            "'OPEX' rather than for 'not Residential'")


# ---------------------------------------------------------------------------
# 3 — the second door: cascade scheduling refuses OPEX (B18)
# ---------------------------------------------------------------------------

class CascadeSchedulingRefusesOpexTests(ManualDatesFixture):
    """The door the original B18 entry did not name.

    Every test here turns the system-wide switch ON first. With it off the view
    refuses on the feature gate and proves nothing about `project_type`.
    """

    def setUp(self):
        super().setUp()
        self._enable_cascade_gate()

    def test_enabling_cascade_is_refused_on_an_opex_site(self):
        response = self._enable_cascade(self.site)
        self.assertEqual(response.status_code, 302)

        self.site.refresh_from_db()
        self.assertFalse(
            self.site.cascade_scheduling,
            'cascade_scheduling was turned ON for an OPEX site. It is irreversible '
            'by design, and once on it locks every non-PM role owner out of '
            'task_set_due_date — which would delete the only scheduling a tender '
            'site has in v1')

    def test_the_refused_cascade_writes_no_due_dates(self):
        self._enable_cascade(self.site)
        self.assertEqual(
            [d for d in self._due_dates(self.site) if d is not None], [],
            'the cascade path reached calculate_due_dates() on an OPEX site')
        self.assertFalse(
            ActivityLog.objects.filter(
                project=self.site, action_code='due_dates_recalculated').exists())

    def test_the_cascade_control_is_not_rendered_for_an_opex_site(self):
        """The visible half. A refusing view with a live button sends the PM into an
        error message; hiding the button while the view still accepts the POST is not
        disabling it. Both are asserted, separately, because they fail separately."""
        response = _client_for(self.pm).get(
            reverse('project_overview', args=[self.site.project_id]))
        self.assertEqual(response.status_code, 200)

        self.assertFalse(
            response.context['show_cascade_option'],
            'show_cascade_option is True for an OPEX site')
        self.assertNotContains(
            response,
            reverse('enable_cascade_scheduling', args=[self.site.project_id]),
            msg_prefix='the OPEX overview still renders the cascade form')

    def test_a_capex_site_is_refused_too(self):
        capex = self._activated_capex_site(customer_name='B18 CAPEX Cascade')

        self._enable_cascade(capex)
        capex.refresh_from_db()
        self.assertFalse(capex.cascade_scheduling)


# ---------------------------------------------------------------------------
# 4 — Residential is untouched (B18's other side)
# ---------------------------------------------------------------------------

class ResidentialSchedulingIsUnchangedTests(ManualDatesFixture):
    """Both controls still work, and produce the same dates they always did.

    "Still allowed" is the weak version of this claim and would pass even if the
    chain had changed. These tests assert the arithmetic: the chain anchors on
    `activated_at.date()` and advances by each Internal task's `duration_days`, which
    is exactly what `calculate_due_dates()` does and what the Residential template's
    real durations are for.
    """

    def test_the_recalculate_view_still_schedules_a_residential_project(self):
        project = self._activated_residential()
        self.assertEqual(
            [d for d in self._due_dates(project) if d is not None], [],
            'the Residential fixture already had due dates before recalculation — '
            'this test would then prove nothing')

        response = self._recalc(project)
        self.assertEqual(response.status_code, 302)

        due_dates = self._due_dates(project)
        self.assertTrue(due_dates, 'the Residential attach produced no tasks')
        self.assertEqual(
            [d for d in due_dates if d is None], [],
            'the Residential recalculation left tasks unscheduled — B18 has leaked '
            'into the Residential path')

    def test_the_residential_chain_lands_where_calculate_due_dates_puts_it(self):
        """The arithmetic, not just the presence of dates."""
        from .utils import add_calendar_days

        project = self._activated_residential()
        self._recalc(project)

        tasks = list(self._tasks(project).order_by('phase__phase_order', 'task_order'))
        cursor = project.activated_at.date()
        for task in tasks:
            if task.task_type == Task.EXTERNAL:
                expected = cursor
            else:
                expected = add_calendar_days(cursor, task.duration_days)
                cursor = expected
            self.assertEqual(
                task.due_date, expected,
                f'{task.task_name!r} landed on {task.due_date} rather than '
                f'{expected} — the Residential cascade has changed')

    def test_the_recalculation_still_writes_its_activity_row(self):
        project = self._activated_residential()
        self._recalc(project)
        self.assertTrue(
            ActivityLog.objects.filter(
                project=project, action_code='due_dates_recalculated').exists(),
            'the Residential recalculation no longer logs its summary row')

    def test_cascade_scheduling_can_still_be_enabled_on_a_residential_project(self):
        self._enable_cascade_gate()
        project = self._activated_residential()

        self._enable_cascade(project)
        project.refresh_from_db()
        self.assertTrue(
            project.cascade_scheduling,
            'cascade scheduling can no longer be enabled on a Residential project — '
            "B18's carve-out has narrowed the Residential path")
        self.assertEqual(
            [d for d in self._due_dates(project) if d is None], [],
            'enabling cascade on a Residential project no longer schedules it')

    def test_the_cascade_control_still_renders_for_a_residential_project(self):
        self._enable_cascade_gate()
        project = self._activated_residential()

        response = _client_for(self.pm).get(
            reverse('project_overview', args=[project.project_id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.context['show_cascade_option'],
            'show_cascade_option is False for a Residential project with the feature '
            'gate ON and the assigned PM viewing — the template half of B18 has '
            'over-reached')
        self.assertContains(
            response,
            reverse('enable_cascade_scheduling', args=[project.project_id]))

    def test_the_draft_gate_still_comes_after_the_type_gate_for_residential(self):
        """A Draft Residential project is refused for being Draft, as before — the
        new type check must not have displaced the original ordering for the type it
        does not apply to."""
        draft = self._make_residential(customer_name='B18 Draft House')
        response = self._recalc(draft)
        self.assertEqual(response.status_code, 302)
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(
            any('activated' in m for m in messages),
            f'a Draft Residential project no longer gets the activation warning. '
            f'Got: {messages}')


# ---------------------------------------------------------------------------
# 5 — no template offers the Recalculate control (B18, the corrected claim)
# ---------------------------------------------------------------------------

class RecalculateControlIsNotRenderedTests(ManualDatesFixture):
    """The correction to B18's text, pinned so it cannot silently reverse.

    B18 recorded that the *Recalculate dates* button sits on `project_overview`. It
    does not, for any project type: nothing in the tree renders
    `project_recalculate_dates`, and the exposure was always the view accepting a
    direct POST. Task 1's template half therefore had no target — which is only safe
    to record if it stays true. If the button is ever added, this fails, and whoever
    adds it is made to decide about OPEX on purpose.
    """

    def test_no_template_renders_the_recalculate_control(self):
        for project, label in (
            (self.site, 'OPEX'),
            (self._activated_residential(), 'Residential'),
        ):
            with self.subTest(project_type=label):
                response = _client_for(self.pm).get(
                    reverse('project_overview', args=[project.project_id]))
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(
                    response,
                    reverse('project_recalculate_dates', args=[project.project_id]),
                    msg_prefix=(
                        f'the {label} overview now renders the recalculate control. '
                        f'B18 records that no template does; if this is deliberate, '
                        f'the OPEX branch needs the same project_type guard the '
                        f'cascade control carries, and B18 needs updating'))


# ---------------------------------------------------------------------------
# 6 — manual dates work on OPEX, for every owning role (B18, Task 2)
# ---------------------------------------------------------------------------

class ManualDueDatesOnOpexTests(ManualDatesFixture):
    """Manual per-task dates are the ONLY scheduling a tender site has in v1.

    Nothing here is new behaviour — `task_set_due_date`'s PM branch has never
    consulted `assigned_role`, and the role match lives entirely inside its
    `if not is_pm:` arm. These tests exist so that stays true: a later tightening
    that made the PM path role-aware would silently strand nine Site Engineer tasks,
    two SCM tasks and a Coordinator task per site across 95 sites, with nothing else
    in the suite noticing.
    """

    def _set_due_date(self, project, task, when, actor=None):
        return _client_for(actor or self.pm).post(
            reverse('task_set_due_date', args=[project.project_id, task.pk]),
            {'due_date': when.isoformat()},
        )

    def test_the_pm_can_set_a_due_date_on_every_owning_role_the_template_uses(self):
        when = date.today() + timedelta(days=30)

        for role, task_name in MANUAL_DATE_TASKS.items():
            with self.subTest(assigned_role=role):
                task = self._task_named(self.site, task_name)
                self.assertEqual(
                    task.assigned_role, role,
                    f'{task_name!r} is no longer a {role} task — this test is '
                    f'covering a different role than it names')
                self.assertIsNone(task.due_date)

                response = self._set_due_date(self.site, task, when)
                self.assertEqual(response.status_code, 302)

                task.refresh_from_db()
                self.assertEqual(
                    task.due_date, when,
                    f'the PM could not set a due date on the {role} task '
                    f'{task_name!r}. Manual dates are the only scheduling an OPEX '
                    f'site has — see B18.')

    def test_the_manual_date_is_read_back_by_the_overview_that_renders_it(self):
        """Persisting is half of it; the PM has to be able to see it again.

        `_task_row.html` renders the date into the `value=` of a `<input type=date>`
        in ISO form, which is what this looks for.
        """
        when = date.today() + timedelta(days=45)
        task = self._task_named(self.site, MANUAL_DATE_TASKS['Site Engineer'])
        self._set_due_date(self.site, task, when)

        response = _client_for(self.pm).get(
            reverse('project_overview', args=[self.site.project_id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, f'value="{when.isoformat()}"',
            msg_prefix='the manually set due date is not rendered back on the '
                       'overview, so the PM cannot see what they set')

    def test_setting_one_date_does_not_ripple_onto_the_other_tasks(self):
        """The whole point of manual scheduling. With `cascade_scheduling` off the
        PM branch saves one row; if a ripple ever reached here it would reintroduce
        the placeholder-duration chain one task at a time."""
        when = date.today() + timedelta(days=30)
        task = self._task_named(self.site, MANUAL_DATE_TASKS['PM'])
        self._set_due_date(self.site, task, when)

        others = self._tasks(self.site).exclude(pk=task.pk)
        self.assertEqual(
            [d for d in others.values_list('due_date', flat=True) if d is not None],
            [],
            'setting one OPEX due date scheduled other tasks — a cascade has '
            'reached the manual path')

    def test_a_manual_date_can_be_cleared_again(self):
        when = date.today() + timedelta(days=30)
        task = self._task_named(self.site, MANUAL_DATE_TASKS['PM'])
        self._set_due_date(self.site, task, when)
        task.refresh_from_db()
        self.assertEqual(task.due_date, when)

        _client_for(self.pm).post(
            reverse('task_set_due_date', args=[self.site.project_id, task.pk]),
            {'due_date': ''},
        )
        task.refresh_from_db()
        self.assertIsNone(task.due_date,
                          'a manually set OPEX due date cannot be cleared')

    def test_the_role_owner_can_also_set_their_own_task_date(self):
        """The non-PM arm, which does consult `assigned_role`. It works on OPEX
        because `cascade_scheduling` is off — and stays off, because
        `enable_cascade_scheduling` now refuses OPEX. That is the connection between
        this test and CascadeSchedulingRefusesOpexTests."""
        when = date.today() + timedelta(days=20)
        task = self._task_named(self.site, MANUAL_DATE_TASKS['Site Engineer'])
        task.assigned_to = self.engineer
        task.save(update_fields=['assigned_to'])

        self.assertFalse(self.site.cascade_scheduling)
        response = self._set_due_date(self.site, task, when, actor=self.engineer)
        self.assertEqual(response.status_code, 302)

        task.refresh_from_db()
        self.assertEqual(task.due_date, when)

    def test_the_recalculate_refusal_does_not_disturb_dates_already_set_by_hand(self):
        """The refusal must be a no-op, not a reset. A PM who has scheduled half a
        site and then presses the wrong button must not lose that work."""
        when = date.today() + timedelta(days=30)
        task = self._task_named(self.site, MANUAL_DATE_TASKS['PM'])
        self._set_due_date(self.site, task, when)

        self._recalc(self.site)

        task.refresh_from_db()
        self.assertEqual(
            task.due_date, when,
            'the OPEX refusal cleared or moved a hand-set due date')


class ManualDueDatesOnMirrorsTests(ManualDatesFixture):
    """A date CAN be set on a mirror today, and this file records that rather than
    preventing it.

    `task_set_due_date` has no `is_mirror` gate — B22's refusal lives on the status
    path and nowhere else. A date on a mirror is meaningless rather than harmful (a
    mirror is nobody's work and is already excluded from the overdue counts), and
    adding a third refusal site is a deliberate pass of its own, not something to
    bolt onto a scheduling session. Recorded under B18 in EXECUTION_MODULE_DEFERRED.md.

    This test asserts the CURRENT behaviour so that if a later session adds the
    refusal, this fails and points at the record rather than at a mystery.
    """

    def test_a_due_date_on_a_mirror_is_currently_accepted(self):
        when = date.today() + timedelta(days=60)
        task = self._task_named(self.site, 'Design')
        self.assertTrue(task.is_mirror, 'Design is no longer a mirror')

        _client_for(self.pm).post(
            reverse('task_set_due_date', args=[self.site.project_id, task.pk]),
            {'due_date': when.isoformat()},
        )
        task.refresh_from_db()
        self.assertEqual(
            task.due_date, when,
            'a mirror now refuses a due date. That is defensible — but B18 records '
            'the opposite as current behaviour, so update EXECUTION_MODULE_DEFERRED.md '
            'rather than deleting this test')


# ---------------------------------------------------------------------------
# 7 — the PM dashboard's draft card (B23)
# ---------------------------------------------------------------------------

class PmDashboardDraftCardActivatesOpexTests(ManualDatesFixture):
    """`dashboard/pm.html` must link where `project_overview.html` links.

    Both templates are rendered and compared against the SAME two URLs, so the claim
    under test is the agreement between the two surfaces rather than a hard-coded
    expectation either could drift away from independently.
    """

    def setUp(self):
        super().setUp()
        # The dashboard's draft strip only shows Draft projects, so the activated
        # fixture from the base class is invisible there. These are the rows it lists.
        self.draft_opex = self._make_opex_site(customer_name='B23 Draft Tender')
        self.draft_house = self._make_residential(customer_name='B23 Draft House')

    def _dashboard(self):
        response = _client_for(self.pm).get(reverse('dashboard_pm'))
        self.assertEqual(response.status_code, 200)
        return response

    def _overview(self, project):
        response = _client_for(self.pm).get(
            reverse('project_overview', args=[project.project_id]))
        self.assertEqual(response.status_code, 200)
        return response

    def test_both_drafts_appear_on_the_dashboard(self):
        """Guard against the whole class passing because the strip is empty."""
        listed = {p.pk for p in self._dashboard().context['draft_projects']}
        self.assertIn(self.draft_opex.pk, listed)
        self.assertIn(self.draft_house.pk, listed)

    def test_the_dashboard_posts_an_opex_draft_to_the_opex_activation_path(self):
        response = self._dashboard()
        self.assertContains(
            response,
            reverse('opex_site_activate', args=[self.draft_opex.project_id]),
            msg_prefix="the PM dashboard's draft card does not offer the OPEX "
                       'activation path, so a tender site cannot be activated from '
                       'the one screen that lists them all (B23)')

    def test_the_dashboard_does_not_send_an_opex_draft_into_the_designer_modal(self):
        """The actual B23 defect. `#activateDesignerModal`'s designer <select> is
        `required`, and OPEX design allocation does not live on that FK — so the
        modal is a dead end for a tender site, not merely an extra click."""
        response = self._dashboard()
        self.assertNotContains(
            response,
            f'data-activate-url="'
            f'{reverse("project_activate", args=[self.draft_opex.project_id])}"',
            msg_prefix='the PM dashboard still wires an OPEX draft to '
                       '#activateDesignerModal via project_activate')

    def test_the_dashboard_keeps_the_designer_modal_for_a_residential_draft(self):
        response = self._dashboard()
        self.assertContains(
            response,
            f'data-activate-url="'
            f'{reverse("project_activate", args=[self.draft_house.project_id])}"',
            msg_prefix='the Residential draft lost its designer modal — B23 was a '
                       'branch, not a replacement')
        self.assertNotContains(
            response,
            reverse('opex_site_activate', args=[self.draft_house.project_id]))

    def test_the_dashboard_and_the_overview_agree_on_both_project_types(self):
        """The comparison the prompt asked for, made directly.

        For each draft, whichever activation URL the overview offers is the one the
        dashboard must offer, and the one it does not offer is the one the dashboard
        must not. Neither side is the fixed expectation.
        """
        dashboard = self._dashboard()

        for project, label in ((self.draft_opex, 'OPEX'),
                               (self.draft_house, 'Residential')):
            with self.subTest(project_type=label):
                overview = self._overview(project)
                opex_url = reverse('opex_site_activate', args=[project.project_id])
                res_url = reverse('project_activate', args=[project.project_id])

                overview_html = overview.content.decode()
                dashboard_html = dashboard.content.decode()

                self.assertEqual(
                    opex_url in dashboard_html, opex_url in overview_html,
                    f'the {label} draft card and its overview disagree about the '
                    f'OPEX activation path')
                self.assertEqual(
                    f'data-activate-url="{res_url}"' in dashboard_html,
                    f'data-activate-url="{res_url}"' in overview_html,
                    f'the {label} draft card and its overview disagree about the '
                    f'designer modal')

    def test_activating_an_opex_draft_from_the_dashboard_path_actually_works(self):
        """End to end. A link that renders and 404s is the same defect in a new place."""
        response = _client_for(self.pm).post(
            reverse('opex_site_activate', args=[self.draft_opex.project_id]))
        self.assertEqual(response.status_code, 302)

        self.draft_opex.refresh_from_db()
        self.assertEqual(self.draft_opex.status, 'Active')
        self.assertEqual(self._tasks(self.draft_opex).count(), 23)
        self.assertEqual(
            [d for d in self._due_dates(self.draft_opex) if d is not None], [],
            'a site activated from the dashboard path came out scheduled')
