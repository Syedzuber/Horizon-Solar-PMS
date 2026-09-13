"""Prompt 3.1a — the design PM-approval status exists, and NOTHING CAN REACH IT.

WHY THIS FILE EXISTS
--------------------
`awaiting_pm_approval` was added to DESIGN_ASSIGNMENT_STATUS_CHOICES, and every list,
map and guard that reads the design status ladder was taught to handle it, BEFORE any
code path can produce it. The transition that writes it is prompt 3.1b, a later session.

The safety property is: NO PRODUCT CODE PATH WRITES THE STATUS OR EITHER OF ITS TWO STAMP
FIELDS (`pm_approved_at`, `pm_approved_by`). Behaviour is therefore unchanged, and that
is proved by grep below (section a) rather than argued.

  (a) the inertness proof — a source walk for writes of the status and the two fields
  (b) the declaration — the audit's §1.4 classification, pinned as intent
  (c) the mirror derivation does not raise on the new status
  (d) the five "work finished" guards each refuse a site in the new status
  (e) _classify() and _DESIGNER_ACTIONS answer for it explicitly

THE ONLY WRITER OF THE NEW STATUS ANYWHERE IN projects/ IS THIS MODULE'S OWN FIXTURE:
`PmGateBase._park_in_pm_gate()`, which moves a fixture row there with a queryset
update(). Nothing writes the two fields at all, fixtures included. Prompt 3.1b will have to
change section (a) to admit its writer — that is the point: the new writer gets reviewed.
"""
import os
import re
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import design_metrics, design_views, models as models_module, views as views_module
from .design_metrics import (
    HEAD_ACTION_STAGES, QC_ACTION_STAGES, STAGE_LABELS, STAGE_ORDER,
    _classify, attention_list, is_overdue,
)
from .design_views import (
    ARKA_SUBMITTABLE_STATUSES, CHANGE_REQUEST_STATUSES, DESIGN_MIRROR_STATE_MAP,
    REALLOCATABLE_STATUSES, _DESIGNER_ACTIONS, derive_design_mirror_state,
    designer_dashboard_context,
)
from .models import (
    DesignAssignment, DueDateCommitment, Program, Project, Task,
    DESIGN_ASSIGNMENT_STATUS_CHOICES, DESIGN_AWAITING_HEAD_QC, DESIGN_AWAITING_PM_APPROVAL,
    DESIGN_IN_DESIGN, DESIGN_IN_QC, DESIGN_RELEASED, DESIGN_SURVEY_RETURNED,
    DESIGN_WORK_FINISHED_STATUSES,
)
from .views import TENDER_DESIGN_SUBMITTED_STATUSES


# ===========================================================================
# (a) THE INERTNESS PROOF
# ===========================================================================
#
# The searched-for names are assembled from pieces so that the pattern lines below never
# contain them whole — otherwise this file would match its own regexes.
_NAME    = 'DESIGN_AWAITING_' + 'PM_APPROVAL'
_LITERAL = "'awaiting_" + "pm_approval'"
_FIELD   = r'pm_approved_(?:at|by)(?:_id)?'

_STATUS_VALUE = rf'(?:\b{_NAME}\b|{_LITERAL}|"awaiting_pm_approval")'

#: Line-level write shapes. Comment lines are skipped — a commented-out write writes
#: nothing — and so are pure queryset READS (filter/exclude/Q with no write call on the
#: same line), which name the value without storing it.
#:   W1  a name containing `status`, assigned (attribute, local or kwarg) an expression
#:       naming the value — covers attribute writes, create()/update() kwargs, the
#:       chokepoint's new_status kwarg, and a computed opening status
#:   W2  a dict key `status` mapped to the value — an extra_fields dict
#:   W4  a `return` of the value, constant form only — a computed-status helper. The
#:       stage KEY of the same spelling is legitimately returned by design_metrics.
#:       _classify(); that is a stage, not a status, so W4 does not look at literals.
#:   F1  either stamp field assigned (attribute or kwarg); model field definitions,
#:       which assign `models.<Field>(...)`, are skipped
#:   F2  either stamp field as a dict key — an extra_fields dict
_LINE_WRITES = [
    ('W1', re.compile(rf'\b\w*status\w*\s*=(?!=)[^#\n]*{_STATUS_VALUE}')),
    ('W2', re.compile(rf'''['"]status['"]\s*:\s*{_STATUS_VALUE}''')),
    ('W4', re.compile(rf'\breturn\b[^#\n]*\b{_NAME}\b')),
    ('F1', re.compile(rf'\b{_FIELD}\s*=(?!=)(?!\s*models\.)')),
    ('F2', re.compile(rf'''['"]{_FIELD}['"]\s*:''')),
]
#: Whole-file shape: the status passed positionally to the chokepoint, across newlines.
_CHOKEPOINT_CALL = re.compile(
    rf'apply_design_status\(\s*[^,()]+,\s*{_STATUS_VALUE}', re.DOTALL)
_READ_ONLY_QUERY = re.compile(r'\.filter\(|\.exclude\(|\bQ\(')
#: A write call on the same line overrides the read exemption: `filter(pk=..).update(..)`
#: is a write, and is exactly the shape of this module's own fixture.
_WRITE_CALL = re.compile(r'\.(?:update|create|get_or_create|update_or_create|bulk_create)\(')

#: THE FIXTURE — the one line in all of projects/ allowed to write the new status.
#: Assembled from pieces for the same reason as the names above.
_ALLOWED = {
    ('tests_design_pm_gate_inert.py', '.update(status=' + _NAME + ')'),
    # PROMPT 3.1b-1 IS THE AUTHORITY FOR THIS ONE. design_pm_approve() writes the two
    # stamp fields in the same write as the release. Stamps only: the STATUS is still
    # written by the fixture above and nothing else. Assembled from pieces like the rest.
    ('design_views.py', "'" + 'pm_approved' + "_at': now, '" + 'pm_approved' + "_by': profile}"),
}


def _is_write(pid, line):
    if pid == 'W1' and _READ_ONLY_QUERY.search(line) and not _WRITE_CALL.search(line):
        return False
    return True

_PROJECTS_DIR = os.path.dirname(os.path.abspath(__file__))


def _python_sources():
    """Every .py under projects/, migrations excluded (a migration declares choices and
    columns; it cannot write a row's status or stamp)."""
    for root, dirs, files in os.walk(_PROJECTS_DIR):
        dirs[:] = [d for d in dirs if d not in ('migrations', '__pycache__')]
        for name in sorted(files):
            if name.endswith('.py'):
                yield os.path.join(root, name)


def find_pm_gate_writes():
    """[(relative path, pattern id, stripped line)] for every write shape found."""
    hits = []
    for path in _python_sources():
        rel = os.path.relpath(path, _PROJECTS_DIR)
        with open(path, encoding='utf-8-sig') as fh:
            text = fh.read()
        for line in text.splitlines():
            if line.lstrip().startswith('#'):
                continue
            for pid, rx in _LINE_WRITES:
                if rx.search(line) and _is_write(pid, line):
                    hits.append((rel, pid, line.strip()))
        for match in _CHOKEPOINT_CALL.finditer(text):
            hits.append((rel, 'W3', ' '.join(match.group(0).split())))
    return hits


class InertnessTests(TestCase):
    """(a) No product code path writes the new status or either stamp field."""

    def test_01_the_only_writer_is_this_modules_fixture(self):
        hits = find_pm_gate_writes()
        offenders = [(rel, pid, line) for rel, pid, line in hits
                     if not any(rel == f and allowed in line for f, allowed in _ALLOWED)]
        self.assertEqual(
            offenders, [],
            'A code path now WRITES the PM-approval status or its stamp fields. Prompt '
            '3.1a made them unreachable on purpose; the writer belongs to prompt 3.1b and '
            'must be admitted here deliberately:\n'
            + '\n'.join(f'  {rel} [{pid}] {line}' for rel, pid, line in offenders))

    def test_02_the_fixture_is_actually_found(self):
        """The walk is not vacuous: it finds exactly the writes _ALLOWED names — the status
        fixture below and design_pm_approve()'s stamp write — each once, and nothing else.

        Asserted as a SET of (file, pattern, allowed-fragment) triples, never by position:
        which hit the walk reaches first depends on os.walk and file-name order, and a proof
        that leans on walk order is encoding an accident rather than the property."""
        hits = find_pm_gate_writes()
        found = {(rel, pid, allowed)
                 for rel, pid, line in hits
                 for f, allowed in _ALLOWED if rel == f and allowed in line}
        self.assertEqual(found, {
            ('tests_design_pm_gate_inert.py', 'W1', '.update(status=' + _NAME + ')'),
            ('design_views.py', 'F2',
             "'" + 'pm_approved' + "_at': now, '" + 'pm_approved' + "_by': profile}"),
        }, hits)
        # And nothing beyond them: one hit per allowed write, no extras hiding in the list.
        self.assertEqual(len(hits), len(found), hits)

    def test_03_the_patterns_catch_every_write_shape(self):
        """Each pattern fires on a synthetic write of the shape it names. The samples are
        built from pieces so this file's source never contains them whole."""
        F = 'pm_approved'
        samples = {
            'W1': [f'assignment.status = {_NAME}', f'x.objects.create(status={_NAME})',
                   f'x.objects.filter(pk=1).update(status={_NAME})',
                   f'new_status={_LITERAL},', f'opening_status = ({_NAME} if a else b)'],
            'W2': [f"extra = {{'status': {_NAME}}}"],
            'W4': [f'    return {_NAME}'],
            'F1': [f'assignment.{F}_at = now', f'update({F}_by=profile)'],
            'F2': [f"extra_fields={{'{F}_at': now}}"],
        }
        rx = dict(_LINE_WRITES)
        for pid, lines in samples.items():
            for line in lines:
                self.assertTrue(rx[pid].search(line) and _is_write(pid, line),
                                f'{pid} missed: {line}')
        self.assertTrue(_CHOKEPOINT_CALL.search(
            f'apply_design_status(\n    assignment, {_NAME}, profile,'))

    def test_04_reads_are_not_reported_as_writes(self):
        """The shapes the product legitimately uses to READ the value stay quiet."""
        F = 'pm_approved'
        rx = dict(_LINE_WRITES)
        quiet = [
            f'if status == {_NAME}:',
            f'.filter(status={_NAME})',
            f'{F}_at = models.DateTimeField(null=True, blank=True)',
            f"readonly_fields = ['status', '{F}_at', '{F}_by']",
            "    return 'awaiting_" + "pm_approval'",       # a design_metrics stage key
        ]
        for line in quiet:
            fired = [pid for pid, r in rx.items() if r.search(line) and _is_write(pid, line)]
            self.assertEqual(fired, [], f'false positive on: {line}')

    def test_05_no_row_on_this_database_carries_the_status_or_a_stamp(self):
        rows = DesignAssignment.objects
        self.assertFalse(rows.filter(status=DESIGN_AWAITING_PM_APPROVAL).exists())
        self.assertFalse(rows.filter(pm_approved_at__isnull=False).exists())
        self.assertFalse(rows.filter(pm_approved_by__isnull=False).exists())


# ===========================================================================
# (b) THE DECLARATION — the audit's §1.4 classification, written by hand
# ===========================================================================
#
# A STATEMENT OF INTENT, NOT A RECORDING. Each entry below is transcribed from
# docs/DESIGN_APPROVAL_AUDIT.md §1.4 and carries that document's reason. None is
# generated by reading the lists: a test that imports the table it checks agrees with any
# mistake the table contains.

class DeclarationTests(TestCase):

    def test_01_the_choices_place_it_immediately_before_released(self):
        values = [v for v, _ in DESIGN_ASSIGNMENT_STATUS_CHOICES]
        self.assertEqual(values.index(DESIGN_AWAITING_PM_APPROVAL) + 1,
                         values.index(DESIGN_RELEASED))
        self.assertEqual(dict(DESIGN_ASSIGNMENT_STATUS_CHOICES)[DESIGN_AWAITING_PM_APPROVAL],
                         'Awaiting PM approval')
        self.assertLessEqual(len(DESIGN_AWAITING_PM_APPROVAL),
                             DesignAssignment._meta.get_field('status').max_length)

    def test_02_lists_that_MUST_contain_it(self):
        # "The designer's work is done" — the Head has passed it. §1.4: Yes.
        self.assertIn(DESIGN_AWAITING_PM_APPROVAL, DESIGN_WORK_FINISHED_STATUSES)
        self.assertIn(DESIGN_RELEASED, DESIGN_WORK_FINISHED_STATUSES)
        # The artifact has been handed over; leaving it out flips the CEO pill
        # Done -> Not Done between the Head's pass and release. §1.4: Yes.
        self.assertIn(DESIGN_AWAITING_PM_APPROVAL, TENDER_DESIGN_SUBMITTED_STATUSES)
        # One entry per status, and exhaustiveness is test-enforced. §1.4: needs its own entry.
        self.assertIn(DESIGN_AWAITING_PM_APPROVAL, DESIGN_MIRROR_STATE_MAP)
        # One entry per status, or the card renders blank. §1.4: needs its own entry.
        self.assertIn(DESIGN_AWAITING_PM_APPROVAL, _DESIGNER_ACTIONS)
        # One tile per bottleneck, never merged; its own entry before 'released'.
        # §1.4: Yes, as its own entry.
        self.assertIn('awaiting_pm_approval', STAGE_LABELS)

    def test_03_the_stage_sits_immediately_before_released_and_released_stays_last(self):
        keys = [k for k, _ in STAGE_ORDER]
        self.assertEqual(keys.index('awaiting_pm_approval') + 1, keys.index('released'))
        # tender_dashboard.html reads the release tile as `m.stages|last`.
        self.assertEqual(keys[-1], 'released')

    def test_04_lists_that_must_NOT_contain_it(self):
        # Work not yet started — a Head-passed design is far past reallocation. §1.4: No.
        self.assertNotIn(DESIGN_AWAITING_PM_APPROVAL, REALLOCATABLE_STATUSES)
        # The designer may submit an Arka — not once both gates have passed. §1.4: No.
        self.assertNotIn(DESIGN_AWAITING_PM_APPROVAL, ARKA_SUBMITTABLE_STATUSES)
        # The change window. §1.4 left it undecided; prompt 3.1c owns the decision, and
        # until it lands the window is left exactly as it was.
        self.assertNotIn(DESIGN_AWAITING_PM_APPROVAL, CHANGE_REQUEST_STATUSES)
        # Ball in the Design Head's court — it is in the PM's. §1.4: No.
        self.assertNotIn('awaiting_pm_approval', HEAD_ACTION_STAGES)
        # Ball in Design QC's court — it is in the PM's. §1.4: No.
        self.assertNotIn('awaiting_pm_approval', QC_ACTION_STAGES)


# ===========================================================================
# Fixtures for (c)–(e)
# ===========================================================================

def _profile(username, role, is_design_head=False):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.is_design_head = is_design_head
    profile.save()
    return profile


class PmGateBase(TestCase):

    def setUp(self):
        self.head     = _profile('pg_head', 'Design', is_design_head=True)
        self.designer = _profile('pg_des',  'Design')
        self.program = Program.objects.create(
            name='Test-PMGate', program_type='OPEX', client_name='PGClient',
            status='Active', short_tender_code='PG')
        self.today = timezone.localdate()

    def _site(self, code, status=DESIGN_IN_QC):
        """An allocated site with an APPROVED agreed date, at `status`."""
        site = Project(
            project_id=code, customer_name='PGClient', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code=code,
            dc_capacity_kw=Decimal('100.00'), status='Draft')
        site.save()
        assignment = DesignAssignment.objects.create(
            project=site, status=status, assigned_to=self.designer,
            assigned_by=self.head, assigned_at=timezone.now(),
            survey_file_bucket='b', survey_file_path=f'{code}/survey/x.pdf')
        DueDateCommitment.objects.create(
            assignment=assignment, proposed_date=self.today + timedelta(days=5),
            proposed_by=self.head, approved_by=self.head, approved_at=timezone.now(),
            is_current=True)
        return site, assignment

    def _park_in_pm_gate(self, assignment):
        """THE FIXTURE WRITER — the only write of the new status in projects/.
        Named in the module docstring and admitted by InertnessTests._ALLOWED."""
        DesignAssignment.objects.filter(pk=assignment.pk).update(status=DESIGN_AWAITING_PM_APPROVAL)
        assignment.refresh_from_db()
        return assignment

    def _login(self, profile):
        self.assertTrue(self.client.login(username=profile.user.username, password='x'))

    def _messages(self, response):
        return ' '.join(str(m) for m in get_messages(response.wsgi_request))


# ===========================================================================
# (c) The mirror derivation
# ===========================================================================

class MirrorDerivationTests(TestCase):

    def test_01_derive_does_not_raise_and_matches_awaiting_head_qc(self):
        state = derive_design_mirror_state(DESIGN_AWAITING_PM_APPROVAL)
        self.assertEqual(state, Task.IN_PROGRESS)
        # A design with the PM is not released: the same mirror state as the last gate.
        self.assertEqual(state, derive_design_mirror_state(DESIGN_AWAITING_HEAD_QC))


# ===========================================================================
# (d) The five "work finished" guards — each refuses a site in the new status
# ===========================================================================

class WorkFinishedGuardTests(PmGateBase):
    """Each guard is asserted on its own, against a control site at `in_design` that the
    same guard ALLOWS — so the refusal is shown to come from the status and nothing else.
    (The control was `in_qc` until the D13 prompt closed the Design Hold there.)"""

    # ── 1. design_my_sites → can_request_extension ───────────────────────────────
    def test_01_can_request_extension_is_false_in_the_pm_gate(self):
        _, control = self._site('PG-EXT-C', status=DESIGN_IN_DESIGN)
        _, parked = self._site('PG-EXT-P')
        self._park_in_pm_gate(parked)
        self._login(self.designer)
        rows = {r['assignment'].pk: r for r in
                self.client.get(reverse('design_my_sites')).context['rows']}
        self.assertTrue(rows[control.pk]['can_request_extension'])
        self.assertFalse(rows[parked.pk]['can_request_extension'])
        # The Design Hold control's flag, and `is_released` left truthful beside it.
        self.assertTrue(rows[parked.pk]['not_with_designer'])
        self.assertFalse(rows[parked.pk]['is_released'])
        self.assertFalse(rows[control.pk]['not_with_designer'])

    # ── 2. design_due_date_propose ───────────────────────────────────────────────
    def test_02_extension_request_is_refused_in_the_pm_gate(self):
        control_site, control = self._site('PG-PRO-C', status=DESIGN_IN_DESIGN)
        parked_site, parked = self._site('PG-PRO-P')
        self._park_in_pm_gate(parked)
        self._login(self.designer)
        data = {'proposed_date': str(self.today + timedelta(days=20)),
                'change_reason': 'more time'}

        self.client.post(reverse('design_due_date_propose',
                                 kwargs={'project_id': control_site.project_id}), data)
        self.assertEqual(control.due_date_commitments.count(), 2)

        response = self.client.post(reverse('design_due_date_propose',
                                            kwargs={'project_id': parked_site.project_id}), data)
        self.assertEqual(parked.due_date_commitments.count(), 1)
        self.assertIn('with the PM for approval', self._messages(response))

    # ── 3. design_due_date_change ────────────────────────────────────────────────
    def test_03_head_date_change_is_refused_in_the_pm_gate(self):
        control_site, control = self._site('PG-CHG-C', status=DESIGN_IN_DESIGN)
        parked_site, parked = self._site('PG-CHG-P')
        self._park_in_pm_gate(parked)
        self._login(self.head)
        data = {'proposed_date': str(self.today + timedelta(days=9)),
                'change_reason': 'pulled in'}

        self.client.post(reverse('design_due_date_change',
                                 kwargs={'project_id': control_site.project_id}), data)
        self.assertEqual(control.due_date_commitments.count(), 2)

        response = self.client.post(reverse('design_due_date_change',
                                            kwargs={'project_id': parked_site.project_id}), data)
        self.assertEqual(parked.due_date_commitments.count(), 1)
        self.assertIn('with the PM for approval', self._messages(response))

    # ── 4. design_mark_blocked ───────────────────────────────────────────────────
    def test_04_design_hold_is_refused_in_the_pm_gate(self):
        """The §6.1 second door: a hold here would clear back to `in_design`."""
        control_site, control = self._site('PG-BLK-C', status=DESIGN_IN_DESIGN)
        parked_site, parked = self._site('PG-BLK-P')
        self._park_in_pm_gate(parked)
        self._login(self.designer)

        self.client.post(reverse('design_mark_blocked',
                                 kwargs={'project_id': control_site.project_id}),
                         {'reason': 'survey is wrong'})
        control.refresh_from_db()
        self.assertEqual(control.status, DESIGN_SURVEY_RETURNED)

        response = self.client.post(reverse('design_mark_blocked',
                                            kwargs={'project_id': parked_site.project_id}),
                                    {'reason': 'survey is wrong'})
        parked.refresh_from_db()
        self.assertEqual(parked.status, DESIGN_AWAITING_PM_APPROVAL)
        self.assertIsNone(parked.survey_returned_at)
        self.assertIn('with the PM for approval', self._messages(response))

    # ── 5. designer_dashboard_context → can_mark_blocked ─────────────────────────
    def test_05_can_mark_blocked_is_false_in_the_pm_gate(self):
        control_site, _ = self._site('PG-CMB-C', status=DESIGN_IN_DESIGN)
        parked_site, parked = self._site('PG-CMB-P')
        self._park_in_pm_gate(parked)
        ctx = designer_dashboard_context(
            self.designer, [Project.objects.get(pk=control_site.pk),
                            Project.objects.get(pk=parked_site.pk)])
        self.assertTrue(ctx[control_site.pk]['can_mark_blocked'])
        self.assertFalse(ctx[parked_site.pk]['can_mark_blocked'])

    # ── and the Head's screen flag that mirrors guard 3 ──────────────────────────
    def test_06_head_sites_flag_hides_change_date_in_the_pm_gate(self):
        control_site, _ = self._site('PG-HS-C', status=DESIGN_IN_DESIGN)
        parked_site, parked = self._site('PG-HS-P')
        self._park_in_pm_gate(parked)
        self._login(self.head)
        rows = {r['site'].pk: r for r in self.client.get(
            reverse('design_head_sites', kwargs={'pk': self.program.pk})).context['rows']}
        self.assertFalse(rows[control_site.pk]['clock_stopped'])
        self.assertTrue(rows[parked_site.pk]['clock_stopped'])


# ===========================================================================
# (e) _classify() and _DESIGNER_ACTIONS answer explicitly — plus the clock (B6)
# ===========================================================================

class LadderAnswerTests(PmGateBase):

    def test_01_classify_returns_its_own_stage_never_in_design(self):
        _, parked = self._site('PG-CL')
        self._park_in_pm_gate(parked)
        stage = _classify(parked, None)
        self.assertEqual(stage, 'awaiting_pm_approval')
        self.assertNotEqual(stage, 'in_design')
        self.assertEqual(STAGE_LABELS[stage], 'Awaiting PM approval')

    def test_02_designer_actions_offer_nothing_and_say_who_holds_it(self):
        kind, label, waiting = _DESIGNER_ACTIONS[DESIGN_AWAITING_PM_APPROVAL]
        self.assertEqual((kind, label), ('none', ''))
        self.assertTrue(waiting.strip())
        self.assertIn('PM', waiting)

    def test_03_is_overdue_stops_the_designers_clock(self):
        _, parked = self._site('PG-OD')
        self._park_in_pm_gate(parked)
        past = parked.due_date_commitments.get()
        past.proposed_date = self.today - timedelta(days=10)
        past.save(update_fields=['proposed_date'])
        self.assertFalse(is_overdue(parked, past, self.today))
        # Control: the same row back at in_qc IS overdue.
        parked.status = DESIGN_IN_QC
        self.assertTrue(is_overdue(parked, past, self.today))

    def test_04_attention_list_does_not_list_revisions_for_a_parked_site(self):
        _, parked = self._site('PG-AT')
        self._park_in_pm_gate(parked)
        site = {'assignment': parked, 'project': parked.project, 'designer': self.designer,
                'stage': _classify(parked, None), 'overdue': False, 'days_over': 0,
                'pending_crs': [], 'blocked': False, 'revisions': 5, 'released': False,
                'arka': None}
        self.assertEqual(attention_list([site], self.today), [])
        # Control: the same site at in_qc is listed for its revisions.
        parked.status = DESIGN_IN_QC
        site['stage'] = _classify(parked, None)
        self.assertEqual(len(attention_list([site], self.today)), 1)


# Kept importable for the report's raw grep: `python -c "from projects.tests_design_pm_gate_inert
# import find_pm_gate_writes; ..."`. The module-level references below only assert the
# names this file depends on still exist where the MODE said they live.
_LOCATIONS = (models_module.DESIGN_WORK_FINISHED_STATUSES, design_views._DESIGNER_ACTIONS,
              design_metrics.STAGE_ORDER, views_module.TENDER_DESIGN_SUBMITTED_STATUSES)
