"""
revert_design_allocation_31oct — the one-off that undoes the 31-Oct bulk allocation.

The fixture REPLAYS the incident through the real allocation code rather than writing rows
by hand: MB0003 goes to one designer at 13:18, then one bulk-style run at 13:28 allocates
MB0003 and three unassigned sites to another. `timezone.now` is pinned to those instants,
so every row the command checks — ActivityLog, StatusTransition, DueDateCommitment, the
allocation stamps — carries the timestamp production carries, and the command's real
EVENT_START/EVENT_END window is exercised unpatched. Only the production ids are patched,
exactly as tests_oneoff_ukru001 does.

Sites here are not activated, so there is no Design mirror; the mirror leg was verified by
rehearsing --apply against a restore of the 31-Oct production dump.
"""
from datetime import date, datetime
from decimal import Decimal
from io import StringIO
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from .design_views import _allocate_one
from .management.commands import revert_design_allocation_31oct as cmd
from .models import (
    ActivityLog, DesignAssignment, DueDateCommitment, Program, Project, StatusTransition,
    DESIGN_AWAITING_ALLOCATION, DESIGN_IN_DESIGN, SUBJECT_DESIGN_ASSIGNMENT,
)

IST = ZoneInfo('Asia/Kolkata')
AT_1318 = datetime(2026, 10, 31, 13, 18, 38, tzinfo=IST)   # the earlier, intended run
AT_1328 = datetime(2026, 10, 31, 13, 28, 41, 700000, tzinfo=IST)   # the event
AT_1340 = datetime(2026, 10, 31, 13, 40, 0, tzinfo=IST)    # someone acts after it
ALLOCATED_ON = date(2026, 10, 31)                           # gives the event's 2 Nov due date


def _profile(username, role, is_design_head=False):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.is_design_head = is_design_head
    profile.save()
    return profile


class RevertDesignAllocation31OctTests(TestCase):

    def setUp(self):
        self.head = _profile('head31', 'Design', is_design_head=True)
        self.anil = _profile('anil31', 'Design')
        self.sud = _profile('sud31', 'Design')
        self.program = Program.objects.create(
            name='Test-Revert31', program_type='OPEX', client_name='R31Client',
            status='Active', short_tender_code='R31')
        self.a = {code: self._site(code) for code in ('MB0003', 'S1', 'S2', 'S3')}

        # 13:18 — MB0003 to the first designer, as on production.
        with mock.patch('django.utils.timezone.now', return_value=AT_1318):
            _allocate_one(self.a['MB0003'], self.sud, self.head, allocated_on=ALLOCATED_ON)
        # 13:28 — the event: MB0003 reallocated (intended) plus three unassigned sites.
        with mock.patch('django.utils.timezone.now', return_value=AT_1328):
            for code in ('MB0003', 'S1', 'S2', 'S3'):
                _allocate_one(self.a[code], self.anil, self.head, allocated_on=ALLOCATED_ON)

        self.commitments = {
            code: DueDateCommitment.objects.get(assignment=self.a[code], is_current=True).pk
            for code in ('S1', 'S2', 'S3')}
        patcher = mock.patch.multiple(
            cmd, SITES=self.commitments, PROGRAM_ID=self.program.pk,
            DESIGNER_PROFILE_ID=self.anil.pk, ACTOR_PROFILE_ID=self.head.pk)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _site(self, code):
        site = Project(
            project_id=code, customer_name='R31Client', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code=code,
            dc_capacity_kw=Decimal('100.00'), status='Draft')
        site.save()
        return DesignAssignment.objects.create(
            project=site, status=DESIGN_AWAITING_ALLOCATION,
            survey_file_bucket='b', survey_file_path=f'{code}/survey/x.pdf')

    def _run(self, *args):
        out = StringIO()
        call_command('revert_design_allocation_31oct', *args, stdout=out)
        return out.getvalue()

    def _snapshot(self):
        """Every column the command could write, for every site, plus row counts."""
        rows = {a.project.project_id: (a.status, a.assigned_to_id, a.assigned_by_id,
                                       a.assigned_at, a.project.assigned_design_id)
                for a in DesignAssignment.objects.select_related('project')}
        return (rows, DueDateCommitment.objects.count(), StatusTransition.objects.count(),
                ActivityLog.objects.count())

    # -- happy path -------------------------------------------------------------------

    def test_dry_run_prints_every_site_and_writes_nothing(self):
        before = self._snapshot()
        out = self._run('--dry-run', '--expect', '3')
        self.assertEqual(self._snapshot(), before)
        for code in ('S1', 'S2', 'S3'):
            self.assertIn(f'{code}: status {DESIGN_IN_DESIGN} -> {DESIGN_AWAITING_ALLOCATION}', out)
        self.assertNotIn('MB0003:', out)
        self.assertIn('DRY RUN - 3 site(s) checked, nothing written.', out)

    def test_apply_restores_the_before_state_and_leaves_mb0003_alone(self):
        self._run('--apply', '--expect', '3', '--actor', 'head31')

        for code in ('S1', 'S2', 'S3'):
            a = DesignAssignment.objects.select_related('project').get(pk=self.a[code].pk)
            self.assertEqual(a.status, DESIGN_AWAITING_ALLOCATION, code)
            self.assertIsNone(a.assigned_to_id, code)
            self.assertIsNone(a.assigned_by_id, code)
            self.assertIsNone(a.assigned_at, code)
            self.assertIsNone(a.project.assigned_design_id, code)
            self.assertFalse(a.due_date_commitments.exists(), code)
            st = StatusTransition.objects.get(
                subject_type=SUBJECT_DESIGN_ASSIGNMENT, subject_id=a.pk,
                reason_code=cmd.REASON_CODE)
            self.assertEqual((st.from_status, st.to_status),
                             (DESIGN_IN_DESIGN, DESIGN_AWAITING_ALLOCATION))
            self.assertEqual(st.remark, 'Revert erroneous bulk allocation 31-Oct')
            self.assertEqual(st.actor_id, self.head.pk)
        self.assertEqual(ActivityLog.objects.filter(action_code=cmd.ACTION_CODE).count(), 3)

        mb3 = DesignAssignment.objects.select_related('project').get(pk=self.a['MB0003'].pk)
        self.assertEqual((mb3.status, mb3.assigned_to_id, mb3.project.assigned_design_id),
                         (DESIGN_IN_DESIGN, self.anil.pk, self.anil.pk))
        self.assertTrue(mb3.due_date_commitments.filter(is_current=True).exists())

    # -- abort on drift ---------------------------------------------------------------

    def test_a_site_reallocated_after_the_event_aborts_the_whole_run(self):
        """The Head fixes S2 by hand at 13:40. The command must not undo that, and must
        not revert S1 or S3 either: one drifted site stops everything."""
        with mock.patch('django.utils.timezone.now', return_value=AT_1340):
            _allocate_one(self.a['S2'], self.sud, self.head, allocated_on=ALLOCATED_ON)
        before = self._snapshot()

        with self.assertRaises(CommandError) as ctx:
            self._run('--apply', '--expect', '3', '--actor', 'head31')

        message = str(ctx.exception)
        self.assertIn('Drift found - nothing was written', message)
        # in_design -> in_design writes no transition, so the drift shows in the designer,
        # the second commitment and the ledger's later allocation row instead.
        self.assertIn('S2: designer is', message)
        self.assertIn('S2: 2 due-date commitment(s)', message)
        self.assertIn('S2: allocated again after the event', message)
        self.assertNotIn('S1:', message)
        self.assertEqual(self._snapshot(), before)

    def test_an_extension_request_after_the_event_is_drift_even_on_a_dry_run(self):
        DueDateCommitment.objects.filter(assignment=self.a['S1']).update(is_current=False)
        DueDateCommitment.objects.create(
            assignment=self.a['S1'], proposed_date=date(2026, 11, 9),
            proposed_by=self.anil, is_current=True, change_reason='need more time')

        with self.assertRaises(CommandError) as ctx:
            self._run('--dry-run', '--expect', '3')
        self.assertIn('S1: 2 due-date commitment(s)', str(ctx.exception))

    def test_a_run_after_a_successful_apply_aborts_rather_than_writing_twice(self):
        self._run('--apply', '--expect', '3', '--actor', 'head31')
        before = self._snapshot()
        with self.assertRaises(CommandError):
            self._run('--apply', '--expect', '3', '--actor', 'head31')
        self.assertEqual(self._snapshot(), before)

    # -- refusals ---------------------------------------------------------------------

    def test_expect_must_equal_the_site_count(self):
        before = self._snapshot()
        with self.assertRaises(CommandError) as ctx:
            self._run('--apply', '--expect', '56', '--actor', 'head31')
        self.assertIn('--expect 56 does not match the 3 sites', str(ctx.exception))
        self.assertEqual(self._snapshot(), before)

    def test_mb0003_is_refused_even_when_listed(self):
        listed = dict(self.commitments, MB0003=DueDateCommitment.objects.get(
            assignment=self.a['MB0003'], is_current=True).pk)
        before = self._snapshot()
        with mock.patch.object(cmd, 'SITES', listed):
            with self.assertRaises(CommandError) as ctx:
                self._run('--apply', '--expect', '4', '--actor', 'head31')
        self.assertIn('MB0003 is protected', str(ctx.exception))
        self.assertEqual(self._snapshot(), before)

    def test_apply_needs_a_design_head_actor(self):
        with self.assertRaises(CommandError) as ctx:
            self._run('--apply', '--expect', '3', '--actor', 'anil31')
        self.assertIn('not an active Design Head', str(ctx.exception))

    def test_a_mode_is_required(self):
        with self.assertRaises(CommandError):
            self._run('--expect', '3')
