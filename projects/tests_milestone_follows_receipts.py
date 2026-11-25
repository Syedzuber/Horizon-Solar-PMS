"""
FIN-M-B5-5a — a milestone follows its confirmed receipts (inert until the switch is on).

Pins, with CLIENT_RECEIPTS_LIVE on unless a test says otherwise:
  switch off  confirm, undo and the three agreed-amount writers leave the milestone and
              its log exactly as before B5-5a; recompute runs no query and opens no
              transaction; the marking command still dry-runs (FR38)
  normal      Received when confirmed money reaches the agreed amount (FR10), the real sum
              when over (H9); part receipts wait; undo goes back to Invoiced or Pending;
              unconfirmed and rejected money never counts (FR2); received_date = the
              latest date received, not the latest confirmation (ruling Q3)
  FR24        no agreed amount never turns Received; setting the amount through each of
              the three writers recomputes it
  legacy      FR37: marked milestones stay Received; confirmed money replaces the old
              figures and an undo brings them back; an amount change never un-receives
  Q1          an unmarked Received milestone with no receipts is left alone until marked
  command     mark_legacy_received_milestones: dry run, --apply, idempotence, reasons,
              --actor limited to the import's people (a PM is refused),
              switch-on warning, report-only confirmed line (Q5)
  logging     R15 old -> new lines with the suffix, a REASON_RECEIPTS transition, nothing
              for a no-change recompute
  H7 / H8     no alert from recompute and no task change
  labels      every word of milestone_receipt_label (H6; "Amount needed" alone at ₹0)
  A           recompute opens its own atomic block before the lock (PM edit path)

Run with:
    python manage.py test projects.tests_milestone_follows_receipts --settings=solarpms.test_settings
"""
import datetime
import json
import shutil
import tempfile
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import Client, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook

from . import receipts as receipts_module
from .milestone_amount_import import COLUMNS, apply_plan, plan_import, read_workbook
from .models import (
    REASON_RECEIPTS, ActivityLog, Notification, NotificationLog, PaymentMilestone,
    PaymentReceipt, Project, ProjectPhase, StatusTransition, SUBJECT_PAYMENT_MILESTONE, Task,
)
from .receipts import (
    RECOMPUTE_SUFFIX, ReceiptTotals, confirm_receipt, milestone_receipt_label,
    recompute_milestone_from_receipts, reject_receipt, query_receipt, unconfirm_receipt,
)
from .tests_receipts import ReceiptBase, _profile

LIVE = override_settings(CLIENT_RECEIPTS_LIVE=True)
OFF = override_settings(CLIENT_RECEIPTS_LIVE=False)
MILESTONE_CODES = ('milestone_updated', 'milestone_amount_received')
D = Decimal


def day(n):
    """n days before today (IST), so a receipt date is never in the future."""
    return timezone.localdate() - datetime.timedelta(days=n)


class FollowBase(ReceiptBase):
    """ReceiptBase plus: confirm-by-Finance, the three amount writers, the milestone's
    confirmation task, and readers for the row, its recompute log and its transitions."""

    def setUp(self):
        super().setUp()
        phase = ProjectPhase.objects.create(project=self.project, phase_name='Finance',
                                            phase_order=1)
        self.task = Task.objects.create(phase=phase, task_name='Advance Payment Confirmation',
                                        task_order=1, assigned_role=Task.FINANCE,
                                        is_payment_milestone=True)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    # -- receipts -------------------------------------------------------------

    def confirmed(self, milestone, amount, date=None, who=None):
        """Recorded by the PM, confirmed by Finance B; returns the receipt."""
        r = self.receipt(self.pm, milestone, amount=amount, date=date or day(1))
        confirm_receipt((who or self.fin_b).user, r, overpayment_note='over by design')
        return self.fresh(r)

    def undo(self, receipt):
        unconfirm_receipt(self.fin_a.user, self.fresh(receipt), 'bank bounce')

    # -- the three amount writers ---------------------------------------------

    def bd_set(self, milestone, amount):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.bd.user)
        key = f'{milestone.milestone_name.lower()}_amount'
        r = c.post(reverse('set_milestone_amounts', args=[self.project.project_id]),
                   data=json.dumps({key: str(amount)}), content_type='application/json')
        self.assertTrue(r.json()['success'], r.content)

    def pm_set(self, milestone, amount):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.pm.user)
        r = c.post(reverse('project_overview', args=[self.project.project_id]),
                   {'action': 'update_milestone', 'milestone_pk': milestone.pk,
                    'milestone_description': milestone.milestone_description,
                    'amount': str(amount), 'due_date': ''})
        self.assertEqual(r.status_code, 302)

    def import_set(self, milestone, amount):
        wb = Workbook()
        ws = wb.active
        ws.append(list(COLUMNS))
        row = {'project_code': self.project.project_id,
               f'{milestone.milestone_name}_new': str(amount)}
        ws.append([row.get(h) for h in COLUMNS])
        path = self.tmp / f'sheet{milestone.pk}.xlsx'
        wb.save(path)
        plans = plan_import(read_workbook(str(path)))
        self.assertEqual(apply_plan(plans, self.fin_a), 1, [p.errors for p in plans])

    WRITERS = ('bd_set', 'pm_set', 'import_set')

    # -- readers --------------------------------------------------------------

    def row(self, milestone):
        m = PaymentMilestone.objects.get(pk=milestone.pk)
        return (m.status, m.amount_received, m.received_date)

    def recompute_logs(self, milestone=None):
        qs = ActivityLog.objects.filter(action_code__in=MILESTONE_CODES)
        if milestone is not None:
            qs = qs.filter(entity_id=milestone.pk)
        return list(qs.order_by('pk').values_list('action_code', 'action'))

    def transitions(self, milestone):
        return list(StatusTransition.objects.filter(
            subject_type=SUBJECT_PAYMENT_MILESTONE, subject_id=milestone.pk)
            .order_by('pk').values_list('from_status', 'to_status', 'reason_code'))

    def task_state(self):
        t = Task.objects.get(pk=self.task.pk)
        return (t.status, t.completed_at, t.assigned_to_id)

    def set_amount(self, milestone, amount):
        PaymentMilestone.objects.filter(pk=milestone.pk).update(amount=D(amount))

    def make_received(self, milestone, got=None, on=None):
        """An old-path Received milestone (set directly, as the task sync's .update())."""
        PaymentMilestone.objects.filter(pk=milestone.pk).update(
            status=PaymentMilestone.RECEIVED, amount_received=D(got) if got else None,
            received_date=on or datetime.date(2026, 8, 3))

    def mark(self, *args):
        out = StringIO()
        call_command('mark_legacy_received_milestones', *args, stdout=out)
        return out.getvalue()


# ===========================================================================
# 1. Switch off: byte-for-byte today's behaviour (FR38)
# ===========================================================================

@OFF
class SwitchOffTests(FollowBase):

    def test_confirm_and_undo_leave_the_milestone_alone(self):
        self.set_amount(self.m1, '50000')
        before = self.row(self.m1)
        r = self.confirmed(self.m1, '50000')
        self.assertEqual(self.row(self.m1), before)
        self.undo(r)
        self.assertEqual(self.row(self.m1), before)
        self.assertEqual(self.recompute_logs(), [])
        self.assertEqual(self.transitions(self.m1), [])

    def test_every_amount_writer_leaves_status_and_logs_as_today(self):
        for milestone, writer in zip((self.m1, self.m2, self.m3), self.WRITERS):
            self.confirmed(milestone, '50000')
            before = self.row(milestone)
            getattr(self, writer)(milestone, '50000')
            self.assertEqual(PaymentMilestone.objects.get(pk=milestone.pk).amount, D('50000'))
            self.assertEqual(self.row(milestone), before, writer)
            self.assertEqual(self.transitions(milestone), [], writer)
        # Only the writers' own lines: the PM edit's amount line; no recompute line.
        self.assertFalse(ActivityLog.objects.filter(action__endswith=RECOMPUTE_SUFFIX).exists())
        self.assertFalse(ActivityLog.objects.filter(
            action_code='milestone_amount_received').exists())

    def test_recompute_runs_no_query_and_no_transaction(self):
        self.set_amount(self.m1, '50000')
        self.confirmed(self.m1, '50000')
        with mock.patch.object(receipts_module.transaction, 'atomic') as atomic, \
                CaptureQueriesContext(connection) as ctx:
            self.assertIsNone(recompute_milestone_from_receipts(self.m1, self.fin_a, 'x'))
        self.assertEqual(len(ctx.captured_queries), 0)
        atomic.assert_not_called()

    def test_command_still_dry_runs(self):
        self.make_received(self.m1, '1000')
        out = self.mark()
        self.assertIn('WOULD MARK 1', out)
        self.assertNotIn('switch is on', out)
        self.assertFalse(PaymentMilestone.objects.get(pk=self.m1.pk).received_before_receipts)


# ===========================================================================
# 2. A normal milestone (FR10, H9, FR35, FR2, Q3)
# ===========================================================================

@LIVE
class NormalMilestoneTests(FollowBase):

    def test_one_receipt_equal_to_the_amount_makes_it_received(self):
        self.set_amount(self.m1, '50000')
        self.confirmed(self.m1, '50000', date=day(3))
        self.assertEqual(self.row(self.m1), ('Received', D('50000.00'), day(3)))

    def test_two_part_receipts_wait_then_take_the_later_date(self):
        self.set_amount(self.m1, '50000')
        self.confirmed(self.m1, '20000', date=day(5))
        self.assertEqual(self.row(self.m1), ('Pending', D('20000.00'), None))
        self.confirmed(self.m1, '30000', date=day(2))
        self.assertEqual(self.row(self.m1), ('Received', D('50000.00'), day(2)))

    def test_received_date_is_the_latest_day_received_not_the_last_confirmed(self):
        # Ruling Q3: confirmed second, but its money arrived earlier.
        self.set_amount(self.m1, '50000')
        self.confirmed(self.m1, '20000', date=day(2))
        self.confirmed(self.m1, '30000', date=day(9))
        self.assertEqual(self.row(self.m1), ('Received', D('50000.00'), day(2)))

    def test_over_the_amount_is_received_with_the_real_sum(self):
        self.set_amount(self.m1, '50000')
        self.confirmed(self.m1, '60000')
        self.assertEqual(self.row(self.m1)[:2], ('Received', D('60000.00')))

    def test_undo_goes_back_to_pending_with_the_remaining_money(self):
        self.set_amount(self.m1, '50000')
        self.confirmed(self.m1, '20000', date=day(5))
        last = self.confirmed(self.m1, '30000', date=day(2))
        self.undo(last)
        self.assertEqual(self.row(self.m1), ('Pending', D('20000.00'), None))

    def test_undo_goes_back_to_invoiced_when_an_invoice_was_recorded(self):
        PaymentMilestone.objects.filter(pk=self.m2.pk).update(
            amount=D('50000'), status=PaymentMilestone.INVOICED,
            invoice_date=day(10), invoice_number='INV-1', invoice_amount=D('50000'))
        r = self.confirmed(self.m2, '50000')
        self.assertEqual(self.row(self.m2)[0], 'Received')
        self.undo(r)
        self.assertEqual(self.row(self.m2), ('Invoiced', None, None))

    def test_undo_of_the_only_receipt_leaves_amount_received_blank(self):
        self.set_amount(self.m1, '50000')
        r = self.confirmed(self.m1, '50000')
        self.undo(r)
        self.assertEqual(self.row(self.m1), ('Pending', None, None))

    def test_unconfirmed_and_rejected_money_never_counts(self):
        self.set_amount(self.m1, '50000')
        self.receipt(self.pm, self.m1, amount='50000')                    # recorded
        queried = self.receipt(self.pm, self.m1, amount='50000', reference='Q')
        query_receipt(self.fin_a.user, queried, 'which bank?')             # queried
        rejected = self.receipt(self.pm, self.m1, amount='50000', reference='R')
        reject_receipt(self.fin_a.user, rejected, 'duplicate')             # rejected
        recompute_milestone_from_receipts(self.m1, self.fin_a, 'check')
        self.assertEqual(self.row(self.m1), ('Pending', None, None))
        self.assertEqual(self.recompute_logs(), [])

    def test_recompute_inside_confirm_sees_the_receipt_just_confirmed(self):
        # The sum runs in confirm's transaction after its save: no stale total.
        self.set_amount(self.m1, '50000')
        r = self.receipt(self.pm, self.m1, amount='50000')
        result = confirm_receipt(self.fin_b.user, r)
        self.assertEqual(result.receipt.status, PaymentReceipt.CONFIRMED)
        self.assertEqual(self.row(self.m1)[0], 'Received')


# ===========================================================================
# 3. No agreed amount (FR24), then set through each writer
# ===========================================================================

@LIVE
class NoAgreedAmountTests(FollowBase):

    def test_confirmed_money_never_makes_it_received(self):
        self.confirmed(self.m1, '90000')
        m = PaymentMilestone.objects.get(pk=self.m1.pk)
        self.assertEqual(self.row(self.m1), ('Pending', D('90000.00'), None))
        totals = receipts_module.receipt_totals([m])[m.pk]
        self.assertEqual(milestone_receipt_label(m, totals),
                         'Amount needed — ₹90,000 confirmed')

    def test_each_writer_recomputes_to_received_when_money_reaches_the_amount(self):
        for milestone, writer in zip((self.m1, self.m2, self.m3), self.WRITERS):
            self.confirmed(milestone, '50000', date=day(4))
            getattr(self, writer)(milestone, '50000')
            self.assertEqual(self.row(milestone), ('Received', D('50000.00'), day(4)), writer)
            self.assertEqual(self.transitions(milestone),
                             [('Pending', 'Received', REASON_RECEIPTS)], writer)

    def test_each_writer_leaves_it_pending_when_money_is_short(self):
        for milestone, writer in zip((self.m1, self.m2, self.m3), self.WRITERS):
            self.confirmed(milestone, '20000')
            getattr(self, writer)(milestone, '50000')
            self.assertEqual(self.row(milestone), ('Pending', D('20000.00'), None), writer)
            self.assertEqual(self.transitions(milestone), [], writer)


# ===========================================================================
# 4. Legacy milestones (FR37) and the unmarked gap (Q1)
# ===========================================================================

@LIVE
class LegacyTests(FollowBase):

    OLD_DAY = datetime.date(2026, 8, 3)

    def legacy(self, milestone, got='75000', amount='100000'):
        PaymentMilestone.objects.filter(pk=milestone.pk).update(amount=D(amount))
        self.make_received(milestone, got, self.OLD_DAY)
        self.mark('--apply', '--actor', self.fin_a.user.username)
        m = PaymentMilestone.objects.get(pk=milestone.pk)
        self.assertTrue(m.received_before_receipts)
        return m

    def test_marked_stays_received_with_the_old_figures(self):
        m = self.legacy(self.m1)
        self.assertEqual(self.row(m), ('Received', D('75000.00'), self.OLD_DAY))
        self.assertEqual((m.legacy_amount_received, m.legacy_received_date),
                         (D('75000.00'), self.OLD_DAY))

    def test_a_smaller_confirmed_receipt_replaces_the_old_amount_and_undo_restores_it(self):
        m = self.legacy(self.m1)
        r = self.confirmed(m, '10000', date=day(1))
        self.assertEqual(self.row(m), ('Received', D('10000.00'), day(1)))
        self.undo(r)
        self.assertEqual(self.row(m), ('Received', D('75000.00'), self.OLD_DAY))

    def test_changing_the_agreed_amount_never_unreceives_it(self):
        m = self.legacy(self.m1)
        for writer in ('bd_set', 'pm_set'):
            getattr(self, writer)(m, '900000')
            self.assertEqual(self.row(m), ('Received', D('75000.00'), self.OLD_DAY), writer)
        self.assertEqual(self.recompute_logs(m), [])

    def test_legacy_without_an_old_amount_keeps_blank(self):
        m = self.legacy(self.m1, got=None)
        r = self.confirmed(m, '5000', date=day(1))
        self.assertEqual(self.row(m), ('Received', D('5000.00'), day(1)))
        self.undo(r)
        self.assertEqual(self.row(m), ('Received', None, self.OLD_DAY))

    def test_unmarked_received_without_receipts_survives_amount_changes_then_marks(self):
        # Ruling Q1: received through an old path after marking — left alone.
        self.make_received(self.m1, '40000', self.OLD_DAY)
        for writer in ('bd_set', 'pm_set'):
            getattr(self, writer)(self.m1, '100000')
            self.assertEqual(self.row(self.m1), ('Received', D('40000.00'), self.OLD_DAY))
        self.assertEqual(self.recompute_logs(self.m1), [])
        out = self.mark('--apply', '--actor', self.fin_a.user.username)
        self.assertIn('switch is on: marking late milestones', out)
        self.assertIn('MARKED 1', out)
        m = PaymentMilestone.objects.get(pk=self.m1.pk)
        r = self.confirmed(m, '1000', date=day(1))
        self.assertEqual(self.row(m), ('Received', D('1000.00'), day(1)))
        self.undo(r)
        self.assertEqual(self.row(m), ('Received', D('40000.00'), self.OLD_DAY))

    def test_unmarked_received_with_any_receipt_row_follows_the_normal_rule(self):
        self.make_received(self.m1, '40000', self.OLD_DAY)
        self.set_amount(self.m1, '100000')
        self.receipt(self.pm, self.m1, amount='1000')                      # recorded only
        recompute_milestone_from_receipts(self.m1, self.fin_a, 'check')
        self.assertEqual(self.row(self.m1), ('Pending', None, None))


# ===========================================================================
# 5. The marking command
# ===========================================================================

@OFF
class CommandTests(FollowBase):

    def build(self):
        """One milestone per outcome. Returns the one that qualifies."""
        self.make_received(self.m1, '1000')                                  # qualifies
        PaymentMilestone.objects.filter(pk=self.m2.pk).update(
            status=PaymentMilestone.INVOICED, invoice_date=day(3))           # Invoiced
        # m3 stays Pending.
        with_receipt = self.milestones(self.site(name='With receipt'))[0]
        self.make_received(with_receipt)
        self.receipt(self.fin_a, with_receipt, amount='10')                  # has receipts
        deleted = self.milestones(self.site(name='Gone', is_deleted=True))[0]
        self.make_received(deleted)
        opex = self.milestones(self.site(name='Tender', project_type='OPEX'))[0]
        self.make_received(opex)
        return with_receipt

    def test_dry_run_writes_nothing_and_prints_the_plan(self):
        with_receipt = self.build()
        before = list(PaymentMilestone.objects.order_by('pk').values())
        logs = ActivityLog.objects.count()
        out = self.mark()
        self.assertEqual(list(PaymentMilestone.objects.order_by('pk').values()), before)
        self.assertEqual(ActivityLog.objects.count(), logs)
        self.assertIn(f'pk {self.m1.pk} | would mark | old amount 1000.00', out)
        self.assertIn(f'pk {with_receipt.pk} | skipped: Received but has receipts', out)
        self.assertIn('WOULD MARK 1 (planned: M1 1; with old amount 1, sum 1000.00; '
                      'without 0)', out)
        self.assertIn('ALREADY 0', out)
        # 12 rows: this site's M2 Invoiced and M3 Pending, the receipt site's M2/M3 Pending.
        self.assertIn('SKIPPED 11 by reason: deleted site 3, not Residential 3, '
                      'not Received 4 (Invoiced 1, Pending 3), has receipts 1', out)
        self.assertIn(f'CHECKED {PaymentMilestone.objects.count()}', out)
        self.assertIn('HAS CONFIRMED RECEIPTS, NOT RECOMPUTED: 0', out)
        self.assertIn('rows written: 0 (expected 0)', out)

    def test_apply_marks_only_qualifying_rows_and_a_second_run_marks_none(self):
        self.build()
        out = self.mark('--apply', '--actor', self.fin_a.user.username)
        self.assertIn('MARKED 1', out)
        self.assertIn('rows written: 1 (expected 1)', out)
        marked = PaymentMilestone.objects.filter(received_before_receipts=True)
        self.assertEqual(list(marked.values_list('pk', flat=True)), [self.m1.pk])
        m = marked.get()
        self.assertEqual((m.status, m.amount_received, m.legacy_amount_received,
                          m.received_date, m.legacy_received_date),
                         ('Received', D('1000.00'), D('1000.00'), m.received_date,
                          datetime.date(2026, 8, 3)))
        self.assertIsNotNone(m.legacy_marked_at)
        line = ActivityLog.objects.get(action_code='milestone_legacy_marked')
        self.assertEqual(line.action, 'M1: marked as received before receipts '
                                      '(old amount ₹1,000, received 3 Aug 2026)')
        self.assertEqual((line.entity_type, line.entity_id, line.actor_id),
                         ('Milestone', m.pk, self.fin_a.pk))
        again = self.mark('--apply', '--actor', self.fin_a.user.username)
        self.assertIn('MARKED 0 (planned: none; with old amount 0, sum 0; without 0)', again)
        self.assertIn('ALREADY 1', again)
        self.assertIn('rows written: 0 (expected 0)', again)

    def test_apply_never_touches_status_money_or_tasks(self):
        self.make_received(self.m1, '1000')
        task = self.task_state()
        self.mark('--apply', '--actor', self.fin_a.user.username)
        self.assertEqual(self.row(self.m1), ('Received', D('1000.00'), datetime.date(2026, 8, 3)))
        self.assertEqual(self.task_state(), task)
        self.assertEqual(self.transitions(self.m1), [])

    def test_apply_needs_an_allowed_active_actor(self):
        self.make_received(self.m1, '1000')
        with self.assertRaisesMessage(CommandError, '--apply needs --actor'):
            self.mark('--apply')
        with self.assertRaisesMessage(CommandError, 'no such user'):
            self.mark('--apply', '--actor', 'nobody')
        # Ruling 25 Nov: the import's people only (user_can_import_milestone_amounts).
        with self.assertRaisesMessage(CommandError, "--actor 'rc_pm' (PM, login active, "
                                      "profile active) may not mark milestones"):
            self.mark('--apply', '--actor', self.pm.user.username)
        with self.assertRaisesMessage(CommandError, 'profile inactive) may not mark'):
            self.mark('--apply', '--actor', self.coord_off.user.username)
        for allowed in (self.ceo, self.admin_p):
            self.assertIn('WOULD MARK 1', self.mark('--actor', allowed.user.username))
        self.assertFalse(PaymentMilestone.objects.filter(received_before_receipts=True).exists())

    def test_switch_on_warns_and_still_applies(self):
        self.make_received(self.m1, '1000')
        with LIVE:
            dry = self.mark()
            out = self.mark('--apply', '--actor', self.fin_a.user.username)
        self.assertIn('switch is on: marking late milestones', dry)
        self.assertIn('WOULD MARK 1', dry)
        self.assertIn('switch is on: marking late milestones', out)
        self.assertIn('MARKED 1', out)

    def test_confirmed_receipts_are_reported_not_recomputed(self):
        self.set_amount(self.m1, '50000')
        self.confirmed(self.m1, '50000')                    # confirmed while the switch is off
        before = self.row(self.m1)
        out = self.mark('--apply', '--actor', self.fin_a.user.username)
        self.assertIn(f'HAS CONFIRMED RECEIPTS, NOT RECOMPUTED: 1 (pks {self.m1.pk})', out)
        self.assertEqual(self.row(self.m1), before)


# ===========================================================================
# 6. Logging, alerts (H7) and tasks (H8)
# ===========================================================================

@LIVE
class LoggingAlertTaskTests(FollowBase):

    def test_a_change_writes_r15_lines_and_one_transition(self):
        self.set_amount(self.m1, '50000')
        self.confirmed(self.m1, '50000', date=datetime.date(2026, 11, 5))
        self.assertEqual(self.recompute_logs(self.m1), [
            ('milestone_updated', 'M1: status Pending → Received; received date blank → '
                                  '5 Nov 2026 (from confirmed receipts)'),
            ('milestone_amount_received', 'M1: amount received blank → ₹50,000'),
        ])
        t = StatusTransition.objects.get(subject_type=SUBJECT_PAYMENT_MILESTONE,
                                         subject_id=self.m1.pk)
        self.assertEqual((t.from_status, t.to_status, t.reason_code, t.remark, t.actor_id),
                         ('Pending', 'Received', REASON_RECEIPTS, 'receipt confirmed',
                          self.fin_b.pk))

    def test_a_money_only_change_writes_no_transition(self):
        self.confirmed(self.m1, '1000')                          # no agreed amount
        self.assertEqual(self.recompute_logs(self.m1), [
            ('milestone_amount_received', 'M1: amount received blank → ₹1,000')])
        self.assertEqual(self.transitions(self.m1), [])

    def test_a_no_change_recompute_writes_nothing(self):
        self.set_amount(self.m1, '50000')
        self.confirmed(self.m1, '50000')
        logs, moves = ActivityLog.objects.count(), StatusTransition.objects.count()
        with CaptureQueriesContext(connection) as ctx:
            recompute_milestone_from_receipts(self.m1, self.fin_a, 'again')
        self.assertEqual((ActivityLog.objects.count(), StatusTransition.objects.count()),
                         (logs, moves))
        self.assertFalse([q for q in ctx.captured_queries
                          if q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE'))])

    def test_recompute_to_received_sends_no_alert(self):
        # H7: confirmed while off (no recompute), then recompute alone with sends executed.
        self.set_amount(self.m1, '50000')
        with OFF:
            self.confirmed(self.m1, '50000')
        notes, sends = Notification.objects.count(), NotificationLog.objects.count()
        with self.captureOnCommitCallbacks(execute=True):
            recompute_milestone_from_receipts(self.m1, self.fin_a, 'check')
        self.assertEqual(self.row(self.m1)[0], 'Received')
        self.assertEqual((Notification.objects.count(), NotificationLog.objects.count()),
                         (notes, sends))

    def test_receipt_actions_never_send_payment_milestone_reached(self):
        self.set_amount(self.m1, '50000')
        with self.captureOnCommitCallbacks(execute=True):
            r = self.confirmed(self.m1, '50000')
        with self.captureOnCommitCallbacks(execute=True):
            self.undo(r)
        self.assertFalse(NotificationLog.objects.filter(
            event_code='payment_milestone_reached').exists())

    # CHANGED AT FIN-M-B5-5b (ruling Q8): H8 "the task is never touched" split in two.
    # Switch off the task is still never touched; switch on it follows (K1), which
    # tests_tasks_follow_receipts covers in full.
    def test_switch_off_the_confirmation_task_is_never_touched(self):
        before = self.task_state()
        self.set_amount(self.m1, '50000')
        with OFF:
            r = self.confirmed(self.m1, '50000')
            self.undo(r)
            self.pm_set(self.m1, '40000')
        self.assertEqual(self.row(self.m1)[0], 'Pending')
        self.assertEqual(self.task_state(), before)

    def test_switch_on_the_confirmation_task_follows(self):
        self.set_amount(self.m1, '50000')
        r = self.confirmed(self.m1, '50000')
        self.assertEqual(self.row(self.m1)[0], 'Received')
        self.assertEqual(self.task_state()[0], Task.DONE)
        self.undo(r)
        self.assertEqual(self.row(self.m1)[0], 'Pending')
        status, completed_at, _ = self.task_state()
        self.assertEqual((status, completed_at), (Task.NOT_STARTED, None))


# ===========================================================================
# 7. Labels (H6)
# ===========================================================================

class LabelTests(FollowBase):

    def label(self, confirmed='0', **fields):
        m = PaymentMilestone(milestone_name='M1', **fields)
        return milestone_receipt_label(m, ReceiptTotals(confirmed=D(confirmed)))

    def test_the_labels(self):
        self.assertEqual(self.label(status='Received', amount=D('100')), 'Received')
        self.assertEqual(self.label(status='Received', received_before_receipts=True),
                         'Received before receipts')
        self.assertEqual(self.label('5', status='Received', received_before_receipts=True),
                         'Received')
        self.assertEqual(self.label('40000', status='Pending', amount=D('100000')),
                         'Part received ₹40,000 of ₹1,00,000')
        self.assertEqual(self.label('2500.50', status='Pending'),
                         'Amount needed — ₹2,500.50 confirmed')
        self.assertEqual(self.label(status='Pending'), 'Amount needed')
        self.assertEqual(self.label(status='Invoiced', amount=D('100')), 'Invoiced')
        self.assertEqual(self.label(status='Pending', amount=D('100')), 'Pending')


# ===========================================================================
# 8. A: recompute opens its own atomic block (the PM edit path has none)
# ===========================================================================

@LIVE
class OwnTransactionTests(TransactionTestCase):
    """Outside TestCase's wrapping transaction, so the PM edit view really runs with no
    atomic block of its own. The lock is taken only once recompute's block is open."""

    def test_pm_edit_recompute_locks_inside_its_own_atomic_block(self):
        pm = _profile('own_pm', 'PM')
        fin = _profile('own_fin', 'Finance')
        project = Project.objects.create(
            customer_name='Own', status='Active', project_type='Residential',
            customer_phone='9876543210', site_address='1 Sun Road', city='Lucknow',
            state='Uttar Pradesh', dc_capacity_kw=Decimal('5.00'), assigned_pm=pm,
            cascade_scheduling=False)
        m1 = PaymentMilestone.objects.create(project=project, milestone_name='M1')
        PaymentReceipt.objects.create(milestone=m1, status=PaymentReceipt.CONFIRMED,
                                      amount=D('50000'), date_received=day(1),
                                      mode=PaymentReceipt.UPI, recorded_by=pm,
                                      confirmed_by=fin, confirmed_at=timezone.now())
        seen = []
        real_lock = receipts_module._lock_milestone

        def lock(pk):
            seen.append(connection.in_atomic_block)
            return real_lock(pk)

        c = Client(SERVER_NAME='localhost')
        c.force_login(pm.user)
        self.assertFalse(connection.in_atomic_block)
        with mock.patch.object(receipts_module, '_lock_milestone', side_effect=lock):
            c.post(reverse('project_overview', args=[project.project_id]),
                   {'action': 'update_milestone', 'milestone_pk': m1.pk,
                    'milestone_description': '', 'amount': '50000', 'due_date': ''})
        self.assertEqual(seen, [True])
        self.assertEqual(PaymentMilestone.objects.get(pk=m1.pk).status, 'Received')
