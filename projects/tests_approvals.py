"""Approvals S1 — the shared approval primitive: models, CHECKs, chokepoint, predicates.

What this file pins, and why each matters:

  * THE STEP ROW IS THE RECORD. Every CHECK on ApprovalStep and ApprovalRequest is
    proven to refuse a bad row AT THE DATABASE (IntegrityError from a raw update), not
    at a view or a form — the chokepoint is not the only thing that can write a row.
  * PARALLEL MEANS PARALLEL; SEQUENCE MEANS SEQUENCE. Material steps activate together;
    a contractor bill's PM waits for the Site Engineer, and is timed from then.
  * FAIL-FAST. The first non-approval closes the round and supersedes whatever is still
    pending, so nobody is asked about something SCM is about to change.
  * ROUNDS ARE NEVER REWRITTEN. A resubmit leaves every earlier row byte-identical.
  * NOBODY DECIDES TWICE, AND NOBODY APPROVES THEIR OWN REQUEST. One helper, applied at
    create, decide, resubmit and reassign.
  * A PROXY SAYS SO. SCM may record a WhatsApp answer, with evidence, and only while
    the step is still undecided.
  * THE LOCK IS TAKEN ON THE REQUEST, BEFORE ANY STEP IS READ (Layer 6 case E).

Run with:
    python manage.py test projects.tests_approvals --settings=solarpms.test_settings
The Postgres-only tests (the concurrency race, and CHECK 5's NULL behaviour against the
constraint the migration actually created) run under the real settings:
    python manage.py test projects.tests_approvals
"""
import threading
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock, skipUnless

from django.contrib import admin as django_admin
from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .approvals import (
    ApprovalRefused, ProxyDecision, apply_approval_decision, create_approval_request,
    reassign_approval_step, resubmit_approval_request, withdraw_approval_request,
)
from .models import (
    AppendOnlyViolation, ApprovalAttachment, ApprovalRequest, ApprovalStep,
    BOQItemMaster, MaterialApprovalDetail, Program, Project, StatusTransition, Vendor,
    VendorOrder,
    APPROVAL_KIND_CONTRACTOR_BILL, APPROVAL_KIND_MATERIAL_PRE_DISPATCH,
    APPROVAL_KIND_MATERIAL_PRE_ORDER,
    APPROVAL_OPEN, APPROVAL_CHANGES_REQUESTED, APPROVAL_APPROVED, APPROVAL_REJECTED,
    APPROVAL_WITHDRAWN,
    APPROVAL_PARTY_DESIGN, APPROVAL_PARTY_PM, APPROVAL_PARTY_SITE_ENGINEER,
    APPROVAL_STEP_PENDING, APPROVAL_STEP_APPROVED, APPROVAL_STEP_CHANGES_REQUESTED,
    APPROVAL_STEP_REJECTED, APPROVAL_STEP_SUPERSEDED,
    APPROVAL_PROXY_WHATSAPP,
    REASON_CREATED, REASON_RESUBMITTED, SUBJECT_APPROVAL_REQUEST,
)
from .permissions import (
    profile_can_be_approval_assignee, user_can_decide_approval_step,
    user_can_raise_approval_request, user_can_reassign_approval_step,
    user_can_record_proxy_decision, user_can_withdraw_approval_request,
)
from .utils import _subject_type_registry

ON_POSTGRES = connection.vendor == 'postgresql'


def _profile(username, role, **flags):
    """A post_save signal creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title())
    profile = user.profile
    profile.role = role
    profile.is_active = True
    for field, value in flags.items():
        setattr(profile, field, value)
    profile.save()
    return profile


def _people(target):
    """The cast every test class shares, set as attributes on `target`."""
    target.scm    = _profile('ap_scm', 'SCM')
    target.scm_b  = _profile('ap_scm_b', 'SCM')
    target.pm     = _profile('ap_pm', 'PM')
    target.pm_b   = _profile('ap_pm_b', 'PM')
    target.head   = _profile('ap_head', 'Design', is_design_head=True)
    target.deputy = _profile('ap_deputy', 'Design')
    target.head.design_head_deputy = target.deputy
    target.head.save(update_fields=['design_head_deputy'])
    target.designer = _profile('ap_designer', 'Design')    # no Head authority
    target.se     = _profile('ap_se', 'Site Engineer')
    target.se_b   = _profile('ap_se_b', 'Site Engineer')
    target.vendor = Vendor.objects.create(name='Approval Vendor', contact_person='R',
                                          phone='9000000001')


class ApprovalFixture(TestCase):

    @classmethod
    def setUpTestData(cls):
        _people(cls)

    # ── helpers ─────────────────────────────────────────────────────────────

    def raise_material(self, design=True, **overrides):
        kwargs = dict(
            kind=APPROVAL_KIND_MATERIAL_PRE_ORDER, raised_by=self.scm,
            title='Module make', description='Propose Waaree 545 Wp for the tender.',
            pm_assignee=self.pm,
            design_signoff_required=design,
            design_assignee=self.head if design else None,
            material={'proposed_make': 'Waaree', 'specification': '545 Wp mono PERC'},
            attachments=[{'file_name': 'datasheet.pdf', 'bucket': 'approvals',
                          'path': 'r1/datasheet.pdf', 'label': 'Datasheet'}],
        )
        kwargs.update(overrides)
        return create_approval_request(**kwargs)

    def raise_bill(self, **overrides):
        kwargs = dict(
            kind=APPROVAL_KIND_CONTRACTOR_BILL, raised_by=self.scm,
            title='Civil works bill', description='Foundation work, block A.',
            pm_assignee=self.pm, site_engineer_assignee=self.se, vendor=self.vendor,
        )
        kwargs.update(overrides)
        return create_approval_request(**kwargs)

    def step(self, approval, party, round_no=None):
        approval.refresh_from_db()
        return ApprovalStep.objects.exclude(verdict=APPROVAL_STEP_SUPERSEDED).get(
            request=approval, party=party, round=round_no or approval.current_round)

    def ledger(self, approval):
        return list(StatusTransition.objects
                    .filter(subject_type=SUBJECT_APPROVAL_REQUEST, subject_id=approval.pk)
                    .order_by('occurred_at', 'pk'))

    def snapshot(self, approval, round_no):
        """Every column of every row of one round — steps and attachments."""
        return (list(ApprovalStep.objects.filter(request=approval, round=round_no)
                     .order_by('pk').values()),
                list(ApprovalAttachment.objects.filter(request=approval, round=round_no)
                     .order_by('pk').values()))


# ===========================================================================
# Layer 6 — the five simulated cases
# ===========================================================================

class CaseAParallelApprovalTests(ApprovalFixture):

    def test_a_both_steps_activate_together_and_both_approvals_close_the_request(self):
        approval = self.raise_material()
        pm_step, design_step = (self.step(approval, APPROVAL_PARTY_PM),
                                self.step(approval, APPROVAL_PARTY_DESIGN))
        self.assertEqual((pm_step.sequence, design_step.sequence), (1, 1))
        self.assertIsNotNone(pm_step.activated_at)
        self.assertEqual(pm_step.activated_at, design_step.activated_at)

        apply_approval_decision(pm_step, APPROVAL_STEP_APPROVED, self.pm)
        approval.refresh_from_db()
        self.assertEqual(approval.status, APPROVAL_OPEN)
        self.assertIsNone(approval.closed_at)

        apply_approval_decision(design_step, APPROVAL_STEP_APPROVED, self.head)
        approval.refresh_from_db()
        self.assertEqual(approval.status, APPROVAL_APPROVED)
        self.assertIsNotNone(approval.closed_at)

        pm_step.refresh_from_db()
        design_step.refresh_from_db()
        self.assertEqual(approval.closed_at, design_step.decided_at)
        # Turnaround = decided_at − activated_at, per step.
        self.assertGreaterEqual(pm_step.decided_at - pm_step.activated_at, timedelta(0))
        self.assertGreaterEqual(design_step.decided_at - design_step.activated_at,
                                timedelta(0))

        rows = self.ledger(approval)
        self.assertEqual([(r.from_status, r.to_status) for r in rows],
                         [('', APPROVAL_OPEN), (APPROVAL_OPEN, APPROVAL_APPROVED)])
        self.assertEqual(rows[0].reason_code, REASON_CREATED)
        self.assertEqual(rows[1].actor, self.head)

    def test_approve_needs_no_note(self):
        approval = self.raise_material(design=False)
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_APPROVED, self.pm, note='')
        approval.refresh_from_db()
        self.assertEqual(approval.status, APPROVAL_APPROVED)


class CaseBChangesThenApprovalTests(ApprovalFixture):

    def test_b_changes_supersede_the_sibling_and_round_two_leaves_round_one_untouched(self):
        approval = self.raise_material()
        design_step = self.step(approval, APPROVAL_PARTY_DESIGN)

        apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.pm,
                                note='Quote the 550 Wp variant too.')
        approval.refresh_from_db()
        self.assertEqual(approval.status, APPROVAL_CHANGES_REQUESTED)
        self.assertIsNone(approval.closed_at)

        design_step.refresh_from_db()
        self.assertEqual(design_step.verdict, APPROVAL_STEP_SUPERSEDED)
        self.assertIsNotNone(design_step.superseded_at)
        self.assertEqual(design_step.superseded_by, self.pm)
        self.assertIsNone(design_step.decided_by)

        round_one = self.snapshot(approval, 1)
        resubmit_approval_request(
            approval, self.scm, note='Added the 550 Wp quote.',
            attachments=[{'file_name': 'quote550.pdf', 'bucket': 'approvals',
                          'path': 'r2/quote550.pdf'}])
        approval.refresh_from_db()
        self.assertEqual((approval.status, approval.current_round), (APPROVAL_OPEN, 2))

        round_two = list(ApprovalStep.objects.filter(request=approval, round=2))
        self.assertEqual(sorted(s.party for s in round_two),
                         [APPROVAL_PARTY_DESIGN, APPROVAL_PARTY_PM])
        self.assertTrue(all(s.verdict == APPROVAL_STEP_PENDING and s.activated_at
                            for s in round_two))

        # The SAME party in a later round is not a same-person conflict: the PM who asked
        # for changes decides round 2.
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_APPROVED, self.pm)
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_DESIGN),
                                APPROVAL_STEP_APPROVED, self.head)
        approval.refresh_from_db()
        self.assertEqual(approval.status, APPROVAL_APPROVED)

        # Byte-identical after the resubmit AND after round 2 was decided.
        self.assertEqual(self.snapshot(approval, 1), round_one)
        self.assertEqual(ApprovalAttachment.objects.filter(request=approval).count(), 2)

        rows = self.ledger(approval)
        self.assertEqual([r.to_status for r in rows],
                         [APPROVAL_OPEN, APPROVAL_CHANGES_REQUESTED, APPROVAL_OPEN,
                          APPROVAL_APPROVED])
        self.assertEqual(rows[1].remark, 'Quote the 550 Wp variant too.')
        self.assertEqual(rows[2].reason_code, REASON_RESUBMITTED)
        self.assertEqual(rows[2].from_status, APPROVAL_CHANGES_REQUESTED)

    def test_a_non_approval_needs_a_note(self):
        approval = self.raise_material()
        for verdict in (APPROVAL_STEP_CHANGES_REQUESTED, APPROVAL_STEP_REJECTED):
            with self.subTest(verdict=verdict):
                with self.assertRaises(ApprovalRefused):
                    apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM),
                                            verdict, self.pm, note='   ')
        self.assertEqual(self.step(approval, APPROVAL_PARTY_PM).verdict,
                         APPROVAL_STEP_PENDING)

    def test_rejection_is_terminal_and_supersedes_the_sibling(self):
        approval = self.raise_material()
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_DESIGN),
                                APPROVAL_STEP_REJECTED, self.head, note='Wrong tech.')
        approval.refresh_from_db()
        self.assertEqual(approval.status, APPROVAL_REJECTED)
        self.assertIsNotNone(approval.closed_at)
        self.assertEqual(
            ApprovalStep.objects.get(request=approval, party=APPROVAL_PARTY_PM).verdict,
            APPROVAL_STEP_SUPERSEDED)


class CaseCProxyTests(ApprovalFixture):

    def proxy(self, decided_by=None, channel=APPROVAL_PROXY_WHATSAPP,
              evidence='PM said "go ahead" on WhatsApp, 26 Sep 11:40.'):
        return ProxyDecision(decided_by or self.pm, channel, evidence)

    def test_c_scm_records_the_pms_whatsapp_approval(self):
        """The raiser MAY be recorded_by on a proxy step (ruling 2)."""
        approval = self.raise_material(design=False)
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_APPROVED, self.scm, proxy=self.proxy())
        row = ApprovalStep.objects.get(request=approval, party=APPROVAL_PARTY_PM)
        self.assertEqual((row.decided_by, row.recorded_by), (self.pm, self.scm))
        self.assertTrue(row.is_proxy)
        self.assertEqual(row.proxy_channel, APPROVAL_PROXY_WHATSAPP)
        self.assertIn('WhatsApp', row.proxy_evidence)
        approval.refresh_from_db()
        self.assertEqual(approval.status, APPROVAL_APPROVED)
        # The ledger names the decider; the step row names who typed it.
        self.assertEqual(self.ledger(approval)[-1].actor, self.pm)

    def test_a_proxy_after_a_real_decision_is_refused(self):
        approval = self.raise_material()
        pm_step = self.step(approval, APPROVAL_PARTY_PM)
        apply_approval_decision(pm_step, APPROVAL_STEP_APPROVED, self.pm)
        before = list(ApprovalStep.objects.filter(pk=pm_step.pk).values())
        with self.assertRaises(ApprovalRefused):
            apply_approval_decision(pm_step, APPROVAL_STEP_REJECTED, self.scm,
                                    note='per call', proxy=self.proxy())
        self.assertEqual(list(ApprovalStep.objects.filter(pk=pm_step.pk).values()), before)

    def test_only_scm_may_record_on_someone_elses_behalf(self):
        approval = self.raise_material(design=False)
        with self.assertRaises(ApprovalRefused):
            apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM),
                                    APPROVAL_STEP_APPROVED, self.pm_b, proxy=self.proxy())

    def test_a_proxy_needs_channel_and_evidence(self):
        approval = self.raise_material(design=False)
        step = self.step(approval, APPROVAL_PARTY_PM)
        for bad in (self.proxy(channel='telegram'), self.proxy(evidence='  ')):
            with self.subTest(bad=bad):
                with self.assertRaises(ApprovalRefused):
                    apply_approval_decision(step, APPROVAL_STEP_APPROVED, self.scm, proxy=bad)

    def test_a_proxy_cannot_name_someone_without_the_authority(self):
        approval = self.raise_material(design=False)
        with self.assertRaises(ApprovalRefused):
            apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM),
                                    APPROVAL_STEP_APPROVED, self.scm,
                                    proxy=self.proxy(decided_by=self.pm_b))


class CaseDContractorBillSequenceTests(ApprovalFixture):

    def test_d_the_pm_is_activated_by_the_site_engineers_approval_and_timed_from_it(self):
        approval = self.raise_bill()
        se_step, pm_step = (self.step(approval, APPROVAL_PARTY_SITE_ENGINEER),
                            self.step(approval, APPROVAL_PARTY_PM))
        self.assertEqual((se_step.sequence, pm_step.sequence), (1, 2))
        self.assertIsNotNone(se_step.activated_at)
        self.assertIsNone(pm_step.activated_at)

        # Not the PM's turn yet.
        with self.assertRaises(ApprovalRefused):
            apply_approval_decision(pm_step, APPROVAL_STEP_APPROVED, self.pm)

        apply_approval_decision(se_step, APPROVAL_STEP_APPROVED, self.se)
        se_step.refresh_from_db()
        pm_step.refresh_from_db()
        approval.refresh_from_db()
        self.assertEqual(approval.status, APPROVAL_OPEN)
        # The PM's clock starts at t1, the SE's decision — not at t0.
        self.assertEqual(pm_step.activated_at, se_step.decided_at)
        self.assertGreater(pm_step.activated_at, se_step.activated_at - timedelta(seconds=1))

        apply_approval_decision(pm_step, APPROVAL_STEP_APPROVED, self.pm)
        approval.refresh_from_db()
        self.assertEqual(approval.status, APPROVAL_APPROVED)

    def test_a_site_engineers_changes_close_the_round_and_the_pm_is_never_asked(self):
        approval = self.raise_bill()
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_SITE_ENGINEER),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.se,
                                note='Block B is not finished.')
        pm_row = ApprovalStep.objects.get(request=approval, party=APPROVAL_PARTY_PM)
        self.assertEqual(pm_row.verdict, APPROVAL_STEP_SUPERSEDED)
        self.assertIsNone(pm_row.activated_at)

    def test_a_bill_takes_no_design_signoff_and_needs_a_vendor(self):
        with self.assertRaises(ApprovalRefused):
            self.raise_bill(design_signoff_required=True, design_assignee=self.head)
        with self.assertRaises(ApprovalRefused):
            self.raise_bill(vendor=None)
        self.assertFalse(ApprovalRequest.objects.exists())


class CaseELockOrderingTests(ApprovalFixture):
    """Case E, the part that runs everywhere: the request row is read — FOR UPDATE on
    Postgres — before any step row. The race itself is ConcurrentDecisionTests below."""

    def test_e_the_request_is_locked_before_any_step_is_read(self):
        approval = self.raise_material()
        pm_step = self.step(approval, APPROVAL_PARTY_PM)
        with CaptureQueriesContext(connection) as ctx:
            apply_approval_decision(pm_step, APPROVAL_STEP_APPROVED, self.pm)
        sqls = [q['sql'] for q in ctx.captured_queries]
        first_request = next(i for i, s in enumerate(sqls)
                             if 'FROM "projects_approvalrequest"' in s)
        first_step = next(i for i, s in enumerate(sqls) if '"projects_approvalstep"' in s)
        self.assertLess(first_request, first_step,
                        'a step was read before the request row was locked')
        if ON_POSTGRES:
            self.assertIn('FOR UPDATE', sqls[first_request])


@skipUnless(ON_POSTGRES, 'select_for_update is a no-op on SQLite; run under real settings')
class ConcurrentDecisionTests(TransactionTestCase):
    """Case E on real Postgres: two parallel approvers, one request.

    If the chokepoint locked only the step rows, the second decider would not wait, would
    read the first's step as pending (uncommitted), and would leave the request open
    although both had approved. The request lock makes it wait and read the commit.
    """

    def setUp(self):
        _people(self)
        self.approval = create_approval_request(
            kind=APPROVAL_KIND_MATERIAL_PRE_ORDER, raised_by=self.scm, title='Race',
            description='Two approvers at once.', pm_assignee=self.pm,
            design_signoff_required=True, design_assignee=self.head, material={})
        self.pm_step = ApprovalStep.objects.get(request=self.approval,
                                                party=APPROVAL_PARTY_PM)
        self.design_step = ApprovalStep.objects.get(request=self.approval,
                                                    party=APPROVAL_PARTY_DESIGN)

    def _approved_rows(self):
        return StatusTransition.objects.filter(
            subject_type=SUBJECT_APPROVAL_REQUEST, subject_id=self.approval.pk,
            to_status=APPROVAL_APPROVED).count()

    def test_e_the_second_decider_waits_for_the_first_and_reads_its_commit(self):
        first_holds_lock, release_first = threading.Event(), threading.Event()
        results = {}

        def first():
            try:
                with transaction.atomic():
                    ApprovalRequest.objects.select_for_update().get(pk=self.approval.pk)
                    first_holds_lock.set()
                    release_first.wait(10)
                    apply_approval_decision(self.pm_step, APPROVAL_STEP_APPROVED, self.pm)
                results['first'] = 'ok'
            except Exception as exc:          # surfaced by the assertion below
                results['first'] = exc
            finally:
                connection.close()

        def second():
            try:
                first_holds_lock.wait(10)
                apply_approval_decision(self.design_step, APPROVAL_STEP_APPROVED,
                                        self.head)
                results['second'] = 'ok'
            except Exception as exc:
                results['second'] = exc
            finally:
                connection.close()

        a, b = threading.Thread(target=first), threading.Thread(target=second)
        a.start()
        b.start()
        self.assertTrue(first_holds_lock.wait(10))
        b.join(timeout=1.5)
        self.assertTrue(b.is_alive(), 'the second decider did not wait for the request lock')
        release_first.set()
        a.join(10)
        b.join(10)

        self.assertEqual(results, {'first': 'ok', 'second': 'ok'})
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, APPROVAL_APPROVED)
        self.assertEqual(self._approved_rows(), 1)

    def test_e_two_approvals_released_at_the_same_instant_approve_the_request(self):
        barrier, results = threading.Barrier(2), {}

        def act(name, step, profile):
            try:
                barrier.wait()
                apply_approval_decision(step, APPROVAL_STEP_APPROVED, profile)
                results[name] = 'ok'
            except Exception as exc:
                results[name] = exc
            finally:
                connection.close()

        threads = [threading.Thread(target=act, args=('pm', self.pm_step, self.pm)),
                   threading.Thread(target=act, args=('design', self.design_step,
                                                      self.head))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

        self.assertEqual(results, {'pm': 'ok', 'design': 'ok'})
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, APPROVAL_APPROVED)
        self.assertEqual(self._approved_rows(), 1)


# ===========================================================================
# The CHECKs, at the database
# ===========================================================================

class ConstraintTests(ApprovalFixture):
    """Each bad row is written with a raw queryset update or create — the chokepoint is
    bypassed on purpose, so what refuses is the database."""

    def setUp(self):
        self.approval = self.raise_material()
        self.pending = self.step(self.approval, APPROVAL_PARTY_PM)
        self.now = timezone.now()

    def assertViolates(self, name, write):
        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                write()
        # SQLite names a CHECK in its message but not a partial unique index; Postgres
        # names both.
        if ON_POSTGRES or not name.startswith('uniq_'):
            self.assertIn(name, str(caught.exception))

    def update_step(self, **fields):
        return lambda: ApprovalStep.objects.filter(pk=self.pending.pk).update(**fields)

    def decided(self, **extra):
        fields = dict(decided_by=self.pm, decided_at=self.now, recorded_by=self.pm)
        fields.update(extra)
        return fields

    # ── ApprovalStep ──

    def test_check_1_a_non_approval_carries_a_note(self):
        for verdict in (APPROVAL_STEP_CHANGES_REQUESTED, APPROVAL_STEP_REJECTED):
            with self.subTest(verdict=verdict):
                self.assertViolates('approval_step_note_required',
                                    self.update_step(verdict=verdict, note='',
                                                     **self.decided()))

    def test_check_2_decided_rows_name_the_decider_and_undecided_rows_do_not(self):
        self.assertViolates('approval_step_decision_fields',
                            self.update_step(verdict=APPROVAL_STEP_APPROVED))
        self.assertViolates('approval_step_decision_fields',
                            self.update_step(**self.decided()))   # still pending

    def test_check_3_superseded_at_iff_superseded(self):
        self.assertViolates('approval_step_superseded_at_iff_superseded',
                            self.update_step(superseded_at=self.now))
        self.assertViolates('approval_step_superseded_at_iff_superseded',
                            self.update_step(verdict=APPROVAL_STEP_SUPERSEDED,
                                             superseded_by=self.scm))

    def test_check_4_a_proxy_carries_channel_and_evidence(self):
        self.assertViolates('approval_step_proxy_evidence',
                            self.update_step(is_proxy=True, proxy_channel='',
                                             proxy_evidence='said yes'))
        self.assertViolates('approval_step_proxy_evidence',
                            self.update_step(is_proxy=True, proxy_channel='whatsapp',
                                             proxy_evidence=''))

    def test_check_5_not_a_proxy_means_the_decider_typed_it(self):
        self.assertViolates('approval_step_self_recorded_unless_proxy',
                            self.update_step(verdict=APPROVAL_STEP_APPROVED,
                                             **self.decided(recorded_by=self.scm)))

    def test_check_5_passes_on_a_pending_row_where_both_columns_are_null(self):
        """NULL = NULL is NULL, and a CHECK passes on NULL. The pending row exists and
        re-saving it through the database is accepted."""
        row = ApprovalStep.objects.get(pk=self.pending.pk)
        self.assertFalse(row.is_proxy)
        self.assertIsNone(row.recorded_by_id)
        self.assertIsNone(row.decided_by_id)
        with transaction.atomic():
            ApprovalStep.objects.filter(pk=row.pk).update(note='touched')

    @skipUnless(ON_POSTGRES, 'the migrated constraint and NULL semantics of real Postgres')
    def test_check_5_null_behaviour_on_postgres(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'approval_step_self_recorded_unless_proxy'")
            definition = cursor.fetchone()[0]
            cursor.execute('SELECT (NULL::integer = NULL::integer) IS NULL')
            null_compare_is_null = cursor.fetchone()[0]
        self.assertIn('recorded_by_id = decided_by_id', definition)
        self.assertTrue(null_compare_is_null)
        # And a fresh pending row inserts with both columns NULL, is_proxy false.
        with transaction.atomic():
            ApprovalStep.objects.create(
                request=self.approval, round=9, party=APPROVAL_PARTY_PM, sequence=1,
                assignee=self.pm, is_proxy=False)

    def test_check_6_one_live_step_per_party_per_round(self):
        self.assertViolates('uniq_approval_step_live_party', lambda: ApprovalStep.objects.create(
            request=self.approval, round=1, party=APPROVAL_PARTY_PM, sequence=1,
            assignee=self.pm_b))
        # A superseded row does not count: the replacement a reassignment creates is legal.
        ApprovalStep.objects.create(
            request=self.approval, round=1, party=APPROVAL_PARTY_PM, sequence=1,
            assignee=self.pm_b, verdict=APPROVAL_STEP_SUPERSEDED,
            superseded_at=self.now, superseded_by=self.scm)

    def test_check_7_superseded_by_iff_superseded(self):
        self.assertViolates('approval_step_superseded_by_iff_superseded',
                            self.update_step(verdict=APPROVAL_STEP_SUPERSEDED,
                                             superseded_at=self.now))
        self.assertViolates('approval_step_superseded_by_iff_superseded',
                            self.update_step(superseded_by=self.scm))

    # ── ApprovalRequest ──

    def update_request(self, **fields):
        return lambda: ApprovalRequest.objects.filter(pk=self.approval.pk).update(**fields)

    def test_request_check_withdrawal_note(self):
        self.assertViolates('approval_request_withdrawal_note_required',
                            self.update_request(status=APPROVAL_WITHDRAWN,
                                                closed_at=self.now, withdrawal_note=''))

    def test_request_check_no_design_signoff_on_a_bill(self):
        self.assertViolates('approval_request_no_design_signoff_on_bill',
                            self.update_request(kind=APPROVAL_KIND_CONTRACTOR_BILL,
                                                vendor=self.vendor,
                                                design_signoff_required=True))

    def test_request_check_vendor_required_for_kind(self):
        for kind in (APPROVAL_KIND_MATERIAL_PRE_DISPATCH, APPROVAL_KIND_CONTRACTOR_BILL):
            with self.subTest(kind=kind):
                self.assertViolates('approval_request_vendor_required_for_kind',
                                    self.update_request(kind=kind, vendor=None,
                                                        design_signoff_required=False))

    def test_request_check_closed_at_iff_terminal(self):
        self.assertViolates('approval_request_closed_at_iff_terminal',
                            self.update_request(status=APPROVAL_APPROVED))
        self.assertViolates('approval_request_closed_at_iff_terminal',
                            self.update_request(closed_at=self.now))


# ===========================================================================
# Same-person rules — the one helper, at every entry point
# ===========================================================================

class SamePersonTests(ApprovalFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # An SCM user who also holds the Head flag: the only way the raiser can hold
        # authority over a step, so the only way to test the rule at decision time.
        cls.scm_head = _profile('ap_scm_head', 'SCM', is_design_head=True)
        # A PM who is also a Head's deputy — Head authority without being the Head.
        cls.head_2 = _profile('ap_head_2', 'Design', is_design_head=True)
        cls.head_2.design_head_deputy = cls.pm_b
        cls.head_2.save(update_fields=['design_head_deputy'])
        # A PM who is also a Head: may be named for either party, never both.
        cls.pm_head = _profile('ap_pm_head', 'PM', is_design_head=True)

    def test_the_raiser_cannot_be_named(self):
        with self.assertRaises(ApprovalRefused):
            self.raise_material(raised_by=self.scm_head, design_assignee=self.scm_head)
        self.assertFalse(ApprovalRequest.objects.exists())

    def test_the_raiser_cannot_decide(self):
        approval = self.raise_material(raised_by=self.scm_head)
        with self.assertRaisesMessage(ApprovalRefused, 'raised this request'):
            apply_approval_decision(self.step(approval, APPROVAL_PARTY_DESIGN),
                                    APPROVAL_STEP_APPROVED, self.scm_head)
        self.assertEqual(self.step(approval, APPROVAL_PARTY_DESIGN).verdict,
                         APPROVAL_STEP_PENDING)

    def test_one_profile_cannot_be_named_for_two_parties(self):
        with self.assertRaises(ApprovalRefused):
            self.raise_material(pm_assignee=self.pm_head, design_assignee=self.pm_head)

    def test_the_pm_assignee_cannot_decide_the_design_step_by_deputy_authority(self):
        approval = self.raise_material(pm_assignee=self.pm_b, design_assignee=self.head_2)
        with self.assertRaisesMessage(ApprovalRefused, 'already acts for the PM'):
            apply_approval_decision(self.step(approval, APPROVAL_PARTY_DESIGN),
                                    APPROVAL_STEP_APPROVED, self.pm_b)

    def test_a_decider_for_one_party_cannot_be_named_for_another_in_a_later_round(self):
        approval = self.raise_material(pm_assignee=self.pm, design_assignee=self.head_2)
        # pm_b decides the DESIGN step by deputy authority, and asks for changes.
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_DESIGN),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.pm_b, note='Redo.')
        with self.assertRaisesMessage(ApprovalRefused, 'already acts for the Design Head'):
            resubmit_approval_request(approval, self.scm, note='Redone.',
                                      assignees={APPROVAL_PARTY_PM: self.pm_b})
        approval.refresh_from_db()
        self.assertEqual((approval.status, approval.current_round),
                         (APPROVAL_CHANGES_REQUESTED, 1))

    def test_a_reassignment_cannot_hand_a_party_to_another_partys_assignee(self):
        approval = self.raise_material(pm_assignee=self.pm, design_assignee=self.pm_head)
        with self.assertRaises(ApprovalRefused):
            reassign_approval_step(self.step(approval, APPROVAL_PARTY_PM), self.pm_head,
                                   self.scm, note='PM on leave.')

    def test_the_deputy_decides_the_heads_step_and_is_recorded_as_decider(self):
        approval = self.raise_material()
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_DESIGN),
                                APPROVAL_STEP_APPROVED, self.deputy)
        row = ApprovalStep.objects.get(request=approval, party=APPROVAL_PARTY_DESIGN)
        self.assertEqual((row.assignee, row.decided_by), (self.head, self.deputy))


# ===========================================================================
# Resubmit, withdraw, reassign
# ===========================================================================

class ResubmitTests(ApprovalFixture):

    def test_terminal_and_open_requests_refuse_resubmit(self):
        approved = self.raise_material(design=False)
        apply_approval_decision(self.step(approved, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_APPROVED, self.pm)
        rejected = self.raise_material(design=False)
        apply_approval_decision(self.step(rejected, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_REJECTED, self.pm, note='No.')
        withdrawn = self.raise_material(design=False)
        withdraw_approval_request(withdrawn, self.scm, note='Vendor dropped out.')
        still_open = self.raise_material(design=False)

        for approval in (approved, rejected, withdrawn, still_open):
            with self.subTest(status=approval.status):
                approval.refresh_from_db()
                before = (approval.status, approval.current_round,
                          ApprovalStep.objects.filter(request=approval).count())
                with self.assertRaises(ApprovalRefused):
                    resubmit_approval_request(approval, self.scm, note='again')
                approval.refresh_from_db()
                self.assertEqual((approval.status, approval.current_round,
                                  ApprovalStep.objects.filter(request=approval).count()),
                                 before)

    def test_resubmit_needs_a_note_and_scm(self):
        approval = self.raise_material(design=False)
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.pm, note='Fix.')
        with self.assertRaises(ApprovalRefused):
            resubmit_approval_request(approval, self.scm, note=' ')
        with self.assertRaises(ApprovalRefused):
            resubmit_approval_request(approval, self.pm, note='Fixed.')

    def test_resubmit_can_name_a_replacement_for_a_departed_assignee(self):
        approval = self.raise_material(design=False)
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.pm, note='Fix.')
        resubmit_approval_request(approval, self.scm, note='Fixed.',
                                  assignees={APPROVAL_PARTY_PM: self.pm_b})
        self.assertEqual(self.step(approval, APPROVAL_PARTY_PM).assignee, self.pm_b)

    # ── the assignee override is a reassignment, and is held to that bar ──

    def changes_requested(self, design=True):
        approval = self.raise_material(design=design)
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.pm, note='Fix.')
        return approval

    def assert_nothing_written(self, approval, ledger_rows):
        approval.refresh_from_db()
        self.assertEqual((approval.status, approval.current_round),
                         (APPROVAL_CHANGES_REQUESTED, 1))
        self.assertFalse(ApprovalStep.objects.filter(request=approval, round=2).exists())
        self.assertEqual(len(self.ledger(approval)), ledger_rows)

    def test_an_override_without_a_note_is_refused(self):
        approval = self.changes_requested()
        rows = len(self.ledger(approval))
        for note in ('', '   ', None):
            with self.subTest(note=note):
                with self.assertRaises(ApprovalRefused):
                    resubmit_approval_request(approval, self.scm, note=note,
                                              assignees={APPROVAL_PARTY_PM: self.pm_b})
                self.assert_nothing_written(approval, rows)

    def test_an_override_writes_the_change_into_the_ledger_remark(self):
        approval = self.changes_requested()
        resubmit_approval_request(approval, self.scm, note='PM on leave; quote revised.',
                                  assignees={APPROVAL_PARTY_PM: self.pm_b})
        row = self.ledger(approval)[-1]
        self.assertEqual(row.reason_code, REASON_RESUBMITTED)
        self.assertEqual(
            row.remark,
            f'Approver changed — pm: {self.pm.user.get_full_name()} → '
            f'{self.pm_b.user.get_full_name()}. PM on leave; quote revised.')

    def test_an_override_of_two_parties_names_both_in_round_order(self):
        head_b = _profile('ap_head_b', 'Design', is_design_head=True)
        approval = self.changes_requested()
        resubmit_approval_request(approval, self.scm, note='New approvers.',
                                  assignees={APPROVAL_PARTY_DESIGN: head_b,
                                             APPROVAL_PARTY_PM: self.pm_b})
        self.assertEqual(
            self.ledger(approval)[-1].remark,
            f'Approver changed — design: {self.head.user.get_full_name()} → '
            f'{head_b.user.get_full_name()}. '
            f'Approver changed — pm: {self.pm.user.get_full_name()} → '
            f'{self.pm_b.user.get_full_name()}. New approvers.')

    def test_an_override_needs_the_reassign_permission(self):
        approval = self.changes_requested()
        rows = len(self.ledger(approval))
        with mock.patch('projects.approvals.user_can_reassign_approval_step',
                        return_value=False) as predicate:
            with self.assertRaisesMessage(ApprovalRefused, 'change the PM approver'):
                resubmit_approval_request(approval, self.scm, note='New PM.',
                                          assignees={APPROVAL_PARTY_PM: self.pm_b})
        # Asked of the step being handed over: round 2, pending, request open.
        (user, step), _ = predicate.call_args
        self.assertEqual(user, self.scm.user)
        self.assertEqual((step.round, step.party, step.verdict, step.assignee),
                         (2, APPROVAL_PARTY_PM, APPROVAL_STEP_PENDING, self.pm_b))
        self.assertEqual(step.request.status, APPROVAL_OPEN)
        self.assert_nothing_written(approval, rows)

    def test_a_resubmit_with_unchanged_assignees_works_exactly_as_before(self):
        """No override, or an override naming the same people: the remark is the note
        alone, and the reassign permission is never consulted."""
        for assignees in (None, {APPROVAL_PARTY_PM: self.pm,
                                 APPROVAL_PARTY_DESIGN: self.head}):
            with self.subTest(assignees=assignees):
                approval = self.changes_requested()
                with mock.patch('projects.approvals.user_can_reassign_approval_step',
                                return_value=False) as predicate:
                    resubmit_approval_request(approval, self.scm, note='Fixed.',
                                              assignees=assignees)
                predicate.assert_not_called()
                approval.refresh_from_db()
                self.assertEqual((approval.status, approval.current_round),
                                 (APPROVAL_OPEN, 2))
                self.assertEqual(self.step(approval, APPROVAL_PARTY_PM).assignee, self.pm)
                self.assertEqual(self.step(approval, APPROVAL_PARTY_DESIGN).assignee,
                                 self.head)
                self.assertEqual(self.ledger(approval)[-1].remark, 'Fixed.')


class WithdrawTests(ApprovalFixture):

    def test_withdraw_supersedes_pending_steps_and_ledgers_the_note(self):
        approval = self.raise_material()
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_APPROVED, self.pm)
        withdraw_approval_request(approval, self.scm_b, note='Tender cancelled.')
        approval.refresh_from_db()
        self.assertEqual(approval.status, APPROVAL_WITHDRAWN)
        self.assertEqual(approval.withdrawal_note, 'Tender cancelled.')
        self.assertIsNotNone(approval.closed_at)
        design = ApprovalStep.objects.get(request=approval, party=APPROVAL_PARTY_DESIGN)
        self.assertEqual((design.verdict, design.superseded_by),
                         (APPROVAL_STEP_SUPERSEDED, self.scm_b))
        # The approval already given stays recorded.
        self.assertEqual(ApprovalStep.objects.get(request=approval,
                                                  party=APPROVAL_PARTY_PM).verdict,
                         APPROVAL_STEP_APPROVED)
        row = self.ledger(approval)[-1]
        self.assertEqual((row.from_status, row.to_status, row.remark),
                         (APPROVAL_OPEN, APPROVAL_WITHDRAWN, 'Tender cancelled.'))

    def test_withdraw_from_changes_requested(self):
        approval = self.raise_material(design=False)
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.pm, note='Fix.')
        withdraw_approval_request(approval, self.scm, note='Not pursuing.')
        approval.refresh_from_db()
        self.assertEqual(approval.status, APPROVAL_WITHDRAWN)

    def test_withdraw_needs_a_note_scm_and_a_live_request(self):
        approval = self.raise_material(design=False)
        with self.assertRaises(ApprovalRefused):
            withdraw_approval_request(approval, self.scm, note='')
        with self.assertRaises(ApprovalRefused):
            withdraw_approval_request(approval, self.pm, note='mine now')
        withdraw_approval_request(approval, self.scm, note='Done.')
        with self.assertRaises(ApprovalRefused):
            withdraw_approval_request(approval, self.scm, note='Again.')


class ReassignTests(ApprovalFixture):

    def test_reassign_supersedes_with_the_reason_and_the_new_assignee_decides(self):
        approval = self.raise_material(design=False)
        old = self.step(approval, APPROVAL_PARTY_PM)
        new = reassign_approval_step(old, self.pm_b, self.scm, note='PM left the company.')
        old.refresh_from_db()
        self.assertEqual((old.verdict, old.note, old.superseded_by),
                         (APPROVAL_STEP_SUPERSEDED, 'PM left the company.', self.scm))
        self.assertEqual((new.round, new.party, new.sequence, new.assignee),
                         (1, APPROVAL_PARTY_PM, 1, self.pm_b))
        self.assertGreaterEqual(new.activated_at, old.activated_at)

        with self.assertRaises(ApprovalRefused):   # the old assignee's step is gone
            apply_approval_decision(old, APPROVAL_STEP_APPROVED, self.pm)
        with self.assertRaises(ApprovalRefused):   # and they hold no other
            apply_approval_decision(new, APPROVAL_STEP_APPROVED, self.pm)
        apply_approval_decision(new, APPROVAL_STEP_APPROVED, self.pm_b)
        approval.refresh_from_db()
        self.assertEqual(approval.status, APPROVAL_APPROVED)
        # A reassignment is not a status move.
        self.assertEqual([r.to_status for r in self.ledger(approval)],
                         [APPROVAL_OPEN, APPROVAL_APPROVED])

    def test_a_waiting_step_is_reassigned_still_waiting(self):
        approval = self.raise_bill()
        new = reassign_approval_step(self.step(approval, APPROVAL_PARTY_PM), self.pm_b,
                                     self.scm, note='PM moved to another region.')
        self.assertIsNone(new.activated_at)

    def test_reassign_refusals(self):
        approval = self.raise_bill()
        se_step = self.step(approval, APPROVAL_PARTY_SITE_ENGINEER)
        for new_assignee, actor, note in (
                (self.se_b, self.scm, ''),            # no reason
                (self.se_b, self.pm, 'x'),            # not SCM
                (self.se, self.scm, 'x'),             # same person
                (self.pm_b, self.scm, 'x'),           # not a Site Engineer
        ):
            with self.subTest(new=new_assignee.user.username, actor=actor.role, note=note):
                with self.assertRaises(ApprovalRefused):
                    reassign_approval_step(se_step, new_assignee, actor, note)
        self.assertEqual(ApprovalStep.objects.filter(request=approval).count(), 2)


# ===========================================================================
# Create — kind rules, material detail, scope, attachments, idempotency
# ===========================================================================

class CreateTests(ApprovalFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.pm_project = Project.objects.create(
            customer_name='Scope Site', status='Active', customer_phone='9876543210',
            site_address='1 Sun Road', city='Lucknow', state='Uttar Pradesh',
            project_type='Residential', dc_capacity_kw=Decimal('5.00'),
            assigned_pm=cls.pm)
        cls.program = Program.objects.create(
            name='Approval Tender', program_type='OPEX', client_name='Client',
            status='Active', short_tender_code='APT')
        cls.item = BOQItemMaster.objects.create(code='APPRT-001', description='Module',
                                                unit='Nos')
        cls.order = VendorOrder.objects.create(
            vendor=cls.vendor, project_type='Residential', total_amount=Decimal('1000'),
            created_by=cls.scm)

    def test_only_scm_may_raise(self):
        with self.assertRaises(ApprovalRefused):
            self.raise_material(raised_by=self.pm_b)

    def test_ineligible_assignees_are_refused(self):
        for overrides in ({'pm_assignee': self.se},
                          {'design_assignee': self.designer},
                          {'design_assignee': self.deputy},      # a deputy is not the Head
                          {'design_signoff_required': False, 'design_assignee': self.head},
                          {'design_assignee': None}):
            with self.subTest(overrides=list(overrides)):
                with self.assertRaises(ApprovalRefused):
                    self.raise_material(**overrides)
        self.assertFalse(ApprovalRequest.objects.exists())

    def test_material_detail_scope_and_attachments_are_written(self):
        approval = self.raise_material(
            programs=[self.program], projects=[self.pm_project],
            material={'proposed_make': ' Waaree ', 'specification': '545 Wp',
                      'boq_items': [self.item]})
        detail = MaterialApprovalDetail.objects.get(request=approval)
        self.assertEqual(detail.proposed_make, 'Waaree')
        self.assertEqual(list(detail.boq_items.all()), [self.item])
        self.assertEqual(list(approval.programs.all()), [self.program])
        self.assertEqual(list(approval.projects.all()), [self.pm_project])
        attachment = ApprovalAttachment.objects.get(request=approval)
        self.assertEqual((attachment.round, attachment.label, attachment.uploaded_by),
                         (1, 'Datasheet', self.scm))

    def test_a_deleted_project_cannot_be_named_in_scope(self):
        deleted = Project.objects.create(
            customer_name='Gone', status='Active', customer_phone='9876543210',
            site_address='x', city='Lucknow', state='Uttar Pradesh',
            project_type='Residential', dc_capacity_kw=Decimal('5.00'), is_deleted=True)
        with self.assertRaises(ApprovalRefused):
            self.raise_material(projects=[deleted])

    def test_pre_dispatch_needs_a_matching_vendor_order(self):
        base = dict(kind=APPROVAL_KIND_MATERIAL_PRE_DISPATCH, vendor=self.vendor)
        with self.assertRaises(ApprovalRefused):
            self.raise_material(**base, material={})
        other = Vendor.objects.create(name='Other', contact_person='O', phone='9')
        with self.assertRaises(ApprovalRefused):
            self.raise_material(**dict(base, vendor=other),
                                material={'vendor_order': self.order})
        approval = self.raise_material(**base, material={'vendor_order': self.order})
        self.assertEqual(approval.material_detail.vendor_order, self.order)

    def test_the_pre_order_link_is_optional_and_must_be_a_pre_order(self):
        pre_order = self.raise_material()
        base = dict(kind=APPROVAL_KIND_MATERIAL_PRE_DISPATCH, vendor=self.vendor)
        linked = self.raise_material(**base, material={'vendor_order': self.order,
                                                       'pre_order_request': pre_order})
        self.assertEqual(linked.material_detail.pre_order_request, pre_order)
        with self.assertRaises(ApprovalRefused):
            self.raise_material(**base, material={'vendor_order': self.order,
                                                  'pre_order_request': linked})
        with self.assertRaises(ApprovalRefused):
            self.raise_material(material={'pre_order_request': pre_order})

    def test_a_repeated_client_uuid_returns_the_first_request(self):
        key = uuid.uuid4()
        first = self.raise_material(client_uuid=key)
        again = self.raise_material(client_uuid=key)
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(ApprovalRequest.objects.count(), 1)
        self.assertEqual(len(self.ledger(first)), 1)

    def test_the_ledger_row_has_no_project_and_copies_the_role(self):
        row = self.ledger(self.raise_material())[0]
        self.assertIsNone(row.project)
        self.assertEqual((row.actor, row.actor_role_code), (self.scm, 'SCM'))


class AttachmentAppendOnlyTests(ApprovalFixture):

    def test_an_attachment_cannot_be_edited_or_deleted(self):
        attachment = ApprovalAttachment.objects.get(request=self.raise_material())
        attachment.label = 'changed'
        with self.assertRaises(AppendOnlyViolation):
            attachment.save()
        with self.assertRaises(AppendOnlyViolation):
            attachment.delete()


# ===========================================================================
# Predicates, ledger registration, admin
# ===========================================================================

class PredicateTests(ApprovalFixture):

    def test_raise_is_scm(self):
        self.assertTrue(user_can_raise_approval_request(self.scm.user))
        for profile in (self.pm, self.head, self.se):
            self.assertFalse(user_can_raise_approval_request(profile.user))

    def test_design_is_head_authority_never_the_designer(self):
        approval = self.raise_material()
        design = self.step(approval, APPROVAL_PARTY_DESIGN)
        self.assertTrue(user_can_decide_approval_step(self.head.user, design))
        self.assertTrue(user_can_decide_approval_step(self.deputy.user, design))
        self.assertFalse(user_can_decide_approval_step(self.designer.user, design))
        self.assertFalse(user_can_decide_approval_step(self.pm.user, design))

    def test_a_pm_step_is_its_assignees_alone(self):
        pm_step = self.step(self.raise_material(), APPROVAL_PARTY_PM)
        self.assertTrue(user_can_decide_approval_step(self.pm.user, pm_step))
        self.assertFalse(user_can_decide_approval_step(self.pm_b.user, pm_step))

    def test_proxy_withdraw_and_reassign_are_scm(self):
        approval = self.raise_material()
        pm_step = self.step(approval, APPROVAL_PARTY_PM)
        self.assertTrue(user_can_record_proxy_decision(self.scm.user, pm_step))
        self.assertFalse(user_can_record_proxy_decision(self.pm.user, pm_step))
        self.assertTrue(user_can_withdraw_approval_request(self.scm_b.user, approval))
        self.assertFalse(user_can_withdraw_approval_request(self.pm.user, approval))
        self.assertTrue(user_can_reassign_approval_step(self.scm.user, pm_step))
        self.assertFalse(user_can_reassign_approval_step(self.head.user, pm_step))

    def test_assignee_eligibility(self):
        self.assertTrue(profile_can_be_approval_assignee(self.pm, APPROVAL_PARTY_PM))
        self.assertTrue(profile_can_be_approval_assignee(self.head, APPROVAL_PARTY_DESIGN))
        self.assertTrue(profile_can_be_approval_assignee(self.se,
                                                         APPROVAL_PARTY_SITE_ENGINEER))
        self.assertFalse(profile_can_be_approval_assignee(self.deputy, APPROVAL_PARTY_DESIGN))
        self.pm_b.is_active = False
        self.assertFalse(profile_can_be_approval_assignee(self.pm_b, APPROVAL_PARTY_PM))


class LedgerRegistrationTests(TestCase):

    def test_the_registry_derives_the_subject_type_from_the_model(self):
        self.assertEqual(_subject_type_registry()[ApprovalRequest], SUBJECT_APPROVAL_REQUEST)
        field = StatusTransition._meta.get_field('subject_type')
        self.assertIn(SUBJECT_APPROVAL_REQUEST, dict(field.choices))


class AdminLockdownTests(TestCase):

    def setUp(self):
        self.request = RequestFactory().get('/')
        self.request.user = User.objects.create_superuser('ap_admin', 'a@example.com', 'pw')

    def test_every_field_is_read_only_and_nothing_can_be_added_changed_or_deleted(self):
        for model in (ApprovalRequest, ApprovalStep, ApprovalAttachment,
                      MaterialApprovalDetail):
            with self.subTest(model=model.__name__):
                model_admin = django_admin.site._registry[model]
                form = model_admin.get_form(self.request, None)
                self.assertEqual(list(form.base_fields), [])
                self.assertFalse(model_admin.has_add_permission(self.request))
                self.assertFalse(model_admin.has_change_permission(self.request))
                self.assertFalse(model_admin.has_delete_permission(self.request))
