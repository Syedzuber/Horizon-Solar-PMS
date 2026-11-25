"""
FIN-M-B5-4b — the alerts a client receipt sends (ruling FR36, 25 Nov 2026).

Pins: five registry events (email paused, deferred, no WhatsApp; tiers ACT / KNOW); each
fired once per successful writer action, after commit, with the right recipients, text
and link; refused actions and a rolled-back transaction send nothing; active people
only, never the actor, one alert per person; the G4 fallback chain for Queried and
Rejected (completer -> PM -> coordinators -> nobody, logged) with its wording, and none
for Confirmation undone; test sites send nothing (G5, new behaviour — no rows at all),
Cancelled sites send (FR28); an alert fault never undoes the action; the old milestone
"Payment received" (payment_milestone_reached) is never sent by a receipt action; and
G9, the over-the-amount sentence shown once on the receipt page.

Run with:
    python manage.py test projects.tests_receipt_alerts --settings=solarpms.test_settings
"""
from decimal import Decimal
from unittest import mock

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from . import receipt_alerts
from .models import Notification, NotificationLog, PaymentMilestone, PaymentReceipt, Project
from .money_format import format_rupees
from .notify import render_email
from .notify_registry import TIER_ACT, TIER_KNOW, get_event
from .receipt_alerts import (
    CONFIRMATION_UNDONE, EVENTS, PAYMENT_RECEIVED, QUERIED, RECORDER_BLANK,
    RECORDER_INACTIVE, REJECTED, TO_CONFIRM,
)
from .receipts import (
    ReceiptRefused, answer_receipt_query, complete_receipt_amount, confirm_receipt,
    query_receipt, reject_receipt, unconfirm_receipt,
)
from .site_bd import display_name
from .tests_receipt_form import LIVE, ReceiptFormBase, _profile

OLD_EVENT = 'payment_milestone_reached'


class Rollback(Exception):
    """Raised to roll an outer transaction back on purpose."""


class AlertBase(ReceiptFormBase):
    """ReceiptFormBase's site (PM, active + inactive coordinator, BD) plus a third active
    Finance user and an inactive one, so "every active Finance user" is proven for
    several people and the inactive one is seen left out."""

    def setUp(self):
        super().setUp()
        self.fin_c = _profile('rf_fin_c', 'Finance')
        self.fin_off = _profile('rf_fin_off', 'Finance', active=False)
        self.today = timezone.localdate()

    def sent(self, fn, *args, **kwargs):
        """Run `fn` with its after-commit callbacks executed; return its result."""
        with self.captureOnCommitCallbacks(execute=True):
            return fn(*args, **kwargs)

    def in_app(self, event):
        """{username: Notification} of the in-app rows `event` wrote."""
        pks = NotificationLog.objects.filter(event_code=event, channel='in_app',
                                             status='sent').values_list('recipient', flat=True)
        rows = (Notification.objects.filter(recipient__in=pks).select_related('recipient__user')
                .order_by('pk'))
        return {n.recipient.user.username: n for n in rows}

    def recipients(self, event):
        """Sorted usernames of everyone `event` reached in-app (one row per send)."""
        return sorted(NotificationLog.objects.filter(event_code=event, channel='in_app')
                      .values_list('recipient__user__username', flat=True))

    def names(self, *profiles):
        return sorted(p.user.username for p in profiles)

    def receipt_link(self, receipt):
        return reverse('receipt_detail', args=[self.project.project_id, receipt.pk])

    def card_link(self):
        return reverse('project_overview', args=[self.project.project_id]) + '#payment-milestones'

    def fresh(self, receipt):
        return PaymentReceipt.objects.get(pk=receipt.pk)

    def deactivate(self, profile):
        profile.is_active = False
        profile.save(update_fields=['is_active'])

    def rows(self):
        return Notification.objects.count(), NotificationLog.objects.count()


# ===========================================================================
# 1. The five events in the registry (G1)
# ===========================================================================

class RegistryTests(AlertBase):

    def test_five_events_paused_deferred_no_whatsapp(self):
        tiers = {TO_CONFIRM: TIER_ACT, QUERIED: TIER_ACT, REJECTED: TIER_ACT,
                 PAYMENT_RECEIVED: TIER_KNOW, CONFIRMATION_UNDONE: TIER_KNOW}
        self.assertEqual(set(EVENTS), set(tiers))
        for code in EVENTS:
            with self.subTest(event=code):
                event = get_event(code)
                self.assertEqual(event.channels, ('email',))
                self.assertEqual(event.default_state, 'paused')
                self.assertEqual(event.default_state_for('email'), 'paused')
                self.assertTrue(event.deferred)
                self.assertIsNone(event.whatsapp)
                self.assertFalse(event.whatsapp_sent_separately)
                self.assertEqual(event.tier, tiers[code])

    def test_old_event_unchanged(self):
        event = get_event(OLD_EVENT)
        self.assertEqual((event.label, event.default_state, event.channels),
                         ('Payment received', 'active', ('email',)))


# ===========================================================================
# 2. Receipt to confirm (FR36 item 1)
# ===========================================================================

class ToConfirmTests(AlertBase):

    def test_pm_records_every_active_finance_user_is_told(self):
        receipt = self.sent(self.record, self.pm, self.m1, note='Paid by the son')
        self.assertEqual(self.recipients(TO_CONFIRM),
                         self.names(self.fin, self.fin_b, self.fin_c))
        n = self.in_app(TO_CONFIRM)['rf_fin']
        self.assertEqual(n.link, self.receipt_link(receipt))
        day = f'{self.today.day} {self.today:%b %Y}'
        self.assertEqual(n.message, f'Receipt to confirm: ₹50,000 on '
                                    f'{self.project.project_id} M1, received {day}, '
                                    f'recorded by {display_name(self.pm)}.')

    def test_finance_recorder_is_not_told(self):
        self.sent(self.record, self.fin, self.m2)
        self.assertEqual(self.recipients(TO_CONFIRM), self.names(self.fin_b, self.fin_c))

    def test_inactive_login_is_left_out(self):
        self.fin_c.user.is_active = False
        self.fin_c.user.save(update_fields=['is_active'])
        self.sent(self.record, self.bd, self.m1)
        self.assertEqual(self.recipients(TO_CONFIRM), self.names(self.fin, self.fin_b))

    def test_each_recording_sends_once(self):
        self.sent(self.record, self.pm, self.m1)
        self.sent(self.record, self.pm, self.m2)
        self.assertEqual(NotificationLog.objects.filter(
            event_code=TO_CONFIRM, channel='in_app').count(), 6)

    def test_email_paused_logged_with_event_code_no_whatsapp(self):
        self.sent(self.record, self.pm, self.m1)
        email = NotificationLog.objects.filter(event_code=TO_CONFIRM, channel='email')
        self.assertEqual(email.count(), 3)
        self.assertEqual(set(email.values_list('status', 'error_detail')),
                         {('skipped', 'event paused')})
        self.assertEqual(set(email.values_list('template_version', flat=True)), {None})
        self.assertFalse(NotificationLog.objects.filter(channel='whatsapp').exists())
        self.assertEqual(Notification.objects.count(), 3)


# ===========================================================================
# 3. Queried and Rejected (FR36 items 2-3) and the G4 fallback
# ===========================================================================

class QueriedRejectedTests(AlertBase):

    def test_queried_goes_to_the_recorder_with_reason_and_link(self):
        receipt = self.record(self.bd, self.m1)
        self.sent(query_receipt, self.fin.user, receipt, 'Which bank?')
        self.assertEqual(self.recipients(QUERIED), ['rf_bd'])
        n = self.in_app(QUERIED)['rf_bd']
        self.assertEqual(n.link, self.receipt_link(receipt))
        self.assertIn('Receipt queried on', n.message)
        self.assertIn('M1 (₹50,000', n.message)
        self.assertIn(f'by {display_name(self.fin)}: Which bank?', n.message)
        self.assertNotIn('Sent to you because', n.message)

    def test_rejected_goes_to_the_recorder_with_reason(self):
        receipt = self.record(self.pm, self.m2)
        self.sent(reject_receipt, self.fin_b.user, receipt, 'Not in the statement')
        self.assertEqual(self.recipients(REJECTED), ['rf_pm'])
        n = self.in_app(REJECTED)['rf_pm']
        self.assertTrue(n.message.endswith(f'by {display_name(self.fin_b)}: Not in the statement'))
        self.assertEqual(n.link, self.receipt_link(receipt))

    def test_rejected_from_queried_also_sends(self):
        receipt = self.record(self.pm, self.m2)
        query_receipt(self.fin.user, receipt, 'Q')
        self.sent(reject_receipt, self.fin.user, self.fresh(receipt), 'Wrong')
        self.assertEqual(self.recipients(REJECTED), ['rf_pm'])

    def test_finance_recorder_rejecting_own_receipt_is_not_told(self):
        receipt = self.record(self.fin, self.m2)
        before = self.rows()
        self.sent(reject_receipt, self.fin.user, receipt, 'Typed twice')
        self.assertEqual(self.rows(), before)

    def test_active_recorder_who_rejects_is_not_replaced_by_a_fallback(self):
        """Ruling 25 Nov (round 4): an ACTIVE recorder who is also the actor is told
        nobody — they already know. The recorder step is not a fallback, so the Q1
        skip-the-actor rule does not move the alert on to the completer, PM or the
        coordinators, and since a recorder WAS found, no "found no recipient" line."""
        receipt = self.record(self.fin, self.m2)
        before = self.rows()
        with self.assertNoLogs('projects.receipt_alerts', level='WARNING'):
            self.sent(reject_receipt, self.fin.user, receipt, 'Typed twice')
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.recipients(REJECTED), [])
        self.assertEqual(self.fresh(receipt).status, PaymentReceipt.REJECTED)

    def test_inactive_recorder_falls_back_to_the_assigned_pm(self):
        receipt = self.record(self.bd, self.m1)
        self.deactivate(self.bd)
        self.sent(query_receipt, self.fin.user, receipt, 'Which bank?')
        self.assertEqual(self.recipients(QUERIED), ['rf_pm'])
        self.assertTrue(self.in_app(QUERIED)['rf_pm'].message.endswith(
            RECORDER_INACTIVE.format(name=display_name(self.bd))))

    def test_then_active_coordinators_when_the_pm_is_inactive(self):
        receipt = self.record(self.bd, self.m1)
        self.deactivate(self.bd)
        self.deactivate(self.pm)
        self.sent(reject_receipt, self.fin.user, receipt, 'Wrong')
        self.assertEqual(self.recipients(REJECTED), ['rf_coord'])   # rf_coord_off left out
        self.assertIn('Sent to you because', self.in_app(REJECTED)['rf_coord'].message)

    def test_then_coordinators_when_there_is_no_pm(self):
        receipt = self.record(self.bd, self.m1)
        self.deactivate(self.bd)
        Project.objects.filter(pk=self.project.pk).update(assigned_pm=None)
        self.sent(query_receipt, self.fin.user, receipt, 'Q')
        self.assertEqual(self.recipients(QUERIED), ['rf_coord'])

    def test_nobody_found_sends_nothing_and_logs_one_line(self):
        receipt = self.record(self.bd, self.m1)
        for p in (self.bd, self.pm, self.coord):
            self.deactivate(p)
        before = self.rows()
        with self.assertLogs('projects.receipt_alerts', level='WARNING') as logs:
            self.sent(query_receipt, self.fin.user, receipt, 'Q')
        self.assertEqual(self.rows(), before)
        self.assertEqual(len(logs.records), 1)
        self.assertIn('found no recipient', logs.output[0])
        self.assertEqual(self.fresh(receipt).status, PaymentReceipt.QUERIED)

    def test_migrated_row_with_no_recorder_goes_to_the_active_completer(self):
        legacy = PaymentReceipt.objects.create(milestone=self.m3,
                                               status=PaymentReceipt.AMOUNT_NEEDED,
                                               source=PaymentReceipt.MIGRATED)
        complete_receipt_amount(self.fin.user, legacy, '5000', self.today, PaymentReceipt.CASH)
        before = self.rows()
        self.sent(query_receipt, self.fin_b.user, self.fresh(legacy), 'Cash or cheque?')
        self.assertEqual(self.recipients(QUERIED), ['rf_fin'])
        self.assertTrue(self.in_app(QUERIED)['rf_fin'].message.endswith(RECORDER_BLANK))
        # complete_receipt_amount itself sent nothing (G10).
        self.assertEqual(NotificationLog.objects.count() - before[1],
                         NotificationLog.objects.filter(event_code=QUERIED).count())

    def _legacy_completed_by_fin_recorded_by_inactive_bd(self):
        """A migrated row whose figures rf_fin completed and whose recorder (rf_bd) has
        since left — the set-up for ruling Q1's skip-the-actor tests."""
        legacy = PaymentReceipt.objects.create(milestone=self.m3,
                                               status=PaymentReceipt.AMOUNT_NEEDED,
                                               source=PaymentReceipt.MIGRATED,
                                               recorded_by=self.bd)
        complete_receipt_amount(self.fin.user, legacy, '5000', self.today, PaymentReceipt.CASH)
        self.deactivate(self.bd)
        return self.fresh(legacy)

    def test_completer_who_rejects_is_skipped_for_the_pm(self):
        """Ruling Q1: the completer is the actor, so the chain moves on to the PM."""
        legacy = self._legacy_completed_by_fin_recorded_by_inactive_bd()
        self.sent(reject_receipt, self.fin.user, legacy, 'No such payment')
        self.assertEqual(self.recipients(REJECTED), ['rf_pm'])
        self.assertTrue(self.in_app(REJECTED)['rf_pm'].message.endswith(
            RECORDER_INACTIVE.format(name=display_name(self.bd))))

    def test_completer_actor_and_pm_actor_both_skipped_for_coordinators(self):
        """Ruling Q1, two steps skipped. Only Finance may query or reject through the
        writer, so a PM can never be the actor of a real reject; the chain is exercised
        by calling the alert directly with the PM as actor, and the completer is made the
        PM, so both the completer step and the PM step are the actor and are skipped."""
        legacy = self._legacy_completed_by_fin_recorded_by_inactive_bd()
        PaymentReceipt.objects.filter(pk=legacy.pk).update(completed_by=self.pm)
        legacy = self.fresh(legacy)
        self.sent(receipt_alerts.receipt_rejected, legacy, self.project, self.pm, 'Wrong')
        self.assertEqual(self.recipients(REJECTED), ['rf_coord'])   # rf_coord_off left out
        self.assertTrue(self.in_app(REJECTED)['rf_coord'].message.endswith(
            RECORDER_INACTIVE.format(name=display_name(self.bd))))

    def test_actor_is_the_only_coordinator_left_nobody_and_one_log_line(self):
        legacy = self._legacy_completed_by_fin_recorded_by_inactive_bd()
        self.deactivate(self.fin)     # the completer has left too
        self.deactivate(self.pm)
        before = self.rows()
        with self.assertLogs('projects.receipt_alerts', level='WARNING') as logs:
            self.sent(receipt_alerts.receipt_rejected, legacy, self.project, self.coord, 'x')
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(self.rows(), before)

    def test_inactive_completer_skipped_for_the_pm(self):
        legacy = PaymentReceipt.objects.create(milestone=self.m3,
                                               status=PaymentReceipt.AMOUNT_NEEDED,
                                               source=PaymentReceipt.MIGRATED)
        complete_receipt_amount(self.fin.user, legacy, '5000', self.today, PaymentReceipt.CASH)
        self.deactivate(self.fin)
        self.sent(reject_receipt, self.fin_b.user, self.fresh(legacy), 'No such payment')
        self.assertEqual(self.recipients(REJECTED), ['rf_pm'])
        self.assertTrue(self.in_app(REJECTED)['rf_pm'].message.endswith(RECORDER_BLANK))


# ===========================================================================
# 4. Payment received and Confirmation undone (FR36 items 4-5)
# ===========================================================================

class PaymentReceivedTests(AlertBase):

    def test_confirm_tells_pm_side_site_bd_and_ceo_not_the_confirmer(self):
        receipt = self.record(self.pm, self.m2, amount='75000.40')
        self.sent(confirm_receipt, self.fin.user, receipt)
        self.assertEqual(self.recipients(PAYMENT_RECEIVED),
                         self.names(self.pm, self.coord, self.bd, self.ceo))
        n = self.in_app(PAYMENT_RECEIVED)['rf_ceo']
        self.assertEqual(n.link, self.card_link())
        day = f'{self.today.day} {self.today:%b %Y}'
        self.assertEqual(n.message, f'Payment received: {format_rupees(Decimal("75000.40"))} '
                                    f'on {self.project.project_id} M2, received {day}, '
                                    f'confirmed by {display_name(self.fin)}.')

    def test_inactive_bd_and_coordinator_left_out(self):
        receipt = self.record(self.pm, self.m2)
        self.deactivate(self.bd)
        self.sent(confirm_receipt, self.fin.user, receipt)
        self.assertEqual(self.recipients(PAYMENT_RECEIVED),
                         self.names(self.pm, self.coord, self.ceo))

    def test_no_bd_set_no_bd_line_no_error(self):
        Project.objects.filter(pk=self.project.pk).update(bd=None)
        receipt = self.record(self.pm, self.m2)
        self.sent(confirm_receipt, self.fin.user, self.fresh(receipt))
        self.assertEqual(self.recipients(PAYMENT_RECEIVED),
                         self.names(self.pm, self.coord, self.ceo))

    def test_pm_recorder_told_once(self):
        receipt = self.record(self.pm, self.m2)
        self.sent(confirm_receipt, self.fin.user, receipt)
        self.assertEqual(self.recipients(PAYMENT_RECEIVED).count('rf_pm'), 1)

    def test_one_alert_per_confirmed_receipt(self):
        a = self.record(self.pm, self.m2)
        b = self.record(self.pm, self.m3)
        self.sent(confirm_receipt, self.fin.user, a)
        self.sent(confirm_receipt, self.fin.user, b)
        self.assertEqual(NotificationLog.objects.filter(
            event_code=PAYMENT_RECEIVED, channel='in_app', recipient=self.ceo).count(), 2)

    def test_undone_adds_the_recorder_and_leaves_out_the_undoer(self):
        receipt = self.record(self.fin_c, self.m2)
        confirm_receipt(self.fin.user, receipt)
        self.sent(unconfirm_receipt, self.fin_b.user, self.fresh(receipt), 'Bank reversed')
        self.assertEqual(self.recipients(CONFIRMATION_UNDONE),
                         self.names(self.pm, self.coord, self.bd, self.ceo, self.fin_c))
        n = self.in_app(CONFIRMATION_UNDONE)['rf_fin_c']
        self.assertEqual(n.link, self.card_link())
        self.assertTrue(n.message.endswith(f'undone by {display_name(self.fin_b)}: Bank reversed'))

    def test_undone_by_the_recorder_excludes_them(self):
        receipt = self.record(self.fin, self.m2)
        confirm_receipt(self.fin_b.user, receipt)
        self.sent(unconfirm_receipt, self.fin.user, self.fresh(receipt), 'Wrong row')
        self.assertNotIn('rf_fin', self.recipients(CONFIRMATION_UNDONE))

    def test_bd_recorder_on_m1_told_once_on_undo(self):
        receipt = self.record(self.bd, self.m1)
        confirm_receipt(self.fin.user, receipt)
        self.sent(unconfirm_receipt, self.fin.user, self.fresh(receipt), 'Bounced')
        self.assertEqual(self.recipients(CONFIRMATION_UNDONE).count('rf_bd'), 1)

    def test_undone_with_inactive_recorder_has_no_substitute(self):
        receipt = self.record(self.bd, self.m1)
        confirm_receipt(self.fin.user, receipt)
        self.deactivate(self.bd)
        self.sent(unconfirm_receipt, self.fin.user, self.fresh(receipt), 'Bounced')
        self.assertEqual(self.recipients(CONFIRMATION_UNDONE),
                         self.names(self.pm, self.coord, self.ceo))
        for n in self.in_app(CONFIRMATION_UNDONE).values():
            self.assertNotIn('Sent to you because', n.message)

    def test_answer_sends_nothing(self):
        receipt = self.record(self.pm, self.m2)
        query_receipt(self.fin.user, receipt, 'Q')
        before = self.rows()
        self.sent(answer_receipt_query, self.pm.user, self.fresh(receipt), 'HDFC')
        self.assertEqual(self.rows(), before)


# ===========================================================================
# 5. Refused and rolled-back actions send nothing
# ===========================================================================

class NothingSentTests(AlertBase):

    def assert_refused_sends_nothing(self, fn, *args):
        before = self.rows()
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaises(ReceiptRefused):
                fn(*args)
        self.assertEqual(callbacks, [])
        self.assertEqual(self.rows(), before)

    def test_permission_refusals(self):
        receipt = self.record(self.fin, self.m2)
        self.assert_refused_sends_nothing(self.record, self.se, self.m1)
        self.assert_refused_sends_nothing(query_receipt, self.fin.user, receipt, 'own')
        self.assert_refused_sends_nothing(confirm_receipt, self.fin.user, receipt)
        self.assert_refused_sends_nothing(reject_receipt, self.pm.user, receipt, 'x')

    def test_validation_refusals(self):
        receipt = self.record(self.pm, self.m2)
        self.assert_refused_sends_nothing(self.record, self.pm, self.m1, '0')
        self.assert_refused_sends_nothing(query_receipt, self.fin.user, receipt, '  ')
        self.assert_refused_sends_nothing(reject_receipt, self.fin.user, receipt, '')
        confirm_receipt(self.fin.user, receipt)
        self.assert_refused_sends_nothing(unconfirm_receipt, self.fin.user,
                                          self.fresh(receipt), '')

    def test_over_the_agreed_amount_without_a_note(self):
        PaymentMilestone.objects.filter(pk=self.m2.pk).update(amount=Decimal('100.00'))
        receipt = self.record(self.pm, self.m2, amount='500')
        self.assert_refused_sends_nothing(confirm_receipt, self.fin.user, receipt)

    def test_stale_state(self):
        receipt = self.record(self.pm, self.m2)
        confirm_receipt(self.fin.user, receipt)     # `receipt` in memory still says recorded
        self.assert_refused_sends_nothing(confirm_receipt, self.fin_b.user, receipt)
        self.assert_refused_sends_nothing(reject_receipt, self.fin_b.user, receipt, 'x')

    def test_rolled_back_outer_transaction_sends_nothing(self):
        receipt = self.record(self.pm, self.m2)
        before = self.rows()
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaises(Rollback):
                with transaction.atomic():
                    self.record(self.pm, self.m3)
                    confirm_receipt(self.fin.user, receipt)
                    raise Rollback
        self.assertEqual(callbacks, [])
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.fresh(receipt).status, PaymentReceipt.RECORDED)

    def test_committed_action_schedules_exactly_one_callback(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            self.record(self.pm, self.m2)
        self.assertEqual(len(callbacks), 1)

    def test_an_alert_fault_never_undoes_the_action(self):
        before = self.rows()
        with mock.patch.object(receipt_alerts, 'active_finance_users',
                               side_effect=RuntimeError('boom')):
            with self.assertLogs('projects.receipt_alerts', level='ERROR'):
                receipt = self.sent(self.record, self.pm, self.m2)
        self.assertEqual(self.fresh(receipt).status, PaymentReceipt.RECORDED)
        self.assertEqual(self.rows(), before)


# ===========================================================================
# 6. Test sites and Cancelled sites (G5, FR28)
# ===========================================================================

class SiteScopeTests(AlertBase):

    def test_test_site_writes_no_notification_and_no_log_row(self):
        Project.objects.filter(pk=self.project.pk).update(is_test=True)
        self.project.refresh_from_db()
        m1, m2, _ = self.milestones(self.project)
        before = self.rows()
        a = self.sent(self.record, self.bd, m1)
        self.sent(query_receipt, self.fin.user, a, 'Q')
        self.sent(answer_receipt_query, self.bd.user, self.fresh(a), 'A')
        self.sent(confirm_receipt, self.fin.user, self.fresh(a))
        self.sent(unconfirm_receipt, self.fin.user, self.fresh(a), 'Undo')
        b = self.sent(self.record, self.pm, m2)
        self.sent(reject_receipt, self.fin.user, b, 'R')
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.fresh(b).status, PaymentReceipt.REJECTED)

    def test_cancelled_site_still_sends(self):
        a = self.record(self.pm, self.m2)
        b = self.record(self.pm, self.m3)
        Project.objects.filter(pk=self.project.pk).update(status='Cancelled')
        self.sent(confirm_receipt, self.fin.user, self.fresh(a))
        self.sent(reject_receipt, self.fin.user, self.fresh(b), 'Wrong')
        self.assertEqual(self.recipients(PAYMENT_RECEIVED),
                         self.names(self.pm, self.coord, self.bd, self.ceo))
        self.assertEqual(self.recipients(REJECTED), ['rf_pm'])


# ===========================================================================
# 7. The old milestone "Payment received" is never sent by a receipt action
# ===========================================================================

class OldEventTests(AlertBase):

    def test_no_receipt_action_sends_payment_milestone_reached(self):
        a = self.sent(self.record, self.bd, self.m1)
        self.sent(query_receipt, self.fin.user, a, 'Q')
        self.sent(answer_receipt_query, self.bd.user, self.fresh(a), 'A')
        self.sent(confirm_receipt, self.fin.user, self.fresh(a))
        self.sent(unconfirm_receipt, self.fin_b.user, self.fresh(a), 'U')
        b = self.sent(self.record, self.pm, self.m2)
        self.sent(reject_receipt, self.fin.user, b, 'R')
        self.assertFalse(NotificationLog.objects.filter(event_code=OLD_EVENT).exists())
        self.assertEqual(set(NotificationLog.objects.values_list('event_code', flat=True)),
                         set(EVENTS))
        self.assertEqual(self.m1.__class__.objects.get(pk=self.m1.pk).status, 'Pending')


# ===========================================================================
# 8. The emails, rendered from the code defaults (sent nowhere)
# ===========================================================================

class RenderedEmailTests(AlertBase):

    def render(self, code, values, link):
        return render_email(get_event(code), None, dict(values, recipient_name='Asha'), link)

    def test_every_event_renders_with_its_values(self):
        common = {'site_code': 'HRP-RES-2026-037', 'milestone': 'M1', 'amount': '₹50,000',
                  'date_received': '12 Nov 2026'}
        cases = {
            TO_CONFIRM: {'recorded_by_name': 'Ravi', 'record_note_line': ''},
            QUERIED: {'queried_by_name': 'Priya', 'reason': 'Which bank?',
                      'fallback_note': RECORDER_BLANK},
            REJECTED: {'rejected_by_name': 'Priya', 'reason': 'Not in statement',
                       'fallback_note': ''},
            PAYMENT_RECEIVED: {'confirmed_by_name': 'Priya',
                               'over_note_line': 'Confirmed over the agreed amount: GST'},
            CONFIRMATION_UNDONE: {'undone_by_name': 'Priya', 'note': 'Bounced'},
        }
        for code, extra in cases.items():
            with self.subTest(event=code):
                subject, text, html = self.render(code, dict(common, **extra), '/x/')
                self.assertIn('HRP-RES-2026-037 M1 (₹50,000)', subject)
                self.assertIn('12 Nov 2026', text)
                self.assertIn('Hello Asha', text)
                for value in extra.values():
                    if value:
                        self.assertIn(value, text)
                self.assertNotIn('{{', text + html)

    def test_blank_optional_line_leaves_no_paragraph(self):
        values = {'site_code': 'S', 'milestone': 'M1', 'amount': '₹1', 'date_received': 'd',
                  'recorded_by_name': 'R', 'record_note_line': ''}
        _, text, _ = self.render(TO_CONFIRM, values, '/x/')
        self.assertNotIn('\n\n\n', text)
        self.assertNotIn("Recorder's note", text)


# ===========================================================================
# 9. G9 — the over-the-amount sentence shows once on the receipt page
# ===========================================================================

@LIVE
class OverSentenceOnceTests(AlertBase):

    def setUp(self):
        super().setUp()
        PaymentMilestone.objects.filter(pk=self.m2.pk).update(amount=Decimal('100.00'))
        self.receipt = self.record(self.pm, self.m2, amount='500')
        self.over = ('Confirming this receipt would bring confirmed money on M2 to ₹500, '
                     'more than its agreed amount of ₹100. Add a note saying why to confirm '
                     'it anyway.')

    def page(self, response):
        return response.content.decode()

    def test_refused_confirm_shows_the_sentence_once(self):
        url = reverse('receipt_confirm', args=[self.project.project_id, self.receipt.pk])
        response = self.client_for(self.fin).post(url, {}, follow=True)
        html = self.page(response)
        self.assertEqual(html.count(self.over), 1)
        self.assertIn('id="rd-over-note"', html)   # the note box is still there
        self.assertEqual(self.fresh(self.receipt).status, PaymentReceipt.RECORDED)

    def test_plain_page_still_shows_the_sentence_in_the_form(self):
        url = reverse('receipt_detail', args=[self.project.project_id, self.receipt.pk])
        html = self.page(self.client_for(self.fin).get(url))
        self.assertEqual(html.count(self.over), 1)
        self.assertIn('alert alert-warning small mb-2', html)
