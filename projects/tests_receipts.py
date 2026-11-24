"""
FIN-M-B5-2 — client payment receipts: PaymentReceipt, PaymentReceiptQuery, the single
writer projects/receipts.py, its permission helpers and storage wrapper.

Pins: who may record (D6) and every refusal writing nothing; the D9 input rules,
including the IST "not after today" edge; FR24 (no agreed amount) and FR20 (overpayment
warning on record, note required to confirm beyond the amount); FR11 (nobody confirms or
queries their own record, the legacy completer included); the query/answer exchange
(D7, D4, D3); rejection (Q5: an unanswered query stays, but is not "open"); legacy
completion (D2) and the database rules; one transition and one log line per write; D11
isolation from PaymentMilestone, tasks and notifications; the stale-state refusal under
the lock; the proof upload and its clean-up on refusal and rollback; the read helpers and
their fixed query counts.
"""
import datetime
from decimal import Decimal
from unittest import mock

from django.contrib import admin as django_admin
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from . import receipts as receipts_module
from .activity_codes import MONEY_ACTIVITY_CODES, NON_ACTIVITY_CODES
from .delivery_storage import DeliveryStorageError
from .models import (
    ActivityLog, Notification, NotificationLog, PaymentMilestone, PaymentReceipt,
    PaymentReceiptQuery, Project, ProjectPhase, StatusTransition, SUBJECT_PAYMENT_RECEIPT,
    SUBJECT_TYPE_CHOICES, Task,
)
from .permissions import (
    RECEIPT_BD_MILESTONES, user_can_answer_receipt_query, user_can_record_receipt,
)
from .receipt_storage import (
    RECEIPT_PROOF_PATH_PREFIX, RECEIPT_STORAGE_OFF, StoredProof, discard_receipt_proof,
    signed_receipt_proof_url, store_receipt_proof,
)
from .receipts import (
    ReceiptRefused, answer_receipt_query, complete_receipt_amount, confirm_receipt,
    open_receipt_queries, possible_duplicates, query_receipt, receipt_totals,
    record_receipt, reject_receipt,
)
from .site_dashboard import is_human_activity
from .utils import _subject_type_registry

ALL_CODES = ('receipt_recorded', 'receipt_queried', 'receipt_query_answered',
             'receipt_confirmed', 'receipt_rejected', 'receipt_amount_completed')


def _profile(username, role, *, active=True, login_active=True):
    """A post_save signal creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title(), last_name='Test')
    if not login_active:
        user.is_active = False
        user.save(update_fields=['is_active'])
    profile = user.profile
    profile.role = role
    profile.is_active = active
    profile.save()
    return profile


class ReceiptBase(TestCase):
    """Three Finance users, the administrators, a site's PM, coordinators and BD, and a
    builder for Residential sites with M1-M3."""

    def setUp(self):
        self.fin_a = _profile('rc_fin_a', 'Finance')
        self.fin_b = _profile('rc_fin_b', 'Finance')
        self.fin_c = _profile('rc_fin_c', 'Finance')
        self.ceo = _profile('rc_ceo', 'CEO')
        self.admin_p = _profile('rc_admin', 'Admin')
        self.sysadmin = _profile('rc_sysadmin', 'System Admin')
        self.se = _profile('rc_se', 'Site Engineer')
        self.pm = _profile('rc_pm', 'PM')
        self.coord = _profile('rc_coord', 'Project Coordinator')
        self.coord_off = _profile('rc_coord_off', 'Project Coordinator', active=False)
        self.coord_nologin = _profile('rc_coord_nl', 'Project Coordinator', login_active=False)
        self.bd = _profile('rc_bd', 'BD')
        self.bd_other = _profile('rc_bd_other', 'BD')
        self.project = self.site()
        self.m1, self.m2, self.m3 = self.milestones(self.project)

    def site(self, *, status='Active', project_type='Residential', amounts=(None, None, None),
             bd='default', is_deleted=False, is_test=False, name='RC Site'):
        project = Project.objects.create(
            customer_name=name, status=status, project_type=project_type,
            customer_phone='9876543210', site_address='1 Sun Road', city='Lucknow',
            state='Uttar Pradesh', dc_capacity_kw=Decimal('5.00'), assigned_pm=self.pm,
            cascade_scheduling=False, bd=self.bd if bd == 'default' else bd,
            is_deleted=is_deleted, is_test=is_test)
        project.coordinators.add(self.coord, self.coord_off, self.coord_nologin)
        for n, amount in enumerate(amounts, start=1):
            PaymentMilestone.objects.create(
                project=project, milestone_name=f'M{n}',
                amount=Decimal(amount) if amount is not None else None)
        return project

    def milestones(self, project):
        return list(PaymentMilestone.objects.filter(project=project).order_by('milestone_name'))

    def rec(self, who, milestone, amount='50000', date=None, mode=PaymentReceipt.UPI, **kw):
        """record_receipt as `who` (a profile) with sensible figures."""
        return record_receipt(who.user, milestone, amount,
                              date if date is not None else timezone.localdate(), mode, **kw)

    def receipt(self, who=None, milestone=None, **kw):
        """A recorded receipt (by the PM on M1 unless told otherwise)."""
        return self.rec(who or self.pm, milestone or self.m1, **kw).receipt

    def counts(self):
        return (PaymentReceipt.objects.count(), PaymentReceiptQuery.objects.count(),
                StatusTransition.objects.filter(subject_type=SUBJECT_PAYMENT_RECEIPT).count(),
                ActivityLog.objects.filter(action_code__in=ALL_CODES).count())

    def assertRefusedNothingWritten(self, fn, *args, **kwargs):
        before = self.counts()
        with self.assertRaises(ReceiptRefused):
            fn(*args, **kwargs)
        self.assertEqual(self.counts(), before)

    def fresh(self, receipt):
        return PaymentReceipt.objects.get(pk=receipt.pk)


# ===========================================================================
# 1. Who may record (D6)
# ===========================================================================

class RecordingPermissionTests(ReceiptBase):

    def test_finance_records_on_every_milestone(self):
        for m in (self.m1, self.m2, self.m3):
            self.assertEqual(self.rec(self.fin_a, m).receipt.status, PaymentReceipt.RECORDED)

    def test_site_bd_records_m1_only(self):
        self.assertEqual(self.rec(self.bd, self.m1).receipt.recorded_by, self.bd)
        for m in (self.m2, self.m3):
            self.assertRefusedNothingWritten(self.rec, self.bd, m)

    def test_pm_and_active_coordinator_record_every_milestone(self):
        for who in (self.pm, self.coord):
            for m in (self.m1, self.m2, self.m3):
                self.assertEqual(self.rec(who, m).receipt.recorded_by, who)

    def test_refused_people(self):
        for who in (self.coord_off, self.coord_nologin, self.bd_other, self.ceo,
                    self.admin_p, self.sysadmin, self.se):
            with self.subTest(who=who.user.username):
                self.assertRefusedNothingWritten(self.rec, who, self.m1)

    def test_administrator_listed_as_coordinator_still_refused(self):
        self.project.coordinators.add(self.admin_p)
        self.assertRefusedNothingWritten(self.rec, self.admin_p, self.m2)

    def test_no_bd_set_means_no_bd_records(self):
        # Ruling Q6: no "any active BD" fallback.
        project = self.site(bd=None, name='No BD')
        m1 = self.milestones(project)[0]
        for who in (self.bd, self.bd_other):
            self.assertRefusedNothingWritten(self.rec, who, m1)
        self.assertEqual(self.rec(self.pm, m1).receipt.status, PaymentReceipt.RECORDED)

    def test_deleted_cancelled_and_non_residential_sites_refused(self):
        for kwargs in ({'is_deleted': True}, {'status': 'Cancelled'},
                       {'project_type': 'OPEX'}):
            with self.subTest(**kwargs):
                m1 = self.milestones(self.site(**kwargs))[0]
                for who in (self.fin_a, self.pm, self.bd):
                    self.assertRefusedNothingWritten(self.rec, who, m1)

    def test_draft_on_hold_and_test_sites_allowed(self):
        for kwargs in ({'status': 'Draft'}, {'status': 'On Hold'}, {'is_test': True}):
            with self.subTest(**kwargs):
                m1 = self.milestones(self.site(**kwargs))[0]
                self.assertEqual(self.rec(self.fin_a, m1).receipt.status,
                                 PaymentReceipt.RECORDED)

    def test_bd_milestone_constant_matches_the_model(self):
        self.assertEqual(RECEIPT_BD_MILESTONES, frozenset({PaymentMilestone.M1}))


# ===========================================================================
# 2. Input rules (D9, FR3)
# ===========================================================================

class ValidationTests(ReceiptBase):

    def test_each_bad_input_refused_and_nothing_written(self):
        today = timezone.localdate()
        cases = {
            'missing amount': dict(amount=None),
            'blank amount': dict(amount='  '),
            'zero': dict(amount='0'),
            'negative': dict(amount='-5'),
            'three decimals': dict(amount='100.005'),
            'not a number': dict(amount='fifty'),
            'float': dict(amount=100.5),
            'too large': dict(amount='10000000000'),
            'missing date': dict(date=''),
            'date as text': dict(date='2026-11-01'),
            'future date': dict(date=today + datetime.timedelta(days=1)),
            'missing mode': dict(mode=''),
            'unknown mode': dict(mode='barter'),
            'other without note': dict(mode=PaymentReceipt.OTHER),
            'long reference': dict(reference='x' * 101),
        }
        for label, kwargs in cases.items():
            with self.subTest(label):
                self.assertRefusedNothingWritten(self.rec, self.fin_a, self.m1, **kwargs)

    def test_missing_date_none_refused(self):
        before = self.counts()
        with self.assertRaises(ReceiptRefused):
            record_receipt(self.fin_a.user, self.m1, '100', None, PaymentReceipt.CASH)
        self.assertEqual(self.counts(), before)

    def test_valid_figures_are_stored_clean(self):
        r = self.rec(self.fin_a, self.m1, amount='1,25,000.5', mode=PaymentReceipt.OTHER,
                     note='  Demand draft  ', reference='  DD-77 ').receipt
        r = self.fresh(r)
        self.assertEqual(r.amount, Decimal('125000.50'))
        self.assertEqual(r.note, 'Demand draft')
        self.assertEqual(r.reference, 'DD-77')
        self.assertEqual(r.source, PaymentReceipt.ENTERED)
        self.assertEqual(r.proof_path, '')

    def test_old_dates_have_no_lower_bound(self):
        r = self.rec(self.fin_a, self.m1, date=datetime.date(2021, 1, 1)).receipt
        self.assertEqual(r.date_received, datetime.date(2021, 1, 1))

    def test_ist_today_edge(self):
        # 18:00 UTC on 24 Nov = 23:30 IST on 24 Nov: 24 Nov allowed, 25 Nov refused.
        late_evening = datetime.datetime(2026, 11, 24, 18, 0, tzinfo=datetime.timezone.utc)
        with mock.patch('django.utils.timezone.now', return_value=late_evening):
            self.rec(self.fin_a, self.m1, date=datetime.date(2026, 11, 24))
            self.assertRefusedNothingWritten(self.rec, self.fin_a, self.m1,
                                             date=datetime.date(2026, 11, 25))
        # 19:00 UTC on 24 Nov = 00:30 IST on 25 Nov: the UTC date is still the 24th, but
        # the IST day is the 25th, so the 25th is allowed.
        after_midnight = datetime.datetime(2026, 11, 24, 19, 0, tzinfo=datetime.timezone.utc)
        with mock.patch('django.utils.timezone.now', return_value=after_midnight):
            self.rec(self.fin_a, self.m1, date=datetime.date(2026, 11, 25))


# ===========================================================================
# 3. No agreed amount (FR24) and overpayment (FR20, D10, Q10)
# ===========================================================================

class AgreedAmountTests(ReceiptBase):

    def test_no_agreed_amount_record_and_confirm_without_warning_or_block(self):
        self.assertIsNone(self.m1.amount)
        result = self.rec(self.pm, self.m1, amount='9999999')
        self.assertEqual(result.warnings, ())
        confirmed = confirm_receipt(self.fin_a.user, result.receipt).receipt
        self.assertEqual(confirmed.status, PaymentReceipt.CONFIRMED)
        self.assertEqual(confirmed.overpayment_note, '')

    def test_warning_when_live_receipts_exceed_the_amount(self):
        project = self.site(amounts=('100000', None, None), name='Amount')
        m1 = self.milestones(project)[0]
        self.assertEqual(self.rec(self.pm, m1, amount='60000').warnings, ())
        self.assertEqual(self.rec(self.pm, m1, amount='40000').warnings, ())   # exactly equal
        result = self.rec(self.pm, m1, amount='1')
        self.assertEqual(len(result.warnings), 1)
        self.assertIn('₹100001.00', result.warnings[0])
        self.assertEqual(result.receipt.status, PaymentReceipt.RECORDED)   # warns, records

    def test_rejected_receipts_do_not_count_toward_the_warning(self):
        project = self.site(amounts=('100000', None, None), name='Rejected')
        m1 = self.milestones(project)[0]
        first = self.rec(self.pm, m1, amount='90000').receipt
        reject_receipt(self.fin_a.user, first, 'wrong site')
        self.assertEqual(self.rec(self.pm, m1, amount='90000').warnings, ())

    def test_confirm_beyond_the_amount_needs_a_note(self):
        project = self.site(amounts=('100000', None, None), name='Confirm')
        m1 = self.milestones(project)[0]
        first = self.rec(self.pm, m1, amount='60000').receipt
        second = self.rec(self.pm, m1, amount='50000').receipt
        confirm_receipt(self.fin_a.user, first)
        self.assertRefusedNothingWritten(confirm_receipt, self.fin_a.user, second)
        self.assertRefusedNothingWritten(confirm_receipt, self.fin_a.user, second, '   ')
        done = confirm_receipt(self.fin_a.user, second, 'Client paid M2 early').receipt
        self.assertEqual(self.fresh(done).overpayment_note, 'Client paid M2 early')

    def test_confirm_exactly_to_the_amount_needs_no_note(self):
        project = self.site(amounts=('100000', None, None), name='Exact')
        m1 = self.milestones(project)[0]
        confirm_receipt(self.fin_a.user, self.rec(self.pm, m1, amount='60000').receipt)
        done = confirm_receipt(self.fin_a.user, self.rec(self.pm, m1, amount='40000').receipt)
        self.assertEqual(done.receipt.status, PaymentReceipt.CONFIRMED)


# ===========================================================================
# 4. Nobody checks their own record (FR11, D2, Q9)
# ===========================================================================

class OwnRecordTests(ReceiptBase):

    def test_finance_recorder_cannot_confirm_or_query_own_receipt(self):
        r = self.receipt(self.fin_a)
        self.assertRefusedNothingWritten(confirm_receipt, self.fin_a.user, r)
        self.assertRefusedNothingWritten(query_receipt, self.fin_a.user, r, 'why?')
        self.assertEqual(confirm_receipt(self.fin_b.user, r).receipt.confirmed_by, self.fin_b)

    def test_second_finance_user_may_query(self):
        r = self.receipt(self.fin_a)
        self.assertEqual(query_receipt(self.fin_b.user, r, 'UTR?').receipt.status,
                         PaymentReceipt.QUERIED)

    def test_finance_recorder_may_reject_own_receipt(self):
        r = self.receipt(self.fin_a)
        self.assertEqual(reject_receipt(self.fin_a.user, r, 'typed twice').receipt.status,
                         PaymentReceipt.REJECTED)

    def test_non_finance_cannot_query_confirm_or_reject(self):
        r = self.receipt(self.pm)
        for who in (self.pm, self.coord, self.bd, self.ceo, self.admin_p, self.se):
            with self.subTest(who=who.user.username):
                self.assertRefusedNothingWritten(query_receipt, who.user, r, 'x')
                self.assertRefusedNothingWritten(confirm_receipt, who.user, r)
                self.assertRefusedNothingWritten(reject_receipt, who.user, r, 'x')

    def test_inactive_finance_refused(self):
        r = self.receipt(self.pm)
        off = _profile('rc_fin_off', 'Finance', active=False)
        self.assertRefusedNothingWritten(confirm_receipt, off.user, r)
        self.assertRefusedNothingWritten(reject_receipt, off.user, r, 'x')


# ===========================================================================
# 5. Query and answer (FR19, D3, D4, D7)
# ===========================================================================

class QueryAnswerTests(ReceiptBase):

    def test_reason_and_answer_required(self):
        r = self.receipt(self.pm)
        self.assertRefusedNothingWritten(query_receipt, self.fin_a.user, r, '   ')
        query_receipt(self.fin_a.user, r, 'Which account?')
        self.assertRefusedNothingWritten(answer_receipt_query, self.pm.user, r, '')

    def test_one_open_query_at_a_time(self):
        r = self.receipt(self.pm)
        query_receipt(self.fin_a.user, r, 'First')
        self.assertRefusedNothingWritten(query_receipt, self.fin_b.user, self.fresh(r), 'Second')

    def test_database_refuses_a_second_unanswered_query(self):
        r = self.receipt(self.pm)
        query_receipt(self.fin_a.user, r, 'First')
        with self.assertRaises(IntegrityError), transaction.atomic():
            PaymentReceiptQuery.objects.create(receipt=r, reason='Second', raised_by=self.fin_b)

    def test_recorder_answers_and_figures_do_not_change(self):
        r = self.receipt(self.pm, amount='12345.67')
        query_receipt(self.fin_a.user, r, 'Which account?')
        answered = answer_receipt_query(self.pm.user, self.fresh(r), 'SBI current').receipt
        self.assertEqual(answered.status, PaymentReceipt.RECORDED)
        q = r.queries.get()
        self.assertEqual((q.answer, q.answered_by), ('SBI current', self.pm))
        self.assertIsNotNone(q.answered_at)
        r = self.fresh(r)
        self.assertEqual((r.amount, r.date_received, r.mode),
                         (Decimal('12345.67'), timezone.localdate(), PaymentReceipt.UPI))

    def test_current_pm_side_answers_when_the_recorder_has_left(self):
        r = self.receipt(self.coord)
        query_receipt(self.fin_a.user, r, 'Cheque number?')
        self.coord.is_active = False
        self.coord.save(update_fields=['is_active'])
        self.assertRefusedNothingWritten(answer_receipt_query, self.coord.user, self.fresh(r), 'x')
        self.assertEqual(answer_receipt_query(self.pm.user, self.fresh(r), '000123').receipt.status,
                         PaymentReceipt.RECORDED)

    def test_bd_cannot_answer_an_m2_query(self):
        r = self.receipt(self.pm, self.m2)
        query_receipt(self.fin_a.user, r, 'Date?')
        self.assertRefusedNothingWritten(answer_receipt_query, self.bd.user, self.fresh(r), 'x')

    def test_bd_recorder_answers_an_m1_query(self):
        r = self.receipt(self.bd, self.m1)
        query_receipt(self.fin_a.user, r, 'Date?')
        self.assertEqual(answer_receipt_query(self.bd.user, self.fresh(r), 'Yes').receipt.status,
                         PaymentReceipt.RECORDED)

    def test_the_person_who_asked_cannot_answer(self):
        r = self.receipt(self.pm)
        query_receipt(self.fin_a.user, r, 'Which bank?')
        self.assertRefusedNothingWritten(answer_receipt_query, self.fin_a.user, self.fresh(r), 'x')

    def test_queried_receipt_cannot_be_confirmed_until_answered(self):
        r = self.receipt(self.pm)
        query_receipt(self.fin_a.user, r, 'Which bank?')
        self.assertRefusedNothingWritten(confirm_receipt, self.fin_b.user, self.fresh(r))
        answer_receipt_query(self.pm.user, self.fresh(r), 'HDFC')
        self.assertEqual(confirm_receipt(self.fin_b.user, self.fresh(r)).receipt.status,
                         PaymentReceipt.CONFIRMED)

    def test_answer_refused_when_nothing_is_queried(self):
        r = self.receipt(self.pm)
        self.assertRefusedNothingWritten(answer_receipt_query, self.pm.user, r, 'x')

    def test_receipt_may_be_queried_again_after_an_answer(self):
        r = self.receipt(self.pm)
        query_receipt(self.fin_a.user, r, 'One')
        answer_receipt_query(self.pm.user, self.fresh(r), 'A')
        query_receipt(self.fin_b.user, self.fresh(r), 'Two')
        self.assertEqual(r.queries.count(), 2)


class CancelledAfterRecordingTests(ReceiptBase):
    """Ruling Q2 (B5-2 follow-up): a Cancelled site refuses NEW recordings only. A receipt
    recorded before the cancellation can still be queried, answered by its recorder,
    confirmed and rejected. Deleted sites refuse everything."""

    def cancel(self):
        Project.objects.filter(pk=self.project.pk).update(status='Cancelled')

    def test_recorder_answers_and_finance_confirms_on_a_cancelled_site(self):
        r = self.receipt(self.bd, self.m1)
        self.cancel()
        query_receipt(self.fin_a.user, self.fresh(r), 'Still with the client?')
        # The recorder's right to answer does not depend on the site being open for
        # recording (user_can_record_receipt now refuses the BD here).
        self.assertFalse(user_can_record_receipt(self.bd.user, self.fresh(r).milestone))
        self.assertEqual(answer_receipt_query(self.bd.user, self.fresh(r), 'Yes, paid').receipt.status,
                         PaymentReceipt.RECORDED)
        self.assertEqual(confirm_receipt(self.fin_b.user, self.fresh(r)).receipt.status,
                         PaymentReceipt.CONFIRMED)

    def test_reject_works_and_new_recording_is_refused_on_a_cancelled_site(self):
        r = self.receipt(self.pm, self.m2)
        self.cancel()
        self.assertRefusedNothingWritten(self.rec, self.pm, self.fresh(r).milestone)
        self.assertEqual(reject_receipt(self.fin_a.user, self.fresh(r), 'Site cancelled').receipt.status,
                         PaymentReceipt.REJECTED)

    def test_non_recorder_pm_side_cannot_answer_on_a_cancelled_site(self):
        # Answering by "anyone who may record now" (D7) follows the recording rule, so on a
        # Cancelled site only the recorder (or legacy completer) answers.
        r = self.receipt(self.coord, self.m1)
        self.cancel()
        query_receipt(self.fin_a.user, self.fresh(r), 'Which bank?')
        self.assertRefusedNothingWritten(answer_receipt_query, self.pm.user, self.fresh(r), 'x')
        self.assertEqual(answer_receipt_query(self.coord.user, self.fresh(r), 'SBI').receipt.status,
                         PaymentReceipt.RECORDED)

    def test_deleted_site_refuses_everything(self):
        r = self.receipt(self.pm, self.m1)
        queried = self.receipt(self.pm, self.m2)
        query_receipt(self.fin_a.user, queried, 'Q')
        Project.objects.filter(pk=self.project.pk).update(is_deleted=True)
        self.assertRefusedNothingWritten(query_receipt, self.fin_a.user, self.fresh(r), 'x')
        self.assertRefusedNothingWritten(confirm_receipt, self.fin_a.user, self.fresh(r))
        self.assertRefusedNothingWritten(reject_receipt, self.fin_a.user, self.fresh(r), 'x')
        self.assertRefusedNothingWritten(answer_receipt_query, self.pm.user, self.fresh(queried), 'x')


# ===========================================================================
# 6. Reject (FR19, D5, D8, Q5)
# ===========================================================================

class RejectTests(ReceiptBase):

    def test_reason_required(self):
        r = self.receipt(self.pm)
        self.assertRefusedNothingWritten(reject_receipt, self.fin_a.user, r, '')

    def test_reject_from_recorded_keeps_the_row(self):
        r = self.receipt(self.pm)
        reject_receipt(self.fin_a.user, r, 'Bounced')
        r = self.fresh(r)
        self.assertEqual((r.status, r.rejected_by, r.reject_reason),
                         (PaymentReceipt.REJECTED, self.fin_a, 'Bounced'))
        self.assertIsNotNone(r.rejected_at)

    def test_reject_from_queried_leaves_the_query_unanswered_but_not_open(self):
        r = self.receipt(self.pm)
        query_receipt(self.fin_a.user, r, 'Proof?')
        self.assertEqual(list(open_receipt_queries()), [r.queries.get()])
        reject_receipt(self.fin_b.user, self.fresh(r), 'No proof')
        q = r.queries.get()
        self.assertIsNone(q.answered_at)                    # Q5: left as it was
        self.assertEqual(q.answer, '')
        self.assertEqual(open_receipt_queries().count(), 0)  # and not counted as open
        # Nobody can answer it now.
        self.assertRefusedNothingWritten(answer_receipt_query, self.pm.user, self.fresh(r), 'x')

    def test_no_move_out_of_rejected(self):
        r = self.receipt(self.pm)
        reject_receipt(self.fin_a.user, r, 'Wrong')
        r = self.fresh(r)
        self.assertRefusedNothingWritten(confirm_receipt, self.fin_b.user, r)
        self.assertRefusedNothingWritten(query_receipt, self.fin_b.user, r, 'x')
        self.assertRefusedNothingWritten(reject_receipt, self.fin_b.user, r, 'x')
        self.assertRefusedNothingWritten(answer_receipt_query, self.pm.user, r, 'x')

    def test_no_move_out_of_confirmed(self):
        r = self.receipt(self.pm)
        confirm_receipt(self.fin_a.user, r)
        r = self.fresh(r)
        self.assertRefusedNothingWritten(confirm_receipt, self.fin_b.user, r)
        self.assertRefusedNothingWritten(query_receipt, self.fin_b.user, r, 'x')
        self.assertRefusedNothingWritten(reject_receipt, self.fin_b.user, r, 'x')
        self.assertRefusedNothingWritten(
            complete_receipt_amount, self.fin_b.user, r, '1', timezone.localdate(),
            PaymentReceipt.CASH)


# ===========================================================================
# 7. Legacy "amount needed" rows (D1, D2, FR16) and the database rules
# ===========================================================================

class LegacyAndDatabaseRuleTests(ReceiptBase):

    def legacy(self, recorded_by=None):
        """An amount_needed row as FIN-M-B5-7 will create it (made directly here)."""
        return PaymentReceipt.objects.create(
            milestone=self.m1, status=PaymentReceipt.AMOUNT_NEEDED,
            source=PaymentReceipt.MIGRATED, recorded_by=recorded_by)

    def test_finance_completes_then_another_finance_user_confirms(self):
        row = self.legacy(recorded_by=self.coord)
        result = complete_receipt_amount(self.fin_a.user, row, '75000', datetime.date(2026, 10, 3),
                                         PaymentReceipt.BANK_TRANSFER, reference='UTR9')
        r = self.fresh(result.receipt)
        self.assertEqual((r.status, r.amount, r.mode, r.reference, r.completed_by),
                         (PaymentReceipt.RECORDED, Decimal('75000.00'),
                          PaymentReceipt.BANK_TRANSFER, 'UTR9', self.fin_a))
        self.assertIsNotNone(r.completed_at)
        # Q9: the completer counts as the recorder — no query, no confirm.
        self.assertRefusedNothingWritten(confirm_receipt, self.fin_a.user, r)
        self.assertRefusedNothingWritten(query_receipt, self.fin_a.user, r, 'x')
        self.assertEqual(confirm_receipt(self.fin_b.user, r).receipt.status,
                         PaymentReceipt.CONFIRMED)

    def test_completer_may_answer_a_query(self):
        row = self.legacy()
        complete_receipt_amount(self.fin_a.user, row, '100', timezone.localdate(),
                                PaymentReceipt.CASH)
        query_receipt(self.fin_b.user, self.fresh(row), 'Branch?')
        self.assertTrue(user_can_answer_receipt_query(
            self.fin_a.user, self.fresh(row), row.queries.get()))

    def test_completion_refusals(self):
        row = self.legacy()
        for who in (self.pm, self.bd, self.ceo):
            self.assertRefusedNothingWritten(complete_receipt_amount, who.user, row, '1',
                                             timezone.localdate(), PaymentReceipt.CASH)
        self.assertRefusedNothingWritten(complete_receipt_amount, self.fin_a.user, row, '0',
                                         timezone.localdate(), PaymentReceipt.CASH)
        recorded = self.receipt(self.pm)
        self.assertRefusedNothingWritten(complete_receipt_amount, self.fin_a.user, recorded,
                                         '1', timezone.localdate(), PaymentReceipt.CASH)

    def test_amount_needed_row_cannot_be_confirmed_queried_or_rejected(self):
        row = self.legacy()
        self.assertRefusedNothingWritten(confirm_receipt, self.fin_a.user, row)
        self.assertRefusedNothingWritten(query_receipt, self.fin_a.user, row, 'x')
        self.assertRefusedNothingWritten(reject_receipt, self.fin_a.user, row, 'x')

    def _refused(self, **fields):
        base = dict(milestone=self.m1, status=PaymentReceipt.RECORDED, amount=Decimal('10'),
                    date_received=timezone.localdate(), mode=PaymentReceipt.CASH,
                    recorded_by=self.pm)
        base.update(fields)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PaymentReceipt.objects.create(**base)

    def test_database_refuses_blank_figures_outside_amount_needed(self):
        for status in (PaymentReceipt.RECORDED, PaymentReceipt.QUERIED):
            with self.subTest(status=status):
                self._refused(status=status, amount=None)
                self._refused(status=status, date_received=None)
                self._refused(status=status, mode='')

    def test_database_refuses_zero_and_negative(self):
        self._refused(amount=Decimal('0'))
        self._refused(amount=Decimal('-1'))

    def test_database_refuses_entered_row_without_recorder(self):
        self._refused(recorded_by=None)
        PaymentReceipt.objects.create(            # allowed when migrated (Q8)
            milestone=self.m1, status=PaymentReceipt.AMOUNT_NEEDED,
            source=PaymentReceipt.MIGRATED)

    def test_database_confirmed_and_rejected_rules(self):
        self._refused(status=PaymentReceipt.CONFIRMED)                     # no stamps
        self._refused(status=PaymentReceipt.CONFIRMED, confirmed_at=timezone.now())  # entered, no confirmer
        PaymentReceipt.objects.create(            # migrated confirmation: no person (FR16)
            milestone=self.m1, status=PaymentReceipt.CONFIRMED, amount=Decimal('10'),
            date_received=timezone.localdate(), mode=PaymentReceipt.CASH,
            source=PaymentReceipt.MIGRATED, confirmed_at=timezone.now())
        self._refused(status=PaymentReceipt.REJECTED, rejected_at=timezone.now(),
                      rejected_by=self.fin_a)                              # no reason

    def test_proof_path_unique_only_when_set(self):
        for _ in range(2):                         # two receipts with no proof are fine
            PaymentReceipt.objects.create(
                milestone=self.m1, status=PaymentReceipt.RECORDED, amount=Decimal('10'),
                date_received=timezone.localdate(), mode=PaymentReceipt.CASH,
                recorded_by=self.pm)
        PaymentReceipt.objects.create(
            milestone=self.m1, status=PaymentReceipt.RECORDED, amount=Decimal('10'),
            date_received=timezone.localdate(), mode=PaymentReceipt.CASH,
            recorded_by=self.pm, proof_path='receipt_proofs/1/a.pdf')
        self._refused(proof_path='receipt_proofs/1/a.pdf')

    def test_database_query_rules(self):
        r = self.receipt(self.pm)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PaymentReceiptQuery.objects.create(receipt=r, reason='', raised_by=self.fin_a)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PaymentReceiptQuery.objects.create(receipt=r, reason='x', raised_by=self.fin_a,
                                               answer='y')   # answer without answerer


# ===========================================================================
# 8. One transition and one log line per write (R-2, R-3), and the codes
# ===========================================================================

class LoggingTests(ReceiptBase):

    def ledger(self, receipt):
        return list(StatusTransition.objects.filter(
            subject_type=SUBJECT_PAYMENT_RECEIPT, subject_id=receipt.pk)
            .order_by('occurred_at', 'pk').values_list('from_status', 'to_status'))

    def logs(self, receipt):
        return list(ActivityLog.objects.filter(entity_type='PaymentReceipt',
                                               entity_id=receipt.pk)
                    .order_by('pk').values_list('action_code', flat=True))

    def test_each_writer_writes_one_transition_and_one_log_line(self):
        r = self.receipt(self.pm)
        query_receipt(self.fin_a.user, r, 'Q')
        answer_receipt_query(self.pm.user, self.fresh(r), 'A')
        confirm_receipt(self.fin_b.user, self.fresh(r))
        self.assertEqual(self.ledger(r), [('', 'recorded'), ('recorded', 'queried'),
                                          ('queried', 'recorded'), ('recorded', 'confirmed')])
        self.assertEqual(self.logs(r), ['receipt_recorded', 'receipt_queried',
                                        'receipt_query_answered', 'receipt_confirmed'])
        rows = StatusTransition.objects.filter(subject_type=SUBJECT_PAYMENT_RECEIPT,
                                               subject_id=r.pk)
        self.assertEqual({row.project_id for row in rows}, {self.project.pk})
        self.assertEqual(rows.get(to_status='queried').remark, 'Q')

        other = self.receipt(self.pm, self.m2)
        reject_receipt(self.fin_a.user, other, 'Wrong')
        self.assertEqual(self.ledger(other), [('', 'recorded'), ('recorded', 'rejected')])
        self.assertEqual(self.logs(other), ['receipt_recorded', 'receipt_rejected'])

        legacy = PaymentReceipt.objects.create(milestone=self.m3,
                                               status=PaymentReceipt.AMOUNT_NEEDED,
                                               source=PaymentReceipt.MIGRATED)
        complete_receipt_amount(self.fin_a.user, legacy, '5', timezone.localdate(),
                                PaymentReceipt.CASH)
        self.assertEqual(self.ledger(legacy), [('amount_needed', 'recorded')])
        self.assertEqual(self.logs(legacy), ['receipt_amount_completed'])

    def test_log_line_names_milestone_amount_date_mode_and_actor_never_the_proof(self):
        stored = StoredProof('bkt', 'receipt_proofs/9/x.pdf', 'Client-Sharma-bank.pdf',
                             'application/pdf', 10)
        with mock.patch.object(receipts_module, 'store_receipt_proof', return_value=stored):
            r = self.rec(self.pm, self.m1, amount='5000', date=datetime.date(2026, 11, 3),
                         proof=SimpleUploadedFile('Client-Sharma-bank.pdf', b'%PDF-1'))
        line = ActivityLog.objects.get(entity_type='PaymentReceipt',
                                       entity_id=r.receipt.pk).action
        for piece in ('M1', '₹5000.00', '03 Nov 2026', 'UPI', 'Rc_Pm Test'):
            self.assertIn(piece, line)
        self.assertNotIn('Sharma', line)

    def test_codes_are_money_and_split_by_activity(self):
        self.assertTrue(set(ALL_CODES) <= MONEY_ACTIVITY_CODES)
        self.assertTrue(is_human_activity('receipt_recorded'))
        self.assertTrue(is_human_activity('receipt_query_answered'))
        for code in ('receipt_queried', 'receipt_confirmed', 'receipt_rejected',
                     'receipt_amount_completed'):
            self.assertIn(code, NON_ACTIVITY_CODES)
            self.assertFalse(is_human_activity(code))

    def test_subject_type_registered(self):
        self.assertEqual(_subject_type_registry()[PaymentReceipt], SUBJECT_PAYMENT_RECEIPT)
        self.assertIn((SUBJECT_PAYMENT_RECEIPT, 'Payment Receipt'), SUBJECT_TYPE_CHOICES)


# ===========================================================================
# 9. Isolation from the milestone, its tasks and notifications (D11)
# ===========================================================================

class IsolationTests(ReceiptBase):

    def test_no_writer_touches_milestone_tasks_or_notifications(self):
        phase = ProjectPhase.objects.create(project=self.project, phase_name='Finance',
                                            phase_order=1)
        Task.objects.create(phase=phase, task_name='Advance Payment Confirmation',
                            task_order=1, assigned_role=Task.BD)

        def state():
            return (list(PaymentMilestone.objects.filter(project=self.project).order_by('pk')
                         .values_list('status', 'amount', 'amount_received', 'received_date',
                                      'invoice_date', 'invoice_amount', 'due_date')),
                    list(Task.objects.filter(phase__project=self.project).order_by('pk')
                         .values_list('status', 'completed_at', 'assigned_to_id')),
                    Notification.objects.count(), NotificationLog.objects.count(),
                    StatusTransition.objects.exclude(
                        subject_type=SUBJECT_PAYMENT_RECEIPT).count())

        before = state()
        r = self.receipt(self.bd)
        self.assertEqual(state(), before)
        query_receipt(self.fin_a.user, r, 'Q')
        self.assertEqual(state(), before)
        answer_receipt_query(self.bd.user, self.fresh(r), 'A')
        self.assertEqual(state(), before)
        confirm_receipt(self.fin_b.user, self.fresh(r))
        self.assertEqual(state(), before)
        other = self.receipt(self.pm, self.m2)
        reject_receipt(self.fin_a.user, other, 'R')
        self.assertEqual(state(), before)
        legacy = PaymentReceipt.objects.create(milestone=self.m3,
                                               status=PaymentReceipt.AMOUNT_NEEDED,
                                               source=PaymentReceipt.MIGRATED)
        complete_receipt_amount(self.fin_a.user, legacy, '5', timezone.localdate(),
                                PaymentReceipt.CASH)
        self.assertEqual(state(), before)


# ===========================================================================
# 10. The state is re-checked under the lock
# ===========================================================================

class StaleStateTests(ReceiptBase):

    def test_second_confirmation_from_a_stale_copy_is_refused(self):
        r = self.receipt(self.pm)
        stale = self.fresh(r)                 # read by Finance C before B confirms
        confirm_receipt(self.fin_b.user, r)
        before = self.counts()
        with self.assertRaises(ReceiptRefused) as caught:
            confirm_receipt(self.fin_c.user, stale)
        self.assertIn('changed while you were looking', str(caught.exception))
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.fresh(r).confirmed_by, self.fin_b)

    def test_reject_from_a_stale_copy_after_confirmation_is_refused(self):
        r = self.receipt(self.pm)
        stale = self.fresh(r)
        confirm_receipt(self.fin_b.user, r)
        self.assertRefusedNothingWritten(reject_receipt, self.fin_c.user, stale, 'late')

    def test_record_refused_when_the_site_is_cancelled_meanwhile(self):
        stale_m1 = PaymentMilestone.objects.select_related('project').get(pk=self.m1.pk)
        Project.objects.filter(pk=self.project.pk).update(status='Cancelled')
        self.assertRefusedNothingWritten(self.rec, self.pm, stale_m1)


# ===========================================================================
# 11. The proof file (FR17, ruling Q2)
# ===========================================================================

PDF = b'%PDF-1.4 tiny'


def _pdf(name='proof.pdf'):
    return SimpleUploadedFile(name, PDF, content_type='application/pdf')


STORED = StoredProof('delivery-bkt', 'receipt_proofs/1/abc.pdf', 'proof.pdf',
                     'application/pdf', len(PDF))


class ProofTests(ReceiptBase):

    def test_proof_fields_stored_and_nothing_discarded(self):
        with mock.patch.object(receipts_module, 'store_receipt_proof',
                               return_value=STORED) as store, \
                mock.patch.object(receipts_module, 'discard_receipt_proof') as discard:
            r = self.rec(self.pm, self.m1, proof=_pdf()).receipt
        store.assert_called_once()
        self.assertEqual(store.call_args.args[0], self.project.pk)
        discard.assert_not_called()
        r = self.fresh(r)
        self.assertEqual((r.proof_bucket, r.proof_path, r.proof_name, r.proof_content_type,
                          r.proof_size), tuple(STORED.__dict__.values()))

    def test_refused_under_the_lock_discards_the_upload(self):
        with mock.patch.object(receipts_module, 'store_receipt_proof', return_value=STORED), \
                mock.patch.object(receipts_module, 'discard_receipt_proof') as discard, \
                mock.patch.object(receipts_module, 'user_can_record_receipt',
                                  side_effect=[True, False]):
            self.assertRefusedNothingWritten(self.rec, self.pm, self.m1, proof=_pdf())
        discard.assert_called_once_with(STORED.bucket, STORED.path)

    def test_rolled_back_transaction_discards_the_upload(self):
        before = self.counts()
        with mock.patch.object(receipts_module, 'store_receipt_proof', return_value=STORED), \
                mock.patch.object(receipts_module, 'discard_receipt_proof') as discard, \
                mock.patch.object(receipts_module, 'record_transition',
                                  side_effect=RuntimeError('ledger down')):
            with self.assertRaises(RuntimeError):
                self.rec(self.pm, self.m1, proof=_pdf())
        discard.assert_called_once_with(STORED.bucket, STORED.path)
        self.assertEqual(self.counts(), before)

    def test_refused_person_never_uploads(self):
        with mock.patch.object(receipts_module, 'store_receipt_proof') as store:
            self.assertRefusedNothingWritten(self.rec, self.ceo, self.m1, proof=_pdf())
        store.assert_not_called()

    def test_storage_error_is_a_refusal(self):
        with mock.patch.object(receipts_module, 'store_receipt_proof',
                               side_effect=DeliveryStorageError('Upload failed.')):
            self.assertRefusedNothingWritten(self.rec, self.pm, self.m1, proof=_pdf())

    @override_settings(SUPABASE_DELIVERY_BUCKET='')
    def test_store_refuses_when_the_bucket_is_off(self):
        with self.assertRaises(DeliveryStorageError) as caught:
            store_receipt_proof(self.project.pk, _pdf())
        self.assertEqual(str(caught.exception), RECEIPT_STORAGE_OFF)

    @override_settings(SUPABASE_DELIVERY_BUCKET='delivery-bkt')
    def test_store_validates_and_uses_its_own_prefix(self):
        client = mock.MagicMock()
        with mock.patch('projects.receipt_storage._client', return_value=client):
            stored = store_receipt_proof(self.project.pk, _pdf('Client Name.pdf'))
            with self.assertRaises(DeliveryStorageError):
                store_receipt_proof(self.project.pk,
                                    SimpleUploadedFile('x.exe', b'MZ', content_type='x'))
        self.assertTrue(stored.path.startswith(
            f'{RECEIPT_PROOF_PATH_PREFIX}/{self.project.pk}/'))
        self.assertTrue(stored.path.endswith('.pdf'))
        self.assertNotIn('Client', stored.path)
        self.assertEqual((stored.bucket, stored.name, stored.content_type),
                         ('delivery-bkt', 'Client Name.pdf', 'application/pdf'))

    @override_settings(SUPABASE_DELIVERY_BUCKET='delivery-bkt')
    def test_discard_never_removes_a_recorded_proof(self):
        PaymentReceipt.objects.create(
            milestone=self.m1, status=PaymentReceipt.RECORDED, amount=Decimal('10'),
            date_received=timezone.localdate(), mode=PaymentReceipt.CASH,
            recorded_by=self.pm, proof_bucket='delivery-bkt', proof_path='receipt_proofs/1/k.pdf')
        client = mock.MagicMock()
        with mock.patch('projects.receipt_storage._client', return_value=client):
            self.assertFalse(discard_receipt_proof('delivery-bkt', 'receipt_proofs/1/k.pdf'))
            self.assertFalse(discard_receipt_proof('other-bkt', 'receipt_proofs/1/z.pdf'))
            self.assertTrue(discard_receipt_proof('delivery-bkt', 'receipt_proofs/1/z.pdf'))
        client.storage.from_.return_value.remove.assert_called_once_with(
            ['receipt_proofs/1/z.pdf'])

    def test_signed_link(self):
        r = self.receipt(self.pm)
        self.assertIsNone(signed_receipt_proof_url(r))
        r.proof_bucket, r.proof_path = 'b', 'receipt_proofs/1/p.pdf'
        with mock.patch('projects.receipt_storage.get_design_file_url',
                        return_value='https://signed') as sign:
            self.assertEqual(signed_receipt_proof_url(r), 'https://signed')
        sign.assert_called_once_with('b', 'receipt_proofs/1/p.pdf', expires_in=300)


# ===========================================================================
# 12. Read helpers
# ===========================================================================

class ReadHelperTests(ReceiptBase):

    def test_receipt_totals_across_states(self):
        confirmed = self.receipt(self.pm, amount='100')
        confirm_receipt(self.fin_a.user, confirmed)
        self.receipt(self.pm, amount='20')                              # recorded
        queried = self.receipt(self.pm, amount='3')
        query_receipt(self.fin_a.user, queried, 'Q')
        rejected = self.receipt(self.pm, amount='7000')
        reject_receipt(self.fin_a.user, rejected, 'R')
        PaymentReceipt.objects.create(milestone=self.m1, status=PaymentReceipt.AMOUNT_NEEDED,
                                      source=PaymentReceipt.MIGRATED)
        totals = receipt_totals([self.m1, self.m2])
        self.assertEqual(totals[self.m1.pk].confirmed, Decimal('100.00'))
        self.assertEqual(totals[self.m1.pk].unconfirmed, Decimal('23.00'))
        self.assertEqual(totals[self.m1.pk].amount_needed, 1)
        self.assertEqual(totals[self.m2.pk], receipts_module.ReceiptTotals())
        self.assertEqual(receipt_totals([]), {})

    def test_receipt_totals_is_one_query_for_1_and_20_milestones(self):
        milestones = [self.m1]
        for n in range(7):
            milestones += self.milestones(self.site(name=f'Many {n}'))
        milestones = milestones[:20]
        for m in milestones:
            self.receipt(self.fin_a, m, amount='10')
        for batch in (milestones[:1], milestones):
            with self.subTest(size=len(batch)), self.assertNumQueries(1):
                totals = receipt_totals(batch)
            self.assertEqual(len(totals), len(batch))

    def test_possible_duplicates(self):
        day = datetime.date(2026, 11, 1)
        same = self.receipt(self.pm, amount='500', date=day)
        by_ref = self.receipt(self.pm, amount='9', date=datetime.date(2026, 10, 1),
                              reference='UTR-1')
        rejected = self.receipt(self.pm, amount='500', date=day)
        reject_receipt(self.fin_a.user, rejected, 'dup')
        self.receipt(self.pm, self.m2, amount='500', date=day)          # other milestone
        found = set(possible_duplicates(self.m1, Decimal('500'), day, ' utr-1 '))
        self.assertEqual(found, {same, by_ref})
        self.assertEqual(set(possible_duplicates(self.m1, Decimal('500'), day, '')), {same})
        self.assertFalse(possible_duplicates(self.m1, Decimal('1'), day, '').exists())


# ===========================================================================
# 13. Django admin is view-only (D12)
# ===========================================================================

class AdminTests(ReceiptBase):

    def test_both_models_are_view_only(self):
        request = RequestFactory().get('/')
        request.user = User.objects.create_superuser('rc_super', 's@example.com', 'pw')
        for model in (PaymentReceipt, PaymentReceiptQuery):
            model_admin = django_admin.site._registry[model]
            with self.subTest(model=model.__name__):
                self.assertFalse(model_admin.has_add_permission(request))
                self.assertFalse(model_admin.has_change_permission(request))
                self.assertFalse(model_admin.has_delete_permission(request))
        form = django_admin.site._registry[PaymentReceipt].get_form(request, None)
        self.assertNotIn('status', form.base_fields)

    def test_record_permission_helper_needs_no_extra_query_for_finance(self):
        m1 = PaymentMilestone.objects.select_related('project').get(pk=self.m1.pk)
        user = User.objects.select_related('profile').get(pk=self.fin_a.user.pk)
        with self.assertNumQueries(0):
            self.assertTrue(user_can_record_receipt(user, m1))
