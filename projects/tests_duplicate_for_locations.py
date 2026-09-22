"""
Duplicate for locations — one template task split into per-location tasks, properly.

WHY THIS EXISTS
---------------
Some sites split one template task into per-location tasks (UKRU001: nine locations).
PMs made those by hand with task_add, so each had no template_task and therefore no
checklist. The action tested here makes them the right way: the SAME template_task as
the source, so the checklist attaches through the existing code lookup, plus a
structured Task.location_label.

THE RULES PINNED HERE, each by a test named for it
    Eligibility   permissions.can_duplicate_task_for_locations(): task_add's gate
                  (PM / Project Coordinator who manages the project), Active, not
                  soft-deleted, not Residential; and the task has a template_task, is
                  not itself a location copy, not a mirror, not a payment milestone.
                  Refused on BOTH the GET panel and the POST, and the POST writes nothing.
    Icon          rendered only where the predicate is True, by every row responder.
    Chips         empty state, already-added disabled, existing spelling wins.
    Create        fields copied, flags forced False, placed after the LAST sibling,
                  later tasks shifted by N, orders unique and contiguous.
    Checklist     a copy resolves the source's checklist; answers stay per task.
    Refusals      duplicates collapse, already-added skipped, empty / >30 / >100 refused.
    Logging       one ActivityLog row per new task, action_code task_duplicated_for_location.
    Notification  the first new task notifies and the rest are silent (P2 decision (b)).

Every site here is REALLY ACTIVATED through opex_site_activate, so each Task under test
carries the template_task the attach wrote — the field this feature turns on.

Run with:
    python manage.py test projects.tests_duplicate_for_locations --settings=solarpms.test_settings
"""
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import AnonymousUser
from django.template import Context, Template
from django.test import TestCase
from django.urls import reverse

from .models import (
    ActivityLog, Checklist, ChecklistItem, ChecklistItemCompletion, ChecklistTaskLink,
    NotificationLog, Project, ProjectPhase, Task,
)
from .permissions import can_duplicate_task_for_locations
from .tests_checklist_task_link import _client_for, _photo, _profile, _seed_opex
from .views import LOCATION_DUPLICATE_MAX, _checklist_for_task

SOURCE_NAME  = 'Module Installation'       # OPEX Installation phase, task_order 2
SIBLING_NAME = 'Inverter Installation'     # same phase, a different template task
MIRROR_NAME  = 'Delivery — MMS'
EMPTY_STATE  = 'No locations on this site yet. Type them below.'


class DuplicateFixture(TestCase):

    @classmethod
    def setUpTestData(cls):
        _seed_opex()
        cls.admin    = _profile('dup_admin', 'Admin')
        cls.pm       = _profile('dup_pm', 'PM')
        cls.other_pm = _profile('dup_other_pm', 'PM')
        cls.coord    = _profile('dup_coord', 'Project Coordinator')
        cls.se       = _profile('dup_se', 'Site Engineer')
        cls.se2      = _profile('dup_se2', 'Site Engineer')
        cls.finance  = _profile('dup_fin', 'Finance')
        cls.scm      = _profile('dup_scm', 'SCM')

    def setUp(self):
        self.site = self._activate_opex('Dup Site A')
        self.source = self._task(SOURCE_NAME)
        Task.objects.filter(pk=self.source.pk).update(assigned_to=self.se)
        self.source.refresh_from_db()

    # -- fixture helpers ------------------------------------------------------

    def _activate_opex(self, name):
        site = Project.objects.create(
            customer_name=name, customer_phone='9876543211', site_address='1 Dup Road',
            city='Lucknow', project_type='OPEX', dc_capacity_kw=Decimal('100.00'),
            status='Draft', assigned_pm=self.pm,
        )
        response = _client_for(self.pm).post(
            reverse('opex_site_activate', args=[site.project_id]))
        self.assertEqual(response.status_code, 302, 'OPEX activation did not redirect')
        site.refresh_from_db()
        self.assertEqual(site.status, 'Active', 'OPEX activation did not take')
        return site

    def _task(self, name, project=None):
        task = Task.objects.filter(phase__project=project or self.site,
                                   task_name=name).first()
        self.assertIsNotNone(task, f'the attach produced no task named {name!r}')
        return task

    def _panel(self, profile, task=None):
        task = task or self.source
        return _client_for(profile).get(
            reverse('task_duplicate_locations', args=[task.phase.project.project_id, task.pk]),
            HTTP_HX_REQUEST='true')

    def _create(self, profile=None, task=None, existing=(), new='', assignee='default',
                hx=True):
        task = task or self.source
        assignee = self.se if assignee == 'default' else assignee
        headers = {'HTTP_HX_REQUEST': 'true'} if hx else {}
        return _client_for(profile or self.pm).post(
            reverse('task_duplicate_locations_create',
                    args=[task.phase.project.project_id, task.pk]),
            {'existing_locations': list(existing), 'new_locations': new,
             'assigned_to': assignee.pk if assignee is not None else ''},
            **headers)

    def _phase_orders(self, phase=None):
        return list(Task.objects.filter(phase=phase or self.source.phase)
                    .order_by('task_order', 'pk').values_list('task_order', flat=True))

    def _copies(self, source=None):
        source = source or self.source
        return list(Task.objects.filter(phase__project=source.phase.project,
                                        template_task_id=source.template_task_id)
                    .exclude(location_label='').order_by('task_order'))

    def assertRefusedBothWays(self, profile, task=None):
        before = Task.objects.count()
        self.assertIn(self._panel(profile, task).status_code, (403, 404))
        self.assertIn(self._create(profile, task, new='Block A').status_code, (403, 404))
        self.assertEqual(Task.objects.count(), before, 'a refused POST wrote tasks')


# ---------------------------------------------------------------------------
# 1 — Eligibility, on the GET panel AND the POST.
# ---------------------------------------------------------------------------

class EligibilityTests(DuplicateFixture):

    def test_the_owning_pm_is_admitted_on_an_eligible_task(self):
        self.assertEqual(self._panel(self.pm).status_code, 200)
        self.assertTrue(can_duplicate_task_for_locations(self.pm.user, self.source))

    def test_a_coordinator_on_the_project_is_admitted(self):
        self.site.coordinators.add(self.coord)
        self.assertEqual(self._panel(self.coord).status_code, 200)

    def test_refused_for_residential(self):
        project = Project.objects.create(
            customer_name='Dup Residential', customer_phone='9876543212',
            site_address='2 Dup Road', city='Lucknow', project_type='Residential',
            dc_capacity_kw=Decimal('5.00'), status='Active', assigned_pm=self.pm,
        )
        phase = ProjectPhase.objects.create(project=project, phase_name='Installation',
                                            phase_order=1)
        task = Task.objects.create(phase=phase, task_name='Module Installation', task_order=1,
                                   assigned_role=Task.SITE_ENGINEER,
                                   template_task=self.source.template_task)
        self.assertFalse(can_duplicate_task_for_locations(self.pm.user, task))
        self.assertRefusedBothWays(self.pm, task)

    def test_refused_for_an_inactive_project(self):
        Project.objects.filter(pk=self.site.pk).update(status='On Hold')
        self.assertRefusedBothWays(self.pm)

    def test_refused_for_a_soft_deleted_project(self):
        Project.objects.filter(pk=self.site.pk).update(is_deleted=True)
        self.assertRefusedBothWays(self.pm)

    def test_refused_for_a_task_with_no_template_task(self):
        Task.objects.filter(pk=self.source.pk).update(template_task=None)
        self.assertRefusedBothWays(self.pm)

    def test_refused_for_a_location_copy_as_source(self):
        Task.objects.filter(pk=self.source.pk).update(location_label='Block A')
        self.assertRefusedBothWays(self.pm)

    def test_refused_for_a_mirror(self):
        self.assertRefusedBothWays(self.pm, self._task(MIRROR_NAME))

    def test_refused_for_a_payment_milestone(self):
        Task.objects.filter(pk=self.source.pk).update(is_payment_milestone=True)
        self.assertRefusedBothWays(self.pm)

    def test_refused_for_a_pm_who_does_not_own_the_project(self):
        self.assertRefusedBothWays(self.other_pm)

    def test_refused_for_a_site_engineer(self):
        self.assertRefusedBothWays(self.se)

    def test_refused_for_finance(self):
        self.assertRefusedBothWays(self.finance)

    def test_refused_for_scm(self):
        self.assertRefusedBothWays(self.scm)

    def test_a_get_to_the_create_route_writes_nothing(self):
        before = Task.objects.count()
        _client_for(self.pm).get(reverse('task_duplicate_locations_create',
                                         args=[self.site.project_id, self.source.pk]))
        self.assertEqual(Task.objects.count(), before)


# ---------------------------------------------------------------------------
# 2 — The icon is rendered only where the predicate is True.
# ---------------------------------------------------------------------------

class IconTests(DuplicateFixture):

    MARK = 'aria-label="Duplicate '

    def _overview(self, profile):
        return _client_for(profile).get(
            reverse('project_overview', args=[self.site.project_id])).content.decode()

    def test_icon_rendered_only_on_eligible_rows(self):
        html = self._overview(self.pm)
        eligible = Task.objects.filter(
            phase__project=self.site, template_task__isnull=False, location_label='',
            is_mirror=False, is_payment_milestone=False)
        self.assertGreater(eligible.count(), 0)
        self.assertEqual(html.count(self.MARK), eligible.count())
        self.assertIn(f'aria-label="Duplicate {SOURCE_NAME} for locations"', html)
        self.assertNotIn(f'aria-label="Duplicate {MIRROR_NAME} for locations"', html)
        self.assertIn('Duplicate for locations</span>', html, 'the tooltip text is missing')

    def test_no_icon_for_a_viewer_who_may_not_duplicate(self):
        self.assertEqual(self._overview(self.se).count(self.MARK), 0)

    def test_a_copy_row_shows_its_badge_and_no_icon(self):
        self._create(new='Block A')
        html = self._overview(self.pm)
        self.assertIn(f'{SOURCE_NAME} — Block A', html)
        self.assertNotIn(f'aria-label="Duplicate {SOURCE_NAME} — Block A for locations"', html)
        self.assertIn('title="Location on this site"', html)

    def test_the_htmx_row_rerender_carries_the_icon_too(self):
        """The success response re-renders the phase through _task_add_success.html —
        the filter must answer there as it does on the page."""
        html = self._create(new='Block A').content.decode()
        self.assertIn(f'aria-label="Duplicate {SOURCE_NAME} for locations"', html)

    def test_the_project_half_is_asked_once_per_request_not_per_row(self):
        tasks = list(Task.objects.filter(phase__project=self.site)
                     .select_related('phase'))
        template = Template('{% load duplicate_tags %}{% for t in tasks %}'
                            '{% if t|can_duplicate_for_locations:user %}x{% endif %}{% endfor %}')
        user = self.pm.user
        with patch('projects.permissions.user_can_manage_project',
                   return_value=True) as manage:
            template.render(Context({'tasks': tasks, 'user': user}))
        self.assertEqual(manage.call_count, 1)

    def test_the_filter_refuses_an_anonymous_user(self):
        template = Template('{% load duplicate_tags %}'
                            '{% if t|can_duplicate_for_locations:user %}x{% endif %}')
        self.assertEqual(template.render(Context({'t': self.source, 'user': AnonymousUser()})), '')


# ---------------------------------------------------------------------------
# 3 — Chips.
# ---------------------------------------------------------------------------

class ChipTests(DuplicateFixture):

    def test_an_empty_project_shows_the_empty_state_line(self):
        html = self._panel(self.pm).content.decode()
        self.assertIn('Locations on this site', html)
        self.assertIn(EMPTY_STATE, html)

    def test_already_added_chips_are_disabled_and_marked(self):
        self._create(new='Block A')
        html = self._panel(self.pm).content.decode()
        self.assertNotIn(EMPTY_STATE, html)
        self.assertInHTML(
            '<input type="checkbox" class="btn-check" name="existing_locations" '
            'value="Block A" id="dup-loc-1" autocomplete="off" disabled>', html)
        self.assertIn('already added', html)

    def test_a_site_location_is_offered_enabled_on_another_task(self):
        self._create(new='Block A')
        html = self._panel(self.pm, self._task(SIBLING_NAME)).content.decode()
        self.assertInHTML(
            '<input type="checkbox" class="btn-check" name="existing_locations" '
            'value="Block A" id="dup-loc-1" autocomplete="off">', html)
        self.assertNotIn('already added', html)

    def test_case_insensitive_match_uses_the_existing_spelling(self):
        self._create(new='Block A')
        sibling = self._task(SIBLING_NAME)
        self._create(task=sibling, new='  block   a ')
        copy = self._copies(sibling)[0]
        self.assertEqual(copy.location_label, 'Block A')
        self.assertEqual(copy.task_name, f'{SIBLING_NAME} — Block A')

    def test_chips_come_in_order_of_first_appearance(self):
        self._create(new='Zeta\nAlpha')
        html = self._panel(self.pm, self._task(SIBLING_NAME)).content.decode()
        self.assertLess(html.index('value="Zeta"'), html.index('value="Alpha"'))

    def test_the_assignee_defaults_to_the_source_assignee(self):
        html = self._panel(self.pm).content.decode()
        self.assertInHTML(f'<option value="{self.se.pk}" selected>'
                          f'{self.se.user.username}</option>', html)
        self.assertNotIn(f'<option value="{self.finance.pk}"', html)


# ---------------------------------------------------------------------------
# 4 — Create.
# ---------------------------------------------------------------------------

class CreateTests(DuplicateFixture):

    def test_creates_n_tasks_with_the_right_fields(self):
        Task.objects.filter(pk=self.source.pk).update(
            due_date=date(2026, 10, 1), duration_days=3, task_type=Task.EXTERNAL)
        self.source.refresh_from_db()
        response = self._create(new='Block A\nBlock B\nBlock C')
        self.assertEqual(response.status_code, 200)
        copies = self._copies()
        self.assertEqual([c.location_label for c in copies], ['Block A', 'Block B', 'Block C'])
        for copy in copies:
            self.assertEqual(copy.phase_id, self.source.phase_id)
            self.assertEqual(copy.task_name, f'{SOURCE_NAME} — {copy.location_label}')
            self.assertEqual(copy.template_task_id, self.source.template_task_id)
            self.assertEqual(copy.assigned_role, self.source.assigned_role)
            self.assertEqual(copy.task_type, Task.EXTERNAL)
            self.assertEqual(copy.duration_days, 3)
            self.assertEqual(copy.due_date, date(2026, 10, 1))
            self.assertEqual(copy.status, Task.NOT_STARTED)
            self.assertEqual(copy.assigned_to_id, self.se.pk)
            self.assertFalse(copy.is_mirror)
            self.assertFalse(copy.is_payment_milestone)
        self.assertIn(f'3 tasks created for {SOURCE_NAME}.', response.content.decode())

    def test_the_template_task_is_shared_not_copied(self):
        self._create(new='Block A\nBlock B')
        self.assertEqual({c.template_task_id for c in self._copies()},
                         {self.source.template_task_id})

    def test_flags_are_forced_false_even_if_the_source_had_them(self):
        """The predicate refuses such a source, so the gate is patched open to reach
        the create path and prove it sets both flags itself."""
        Task.objects.filter(pk=self.source.pk).update(is_mirror=True, is_payment_milestone=True)
        with patch('projects.views.can_duplicate_task_for_locations', return_value=True):
            self._create(new='Block A')
        copy = self._copies()[0]
        self.assertFalse(copy.is_mirror)
        self.assertFalse(copy.is_payment_milestone)

    def test_new_tasks_are_placed_after_the_last_sibling(self):
        self._create(new='Block A\nBlock B')
        self._create(new='Block C')
        orders = {c.location_label: c.task_order for c in self._copies()}
        src = self.source.task_order
        self.assertEqual(orders, {'Block A': src + 1, 'Block B': src + 2, 'Block C': src + 3})

    def test_later_tasks_are_shifted_by_n(self):
        later = dict(Task.objects.filter(phase=self.source.phase,
                                         task_order__gt=self.source.task_order)
                     .values_list('pk', 'task_order'))
        earlier = dict(Task.objects.filter(phase=self.source.phase,
                                           task_order__lte=self.source.task_order)
                       .values_list('pk', 'task_order'))
        self._create(new='Block A\nBlock B\nBlock C')
        for pk, order in later.items():
            self.assertEqual(Task.objects.get(pk=pk).task_order, order + 3)
        for pk, order in earlier.items():
            self.assertEqual(Task.objects.get(pk=pk).task_order, order)

    def test_task_order_is_unique_and_contiguous_in_the_phase_afterwards(self):
        self._create(new='Block A\nBlock B\nBlock C')
        orders = self._phase_orders()
        self.assertEqual(len(orders), len(set(orders)))
        self.assertEqual(orders, list(range(1, len(orders) + 1)))

    def test_other_phases_are_untouched(self):
        other = dict(Task.objects.filter(phase__project=self.site)
                     .exclude(phase=self.source.phase).values_list('pk', 'task_order'))
        self._create(new='Block A\nBlock B')
        self.assertEqual(dict(Task.objects.filter(pk__in=other).values_list('pk', 'task_order')),
                         other)

    def test_a_non_htmx_post_redirects_to_the_overview(self):
        response = self._create(new='Block A', hx=False)
        self.assertRedirects(response, reverse('project_overview', args=[self.site.project_id]),
                             fetch_redirect_response=False)
        self.assertEqual(len(self._copies()), 1)

    def test_the_task_detail_header_shows_the_location_badge(self):
        self._create(new='Block A')
        copy = self._copies()[0]
        html = _client_for(self.pm).get(
            reverse('task_detail', args=[self.site.project_id, copy.pk])).content.decode()
        self.assertIn('title="Location on this site"', html)
        self.assertIn('Block A', html)


# ---------------------------------------------------------------------------
# 5 — Checklist.
# ---------------------------------------------------------------------------

class ChecklistTests(DuplicateFixture):

    def _link_checklist(self, labels=('Rails torqued', 'Clamps fitted')):
        checklist = Checklist.objects.create(name='Module QMS', created_by=self.admin.user)
        items = [ChecklistItem.objects.create(checklist=checklist, label=label, order=n)
                 for n, label in enumerate(labels, start=1)]
        checklist.activate()
        ChecklistTaskLink.objects.create(checklist=checklist,
                                         template_task=self.source.template_task)
        return checklist, items

    def test_a_created_task_resolves_the_same_checklist_as_the_source(self):
        checklist, _items = self._link_checklist()
        self._create(new='Block A')
        copy = self._copies()[0]
        self.assertEqual(_checklist_for_task(copy, self.site), checklist)
        self.assertEqual(_checklist_for_task(self.source, self.site), checklist)

    def test_the_panel_names_the_checklist_and_its_item_count(self):
        self._link_checklist()
        html = self._panel(self.pm).content.decode()
        self.assertIn('Each new task gets the Module QMS checklist (2 items), '
                      'answered separately at each location.', html)

    def test_the_panel_says_when_there_is_no_checklist(self):
        html = self._panel(self.pm).content.decode()
        self.assertIn('No checklist is defined for this task yet, so the new tasks will '
                      'have none until one is added.', html)

    def test_answers_on_one_location_do_not_appear_on_another(self):
        _checklist, items = self._link_checklist()
        self._create(new='Block A\nBlock B')
        block_a, block_b = self._copies()
        with patch('projects.supabase_storage.get_supabase_client', return_value=MagicMock()):
            response = _client_for(self.pm).post(
                reverse('checklist_item_complete',
                        args=[self.site.project_id, block_a.pk, items[0].pk]),
                {'answer': 'yes', 'photo': _photo()})
        self.assertIn(response.status_code, (200, 302))
        self.assertTrue(ChecklistItemCompletion.objects.filter(
            item=items[0], task=block_a, is_checked=True).exists())
        self.assertFalse(ChecklistItemCompletion.objects.filter(task=block_b).exists())
        self.assertFalse(ChecklistItemCompletion.objects.filter(task=self.source).exists())


# ---------------------------------------------------------------------------
# 6 — Skips and refusals.
# ---------------------------------------------------------------------------

class SkipAndRefusalTests(DuplicateFixture):

    def test_duplicate_labels_collapse_to_one(self):
        self._create(new='Block A\nblock a\n Block  A ')
        self.assertEqual([c.location_label for c in self._copies()], ['Block A'])

    def test_a_chip_and_a_typed_repeat_collapse_to_one(self):
        self._create(task=self._task(SIBLING_NAME), new='Block A')
        self._create(existing=['Block A'], new='BLOCK A')
        self.assertEqual([c.location_label for c in self._copies()], ['Block A'])

    def test_already_added_locations_are_skipped(self):
        self._create(new='Block A')
        response = self._create(existing=['Block A'], new='Block B')
        self.assertEqual([c.location_label for c in self._copies()], ['Block A', 'Block B'])
        self.assertIn('Skipped, this task already exists at: Block A.',
                      response.content.decode())

    def test_only_already_added_locations_is_refused(self):
        self._create(new='Block A')
        response = self._create(new='block a')
        self.assertEqual(len(self._copies()), 1)
        self.assertIn('No new locations to create.', response.content.decode())

    def test_empty_input_is_refused(self):
        response = self._create(new='  \n\n ')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._copies(), [])
        html = response.content.decode()
        self.assertIn('No new locations to create.', html)
        self.assertIn('data-dup-locations', html, 'a refusal must re-render the panel')

    def test_more_than_thirty_labels_is_refused(self):
        labels = '\n'.join(f'Block {n}' for n in range(LOCATION_DUPLICATE_MAX + 1))
        response = self._create(new=labels)
        self.assertEqual(self._copies(), [])
        self.assertIn(f'more than the {LOCATION_DUPLICATE_MAX} allowed', response.content.decode())

    def test_thirty_labels_is_accepted(self):
        labels = '\n'.join(f'Block {n}' for n in range(LOCATION_DUPLICATE_MAX))
        self._create(new=labels)
        self.assertEqual(len(self._copies()), LOCATION_DUPLICATE_MAX)
        orders = self._phase_orders()
        self.assertEqual(orders, list(range(1, len(orders) + 1)))

    def test_a_label_over_100_characters_is_refused(self):
        response = self._create(new='Block A\n' + 'x' * 101)
        self.assertEqual(self._copies(), [], 'a refusal must write nothing, not a partial set')
        self.assertIn('longer than 100 characters', response.content.decode())

    def test_a_label_of_exactly_100_characters_is_accepted(self):
        self._create(new='y' * 100)
        self.assertEqual([c.location_label for c in self._copies()], ['y' * 100])

    def test_an_assignee_outside_the_task_role_is_refused(self):
        response = self._create(new='Block A', assignee=self.finance)
        self.assertEqual(self._copies(), [])
        self.assertIn('Choose who the new tasks are assigned to.', response.content.decode())

    def test_no_assignee_is_refused(self):
        self._create(new='Block A', assignee=None)
        self.assertEqual(self._copies(), [])

    def test_another_eligible_engineer_may_be_chosen(self):
        self._create(new='Block A', assignee=self.se2)
        self.assertEqual(self._copies()[0].assigned_to_id, self.se2.pk)


# ---------------------------------------------------------------------------
# 7 — Concurrency, as far as a single-connection test can reach.
# ---------------------------------------------------------------------------

class ConcurrencyTests(DuplicateFixture):

    def test_two_creates_in_sequence_leave_orders_unique_and_contiguous(self):
        sibling = self._task(SIBLING_NAME)
        self._create(new='Block A\nBlock B')
        self._create(task=sibling, new='Block A\nBlock C')
        self._create(new='Block C')
        orders = self._phase_orders()
        self.assertEqual(len(orders), len(set(orders)))
        self.assertEqual(orders, list(range(1, len(orders) + 1)))
        self.source.refresh_from_db()
        self.assertEqual([c.task_order for c in self._copies()],
                         [self.source.task_order + n for n in (1, 2, 3)])


# ---------------------------------------------------------------------------
# 8 — Logging and notification.
# ---------------------------------------------------------------------------

class LoggingAndNotificationTests(DuplicateFixture):

    def test_one_activity_log_row_per_new_task(self):
        self._create(new='Block A\nBlock B\nBlock C')
        copies = self._copies()
        rows = ActivityLog.objects.filter(action_code='task_duplicated_for_location')
        self.assertEqual(rows.count(), 3)
        self.assertEqual(sorted(rows.values_list('entity_id', flat=True)),
                         sorted(c.pk for c in copies))
        for row in rows:
            copy = Task.objects.get(pk=row.entity_id)
            self.assertEqual(row.project_id, self.site.pk)
            self.assertEqual(row.actor_id, self.pm.pk)
            self.assertEqual(row.entity_type, 'Task')
            self.assertIn(f'#{self.source.pk}', row.action)
            self.assertIn(f"'{copy.location_label}'", row.action)

    def test_a_refused_create_logs_nothing(self):
        self._create(new='')
        self.assertFalse(ActivityLog.objects.filter(
            action_code='task_duplicated_for_location').exists())

    def test_only_the_first_new_task_notifies(self):
        NotificationLog.objects.all().delete()
        self._create(new='Block A\nBlock B\nBlock C')
        in_app = NotificationLog.objects.filter(recipient=self.se, channel='in_app')
        self.assertEqual(in_app.filter(template_name='assign_task').count(), 1)
        self.assertFalse(in_app.filter(template_name='assign_tasks_bulk').exists())
