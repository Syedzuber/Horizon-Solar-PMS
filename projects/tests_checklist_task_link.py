"""
A checklist is assigned to a TEMPLATE TASK, not to a task's name. Prompt 2.4.

WHAT WAS WRONG
--------------
`ChecklistTaskLink` matched `(task_name, project_type)` against `Task.task_name`.
Two failures fell out of that, and both are asserted here rather than described:

  1. RENAMING DETACHED THE CHECKLIST. A template task's label is version CONTENT and
     moves when a new template version is authored (R-7). The assignment is not content
     and must not move with it — but a string key made the two the same thing, so
     rewording "SLD" to "Single Line Diagram" silently emptied the checklist section on
     every task built from the reworded row. Nothing errored. `TheRenameTests` is the
     pin: the label changes, the checklist stays.

  2. OPEX COULD NOT BE LINKED AT ALL. The authoring picker sourced its names from the
     Residential template alone, so there was no OPEX name to pick and therefore no OPEX
     link anywhere — not a policy, just a list nobody extended. `ThePickerTests` asserts
     both types are offered and that a link created through the real screen for a real
     OPEX task resolves.

THE KEY IS `code`, AND THE FK IS NOT
------------------------------------
`ChecklistTaskLink.template_task` points at a concrete row, but no lookup matches on it.
A new template version writes FRESH `TaskTemplateTask` rows with fresh pks, so a pk
identifies a task only within one version; `code` is what one task is called across all
of them. `TheVersionBumpTests` is the test that would fail if anyone "simplified" the
join to `template_task_id`: it links against v1's row, points a task at v2's row, and
requires the checklist to still resolve.

THE OLD PATH IS KEPT AND IS NOISY
---------------------------------
About 2% of tasks were added by hand and have no template provenance at all, and a link
whose name no live template contains cannot be keyed. Both still resolve by name —
dropping them would be a regression dressed up as a migration — but every such
resolution logs a warning, because a fallback nobody can see is a fallback nobody
retires. `TheFallbackTests` asserts the answer AND the warning.

WHAT THIS FILE DOES NOT TOUCH
-----------------------------
`ChecklistItemCompletion`'s snapshot behaviour is prompt 0.5's guarantee, not this one's.
`TheRenameTests.test_renaming_a_template_task_does_not_disturb_completions` exists only
to prove 2.4 did not weaken it.

Run with:
    python manage.py test projects.tests_checklist_task_link --settings=solarpms.test_settings
"""
from decimal import Decimal
from importlib import import_module
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from .models import (
    Checklist, ChecklistItem, ChecklistItemCompletion, ChecklistTaskLink, Project, Task,
    TaskTemplate, TaskTemplatePhase, TaskTemplateTask, UserProfile,
)
from .utils import RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL, resolve_residential_template
from .views import _checklist_for_task, _checklist_task_name_choices


# The Residential task this file links and rewords. Named rather than picked by index so
# that a template change removing it fails loudly here instead of silently testing some
# other row. Its label and its code happen to be the same string, which is why both
# constants exist — asserting `code` against a label-shaped literal would pass for the
# wrong reason the day they diverge.
RES_TASK_NAME = 'SLD'
RES_TASK_CODE = 'SLD'

# A non-mirror OPEX task. The picker could not offer any of these before 2.4.
OPEX_TASK_NAME = 'Net Metering Approval'


class _ConcreteApps:
    """Stands in for the `apps` registry a RunPython function is handed. Copied from
    tests_two_step_completion.py — the seed only calls apps.get_model()."""

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
    """Create a user and give their profile `role`. signals.py creates the profile on
    post_save with the default role, so this UPDATES rather than creates."""
    user = User.objects.create_user(username=username, password='pw12345', email=email)
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.is_active = True
    profile.save()
    return profile


def _client_for(profile):
    client = Client()
    client.force_login(profile.user)
    return client


def _photo():
    return SimpleUploadedFile('site.jpg', b'\xff\xd8\xff\xdb' + b'0' * 64,
                              content_type='image/jpeg')


class LinkFixture(TestCase):
    """One activated Residential project, one activated OPEX site, and an Admin who can
    reach the checklist authoring screen.

    BOTH SITES ARE REALLY ACTIVATED, so every Task under test is a row the attach
    produced with its `template_task` populated — which is the field this whole prompt
    turns on. A hand-made Task would prove the `if` works and nothing about whether
    production reaches it."""

    @classmethod
    def setUpTestData(cls):
        cls.res_template = resolve_residential_template()   # bootstraps RESIDENTIAL v1
        _seed_opex()

        cls.admin   = _profile('t24_admin', 'Admin')
        cls.pm      = _profile('t24_pm', 'PM')
        # Residential activation refuses without a designer to hand the Design tasks to,
        # and rolls back entirely without the Finance account that owns the invoice
        # tasks. Both are activation's preconditions, not this prompt's — they are here
        # so the fixture activates the way the product does.
        cls.design  = _profile('t24_design', 'Design')
        cls.finance = _profile('t24_fin', 'Finance',
                               email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)

    def setUp(self):
        self.project = self._activate_residential()
        self.site    = self._activate_opex()

    # -- fixture helpers ------------------------------------------------------

    def _activate_residential(self):
        project = Project.objects.create(
            customer_name='2.4 Residential', customer_phone='9876543210',
            site_address='1 Link Road', city='Lucknow', project_type='Residential',
            capacity_kw=Decimal('5.00'), status='Draft', assigned_pm=self.pm,
        )
        response = _client_for(self.pm).post(
            reverse('project_activate', args=[project.project_id]),
            {'assigned_design_id': self.design.pk})
        self.assertEqual(response.status_code, 302, 'Residential activation did not redirect')
        project.refresh_from_db()
        self.assertEqual(project.status, 'Active', 'Residential activation did not take')
        return project

    def _activate_opex(self):
        site = Project.objects.create(
            customer_name='2.4 OPEX Site', customer_phone='9876543211',
            site_address='2 Link Road', city='Lucknow', project_type='OPEX',
            capacity_kw=Decimal('100.00'), status='Draft', assigned_pm=self.pm,
        )
        response = _client_for(self.pm).post(
            reverse('opex_site_activate', args=[site.project_id]))
        self.assertEqual(response.status_code, 302, 'OPEX activation did not redirect')
        site.refresh_from_db()
        self.assertEqual(site.status, 'Active', 'OPEX activation did not take')
        return site

    def _task(self, project, task_name):
        task = Task.objects.filter(phase__project=project, task_name=task_name).first()
        self.assertIsNotNone(task, f'the attach produced no task named {task_name!r}')
        return task

    def _template_task(self, project_type, code):
        tt = TaskTemplateTask.objects.filter(
            code=code,
            phase__template__project_type=project_type,
            phase__template__status=TaskTemplate.ACTIVE,
        ).first()
        self.assertIsNotNone(tt, f'no active {project_type} template task coded {code!r}')
        return tt

    def _opex_template_task(self, label):
        tt = TaskTemplateTask.objects.filter(
            label=label,
            phase__template__project_type='OPEX',
            phase__template__status=TaskTemplate.ACTIVE,
        ).first()
        self.assertIsNotNone(tt, f'no active OPEX template task labelled {label!r}')
        return tt

    def _offered(self, project_type):
        """{task_name: choice dict} for one project type, straight out of the picker —
        so a test that assigns something assigns exactly what an admin could have."""
        return {
            t['task_name']: t
            for group_type, _label, tasks in _checklist_task_name_choices()
            if group_type == project_type
            for t in tasks
        }

    def _published_checklist(self, name, labels):
        """Author a draft, fill it, publish it. R-7 refuses an item added to an active
        version, so the fixture has to build one the way the product does."""
        checklist = Checklist.objects.create(name=name, created_by=self.admin.user)
        items = [
            ChecklistItem.objects.create(checklist=checklist, label=label, order=n)
            for n, label in enumerate(labels, start=1)
        ]
        checklist.activate()
        return checklist, items

    def _author_next_template_version(self, base_template, relabel):
        """Publish version N+1 of a template with some tasks' labels changed.

        `relabel` is {code: new_label}. This is the ONLY legal way to reword a live
        template (R-7) and therefore the only honest way to test a rename: the rows are
        new, with new pks, and only `code` carries across."""
        new_template = TaskTemplate.objects.create(
            code=base_template.code, label=base_template.label,
            project_type=base_template.project_type,
            version_no=base_template.version_no + 1, status=TaskTemplate.DRAFT,
        )
        for phase in base_template.phases.all():
            new_phase = TaskTemplatePhase.objects.create(
                template=new_template, code=phase.code, label=phase.label,
                sort_order=phase.sort_order,
            )
            for tt in phase.tasks.all():
                TaskTemplateTask.objects.create(
                    phase=new_phase, code=tt.code,
                    label=relabel.get(tt.code, tt.label),
                    sort_order=tt.sort_order, assigned_role=tt.assigned_role,
                    task_type=tt.task_type, duration_days=tt.duration_days,
                    is_payment_milestone=tt.is_payment_milestone,
                    is_mirror=tt.is_mirror,
                )
        new_template.activate()
        return new_template


# ---------------------------------------------------------------------------
# 1 — The FK is the key, and the strings are its projection.
# ---------------------------------------------------------------------------

class TheKeyTests(LinkFixture):

    def test_a_link_created_from_a_template_task_derives_its_strings(self):
        """task_name/project_type are written by save() FROM the FK. They are kept so
        anything still reading them keeps working; they are derived so a row can never
        name one task and point at another."""
        tt = self._opex_template_task(OPEX_TASK_NAME)
        checklist, _items = self._published_checklist('OPEX pre-check', ['Meter sealed'])

        link = ChecklistTaskLink.objects.create(checklist=checklist, template_task=tt)

        self.assertEqual(link.task_name, OPEX_TASK_NAME)
        self.assertEqual(link.project_type, 'OPEX')
        link.refresh_from_db()
        self.assertEqual(link.task_name, OPEX_TASK_NAME)
        self.assertEqual(link.project_type, 'OPEX')

    def test_strings_that_disagree_with_the_fk_are_overwritten_not_kept(self):
        """A caller that passes both does not get to create a contradiction."""
        tt = self._template_task('Residential', RES_TASK_CODE)
        checklist, _items = self._published_checklist('Design check', ['Drawing signed'])

        link = ChecklistTaskLink.objects.create(
            checklist=checklist, template_task=tt,
            task_name='Something Else', project_type='OPEX',
        )

        self.assertEqual(link.task_name, RES_TASK_NAME)
        self.assertEqual(link.project_type, 'Residential')

    def test_a_link_with_no_template_task_keeps_the_strings_it_was_given(self):
        """The backfill's unresolvable rows: the strings are then the only record of
        what the link points at, so save() must not touch them."""
        checklist, _items = self._published_checklist('Legacy', ['Old question'])

        link = ChecklistTaskLink.objects.create(
            checklist=checklist, task_name='A Task No Template Has',
            project_type='Residential',
        )

        self.assertIsNone(link.template_task_id)
        self.assertEqual(link.task_name, 'A Task No Template Has')


# ---------------------------------------------------------------------------
# 2 — Resolution goes through `code`, so a version bump does not break it.
# ---------------------------------------------------------------------------

class TheVersionBumpTests(LinkFixture):

    def test_a_link_authored_against_v1_resolves_for_a_task_built_from_v2(self):
        """THE TEST THAT FAILS IF THE JOIN IS EVER 'SIMPLIFIED' TO template_task_id.

        v2 writes a FRESH row for the same task with a fresh pk. The link still points at
        v1's row — nothing re-points it, and nothing should have to. `code` is what the
        two rows share, so `code` is what the lookup matches."""
        v1_task = self._template_task('Residential', RES_TASK_CODE)
        checklist, _items = self._published_checklist('SLD check', ['North arrow present'])
        ChecklistTaskLink.objects.create(checklist=checklist, template_task=v1_task)

        self._author_next_template_version(self.res_template, relabel={})
        v2_task = self._template_task('Residential', RES_TASK_CODE)
        self.assertNotEqual(v1_task.pk, v2_task.pk,
                            'a new version must write fresh rows, or this proves nothing')

        task = self._task(self.project, RES_TASK_NAME)
        task.template_task = v2_task          # as a project activated after v2 would be
        task.save(update_fields=['template_task'])

        self.assertEqual(_checklist_for_task(task, self.project), checklist)


class TheRenameTests(LinkFixture):

    def test_rewording_a_template_task_label_does_not_detach_its_checklist(self):
        """THE ORIGINAL BUG. Under the string key this returned None."""
        v1_task = self._template_task('Residential', RES_TASK_CODE)
        checklist, _items = self._published_checklist('SLD check', ['North arrow present'])
        link = ChecklistTaskLink.objects.create(checklist=checklist, template_task=v1_task)

        self._author_next_template_version(
            self.res_template, relabel={RES_TASK_CODE: 'Single Line Diagram'})
        v2_task = self._template_task('Residential', RES_TASK_CODE)
        self.assertEqual(v2_task.label, 'Single Line Diagram')

        task = self._task(self.project, RES_TASK_NAME)
        task.template_task = v2_task
        task.task_name = 'Single Line Diagram'   # the copy activation would have taken
        task.save(update_fields=['template_task', 'task_name'])

        # Nothing about the strings can match any more — the link still says 'SLD' and
        # the task now says 'Single Line Diagram'. Only the code path can answer.
        link.refresh_from_db()
        self.assertEqual(link.task_name, RES_TASK_NAME)
        self.assertNotEqual(link.task_name, task.task_name)
        self.assertEqual(_checklist_for_task(task, self.project), checklist)

    def test_renaming_a_template_task_does_not_disturb_completions(self):
        """0.5's guarantee, re-asserted because 2.4 must not weaken it. The completion
        holds the text of the CHECKLIST ITEM it answered; a template task's label is a
        different string entirely and renaming it may not touch a single field."""
        v1_task = self._template_task('Residential', RES_TASK_CODE)
        checklist, items = self._published_checklist('SLD check', ['North arrow present'])
        ChecklistTaskLink.objects.create(checklist=checklist, template_task=v1_task)
        task = self._task(self.project, RES_TASK_NAME)

        with patch('projects.supabase_storage.get_supabase_client',
                   return_value=MagicMock()):
            response = _client_for(self.pm).post(
                reverse('checklist_item_complete',
                        args=[self.project.project_id, task.pk, items[0].pk]),
                {'photo': _photo()})
        self.assertEqual(response.status_code, 302)

        completion = ChecklistItemCompletion.objects.get(item=items[0], task=task)
        tracked = ('item_id', 'task_id', 'is_checked', 'item_text_snapshot',
                   'photo_file_name', 'photo_url', 'photo_supabase_path',
                   'checked_by_id', 'checked_at')
        before = {f: getattr(completion, f) for f in tracked}
        self.assertTrue(before['is_checked'])
        self.assertEqual(before['item_text_snapshot'], 'North arrow present')

        self._author_next_template_version(
            self.res_template, relabel={RES_TASK_CODE: 'Single Line Diagram'})

        completion.refresh_from_db()
        after = {f: getattr(completion, f) for f in tracked}
        self.assertEqual(before, after)
        self.assertEqual(ChecklistItemCompletion.objects.count(), 1)


# ---------------------------------------------------------------------------
# 3 — The name fallback still answers, and says so.
# ---------------------------------------------------------------------------

class TheFallbackTests(LinkFixture):

    def test_a_hand_added_task_still_finds_its_checklist_by_name(self):
        """The ~2% with no template provenance. Losing their checklist would be a
        regression dressed up as a migration."""
        checklist, _items = self._published_checklist('Ad hoc', ['Site cleared'])
        task = self._task(self.project, RES_TASK_NAME)
        task.template_task = None
        task.save(update_fields=['template_task'])
        ChecklistTaskLink.objects.create(
            checklist=checklist, task_name=RES_TASK_NAME, project_type='Residential')

        with self.assertLogs('projects.views', level='WARNING') as caught:
            self.assertEqual(_checklist_for_task(task, self.project), checklist)
        logged = '\n'.join(caught.output)
        self.assertIn('resolved by NAME', logged)
        self.assertIn('no template_task', logged)

    def test_an_unkeyed_link_still_answers_for_a_keyed_task(self):
        """The other half: the TASK has provenance, the LINK does not — which is exactly
        the shape the 2.4 backfill leaves behind when a name matches no live template."""
        checklist, _items = self._published_checklist('Legacy link', ['Old question'])
        ChecklistTaskLink.objects.create(
            checklist=checklist, task_name=RES_TASK_NAME, project_type='Residential')
        task = self._task(self.project, RES_TASK_NAME)
        self.assertIsNotNone(task.template_task_id)

        with self.assertLogs('projects.views', level='WARNING') as caught:
            self.assertEqual(_checklist_for_task(task, self.project), checklist)
        self.assertIn('no link keyed to template task code', '\n'.join(caught.output))

    def test_a_keyed_link_does_not_take_the_fallback_at_all(self):
        """The warning is only worth logging if the new path is genuinely silent."""
        tt = self._template_task('Residential', RES_TASK_CODE)
        checklist, _items = self._published_checklist('SLD check', ['North arrow present'])
        ChecklistTaskLink.objects.create(checklist=checklist, template_task=tt)
        task = self._task(self.project, RES_TASK_NAME)

        with self.assertNoLogs('projects.views', level='WARNING'):
            self.assertEqual(_checklist_for_task(task, self.project), checklist)

    def test_no_link_at_all_is_still_none_and_logs_nothing(self):
        """An unassigned task is the common case; it must not be noisy."""
        task = self._task(self.project, RES_TASK_NAME)
        with self.assertNoLogs('projects.views', level='WARNING'):
            self.assertIsNone(_checklist_for_task(task, self.project))


# ---------------------------------------------------------------------------
# 4 — The picker offers both project types, and the screen creates keyed links.
# ---------------------------------------------------------------------------

class ThePickerTests(LinkFixture):

    def test_the_picker_offers_opex_tasks_as_well_as_residential(self):
        """Why zero OPEX links existed before 2.4: there was nothing to pick."""
        by_type = {
            project_type: tasks
            for project_type, _label, tasks in _checklist_task_name_choices()
        }

        self.assertIn('Residential', by_type)
        self.assertIn('OPEX', by_type)
        self.assertIn(RES_TASK_NAME, {t['task_name'] for t in by_type['Residential']})
        self.assertIn(OPEX_TASK_NAME, {t['task_name'] for t in by_type['OPEX']})
        # Sourced from the ACTIVE template rows, not a list — every entry carries the
        # code the link will actually be matched on.
        self.assertTrue(all(t['code'] for t in by_type['OPEX']))

    def test_the_edit_screen_renders_an_opex_task_in_its_picker(self):
        checklist, _items = self._published_checklist('OPEX pre-check', ['Meter sealed'])
        response = _client_for(self.admin).get(
            reverse('admin_checklist_edit', args=[checklist.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, OPEX_TASK_NAME)

    def test_assigning_an_opex_task_through_the_screen_creates_a_keyed_link(self):
        """VERIFICATION 4. The whole round trip: pick an OPEX task on the real screen,
        then look a checklist up for a real OPEX task and get it — off the code path,
        with the name fallback never reached."""
        checklist, _items = self._published_checklist('OPEX pre-check', ['Meter sealed'])
        offered = self._offered('OPEX')
        self.assertIn(OPEX_TASK_NAME, offered)

        response = _client_for(self.admin).post(
            reverse('admin_checklist_link_add', args=[checklist.pk]),
            {'template_task': offered[OPEX_TASK_NAME]['pk']})
        self.assertEqual(response.status_code, 302)

        link = ChecklistTaskLink.objects.get(checklist=checklist)
        self.assertIsNotNone(link.template_task_id)
        self.assertEqual(link.template_task.code, offered[OPEX_TASK_NAME]['code'])
        self.assertEqual(link.task_name, OPEX_TASK_NAME)
        self.assertEqual(link.project_type, 'OPEX')

        opex_task = self._task(self.site, OPEX_TASK_NAME)
        with self.assertNoLogs('projects.views', level='WARNING'):
            self.assertEqual(_checklist_for_task(opex_task, self.site), checklist)

    def test_the_old_name_and_type_pair_still_posts_and_still_lands_on_the_fk(self):
        """A post from an old form or a script is resolved the same way and ends at the
        same FK — the old shape is not tolerated-but-degraded, it is converted."""
        checklist, _items = self._published_checklist('SLD check', ['North arrow present'])

        response = _client_for(self.admin).post(
            reverse('admin_checklist_link_add', args=[checklist.pk]),
            {'task_name': RES_TASK_NAME, 'project_type': 'Residential'})
        self.assertEqual(response.status_code, 302)

        link = ChecklistTaskLink.objects.get(checklist=checklist)
        self.assertEqual(link.template_task.code, RES_TASK_CODE)

    def test_a_task_no_active_template_contains_is_refused(self):
        """Stricter than the old membership test in one way only: it now checks against
        both types' live templates rather than one hardcoded Residential list."""
        checklist, _items = self._published_checklist('Nope', ['Line'])
        response = _client_for(self.admin).post(
            reverse('admin_checklist_link_add', args=[checklist.pk]),
            {'task_name': 'Invented Task', 'project_type': 'Residential'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ChecklistTaskLink.objects.count(), 0)

    def test_a_second_checklist_on_the_same_template_task_is_refused(self):
        first, _i1 = self._published_checklist('First', ['A'])
        second, _i2 = self._published_checklist('Second', ['B'])
        payload = {'template_task': self._offered('OPEX')[OPEX_TASK_NAME]['pk']}

        _client_for(self.admin).post(
            reverse('admin_checklist_link_add', args=[first.pk]), payload)
        _client_for(self.admin).post(
            reverse('admin_checklist_link_add', args=[second.pk]), payload)

        self.assertEqual(ChecklistTaskLink.objects.count(), 1)
        self.assertEqual(ChecklistTaskLink.objects.get().checklist, first)

    def test_a_cross_version_duplicate_is_refused_by_code_not_by_name(self):
        """unique_together on the strings cannot see this one: v1's label and v2's label
        differ, so two rows would both be legal there while both holding one `code` —
        and `code` is what the lookup matches, so the answer would be arbitrary."""
        v1_task = self._template_task('Residential', RES_TASK_CODE)
        first, _i1 = self._published_checklist('First', ['A'])
        ChecklistTaskLink.objects.create(checklist=first, template_task=v1_task)

        self._author_next_template_version(
            self.res_template, relabel={RES_TASK_CODE: 'Single Line Diagram'})
        v2_task = self._template_task('Residential', RES_TASK_CODE)

        second, _i2 = self._published_checklist('Second', ['B'])
        response = _client_for(self.admin).post(
            reverse('admin_checklist_link_add', args=[second.pk]),
            {'template_task': v2_task.pk})
        self.assertEqual(response.status_code, 302)

        self.assertEqual(ChecklistTaskLink.objects.count(), 1)
        self.assertEqual(ChecklistTaskLink.objects.get().checklist, first)
