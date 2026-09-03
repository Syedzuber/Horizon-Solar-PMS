"""
Prompt DEMO-1 — the demo-data commands.

WHAT IS UNDER TEST HERE IS TOOLING, NOT PRODUCT. `seed_opex_test_data` and
`teardown_opex_test_data` ship no user-visible behaviour; they exist so that somebody
can open an activated OPEX site in a browser before the merge. So this module is small
and pins exactly five properties — the ones that, if they broke, would make the tooling
dangerous rather than merely broken:

  1. A seed followed by a teardown leaves the database EXACTLY as it was found, per
     model. This is the property the whole manifest design exists to produce, and a
     per-model row-count comparison is the only assertion that actually proves it —
     "the demo rows are gone" would pass while a cascade quietly took something else.
  2. The teardown REFUSES without a manifest, and its refusal names the regression.
     A fallback to name-matching live tables is the failure mode the manifest replaced,
     and an error path is the worst place to reintroduce it.
  3. Both commands REFUSE a non-local database without the override flag. Demo data
     reaching production would pollute the CEO dashboard, the EOD digest and every
     execution counter.
  4. Seed → teardown → seed → teardown runs clean. Two known hazards make repeat runs
     the interesting case: a soft-deleted row keeps reserving its unique values, and
     `generate_project_id()` hands a hard-deleted row's number back for reuse.
  5. An activated demo OPEX site really does carry 23 tasks and exactly 8 mirrors,
     BY NAME. Counting alone would pass if the template changed shape underneath.

FIXTURES. `test_settings` disables migrations, so the schema is built from model state
and neither the OPEX task template (migration 0075) nor the BOQ catalogue (0047) exists.
Both are seeded in `setUp`. `_seed_opex()` is imported from tests_opex_activation rather
than copied — one definition of "seed the OPEX template under test", not two.
"""
import json
import shutil
import tempfile
from io import StringIO
from pathlib import Path

from django.apps import apps
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from .models import BOQItemMaster, Project, Task, UserProfile
from .tests_opex_activation import _seed_opex
from .utils import RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL

# The eight OPEX template rows that carry is_mirror=True, stated LITERALLY rather than
# imported. This is a pin: if the template's mirror set changes, this test is supposed
# to fail and make somebody say so out loud, which importing the set would prevent.
EXPECTED_MIRROR_NAMES = {
    'Design',
    # Spec v1.4 split Material Delivery into four and removed the two SCM inspections.
    'Delivery — Solar Panels',
    'Delivery — Inverters',
    'Delivery — BOS Kit',
    'Delivery — MMS',
    'COD',
    'As-Built Drawings',
    'HOTO',
}

EXPECTED_TASK_COUNT  = 23
EXPECTED_PHASE_COUNT = 7

REMOTE_DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME':   ':memory:',
        'HOST':   'acela.proxy.rlwy.net',
    }
}


def _census():
    """Row count for every model the demo commands could touch."""
    counts = {m.__name__: m.objects.count()
              for m in apps.get_app_config('projects').get_models()}
    counts['auth.User'] = User.objects.count()
    return counts


class DemoDataCommandTests(TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='demo-manifest-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.manifest = self.tmp / 'demo_manifest.json'

        # The OPEX template migration 0075 would have created.
        _seed_opex()

        # A Residential BOQ catalogue. get_standard_boq_items() RAISES on an empty one
        # rather than falling back to a literal list, so the seed cannot run without it.
        for order, (code, description, unit, category) in enumerate([
            ('ITM-001', 'Solar Module 540Wp',      'Nos', 'Solar Modules'),
            ('ITM-002', 'Module Mounting Structure', 'Kg', 'Structure'),
            ('ITM-003', 'String Inverter 10kW',    'Nos', 'Inverter'),
            ('ITM-005', 'DC Cable 4sqmm',          'Mtr', 'BOS'),
            ('ITM-008', 'Earthing Electrode',      'Nos', 'BOS'),
        ], start=1):
            BOQItemMaster.objects.create(
                code=code, description=description, unit=unit, category=category,
                project_type='Residential', sort_order=order,
            )
        # A small OPEX catalogue, so the OPEX BOQ half of the seed does real work.
        for order, (code, description, unit, category) in enumerate([
            ('OPX-001', 'Tender Module 545Wp', 'Nos', 'Solar Modules'),
            ('OPX-002', 'Tender Structure',    'Kg',  'Structure'),
            ('OPX-003', 'Tender AC Cable',     'Mtr', 'BOS'),
        ], start=1):
            BOQItemMaster.objects.create(
                code=code, description=description, unit=unit, category=category,
                project_type='OPEX', sort_order=order,
            )

        # attach_residential_template() raises and rolls the whole activation back
        # without this account, so it is required data for the Residential half.
        finance = User.objects.create_user(
            username='fixture.finance', password='pw12345678',
            email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)
        UserProfile.objects.filter(user=finance).update(role='Finance')

    # ---------------------------------------------------------------- helpers
    def _seed(self, **kwargs):
        out = StringIO()
        call_command('seed_opex_test_data', manifest=str(self.manifest),
                     stdout=out, stderr=StringIO(), **kwargs)
        return out.getvalue()

    def _teardown(self, **kwargs):
        out = StringIO()
        call_command('teardown_opex_test_data', manifest=str(self.manifest),
                     confirm=True, stdout=out, stderr=StringIO(), **kwargs)
        return out.getvalue()

    # ------------------------------------------------------------------ 1
    def test_seed_then_teardown_leaves_every_row_count_as_it_found_them(self):
        """The property the manifest exists to produce.

        Per model, not in total: a cascade that took one row too many and one too few
        would net to zero and pass a total-only assertion.
        """
        before = _census()

        self._seed()
        seeded = _census()
        self.assertGreater(seeded['Project'], before['Project'],
                           'the seed created no projects — the fixture is wrong')

        self._teardown()
        self.assertEqual(_census(), before)

    # ------------------------------------------------------------------ 2
    def test_teardown_refuses_without_a_manifest(self):
        missing = self.tmp / 'not-written.json'
        with self.assertRaises(CommandError) as caught:
            call_command('teardown_opex_test_data', manifest=str(missing),
                         confirm=True, stdout=StringIO(), stderr=StringIO())
        message = str(caught.exception)
        self.assertIn(str(missing), message)
        # It must not merely say "not found". Whoever hits this needs to be told that
        # rows from the OLD prefix-matching seed are the known casualty, or they will
        # conclude the tool is broken and go looking for a bug that is not there.
        self.assertIn('Test-', message)
        self.assertIn('by hand', message)

    def test_teardown_refuses_a_corrupt_manifest_rather_than_guessing(self):
        self.manifest.write_text('{"entries": not json at all', encoding='utf-8')
        with self.assertRaises(CommandError):
            call_command('teardown_opex_test_data', manifest=str(self.manifest),
                         confirm=True, stdout=StringIO(), stderr=StringIO())

    def test_teardown_deletes_nothing_when_it_refuses(self):
        """A refusal is not a partial delete."""
        self._seed()
        before = _census()
        with self.assertRaises(CommandError):
            call_command('teardown_opex_test_data',
                         manifest=str(self.tmp / 'elsewhere.json'),
                         confirm=True, stdout=StringIO(), stderr=StringIO())
        self.assertEqual(_census(), before)

    # ------------------------------------------------------------------ 3
    @override_settings(DATABASES=REMOTE_DATABASES)
    def test_seed_refuses_a_non_local_host_without_the_flag(self):
        err = StringIO()
        with self.assertRaises(SystemExit):
            call_command('seed_opex_test_data', manifest=str(self.manifest),
                         stdout=StringIO(), stderr=err)
        message = err.getvalue()
        self.assertIn('REFUSING TO RUN', message)
        self.assertIn('acela.proxy.rlwy.net', message)     # names the host
        self.assertIn('demo users', message)               # and what it would write
        self.assertEqual(User.objects.filter(username__startswith='demo.').count(), 0)

    @override_settings(DATABASES=REMOTE_DATABASES)
    def test_teardown_refuses_a_non_local_host_without_the_flag(self):
        """And it refuses on the HOST before it ever looks for a manifest — so the
        interlock cannot be sidestepped by pointing it at a missing file."""
        err = StringIO()
        with self.assertRaises(SystemExit):
            call_command('teardown_opex_test_data', manifest=str(self.manifest),
                         confirm=True, stdout=StringIO(), stderr=err)
        self.assertIn('REFUSING TO RUN', err.getvalue())
        self.assertIn('acela.proxy.rlwy.net', err.getvalue())

    def test_a_local_host_is_not_refused(self):
        """The counterpart assertion: the interlock must not refuse everything.

        test_settings supplies no HOST at all, which is what a socket connection looks
        like, and is the case the LOCAL_HOSTS set has to admit.
        """
        self._seed()
        self.assertTrue(Project.objects.filter(project_id__startswith='DEMO').exists())

    # ------------------------------------------------------------------ 4
    def test_seed_teardown_seed_teardown_runs_clean(self):
        """Two full cycles.

        The hazards this covers are real and recorded: a soft-deleted row keeps
        reserving its unique values (`Project.project_id` is UNIQUE at the database
        level), and `generate_project_id()` derives the next number from the highest
        suffix ever issued, so a hard-deleted row's number becomes reusable. Both bite
        on the SECOND seed or not at all.
        """
        before = _census()

        for cycle in (1, 2):
            self._seed()
            self.assertEqual(
                Project.objects.filter(project_id__startswith='DEMO').count(), 5,
                f'cycle {cycle}: wrong number of demo projects')
            self.assertEqual(
                User.objects.filter(username__startswith='demo.').count(), 7,
                f'cycle {cycle}: wrong number of demo users')
            self._teardown()
            self.assertEqual(_census(), before, f'cycle {cycle} did not restore state')

    def test_teardown_consumes_the_manifest(self):
        """So the next teardown refuses rather than reporting a database-wide
        'already gone', which reads as a failure."""
        self._seed()
        self.assertTrue(self.manifest.exists())
        self._teardown()
        self.assertFalse(self.manifest.exists())

    def test_a_second_seed_is_refused_while_demo_data_is_present(self):
        self._seed()
        output = self._seed()
        self.assertIn('already present', output)
        self.assertEqual(User.objects.filter(username__startswith='demo.').count(), 7)

    # ------------------------------------------------------------------ 5
    def test_an_activated_demo_opex_site_has_23_tasks_and_8_mirrors_by_name(self):
        self._seed()
        site = Project.objects.get(project_id='DEMOOPEX01')

        self.assertEqual(site.status, 'Active')
        self.assertEqual(site.phases.count(), EXPECTED_PHASE_COUNT)

        tasks = Task.objects.filter(phase__project=site)
        self.assertEqual(tasks.count(), EXPECTED_TASK_COUNT)

        mirrors = tasks.filter(is_mirror=True)
        self.assertEqual(mirrors.count(), 8)
        self.assertEqual(set(mirrors.values_list('task_name', flat=True)),
                         EXPECTED_MIRROR_NAMES)

    def test_the_seed_never_moves_a_mirror_off_its_seeded_status(self):
        """A mirror's status is derived and NO HUMAN MAY WRITE IT (R-18, R-20).

        The seed varies task statuses so dashboards have something to show. If it ever
        moved a mirror it would be manufacturing exactly the state the refusal exists to
        prevent — and demo data would then be cited as proof the workflow works.
        """
        self._seed()
        moved = Task.objects.filter(
            phase__project__project_id__startswith='DEMO', is_mirror=True,
        ).exclude(status=Task.NOT_STARTED)
        self.assertEqual(list(moved), [])

    def test_the_draft_demo_site_is_left_activatable(self):
        """One site stays in Draft with no phases, so activation can be exercised by
        hand in a browser — which is the entire reason this tooling exists."""
        self._seed()
        draft = Project.objects.get(project_id='DEMOOPEX03')
        self.assertEqual(draft.status, 'Draft')
        self.assertEqual(draft.phases.count(), 0)

    # ------------------------------------------------- the manifest itself
    def test_the_manifest_records_the_rows_the_real_paths_wrote(self):
        """Not just the rows the seed constructs by hand.

        StatusTransition is the case that matters: `StatusTransition.project` is
        SET_NULL precisely so a hard-deleted project cannot erase its own history, so
        without an explicit manifest entry every teardown would leave orphaned ledger
        rows behind — permanently, one set per cycle.
        """
        self._seed()
        payload = json.loads(self.manifest.read_text(encoding='utf-8'))
        recorded = {row['model'] for row in payload['entries']}
        for label in ('projects.StatusTransition', 'projects.ActivityLog',
                      'projects.Project', 'projects.Task', 'projects.ProjectPhase',
                      'projects.StockLocation', 'auth.User'):
            self.assertIn(label, recorded)

    def test_the_dry_run_writes_nothing_at_all(self):
        before = _census()
        output = self._seed(dry_run=True)
        self.assertIn('DRY RUN', output)
        self.assertFalse(self.manifest.exists())
        self.assertEqual(_census(), before)
