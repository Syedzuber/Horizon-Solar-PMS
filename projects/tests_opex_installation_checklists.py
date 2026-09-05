"""
The seven real HRPPL installation checklists, as seeded onto the OPEX Installation phase.

WHAT THIS FILE IS FOR
---------------------
`seed_opex_installation_checklists` writes content, not code, and content is exactly the
kind of thing that rots without anyone noticing: a template task renamed, a checklist
edited to v2, an item dropped in a careless merge. Nothing in the product would raise —
`_checklist_for_task()` would simply return None and the task would render with no
checklist, which looks identical to a task that never had one. These tests are the
difference between those two states.

THE RESOLUTION TESTS GO THROUGH `_checklist_for_task()`, NOT THROUGH A QUERY
---------------------------------------------------------------------------
Asserting `ChecklistTaskLink.objects.get(...)` proves the seed wrote a row. It does not
prove a site engineer opening the task sees anything, because between the row and the
screen sit two joins the seed does not control: the link resolves on `template_task__code`
+ project_type, and the checklist family resolves on `status='active'`. Both were re-keyed
after the seed's mechanism was built (2.4 and 0070), and both are what an OPEX link had
historically failed. So every resolution assertion here calls the real resolver against a
real Task on a really activated OPEX site.

THE TWO ABSENCES ARE ASSERTED AS LOUDLY AS THE SEVEN PRESENCES
--------------------------------------------------------------
RMS Installation and Solar Generation Meter Installation deliberately have no checklist.
`RmsAndMeterTests` pins BOTH halves of that decision: that they resolve to None, and that
resolving to None does not stand between them and Done. The second half is the one worth
having — an absent checklist becoming a silent completion gate is the failure mode that
would strand two tasks on every OPEX site, and it would be found on site, not here.

Run with:
    python manage.py test projects.tests_opex_installation_checklists --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from importlib import import_module

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse

from .models import (
    Checklist, ChecklistItem, ChecklistItemCompletion, ChecklistTaskLink,
    Project, Task, TaskTemplate, TaskTemplatePhase, TaskTemplateTask, UserProfile,
)
from .utils import assign_task_to, resolve_residential_template
from .views import _checklist_for_task


# The seven, by template-task code, with the item count each must carry. Written out
# rather than imported from the command: a test that reads its expectations from the
# thing under test cannot fail when that thing changes, which is the whole point.
EXPECTED = {
    'CIVIL_WORK_AND_MMS_INSTALLATION': 46,
    'MODULE_INSTALLATION':             29,
    'LA_AND_EARTHING_INSTALLATION':    54,
    'DC_CABLE_LAYING_WITH_CONDUIT':    40,
    'DCDB_AND_ACDB_INSTALLATION':      58,
    'INVERTER_INSTALLATION':           34,
    'AC_CABLE_LAYING':                 11,
}
TOTAL_ITEMS = 272

# The two Installation tasks that must have NO checklist. Deferred, not forgotten.
UNCHECKLISTED = ['RMS_INSTALLATION', 'SOLAR_GENERATION_METER_INSTALLATION']

# Section names, per checklist, in item order. Pinned because a section is the only thing
# telling a reader which source document a merged checklist's line came from, and a
# reordering that mixed two documents together would otherwise pass every count check.
EXPECTED_SECTIONS = {
    'CIVIL_WORK_AND_MMS_INSTALLATION': [
        'Concrete Block (RCC/PCC)',
        'Type of PV Module Structure Installed',
        'Fixed type structure Installation',
        'Tracker type structure Installation',
    ],
    'MODULE_INSTALLATION': [''],
    'LA_AND_EARTHING_INSTALLATION': [
        'Earth Pits',
        'Earthing Installation',
        'Lightning Arrester Installation',
        'ESE Type Lightning Arrestor',
    ],
    'DC_CABLE_LAYING_WITH_CONDUIT': [
        'String Combiner Box / Array Junction Box',
        'DC Solar Cable',
    ],
    'DCDB_AND_ACDB_INSTALLATION': ['DCDB', 'LT Panel / ACDB'],
    'INVERTER_INSTALLATION': [''],
    'AC_CABLE_LAYING': [''],
}


class _ConcreteApps:
    """Stands in for the `apps` registry a RunPython function is handed.
    Copied from tests_two_step_completion.py — the seed only calls apps.get_model()."""

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
    """Migrations are disabled under test_settings, so the OPEX template is not in the
    test database until its seed function is called by hand."""
    module = import_module('projects.migrations.0075_seed_opex_template_v1')
    module.seed_opex_v1(_ConcreteApps(), None)


def _profile(username, role, **flags):
    user = User.objects.create_user(
        username=username, password='x',
        first_name=username.title(), last_name='Test',
    )
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    for field, value in flags.items():
        setattr(profile, field, value)
    profile.save(update_fields=['role', *flags])
    return profile


class SeededContentTests(TestCase):
    """What the command wrote, read straight off the tables. No project needed."""

    @classmethod
    def setUpTestData(cls):
        _seed_opex()
        call_command('seed_opex_installation_checklists', stdout=StringIO())

    def test_seven_checklists_exist_at_version_one_and_active(self):
        checklists = Checklist.objects.filter(code__startswith='OPEX-INST-')
        self.assertEqual(checklists.count(), 7)
        for checklist in checklists:
            with self.subTest(checklist.code):
                self.assertEqual(checklist.version_no, 1)
                # ACTIVE is not cosmetic: _checklist_for_task() resolves the family by
                # status and a draft would render as no checklist at all.
                self.assertEqual(checklist.status, Checklist.ACTIVE)
                self.assertIsNotNone(checklist.effective_from)

    def test_no_checklist_requires_a_photo(self):
        """The source documents have no per-item evidence column — see the command's
        docstring. This is the assertion that keeps someone from flipping the default
        back on for 'consistency' with the Residential checklist."""
        for checklist in Checklist.objects.filter(code__startswith='OPEX-INST-'):
            with self.subTest(checklist.code):
                self.assertFalse(checklist.requires_photo)

    def test_item_counts_match_the_source_documents(self):
        for code, count in EXPECTED.items():
            with self.subTest(code):
                link = ChecklistTaskLink.objects.get(template_task__code=code)
                self.assertEqual(link.checklist.items.count(), count)

    def test_total_items_seeded(self):
        seeded = ChecklistItem.objects.filter(
            checklist__code__startswith='OPEX-INST-').count()
        self.assertEqual(seeded, TOTAL_ITEMS)

    def test_sections_are_in_source_document_order(self):
        for code, sections in EXPECTED_SECTIONS.items():
            with self.subTest(code):
                link = ChecklistTaskLink.objects.get(template_task__code=code)
                self.assertEqual(link.checklist.sections, sections)

    def test_links_are_keyed_by_template_task_not_by_name(self):
        """2.4's key. A link with template_task=None resolves only through the logged
        string fallback, which is the path this content must never be on."""
        for code in EXPECTED:
            with self.subTest(code):
                link = ChecklistTaskLink.objects.get(template_task__code=code)
                self.assertIsNotNone(link.template_task_id)
                self.assertEqual(link.project_type, 'OPEX')
                # save() derives the strings from the FK; they must agree with it.
                self.assertEqual(link.task_name, link.template_task.label)

    def test_no_items_carry_blank_labels(self):
        self.assertFalse(
            ChecklistItem.objects.filter(
                checklist__code__startswith='OPEX-INST-', label='').exists())

    def test_rms_and_meter_have_no_placeholder_checklist(self):
        for code in UNCHECKLISTED:
            with self.subTest(code):
                self.assertFalse(
                    ChecklistTaskLink.objects.filter(template_task__code=code).exists())

    def test_running_the_command_twice_creates_nothing_further(self):
        """Idempotent by checklist code. A second run on a live database must not
        create a second family, a second link, or 272 more items."""
        call_command('seed_opex_installation_checklists', stdout=StringIO())
        self.assertEqual(Checklist.objects.filter(code__startswith='OPEX-INST-').count(), 7)
        self.assertEqual(
            ChecklistItem.objects.filter(
                checklist__code__startswith='OPEX-INST-').count(),
            TOTAL_ITEMS)

    def test_dry_run_writes_nothing(self):
        Checklist.objects.filter(code__startswith='OPEX-INST-').delete()
        call_command('seed_opex_installation_checklists', dry_run=True, stdout=StringIO())
        self.assertEqual(Checklist.objects.filter(code__startswith='OPEX-INST-').count(), 0)


class _ActivatedSiteFixture(TestCase):
    """One really activated OPEX site, so the tasks under test are the rows
    `attach_opex_template()` produced rather than hand-made Task objects."""

    @classmethod
    def setUpTestData(cls):
        resolve_residential_template()   # bootstraps RESIDENTIAL v1 on a virgin DB
        _seed_opex()
        call_command('seed_opex_installation_checklists', stdout=StringIO())
        cls.pm = _profile('ck_pm', 'PM')

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.pm.user)

        self.site = Project.objects.create(
            customer_name='Checklist Content Site',
            customer_phone='9876543210',
            site_address='1 Checklist Road',
            city='Lucknow',
            project_type='OPEX',
            capacity_kw=Decimal('100.00'),
            status='Draft',
            assigned_pm=self.pm,
        )
        response = self.client.post(
            reverse('opex_site_activate', args=[self.site.project_id]))
        self.assertEqual(response.status_code, 302, 'OPEX activation did not redirect')
        self.site.refresh_from_db()
        self.assertEqual(self.site.status, 'Active')

    def _task(self, code):
        return Task.objects.get(phase__project=self.site, template_task__code=code)


class ResolutionTests(_ActivatedSiteFixture):
    """Every assertion goes through the real resolver — see the module docstring."""

    def test_each_of_the_seven_resolves_to_its_own_checklist(self):
        for code, count in EXPECTED.items():
            with self.subTest(code):
                task      = self._task(code)
                checklist = _checklist_for_task(task, self.site)
                self.assertIsNotNone(checklist, f'{code} resolved to no checklist')
                self.assertEqual(checklist.items.count(), count)
                self.assertFalse(checklist.requires_photo)

    def test_resolution_returns_the_checklist_named_for_the_task(self):
        """Guards against every task resolving to the SAME checklist, which the count
        assertions above would not catch for the two 11/29-item pairs."""
        resolved = {
            code: _checklist_for_task(self._task(code), self.site).code
            for code in EXPECTED
        }
        self.assertEqual(len(set(resolved.values())), 7)

    def test_sections_survive_resolution(self):
        for code, sections in EXPECTED_SECTIONS.items():
            with self.subTest(code):
                checklist = _checklist_for_task(self._task(code), self.site)
                self.assertEqual(checklist.sections, sections)


class MergedChecklistRenderTests(_ActivatedSiteFixture):
    """Civil Work merges two documents. The screen must show both, apart, and numbered
    as one list — the numbering is what makes 'item 27' mean one thing on site."""

    def _rows(self):
        task     = self._task('CIVIL_WORK_AND_MMS_INSTALLATION')
        response = self.client.get(
            reverse('task_detail', args=[self.site.project_id, task.pk]))
        self.assertEqual(response.status_code, 200)
        return response

    def test_both_source_documents_appear_in_their_own_sections(self):
        response = self._rows()
        sections = [group['name'] for group in response.context['checklist_sections']]
        self.assertEqual(
            sections,
            EXPECTED_SECTIONS['CIVIL_WORK_AND_MMS_INSTALLATION'])

    def test_numbering_is_continuous_across_the_merge(self):
        """Section 2 restarts its own loop; the numbers must not restart with it."""
        response = self._rows()
        numbers  = [row['number'] for row in response.context['checklist_rows']]
        self.assertEqual(numbers, list(range(1, 47)))

        # The same numbers, read out of the grouped structure the template renders from.
        grouped = [
            row['number']
            for group in response.context['checklist_sections']
            for row in group['rows']
        ]
        self.assertEqual(grouped, list(range(1, 47)))

    def test_the_first_and_last_lines_are_the_documents_own_text(self):
        rows = self._rows().context['checklist_rows']
        self.assertEqual(rows[0]['label'],
                         'Location Marking as per approved MMS layout GA/drawing')
        self.assertEqual(
            rows[-1]['label'],
            'Sag Rod - Check for the tightness of fixing nut-bolts : Torque value '
            'applied as per design/specification and torque marking')


class NoPhotoAnswerTests(_ActivatedSiteFixture):
    """A Yes with no photograph is a complete answer on these seven."""

    def test_yes_without_a_photo_is_accepted(self):
        task = self._task('AC_CABLE_LAYING')
        item = _checklist_for_task(task, self.site).items.first()

        response = self.client.post(
            reverse('checklist_item_complete',
                    args=[self.site.project_id, task.pk, item.pk]),
            {'answer': 'yes'})
        self.assertIn(response.status_code, (200, 302))

        completion = ChecklistItemCompletion.objects.get(item=item, task=task)
        self.assertTrue(completion.is_checked)
        self.assertEqual(completion.answer, 'yes')
        self.assertFalse(completion.photo_url)
        # R-8: the answer keeps the text it was answering.
        self.assertEqual(completion.item_text_snapshot, item.label)

    def test_no_with_a_remark_is_accepted_without_a_photo(self):
        task = self._task('AC_CABLE_LAYING')
        item = _checklist_for_task(task, self.site).items.all()[1]

        response = self.client.post(
            reverse('checklist_item_complete',
                    args=[self.site.project_id, task.pk, item.pk]),
            {'answer': 'no', 'remarks': 'Routing deviates from drawing at row 4.'})
        self.assertIn(response.status_code, (200, 302))

        completion = ChecklistItemCompletion.objects.get(item=item, task=task)
        self.assertTrue(completion.is_checked)
        self.assertEqual(completion.answer, 'no')
        self.assertFalse(completion.photo_url)


class RmsAndMeterTests(_ActivatedSiteFixture):
    """The two deliberate absences, and the thing that matters about them."""

    def test_they_resolve_to_no_checklist(self):
        for code in UNCHECKLISTED:
            with self.subTest(code):
                self.assertIsNone(
                    _checklist_for_task(self._task(code), self.site))

    def test_the_task_page_renders_without_a_checklist(self):
        for code in UNCHECKLISTED:
            with self.subTest(code):
                task     = self._task(code)
                response = self.client.get(
                    reverse('task_detail', args=[self.site.project_id, task.pk]))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['checklist_rows'], [])

    def test_having_no_checklist_does_not_gate_their_completion(self):
        """The failure mode worth testing for. If an absent checklist ever becomes a
        completion gate, these two tasks strand on every OPEX site — and the gate that
        does exist (2.1's submit/approve) must be the only thing in the way."""
        for code in UNCHECKLISTED:
            with self.subTest(code):
                task = self._task(code)
                # Two rules that have nothing to do with checklists stand in front of
                # this transition on a fresh OPEX site -- the task is unassigned, and
                # OPEX activation deliberately sets no due dates. Both are cleared here
                # so that what the assertion below measures is the checklist question and
                # not either of them.
                assign_task_to(task, self.pm, notify=False)
                task.due_date = date.today() + timedelta(days=7)
                task.save(update_fields=['due_date'])
                response = self.client.post(
                    reverse('task_status_update',
                            args=[self.site.project_id, task.pk]),
                    {'status': Task.IN_PROGRESS})
                self.assertIn(response.status_code, (200, 302))
                task.refresh_from_db()
                self.assertEqual(task.status, Task.IN_PROGRESS)


class ResidentialIsUntouchedTests(TestCase):
    """The seed is scoped to OPEX. A Residential task whose label happens to look like
    one of these must not pick a link up — project_type is carried on the resolver's
    join for exactly this reason."""

    @classmethod
    def setUpTestData(cls):
        resolve_residential_template()
        _seed_opex()
        call_command('seed_opex_installation_checklists', stdout=StringIO())

    def test_every_seeded_link_is_opex(self):
        self.assertFalse(
            ChecklistTaskLink.objects.filter(
                checklist__code__startswith='OPEX-INST-'
            ).exclude(project_type='OPEX').exists())

    def test_residential_checklist_links_are_unchanged(self):
        """Nothing the seed wrote may appear under RESIDENTIAL."""
        self.assertEqual(
            ChecklistTaskLink.objects.filter(
                project_type='RESIDENTIAL',
                checklist__code__startswith='OPEX-INST-').count(),
            0)
