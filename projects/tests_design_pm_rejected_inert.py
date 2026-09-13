"""The PM-rejected design status, its attempt reason and the Design Head's classification
fields exist — and NOTHING CAN WRITE ANY OF THEM.

WHY THIS FILE EXISTS
--------------------
The third session of this shape, after 3.1a (the PM-approval status) and 3.1b-1 (the PM's
approval surface). A PM who rejects a package sends it back to the Design Head. The
package's attempt already carries head_verdict='passed', so the Head cannot act on it
through design_head_qc_fail() without overwriting that verdict; the rejection needs a status
of its own, a reason for the rework loop it may open, and somewhere for the Head to classify
whose fault it was. All three are added here, and every list, map, guard and figure that
reads them is taught to handle them, BEFORE any code path can produce them. The
transitions are prompt 3.1b-2b.

  (a) the inertness proof — a source walk for writes of the status, the reason, the fields
  (b) the declaration — every named list, IS or IS NOT, with its reason, written by hand
  (c) the CHECK constraint, both directions, against the real database
  (d) the mirror derivation does not raise
  (e) the four guards and three screen flags refuse the status — and still answer
      identically for every other status
  (f) classify_attempt_causes and _failure_rows on fixture attempts

THE GREP KEYS ON CONSTANT NAMES, NEVER ON THE LITERAL. Three columns share the spelling of
the new value: DesignAssignment.status, DesignAttempt.opened_reason and
StatusTransition.reason_code. design_pm_reject() already writes the third one, and that
write must not read as a status write. Word boundaries do the separating — the reason_code
constant's name CONTAINS the status constant's name, but with an underscore in front of it,
so \\b cannot match inside it. A bare literal is caught only where it is bound to a status or
opened_reason name, which a reason_code= kwarg never is. test_04 pins that quiet line.
"""
import os
import re
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .design_analytics import _failure_rows
from .design_metrics import (
    CAUSE_UNCATEGORISED, HEAD_ACTION_STAGES, QC_ACTION_STAGES, STAGE_ORDER,
    _classify, attempt_cause_split, attention_list, classify_attempt_causes, is_overdue,
)
from .design_views import (
    ARKA_SUBMITTABLE_STATUSES, CHANGE_REQUEST_STATUSES, DESIGN_MIRROR_STATE_MAP,
    REALLOCATABLE_STATUSES, _DESIGNER_ACTIONS, derive_design_mirror_state,
    designer_dashboard_context,
)
from .models import (
    DesignAssignment, DesignAttempt, DueDateCommitment, Program, Project, Task,
    ATTEMPT_REASON_INITIAL, ATTEMPT_REASON_PM_REJECTED, DESIGN_ATTEMPT_REASON_CHOICES,
    DESIGN_ASSIGNMENT_STATUS_CHOICES, DESIGN_AWAITING_ALLOCATION, DESIGN_AWAITING_HEAD_QC,
    DESIGN_AWAITING_PM_APPROVAL, DESIGN_AWAITING_SURVEY, DESIGN_IN_QC,
    DESIGN_NOT_WITH_DESIGNER_STATUSES, DESIGN_PM_REJECTED, DESIGN_RELEASED,
    DESIGN_SURVEY_RETURNED, DESIGN_WORK_FINISHED_STATUSES, DESIGN_ARTIFACTS_UPLOADED,
    ERR_LAYOUT, ERR_REQUIREMENT_CHANGED, ERR_SURVEY_INADEQUATE, ERROR_GROUP_A,
    ERROR_GROUP_B, ERROR_GROUP_C, REASON_DESIGN_PM_REJECTED,
)
from .views import TENDER_DESIGN_SUBMITTED_STATUSES

_THIS_FILE = 'tests_design_pm_rejected_inert.py'


# ===========================================================================
# (a) THE INERTNESS PROOF
# ===========================================================================
#
# Every searched-for name is assembled from pieces so that no line of this file contains
# it whole outside the fixtures the walk is MEANT to find.
_STATUS  = 'DESIGN_' + 'PM_REJECTED'
_REASON  = 'ATTEMPT_REASON_' + 'PM_REJECTED'
_FIELD   = 'pm_rejection' + r'_(?:category|remarks)'
_LITERAL = r'''['"]pm_''' + r'''rejected['"]'''

#: Line-level write shapes. Comment lines are skipped (a commented-out write writes
#: nothing), and so are pure queryset READS — a filter/exclude/Q with no write call on the
#: same line names a value without storing it; the model's CHECK constraint is one.
#:   S1  a `status`-named target assigned the status constant (attribute, local, kwarg)
#:   S2  a dict key 'status' mapped to it — an extra_fields dict
#:   S4  a `return` of it — a computed-status helper
#:   S5  a `status`-named target assigned the bare literal
#:   R1  `opened_reason` assigned the reason constant, or the bare literal
#:   R3  a `reason` kwarg or local given the reason constant — _open_next_attempt(reason=)
#:   F1  either new field assigned (attribute or kwarg); model definitions are skipped
#:   F2  either new field as a dict key
_LINE_WRITES = [
    ('S1', re.compile(rf'\b\w*status\w*\s*=(?!=)[^#\n]*\b{_STATUS}\b')),
    ('S2', re.compile(rf'''['"]status['"]\s*:\s*[^#\n]*\b{_STATUS}\b''')),
    ('S4', re.compile(rf'\breturn\b[^#\n]*\b{_STATUS}\b')),
    ('S5', re.compile(rf'\b\w*status\w*\s*=(?!=)\s*{_LITERAL}')),
    ('R1', re.compile(rf'\bopened_reason\s*=(?!=)\s*(?:[^#\n]*\b{_REASON}\b|{_LITERAL})')),
    ('R3', re.compile(rf'\breason\s*=(?!=)[^#\n]*\b{_REASON}\b')),
    ('F1', re.compile(rf'\b{_FIELD}\s*=(?!=)(?!\s*models\.)')),
    ('F2', re.compile(rf'''['"]{_FIELD}['"]\s*:''')),
]
#: Whole-file shapes, across newlines: the status passed positionally to the chokepoint,
#: and the reason passed positionally to the one function that opens an attempt.
_WHOLE_FILE = [
    ('S3', re.compile(rf'apply_design_status\(\s*[^,()]+,\s*{_STATUS}\b', re.DOTALL)),
    ('R2', re.compile(rf'_open_next_attempt\(\s*[^,()]+,\s*{_REASON}\b', re.DOTALL)),
]
_READ_ONLY_QUERY = re.compile(r'\.filter\(|\.exclude\(|\bQ\(')
_WRITE_CALL = re.compile(r'\.(?:update|create|get_or_create|update_or_create|bulk_create)\(')

#: THE FIXTURES — the only writes of any of the three anywhere in projects/, all three in
#: this module: the status (RejectedBase._park_rejected), the two fields
#: (RejectedBase._attempt), and the reason (CauseTests._loop). Nothing else is admitted;
#: prompt 3.1b-2b's writers must be added here, by name, which is the point.
_ALLOWED = {
    (_THIS_FILE, 'S1', '.update(status=' + _STATUS + ')'),
    (_THIS_FILE, 'F1', 'pm_rejection' + '_category=category, ' + 'pm_rejection' + '_remarks=remarks'),
    (_THIS_FILE, 'R3', 'reason=' + _REASON + ','),
}


def _is_write(line):
    return not (_READ_ONLY_QUERY.search(line) and not _WRITE_CALL.search(line))


_PROJECTS_DIR = os.path.dirname(os.path.abspath(__file__))


def _python_sources():
    """Every .py under projects/, migrations excluded (a migration declares choices and
    columns; it cannot write a row's value)."""
    for root, dirs, files in os.walk(_PROJECTS_DIR):
        dirs[:] = [d for d in dirs if d not in ('migrations', '__pycache__')]
        for name in sorted(files):
            if name.endswith('.py'):
                yield os.path.join(root, name)


def find_pm_rejected_writes():
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
                if rx.search(line) and _is_write(line):
                    hits.append((rel, pid, line.strip()))
        for pid, rx in _WHOLE_FILE:
            for match in rx.finditer(text):
                hits.append((rel, pid, ' '.join(match.group(0).split())))
    return hits


class InertnessTests(TestCase):
    """(a) No product code path writes the status, the attempt reason or either field."""

    def test_01_the_only_writers_are_this_modules_fixtures(self):
        offenders = [(rel, pid, line) for rel, pid, line in find_pm_rejected_writes()
                     if not any(rel == f and pid == p and allowed in line
                                for f, p, allowed in _ALLOWED)]
        self.assertEqual(
            offenders, [],
            'A code path now WRITES the PM-rejected status, its attempt reason or the Head\'s '
            'classification fields. They are unreachable on purpose; the writer belongs to '
            'prompt 3.1b-2b and must be admitted in _ALLOWED deliberately:\n'
            + '\n'.join(f'  {rel} [{pid}] {line}' for rel, pid, line in offenders))

    def test_02_the_walk_finds_exactly_the_fixtures(self):
        """Not vacuous: it finds each allowed fixture once and nothing else. Asserted as a
        SET, never by position — walk order is an accident, not the property."""
        hits = find_pm_rejected_writes()
        found = {(rel, pid, allowed) for rel, pid, line in hits
                 for f, p, allowed in _ALLOWED if rel == f and pid == p and allowed in line}
        self.assertEqual(found, _ALLOWED, hits)
        self.assertEqual(len(hits), len(_ALLOWED), hits)

    def test_03_the_patterns_catch_every_write_shape(self):
        """Each pattern fires on a synthetic write of the shape it names. Built from pieces
        so this file's source never contains them whole."""
        S, R, F = _STATUS, _REASON, 'pm_rejection'
        lit = "'pm_" + "rejected'"
        samples = {
            'S1': [f'assignment.status = {S}', f'x.objects.create(status={S})',
                   f'x.objects.filter(pk=1).update(status={S})', f'new_status={S},',
                   f'opening_status = ({S} if a else b)'],
            'S2': [f"extra = {{'status': {S}}}"],
            'S4': [f'    return {S}'],
            'S5': [f'assignment.status = {lit}', f'x.objects.filter(pk=1).update(status={lit})'],
            'R1': [f'opened_reason={R},', f'attempt.opened_reason = {R}',
                   f'DesignAttempt.objects.create(opened_reason={lit})'],
            'R3': [f'_open_next_attempt(a, reason={R}, actor=p)'],
            'F1': [f'attempt.{F}_category = category', f'x.update({F}_remarks=text)'],
            'F2': [f"extra_fields={{'{F}_category': c}}"],
        }
        rx = dict(_LINE_WRITES)
        for pid, lines in samples.items():
            for line in lines:
                self.assertTrue(rx[pid].search(line) and _is_write(line),
                                f'{pid} missed: {line}')
        whole = dict(_WHOLE_FILE)
        self.assertTrue(whole['S3'].search(f'apply_design_status(\n    assignment, {S}, p,'))
        self.assertTrue(whole['R2'].search(f'_open_next_attempt(\n    assignment, {R}, p,'))

    def test_04_reads_and_the_reason_code_write_stay_quiet(self):
        """The shapes the product legitimately uses to READ the values stay quiet — and so
        does design_pm_reject()'s EXISTING reason_code write, which shares the spelling."""
        S, R, F = _STATUS, _REASON, 'pm_rejection'
        rx = dict(_LINE_WRITES)
        quiet = [
            f'if assignment.status == {S}:',
            f"and x['assignment'].status != {S}),",
            f'.filter(status={S})',
            f'elif t.opened_reason == {R}:',
            f'{F}_category = models.CharField(',
            f"condition=models.Q({F}_category='') | ~models.Q({F}_remarks=''),",
            f"category = previous.{F}_category if previous is not None else ''",
            # THE ONE THAT MATTERS: the ledger's reason_code, same spelling, not a status.
            'reason_code=REASON_DESIGN_' + 'PM_REJECTED, remark=remark)',
            "REASON_DESIGN_PM_REJECTED = 'pm_" + "rejected'",
            f'{S}:          Task.IN_PROGRESS,',
        ]
        for line in quiet:
            fired = [pid for pid, r in rx.items() if r.search(line) and _is_write(line)]
            self.assertEqual(fired, [], f'false positive on: {line}')
        self.assertEqual(REASON_DESIGN_PM_REJECTED, DESIGN_PM_REJECTED,
                         'the premise of test_04: the two columns share a spelling')

    def test_05_no_row_carries_the_status_the_reason_or_a_field(self):
        self.assertFalse(DesignAssignment.objects.filter(status=DESIGN_PM_REJECTED).exists())
        attempts = DesignAttempt.objects
        self.assertFalse(attempts.filter(opened_reason=ATTEMPT_REASON_PM_REJECTED).exists())
        self.assertFalse(attempts.exclude(pm_rejection_category='').exists())
        self.assertFalse(attempts.exclude(pm_rejection_remarks='').exists())


# ===========================================================================
# (b) THE DECLARATION — written by hand, never generated from the lists
# ===========================================================================
#
# A STATEMENT OF INTENT, NOT A RECORDING. Each line carries the pre-flight's (A3) reason. A
# test that imported the table it checks would agree with any mistake the table contains.

class DeclarationTests(TestCase):

    def test_01_the_choices(self):
        values = [v for v, _ in DESIGN_ASSIGNMENT_STATUS_CHOICES]
        self.assertEqual(dict(DESIGN_ASSIGNMENT_STATUS_CHOICES)[DESIGN_PM_REJECTED],
                         'PM rejected — awaiting Design Head')
        # Immediately BEFORE awaiting_pm_approval, so that awaiting_pm_approval stays
        # immediately before released — 3.1a's ordering test.
        self.assertEqual(values.index(DESIGN_PM_REJECTED) + 1,
                         values.index(DESIGN_AWAITING_PM_APPROVAL))
        self.assertEqual(values.index(DESIGN_AWAITING_PM_APPROVAL) + 1,
                         values.index(DESIGN_RELEASED))
        self.assertEqual(dict(DESIGN_ATTEMPT_REASON_CHOICES)[ATTEMPT_REASON_PM_REJECTED],
                         'PM rejected')
        self.assertLessEqual(len(DESIGN_PM_REJECTED),
                             DesignAssignment._meta.get_field('status').max_length)
        self.assertLessEqual(len(ATTEMPT_REASON_PM_REJECTED),
                             DesignAttempt._meta.get_field('opened_reason').max_length)

    def test_02_lists_that_MUST_contain_it(self):
        # Submitted and bounced — the `qc_failed` note on the list; leaving it out flips
        # the CEO's design pill backwards on a rejection.
        self.assertIn(DESIGN_PM_REJECTED, TENDER_DESIGN_SUBMITTED_STATUSES)
        # One entry per status or derive raises. In Progress: not released.
        self.assertEqual(DESIGN_MIRROR_STATE_MAP[DESIGN_PM_REJECTED], Task.IN_PROGRESS)
        # One entry per status, or the card renders blank.
        self.assertIn(DESIGN_PM_REJECTED, _DESIGNER_ACTIONS)
        # The guards' question: the designer does not hold a PM-rejected package.
        self.assertIn(DESIGN_PM_REJECTED, DESIGN_NOT_WITH_DESIGNER_STATUSES)

    def test_03_lists_that_must_NOT_contain_it(self):
        # Product decision D2: a PM-rejected package is not finished — out of every
        # attempt-figure denominator while it is with the Head.
        self.assertNotIn(DESIGN_PM_REJECTED, DESIGN_WORK_FINISHED_STATUSES)
        # Product decision D3: not a change-request window.
        self.assertNotIn(DESIGN_PM_REJECTED, CHANGE_REQUEST_STATUSES)
        # An Arka exists; reallocating would orphan its artifacts.
        self.assertNotIn(DESIGN_PM_REJECTED, REALLOCATABLE_STATUSES)
        # The designer does not hold it, so may not submit an Arka onto it.
        self.assertNotIn(DESIGN_PM_REJECTED, ARKA_SUBMITTABLE_STATUSES)

    def test_04_no_stage_of_its_own_and_the_stage_list_is_unchanged(self):
        """Folded into 'awaiting_head_qc' (decision 1): review_queue_age() takes ONE package
        stage, and a new tile would change every tender dashboard."""
        self.assertEqual([k for k, _ in STAGE_ORDER], [
            'awaiting_survey', 'awaiting_allocation', 'allocated', 'due_date_proposed',
            'in_design', 'arka_submitted', 'awaiting_head_arka', 'arka_approved',
            'artifacts_uploaded', 'in_qc', 'awaiting_head_qc', 'blocked',
            'awaiting_pm_approval', 'released'])
        # HEAD_ACTION_STAGES follows from the fold; QC_ACTION_STAGES is not the Head's.
        self.assertIn('awaiting_head_qc', HEAD_ACTION_STAGES)
        self.assertNotIn('awaiting_head_qc', QC_ACTION_STAGES)

    def test_05_the_guard_set_is_the_finished_set_plus_one_and_D13_stays_out(self):
        self.assertEqual(DESIGN_NOT_WITH_DESIGNER_STATUSES,
                         DESIGN_WORK_FINISHED_STATUSES | {DESIGN_PM_REJECTED})
        # §D13: the designer does not hold these either, and a hold there is the live
        # reopen route. Their addition to THIS set is D13's fix, in the NEXT prompt — left
        # out here so no guard answers differently for a reachable status. When D13 lands,
        # this assertion is the one it deliberately changes.
        for status in (DESIGN_ARTIFACTS_UPLOADED, DESIGN_IN_QC, DESIGN_AWAITING_HEAD_QC):
            self.assertNotIn(status, DESIGN_NOT_WITH_DESIGNER_STATUSES)


# ===========================================================================
# Fixtures for (c)–(f)
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


class RejectedBase(TestCase):

    def setUp(self):
        self.head     = _profile('pr_head', 'Design', is_design_head=True)
        self.designer = _profile('pr_des',  'Design')
        self.program = Program.objects.create(
            name='Test-PMRejected', program_type='OPEX', client_name='PRClient',
            status='Active', short_tender_code='PR')
        self.today = timezone.localdate()

    def _site(self, code, status=DESIGN_IN_QC):
        """An allocated site with an APPROVED agreed date, at `status`."""
        site = Project(
            project_id=code, customer_name='PRClient', customer_phone='9876543210',
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

    def _park_rejected(self, assignment):
        """FIXTURE WRITER 1 of 2 — the only write of the status in projects/. Admitted by
        _ALLOWED as S1."""
        DesignAssignment.objects.filter(pk=assignment.pk).update(status=DESIGN_PM_REJECTED)
        assignment.refresh_from_db()
        return assignment

    @staticmethod
    def _attempt(assignment, number, reason=ATTEMPT_REASON_INITIAL, category='', remarks='',
                 save=True):
        """FIXTURE WRITER 2 of 3 — the only write of either field in projects/, admitted by
        _ALLOWED as F1. The reason arrives as a variable here; the one call site that passes
        the reason constant (CauseTests._loop) is fixture writer 3, admitted as R3."""
        attempt = DesignAttempt(
            assignment=assignment, attempt_number=number, opened_reason=reason,
            pm_rejection_category=category, pm_rejection_remarks=remarks)
        if save:
            attempt.save()
        return attempt

    def _login(self, profile):
        self.assertTrue(self.client.login(username=profile.user.username, password='x'))

    def _messages(self, response):
        return ' '.join(str(m) for m in get_messages(response.wsgi_request))


# ===========================================================================
# (c) The CHECK constraint — both directions, enforced by the database
# ===========================================================================

class ConstraintTests(RejectedBase):
    """pm_rejection_remarks_required_with_category: a category requires remarks."""

    def setUp(self):
        super().setUp()
        _, self.assignment = self._site('PR-CK')

    def test_01_both_blank_saves(self):
        """The shape of every existing row. The first draft's constraint refused it, which
        would have failed the migration on all ten attempts in the database."""
        attempt = self._attempt(self.assignment, 1)
        self.assertEqual((attempt.pm_rejection_category, attempt.pm_rejection_remarks), ('', ''))

    def test_02_category_with_blank_remarks_is_refused(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._attempt(self.assignment, 1, category=ERR_LAYOUT)
        self.assertFalse(DesignAttempt.objects.filter(assignment=self.assignment).exists())

    def test_03_category_with_remarks_saves(self):
        attempt = self._attempt(self.assignment, 1, category=ERR_LAYOUT, remarks='wrong rows')
        self.assertEqual(attempt.pm_rejection_category, ERR_LAYOUT)

    def test_04_remarks_without_a_category_saves(self):
        """An implication, not an equivalence: words with no category are not refused by
        the database. Requiring the category is 3.1b-2b's view's job (see B4)."""
        self._attempt(self.assignment, 1, remarks='a note')


# ===========================================================================
# (d) The mirror derivation
# ===========================================================================

class MirrorDerivationTests(TestCase):

    def test_01_derive_does_not_raise_and_matches_the_head_gate(self):
        self.assertEqual(derive_design_mirror_state(DESIGN_PM_REJECTED), Task.IN_PROGRESS)
        self.assertEqual(derive_design_mirror_state(DESIGN_PM_REJECTED),
                         derive_design_mirror_state(DESIGN_AWAITING_HEAD_QC))


# ===========================================================================
# (e) The four guards and three screen flags
# ===========================================================================

class GuardTests(RejectedBase):
    """Each guard on its own, against a control site at `in_qc` that the same guard ALLOWS,
    so the refusal is shown to come from the status and nothing else."""

    # ── 1. design_due_date_propose ───────────────────────────────────────────────
    def test_01_extension_request_is_refused(self):
        control_site, control = self._site('PR-PRO-C')
        parked_site, parked = self._site('PR-PRO-P')
        self._park_rejected(parked)
        self._login(self.designer)
        data = {'proposed_date': str(self.today + timedelta(days=20)),
                'change_reason': 'more time'}
        self.client.post(reverse('design_due_date_propose',
                                 kwargs={'project_id': control_site.project_id}), data)
        self.assertEqual(control.due_date_commitments.count(), 2)
        response = self.client.post(reverse('design_due_date_propose',
                                            kwargs={'project_id': parked_site.project_id}), data)
        self.assertEqual(parked.due_date_commitments.count(), 1)
        self.assertIn('back with the Design Head after the PM rejected it',
                      self._messages(response))

    # ── 2. design_due_date_change ────────────────────────────────────────────────
    def test_02_head_date_change_is_refused(self):
        control_site, control = self._site('PR-CHG-C')
        parked_site, parked = self._site('PR-CHG-P')
        self._park_rejected(parked)
        self._login(self.head)
        data = {'proposed_date': str(self.today + timedelta(days=9)),
                'change_reason': 'pulled in'}
        self.client.post(reverse('design_due_date_change',
                                 kwargs={'project_id': control_site.project_id}), data)
        self.assertEqual(control.due_date_commitments.count(), 2)
        response = self.client.post(reverse('design_due_date_change',
                                            kwargs={'project_id': parked_site.project_id}), data)
        self.assertEqual(parked.due_date_commitments.count(), 1)
        self.assertIn('back with the Design Head after the PM rejected it',
                      self._messages(response))

    # ── 3. design_mark_blocked ───────────────────────────────────────────────────
    def test_03_design_hold_is_refused(self):
        """The reopen door: a hold here would clear back to `in_design`."""
        control_site, control = self._site('PR-BLK-C')
        parked_site, parked = self._site('PR-BLK-P')
        self._park_rejected(parked)
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
        self.assertEqual(parked.status, DESIGN_PM_REJECTED)
        self.assertIsNone(parked.survey_returned_at)
        self.assertIn('was rejected by the PM and is back with the Design Head',
                      self._messages(response))

    # ── 4. designer_dashboard_context → can_mark_blocked ─────────────────────────
    def test_04_can_mark_blocked_is_false(self):
        control_site, _ = self._site('PR-CMB-C')
        parked_site, parked = self._site('PR-CMB-P')
        self._park_rejected(parked)
        ctx = designer_dashboard_context(
            self.designer, [Project.objects.get(pk=control_site.pk),
                            Project.objects.get(pk=parked_site.pk)])
        self.assertTrue(ctx[control_site.pk]['can_mark_blocked'])
        self.assertFalse(ctx[parked_site.pk]['can_mark_blocked'])

    # ── the three screen flags that show those controls ──────────────────────────
    def test_05_my_sites_flags_hide_extension_and_hold(self):
        _, control = self._site('PR-MS-C')
        _, parked = self._site('PR-MS-P')
        self._park_rejected(parked)
        self._login(self.designer)
        rows = {r['assignment'].pk: r for r in
                self.client.get(reverse('design_my_sites')).context['rows']}
        self.assertTrue(rows[control.pk]['can_request_extension'])
        self.assertFalse(rows[parked.pk]['can_request_extension'])
        self.assertFalse(rows[control.pk]['design_work_finished'])
        self.assertTrue(rows[parked.pk]['design_work_finished'])

    def test_06_head_sites_flag_hides_change_date(self):
        control_site, _ = self._site('PR-HS-C')
        parked_site, parked = self._site('PR-HS-P')
        self._park_rejected(parked)
        self._login(self.head)
        rows = {r['site'].pk: r for r in self.client.get(
            reverse('design_head_sites', kwargs={'pk': self.program.pk})).context['rows']}
        self.assertFalse(rows[control_site.pk]['design_work_finished'])
        self.assertTrue(rows[parked_site.pk]['design_work_finished'])

    # ── and every OTHER status answers exactly as it did before ──────────────────
    def test_07_every_other_status_answers_identically(self):
        """THE STOP CONDITION, as a test. The guards and flags moved from the finished set
        to the new one; for every status except the new one, membership is identical, and
        each flag is re-derived through the real code and compared with the pre-change
        formula (which read DESIGN_WORK_FINISHED_STATUSES)."""
        others = [v for v, _ in DESIGN_ASSIGNMENT_STATUS_CHOICES if v != DESIGN_PM_REJECTED]
        for status in others:
            self.assertEqual(status in DESIGN_NOT_WITH_DESIGNER_STATUSES,
                             status in DESIGN_WORK_FINISHED_STATUSES, status)

        sites = {status: self._site(f'PR-ALL-{i:02d}', status=status)
                 for i, status in enumerate(others)}
        ctx = designer_dashboard_context(
            self.designer, [Project.objects.get(pk=s.pk) for s, _ in sites.values()])
        self._login(self.designer)
        my_rows = {r['assignment'].pk: r for r in
                   self.client.get(reverse('design_my_sites')).context['rows']}
        self._login(self.head)
        head_rows = {r['site'].pk: r for r in self.client.get(
            reverse('design_head_sites', kwargs={'pk': self.program.pk})).context['rows']}

        for status, (site, assignment) in sites.items():
            finished = status in DESIGN_WORK_FINISHED_STATUSES
            self.assertEqual(ctx[site.pk]['can_mark_blocked'],
                             status not in (DESIGN_SURVEY_RETURNED, DESIGN_AWAITING_SURVEY,
                                            DESIGN_AWAITING_ALLOCATION) and not finished,
                             status)
            self.assertEqual(my_rows[assignment.pk]['can_request_extension'], not finished,
                             status)
            self.assertEqual(my_rows[assignment.pk]['design_work_finished'], finished, status)
            self.assertEqual(head_rows[site.pk]['design_work_finished'], finished, status)


# ===========================================================================
# (e, continued) The ladder answers — _classify, the designer card, the clock
# ===========================================================================

class LadderAnswerTests(RejectedBase):

    def test_01_classify_files_it_with_the_head_explicitly_never_in_design(self):
        _, parked = self._site('PR-CL')
        self._park_rejected(parked)
        self.assertEqual(_classify(parked, None), 'awaiting_head_qc')

    def test_02_designer_card_offers_nothing_and_says_who_holds_it(self):
        kind, label, waiting = _DESIGNER_ACTIONS[DESIGN_PM_REJECTED]
        self.assertEqual((kind, label), ('none', ''))
        self.assertIn('PM', waiting)
        self.assertIn('Design Head', waiting)

    def test_03_is_overdue_is_false_because_the_designer_delivered(self):
        _, parked = self._site('PR-OD')
        self._park_rejected(parked)
        past = parked.due_date_commitments.get()
        past.proposed_date = self.today - timedelta(days=10)
        past.save(update_fields=['proposed_date'])
        self.assertFalse(is_overdue(parked, past, self.today))
        # Control: the same row at in_qc or awaiting_head_qc IS overdue — the designer does
        # not hold those either, which is why "does not hold it" is not the rule.
        for status in (DESIGN_IN_QC, DESIGN_AWAITING_HEAD_QC):
            parked.status = status
            self.assertTrue(is_overdue(parked, past, self.today), status)

    def test_04_attention_lists_it_as_the_heads_not_for_its_revisions(self):
        _, parked = self._site('PR-AT')
        self._park_rejected(parked)
        site = {'assignment': parked, 'project': parked.project, 'designer': self.designer,
                'stage': _classify(parked, None), 'overdue': False, 'days_over': 0,
                'pending_crs': [], 'blocked': False, 'revisions': 5, 'released': False,
                'arka': None}
        rows = attention_list([site], self.today)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['reason'].startswith('Waiting on you'), rows[0]['reason'])
        # Control: the same site at in_qc is listed for its revisions.
        parked.status = DESIGN_IN_QC
        site['stage'] = _classify(parked, None)
        self.assertTrue(attention_list([site], self.today)[0]['reason']
                        .startswith('Due date revised'))


# ===========================================================================
# (f) classify_attempt_causes and _failure_rows
# ===========================================================================

class CauseTests(RejectedBase):
    """Unsaved attempts: classify_attempt_causes() and _failure_rows() read rows in hand
    and issue no query, so none is needed."""

    def setUp(self):
        super().setUp()
        _, self.assignment = self._site('PR-CA')

    def _loop(self, category):
        first = self._attempt(self.assignment, 1, category=category,
                              remarks='sent back' if category else '', save=False)
        second = self._attempt(self.assignment, 2, reason=ATTEMPT_REASON_PM_REJECTED,
                               save=False)
        return [first, second]

    def test_01_group_a_charges_the_designer(self):
        attempts = self._loop(ERR_LAYOUT)
        self.assertEqual(classify_attempt_causes(attempts)[2], ERROR_GROUP_A)
        split = attempt_cause_split(attempts)
        self.assertEqual((split['designer'], split['input']), (1, 0))

    def test_02_group_b_and_c_do_not(self):
        for category, group in ((ERR_SURVEY_INADEQUATE, ERROR_GROUP_B),
                                (ERR_REQUIREMENT_CHANGED, ERROR_GROUP_C)):
            attempts = self._loop(category)
            self.assertEqual(classify_attempt_causes(attempts)[2], group)
            split = attempt_cause_split(attempts)
            self.assertEqual((split['designer'], split['input']), (0, 1), category)

    def test_03_blank_is_uncategorised_and_therefore_charged(self):
        """Why 3.1b-2b must require the category on the send-back path."""
        attempts = self._loop('')
        self.assertEqual(classify_attempt_causes(attempts)[2], CAUSE_UNCATEGORISED)
        self.assertEqual(attempt_cause_split(attempts)['designer'], 1)

    def test_04_it_is_never_counted_as_an_initial_attempt(self):
        """The bug the branch closes: the old `else` filed an unknown reason as initial."""
        split = attempt_cause_split(self._loop(ERR_LAYOUT))
        self.assertEqual(split['initial'], 1)

    def test_05_failure_rows_gains_a_PM_source_and_the_other_two_are_unchanged(self):
        first, second = self._loop(ERR_LAYOUT)
        data = {'attempts': [first, second], 'arkas': [], 'attempt_by_id': {},
                'assignment_by_id': {self.assignment.pk: self.assignment}}
        rows = _failure_rows(data)
        self.assertEqual([(r['category'], r['gate'], r['source']) for r in rows],
                         [(ERR_LAYOUT, 'PM', 'Package')])
        # An attempt with only a gate field set reports exactly as before: one row, from
        # that gate, and no PM row (its new field is blank, as on every real row today).
        gate_only = self._attempt(self.assignment, 3, save=False)
        gate_only.head_failure_category = ERR_LAYOUT
        data['attempts'] = [gate_only]
        self.assertEqual([(r['category'], r['gate']) for r in _failure_rows(data)],
                         [(ERR_LAYOUT, 'Design Head')])
