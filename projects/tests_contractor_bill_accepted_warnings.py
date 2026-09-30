"""Contractor bills 5c (D-A59) — the warnings SCM accepted are recorded and shown.

What this file pins, and why each matters:

  * THE CHOKEPOINT STORES WHAT IT IS GIVEN. create and resubmit write `accepted_warnings`
    into the round's snapshot, in order, verbatim — it never asks bill_rules whether a
    warning is true, and it checks shape only (a known kind, a message). A round with none
    stores an empty list; a material request stores no key and refuses any.
  * THE RECORD NEVER CHANGES. The snapshot row is append-only; deciding, reassigning,
    resubmitting and withdrawing leave an earlier round's row as written; and the text does
    not follow the tasks or the other bills afterwards.
  * THE PAGES ALWAYS COMPUTE THE WARNINGS (ruling Q1). Without the tick they come back;
    with it the bill goes ahead and what holds at that POST is recorded — a ticked POST no
    longer skips the check.
  * WHO READS THEM. Everyone who sees the bill in full reads every accepted warning on the
    round card ("Raised despite:" / "Resubmitted despite:"), whatever the bill's status —
    Finance on a forwarded bill included. A viewer who is only the Site Engineer reads the
    task warnings and nothing that names another bill, the PM or the Site Engineer; the
    unfiltered list never reaches their template.
  * OLD ROUNDS DRAW NOTHING, WITH NO ERROR: no key, no snapshot, or a record that is not
    what 5c writes.
  * NO QUERY IS ADDED to the bill's page.
  * THE PAYMENTS QUEUE MARKS a bill payment whose CURRENT round was raised over warnings,
    with no warning text, for one query on a page that lists a bill and none otherwise.

Run with:
    python manage.py test projects.tests_contractor_bill_accepted_warnings --settings=solarpms.test_settings
"""
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.html import escape

from .approval_views import accepted_warnings_display
from .approvals import (
    ApprovalRefused, apply_approval_decision, reassign_approval_step,
    resubmit_approval_request, round_snapshot, withdraw_approval_request,
)
from .bill_rules import (
    WARNING_BILL_NUMBER, WARNING_KINDS, WARNING_OTHER_BILL, WARNING_PM,
    WARNING_SITE_ENGINEER, WARNING_TASK, WORK_WARNING_KINDS, bill_warnings,
    bill_warnings_tagged,
)
from .models import (
    AppendOnlyViolation, ApprovalRequest, ApprovalRoundSnapshot, ApprovalStep,
    ContractorBillDetail, StatusTransition, Task,
    APPROVAL_APPROVED, APPROVAL_PARTY_PM, APPROVAL_PARTY_SITE_ENGINEER,
    APPROVAL_STEP_APPROVED, APPROVAL_STEP_CHANGES_REQUESTED,
)
from .tests_approvals import SE_PHOTO
from .tests_bill_payment_forward import BillPayFixture
from .tests_contractor_bill_revision import RevisionFixture
from .tests_contractor_bill_screens import ScreenFixture
from .tests_contractor_bills import BillFixture, _task

TASK_WARNING = "'Foundation' is not complete — it is Not Started."
#: One warning of each kind, as bill_rules words them. Handed to the chokepoint as they
#: are: none of them needs to be true of the fixture's bill, which is the point.
FIVE = [
    (WARNING_TASK, TASK_WARNING),
    (WARNING_OTHER_BILL, "'Wiring — Block A' is also on bill CB-3 — \"Earlier bill\" (approved)."),
    (WARNING_BILL_NUMBER, 'Civil Co already has bill number CB-7 on "Earlier bill" (approved).'),
    (WARNING_SITE_ENGINEER, 'Cb_Se holds no task on HRP-0001.'),
    (WARNING_PM, 'Cb_Pm is not the assigned PM on HRP-0001.'),
]
FIVE_STORED = [{'kind': kind, 'message': message} for kind, message in FIVE]
#: What a Site Engineer must never read from FIVE: a piece of each of the other four.
NOT_FOR_THE_SITE_ENGINEER = ('CB-3', 'already has bill number', 'holds no task',
                             'is not the assigned PM')


def _accepted(approval, round_no=1):
    return round_snapshot(approval, round_no).get('accepted_warnings')


def _despite(response):
    """{round number: the messages its card draws} from a detail page's context."""
    return {r['number']: r['despite'] for r in response.context['rounds']}


# ===========================================================================
# bill_rules — the kind tag
# ===========================================================================

class TaggedWarningTests(ScreenFixture):

    def every_kind(self):
        """A bill that draws one warning of each kind (the 4a-2 screens test's setup)."""
        self.finish_foundation()
        self.foundation.refresh_from_db()
        self.raise_bill(bill=self.bill(tasks=[self.foundation], bill_number='CB-9'))
        na = _task(self.site, 'Earthing', is_not_applicable=True)
        return (self.site, [self.foundation, na], self.contractor, 'CB-9', self.se_b,
                self.pm_b)

    def test_each_warning_carries_its_functions_kind_in_the_reading_order(self):
        tagged = bill_warnings_tagged(*self.every_kind())
        self.assertEqual([kind for kind, _ in tagged],
                         [WARNING_TASK, WARNING_OTHER_BILL, WARNING_BILL_NUMBER,
                          WARNING_SITE_ENGINEER, WARNING_PM])
        by_kind = dict(tagged)
        self.assertEqual(by_kind[WARNING_TASK], "'Earthing' is marked Not Applicable.")
        self.assertIn('is also on bill CB-9', by_kind[WARNING_OTHER_BILL])
        self.assertIn('already has bill number CB-9', by_kind[WARNING_BILL_NUMBER])
        self.assertIn('holds no task on', by_kind[WARNING_SITE_ENGINEER])
        self.assertIn('is not the assigned PM on', by_kind[WARNING_PM])

    def test_the_untagged_list_is_the_same_messages_in_the_same_order(self):
        args = self.every_kind()
        self.assertEqual(bill_warnings(*args),
                         [message for _, message in bill_warnings_tagged(*args)])

    def test_unfinished_and_not_applicable_tasks_share_the_task_kind(self):
        na = _task(self.site, 'Earthing', is_not_applicable=True)
        tagged = bill_warnings_tagged(self.site, [self.foundation, na], self.contractor,
                                      'CB-9', self.se, self.pm)
        self.assertEqual(tagged, [(WARNING_TASK, TASK_WARNING),
                                  (WARNING_TASK, "'Earthing' is marked Not Applicable.")])

    def test_tagging_costs_the_same_three_queries(self):
        args = self.every_kind()
        with self.assertNumQueries(3):
            bill_warnings_tagged(*args)

    def test_the_site_engineer_reads_the_task_kind_only(self):
        self.assertEqual(WORK_WARNING_KINDS, {WARNING_TASK})
        self.assertLess(WORK_WARNING_KINDS, WARNING_KINDS)
        self.assertEqual(len(WARNING_KINDS), 5)


# ===========================================================================
# The chokepoint
# ===========================================================================

class ChokepointTests(BillFixture):

    def counts(self):
        return [model.objects.count() for model in (
            ApprovalRequest, ContractorBillDetail, ApprovalStep, ApprovalRoundSnapshot,
            StatusTransition)]

    def sent_back(self, approval):
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_SITE_ENGINEER),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.se, note='Not done.')

    def test_create_stores_them_in_order_exactly_as_given(self):
        given = list(reversed(FIVE)) + [(WARNING_TASK, '  padded, and true of nothing  ')]
        approval = self.raise_bill(accepted_warnings=given)
        self.assertEqual(_accepted(approval),
                         [{'kind': kind, 'message': message} for kind, message in given])

    def test_the_chokepoint_never_asks_bill_rules(self):
        refuse = mock.Mock(side_effect=AssertionError('the chokepoint asked bill_rules'))
        with mock.patch.multiple(
                'projects.bill_rules', bill_warnings_tagged=refuse, bill_warnings=refuse,
                incomplete_task_warnings=refuse, other_bill_warnings=refuse,
                repeated_bill_number_warnings=refuse, site_engineer_warnings=refuse,
                pm_warnings=refuse):
            approval = self.raise_bill(accepted_warnings=FIVE)
            self.sent_back(approval)
            resubmit_approval_request(approval, self.scm, 'Revised.',
                                      accepted_warnings=FIVE[:1])
        refuse.assert_not_called()
        self.assertEqual(_accepted(approval, 1), FIVE_STORED)
        self.assertEqual(_accepted(approval, 2), FIVE_STORED[:1])

    def test_a_bill_raised_over_nothing_stores_an_empty_list(self):
        approval = self.raise_bill()
        snap = round_snapshot(approval, 1)
        self.assertEqual(snap['accepted_warnings'], [])
        self.assertEqual(snap['schema'], 3)
        self.assertNotIn('accepted_warnings', snap['bill'])

    def test_a_material_request_stores_no_key_and_refuses_any(self):
        self.assertNotIn('accepted_warnings', round_snapshot(self.raise_material(), 1))
        before = self.counts()
        with self.assertRaises(ApprovalRefused) as caught:
            self.raise_material(accepted_warnings=FIVE[:1])
        self.assertIn('Only a contractor bill records accepted warnings', str(caught.exception))
        self.assertEqual(self.counts(), before)

    def test_a_malformed_record_is_refused_and_writes_nothing(self):
        cases = {
            'unknown kind':   [('amount', 'The amount looks high.')],
            'blank message':  [(WARNING_TASK, '   ')],
            'no message':     [(WARNING_TASK, None)],
            'not text':       [(WARNING_TASK, 7)],
            'not a pair':     [(WARNING_TASK, 'a', 'b')],
            'a bare string':  ['task'],
            'a dict':         [{'kind': WARNING_TASK, 'message': TASK_WARNING}],
            'a list kind':    [([WARNING_TASK], TASK_WARNING)],
            'one bad of two': [FIVE[0], ('nonsense', 'x')],
        }
        for name, given in cases.items():
            with self.subTest(name):
                before = self.counts()
                with self.assertRaises(ApprovalRefused):
                    self.raise_bill(accepted_warnings=given)
                self.assertEqual(self.counts(), before, 'a refused bill wrote something')

    def test_a_resubmit_stores_its_own_and_leaves_round_one_as_written(self):
        approval = self.raise_bill(accepted_warnings=FIVE[:2])
        round_one = list(ApprovalRoundSnapshot.objects.filter(request=approval, round=1)
                         .values())
        self.sent_back(approval)
        resubmit_approval_request(approval, self.scm, 'Revised.',
                                  accepted_warnings=FIVE[2:])
        self.assertEqual(_accepted(approval, 2), FIVE_STORED[2:])
        self.assertEqual(list(ApprovalRoundSnapshot.objects.filter(request=approval, round=1)
                              .values()), round_one)

    def test_a_resubmit_over_nothing_stores_an_empty_list(self):
        approval = self.raise_bill(accepted_warnings=FIVE)
        self.sent_back(approval)
        resubmit_approval_request(approval, self.scm, 'Revised.')
        self.assertEqual(_accepted(approval, 2), [])
        self.assertEqual(_accepted(approval, 1), FIVE_STORED)

    def test_a_refused_resubmit_writes_nothing(self):
        approval = self.raise_bill()
        self.sent_back(approval)
        before = self.counts()
        with self.assertRaises(ApprovalRefused):
            resubmit_approval_request(approval, self.scm, 'Revised.',
                                      accepted_warnings=[('nonsense', 'x')])
        self.assertEqual(self.counts(), before)
        approval.refresh_from_db()
        self.assertEqual(approval.current_round, 1)

    def test_a_material_resubmit_refuses_any(self):
        approval = self.raise_material()
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.pm, note='Which make?')
        with self.assertRaises(ApprovalRefused):
            resubmit_approval_request(approval, self.scm, 'Revised.',
                                      accepted_warnings=FIVE[:1])
        resubmit_approval_request(approval, self.scm, 'Revised.')
        self.assertNotIn('accepted_warnings', round_snapshot(approval, 2))

    def test_a_repeated_key_returns_the_bill_and_changes_nothing(self):
        key = '33333333-3333-3333-3333-333333333333'
        first = self.raise_bill(client_uuid=key, accepted_warnings=FIVE[:1])
        again = self.raise_bill(client_uuid=key, accepted_warnings=FIVE)
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(ApprovalRoundSnapshot.objects.filter(request=first).count(), 1)
        self.assertEqual(_accepted(first), FIVE_STORED[:1])


# ===========================================================================
# The record never changes
# ===========================================================================

class ImmutabilityTests(ScreenFixture):

    def rows(self, approval):
        return list(ApprovalRoundSnapshot.objects.filter(request=approval).order_by('round')
                    .values())

    def test_the_row_cannot_be_edited_or_deleted(self):
        approval = self.raise_bill(accepted_warnings=FIVE)
        row = ApprovalRoundSnapshot.objects.get(request=approval)
        row.snapshot = dict(row.snapshot, accepted_warnings=[])
        with self.assertRaises(AppendOnlyViolation):
            row.save()
        with self.assertRaises(AppendOnlyViolation):
            row.delete()
        self.assertEqual(_accepted(approval), FIVE_STORED)

    def test_no_later_action_touches_an_earlier_rounds_record(self):
        approval = self.raise_bill(accepted_warnings=FIVE)
        round_one = self.rows(approval)
        reassign_approval_step(self.step(approval, APPROVAL_PARTY_SITE_ENGINEER), self.se_b,
                               self.scm, 'On leave.')
        self.assertEqual(self.rows(approval), round_one)
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_SITE_ENGINEER),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.se_b, note='Not done.')
        self.assertEqual(self.rows(approval), round_one)
        resubmit_approval_request(approval, self.scm, 'Revised.',
                                  accepted_warnings=FIVE[:1])
        two_rounds = self.rows(approval)
        self.assertEqual(two_rounds[:1], round_one)
        withdraw_approval_request(approval, self.scm, 'Sent twice.')
        self.assertEqual(self.rows(approval), two_rounds)

    def test_the_text_does_not_follow_the_task_afterwards(self):
        """The task is finished and renamed after the bill was raised over it: the round
        still reads what SCM was told then. (The live warnings can no longer say it.)"""
        approval = self.raise_bill(accepted_warnings=[(WARNING_TASK, TASK_WARNING)])
        Task.objects.filter(pk=self.foundation.pk).update(status=Task.DONE,
                                                          task_name='Footings')
        page = self.client_for(self.scm).get(reverse('approval_detail', args=[approval.pk]))
        self.assertEqual(_despite(page), {1: [TASK_WARNING]})
        self.assertContains(page, escape(TASK_WARNING))
        self.assertEqual(_accepted(approval), [{'kind': WARNING_TASK, 'message': TASK_WARNING}])


# ===========================================================================
# The raise page
# ===========================================================================

class RaisePageTests(ScreenFixture):

    def raised(self):
        return ApprovalRequest.objects.latest('pk')

    def test_without_the_tick_the_warnings_come_back_and_nothing_is_stored(self):
        self.uploads()
        client = self.client_for(self.scm)
        response = client.post(self.step2_url(client), self.form())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, escape(TASK_WARNING))
        self.assertFalse(ApprovalRequest.objects.exists())
        self.assertFalse(ApprovalRoundSnapshot.objects.exists())

    def test_raise_anyway_records_exactly_what_the_page_showed(self):
        self.finish_foundation()
        self.raise_bill(bill=self.bill(tasks=[self.foundation], bill_number='CB-9'))
        na = _task(self.site, 'Earthing', is_not_applicable=True)
        self.uploads()
        client = self.client_for(self.scm)
        url = self.step2_url(client)
        overrides = dict(task=[str(self.foundation.pk), str(na.pk)],
                         site_engineer_assignee=str(self.se_b.pk),
                         pm_assignee=str(self.pm_b.pk))
        shown = client.post(url, self.form(**overrides))
        self.assertEqual(shown.status_code, 200)
        self.assertEqual(ApprovalRequest.objects.count(), 1)

        response = client.post(url, self.form(confirm_warnings='1', **overrides))
        self.assertEqual(response.status_code, 302)
        accepted = _accepted(self.raised())
        self.assertEqual([item['kind'] for item in accepted],
                         [WARNING_TASK, WARNING_OTHER_BILL, WARNING_BILL_NUMBER,
                          WARNING_SITE_ENGINEER, WARNING_PM])
        self.assertEqual([item['message'] for item in accepted],
                         shown.context['warnings'])
        content = shown.content.decode()
        for item in accepted:
            with self.subTest(kind=item['kind']):
                self.assertIn(escape(item['message']), content)

    def test_a_ticked_post_no_longer_skips_the_check(self):
        """Ruling Q1: a POST that arrives already ticked — no warnings screen before it —
        still has the warnings computed, and recorded."""
        self.uploads()
        client = self.client_for(self.scm)
        response = client.post(self.step2_url(client), self.form(confirm_warnings='1'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(_accepted(self.raised()),
                         [{'kind': WARNING_TASK, 'message': TASK_WARNING}])

    def nothing_to_warn_about(self, ticked):
        self.finish_foundation()
        self.uploads()
        client = self.client_for(self.scm)
        response = client.post(self.step2_url(client), self.form(confirm_warnings=ticked))
        self.assertEqual(response.status_code, 302)
        approval = self.raised()
        self.assertEqual(_accepted(approval), [])
        page = client.get(reverse('approval_detail', args=[approval.pk]))
        self.assertNotContains(page, 'Raised despite')

    def test_a_bill_with_nothing_to_warn_about_records_none(self):
        self.nothing_to_warn_about(ticked=None)

    def test_a_tick_with_nothing_to_warn_about_records_none(self):
        self.nothing_to_warn_about(ticked='1')

    def test_the_raised_bills_page_reads_raised_despite(self):
        self.uploads()
        client = self.client_for(self.scm)
        client.post(self.step2_url(client), self.form(confirm_warnings='1'))
        page = client.get(reverse('approval_detail', args=[self.raised().pk]))
        self.assertContains(page, 'Raised despite:')
        self.assertNotContains(page, 'Resubmitted despite:')
        self.assertEqual(_despite(page), {1: [TASK_WARNING]})


# ===========================================================================
# The resubmit page
# ===========================================================================

class ResubmitPageTests(RevisionFixture):
    """RevisionFixture raises the bill through the chokepoint with nothing accepted, so
    round 1 has an empty record unless a test raises its own."""

    WIRING_WARNING = "'Wiring — Block A' is not complete — it is Not Started."

    def post(self, approval=None, **overrides):
        approval = approval or self.approval
        values = {
            'revise': '1', 'title': 'Civil works bill', 'description': 'Foundation, block A.',
            'amount': '12500.00', 'bill_number': 'CB-7',
            'bill_date': ContractorBillDetail.objects.get(request=approval)
                                             .bill_date.isoformat(),
            'task': [str(self.foundation.pk), str(self.wiring.pk)],
            'assignee_site_engineer': str(self.se.pk), 'assignee_pm': str(self.pm.pk),
            'note': 'Revised as asked.'}
        values.update(overrides)
        return self.client_for(self.scm).post(
            reverse('approval_resubmit', args=[approval.pk]),
            {key: value for key, value in values.items() if value is not None})

    def test_without_the_tick_nothing_is_resubmitted_or_stored(self):
        self.sent_back_by_se()
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['warnings'], [TASK_WARNING, self.WIRING_WARNING])
        self.assertIsNone(round_snapshot(self.approval, 2))

    def test_resubmit_anyway_records_them_on_the_new_round(self):
        self.sent_back_by_se()
        self.uploads()
        response = self.post(confirm_warnings='1')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(_accepted(self.approval, 2),
                         [{'kind': WARNING_TASK, 'message': TASK_WARNING},
                          {'kind': WARNING_TASK, 'message': self.WIRING_WARNING}])
        self.assertEqual(_accepted(self.approval, 1), [])
        page = self.client_for(self.scm).get(
            reverse('approval_detail', args=[self.approval.pk]))
        self.assertContains(page, 'Resubmitted despite:')
        self.assertNotContains(page, 'Raised despite:')
        self.assertEqual(_despite(page), {2: [TASK_WARNING, self.WIRING_WARNING], 1: []})

    def test_each_round_keeps_its_own_label_and_list(self):
        approval = self.raise_bill(bill=self.bill(bill_number='CB-8'),
                                   accepted_warnings=FIVE)
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_SITE_ENGINEER),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.se, note='Not done.')
        self.uploads()
        response = self.post(approval, bill_number='CB-8', confirm_warnings='1')
        self.assertEqual(response.status_code, 302)
        page = self.client_for(self.scm).get(reverse('approval_detail', args=[approval.pk]))
        content = page.content.decode()
        self.assertLess(content.index('Resubmitted despite:'),
                        content.index('Raised despite:'))     # rounds are newest first
        despite = _despite(page)
        self.assertEqual(despite[1], [message for _, message in FIVE])
        self.assertEqual(despite[2][:2], [TASK_WARNING, self.WIRING_WARNING])

    def test_a_clean_resubmit_draws_nothing_for_its_round(self):
        approval = self.raise_bill(bill=self.bill(bill_number='CB-8'),
                                   accepted_warnings=FIVE[:1])
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_SITE_ENGINEER),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.se, note='Not done.')
        Task.objects.filter(pk__in=[self.foundation.pk, self.wiring.pk]).update(
            status=Task.DONE)
        self.uploads()
        # CB-8 is this bill's own number; self.approval (CB-7) names the same tasks.
        resubmit_approval_request(approval, self.scm, 'Revised.')
        page = self.client_for(self.scm).get(reverse('approval_detail', args=[approval.pk]))
        self.assertEqual(_despite(page), {2: [], 1: [TASK_WARNING]})
        self.assertContains(page, 'Raised despite:')
        self.assertNotContains(page, 'Resubmitted despite:')


# ===========================================================================
# Who reads them
# ===========================================================================

class ViewerTests(BillPayFixture):
    """An APPROVED bill raised over one warning of each kind, forwarded to Finance. The
    live warnings are not drawn on an approved bill, so every message on the page is the
    record's."""

    def setUp(self):
        super().setUp()
        self.despite_bill = self.approved_bill(bill=self.bill(bill_number='CB-20'),
                                               accepted_warnings=FIVE)
        self.url = reverse('approval_detail', args=[self.despite_bill.pk])
        self.detail = ContractorBillDetail.objects.get(request=self.despite_bill)
        self.form_url = reverse('approval_bill_payment', args=[self.despite_bill.pk])
        self.forwarded('5000')

    def doctor(self, approval, **changes):
        """Rewrite a stored snapshot with the queryset (which the model's save() guard
        does not cover), to stand in for a round written before 5c or by other code."""
        row = ApprovalRoundSnapshot.objects.get(request=approval, round=1)
        payload = dict(row.snapshot)
        for key, value in changes.items():
            if value is KeyError:
                payload.pop(key, None)
            else:
                payload[key] = value
        ApprovalRoundSnapshot.objects.filter(pk=row.pk).update(snapshot=payload)

    def test_every_full_reader_sees_all_five(self):
        for profile in (self.scm, self.pm, self.ceo, self.admin, self.approver, self.fin_b):
            with self.subTest(user=profile.user.username):
                page = self.client_for(profile).get(self.url)
                self.assertEqual(page.status_code, 200)
                content = page.content.decode()
                self.assertIn('Raised despite:', content)
                for _, message in FIVE:
                    self.assertIn(escape(message), content)
                self.assertEqual(_despite(page), {1: [message for _, message in FIVE]})

    def test_the_site_engineer_sees_the_task_warning_only(self):
        page = self.client_for(self.se).get(self.url)
        self.assertEqual(page.status_code, 200)
        content = page.content.decode()
        self.assertIn('Raised despite:', content)
        self.assertIn(escape(TASK_WARNING), content)
        for text in NOT_FOR_THE_SITE_ENGINEER:
            with self.subTest(text=text):
                self.assertNotIn(text, content)
        self.assertEqual(_despite(page), {1: [TASK_WARNING]})

    def test_the_unfiltered_list_never_reaches_a_template(self):
        for profile in (self.se, self.scm):
            with self.subTest(user=profile.user.username):
                page = self.client_for(profile).get(self.url)
                for row in page.context['rounds']:
                    self.assertNotIn('accepted_warnings', row['details'])

    def test_a_site_engineer_with_nothing_to_read_gets_no_heading(self):
        approval = self.approved_bill(bill=self.bill(bill_number='CB-21'),
                                      accepted_warnings=FIVE[1:])
        url = reverse('approval_detail', args=[approval.pk])
        self.assertNotContains(self.client_for(self.se).get(url), 'Raised despite')
        self.assertContains(self.client_for(self.scm).get(url), 'Raised despite:')

    def test_a_kind_nobody_listed_is_hidden_from_the_site_engineer(self):
        self.doctor(self.despite_bill,
                    accepted_warnings=[{'kind': 'rate', 'message': 'The rate looks high.'}])
        self.assertContains(self.client_for(self.scm).get(self.url), 'The rate looks high.')
        self.assertNotContains(self.client_for(self.se).get(self.url), 'Raised despite')

    def test_a_bill_raised_over_nothing_draws_nothing(self):
        for profile in (self.scm, self.se):
            with self.subTest(user=profile.user.username):
                self.assertNotContains(self.client_for(profile).get(self.page_url),
                                       'despite')

    def test_a_material_request_draws_nothing(self):
        material = self.raise_material()
        page = self.client_for(self.scm).get(reverse('approval_detail', args=[material.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'despite')
        self.assertEqual(_despite(page), {1: []})

    def test_an_old_or_odd_record_draws_nothing_and_no_error(self):
        cases = {
            'no key (before 5c)': KeyError,
            'null':               None,
            'a string':           'task',
            'a dict':             {'kind': WARNING_TASK, 'message': TASK_WARNING},
            'strings':            ['one', 'two'],
            'no message':         [{'kind': WARNING_TASK}],
            'message not text':   [{'kind': WARNING_TASK, 'message': 5}],
        }
        for name, value in cases.items():
            self.doctor(self.despite_bill, accepted_warnings=value)
            for profile in (self.scm, self.se, self.approver):
                with self.subTest(name, user=profile.user.username):
                    page = self.client_for(profile).get(self.url)
                    self.assertEqual(page.status_code, 200)
                    self.assertNotContains(page, 'despite')

    def test_the_readable_entries_of_a_mixed_record_are_drawn(self):
        self.doctor(self.despite_bill,
                    accepted_warnings=['junk', {'kind': WARNING_TASK, 'message': TASK_WARNING},
                                       {'kind': [], 'message': 'Kind is a list.'}])
        self.assertEqual(_despite(self.client_for(self.scm).get(self.url)),
                         {1: [TASK_WARNING, 'Kind is a list.']})
        self.assertEqual(_despite(self.client_for(self.se).get(self.url)),
                         {1: [TASK_WARNING]})

    def test_a_round_with_no_snapshot_draws_nothing(self):
        ApprovalRoundSnapshot.objects.filter(request=self.despite_bill).delete()
        for profile in (self.scm, self.se):
            with self.subTest(user=profile.user.username):
                page = self.client_for(profile).get(self.url)
                self.assertEqual(page.status_code, 200)
                self.assertNotContains(page, 'despite')

    def test_the_reader_alone(self):
        self.assertEqual(accepted_warnings_display(None), [])
        self.assertEqual(accepted_warnings_display({}), [])
        snapshot = {'accepted_warnings': FIVE_STORED}
        self.assertEqual(accepted_warnings_display(snapshot),
                         [message for _, message in FIVE])
        self.assertEqual(accepted_warnings_display(snapshot, full=False), [TASK_WARNING])
        with self.assertNumQueries(0):
            accepted_warnings_display(snapshot, full=False)

    def test_the_page_costs_the_same_with_and_without_a_record(self):
        plain = self.approved_bill(bill=self.bill(bill_number='CB-22'))
        recorded = self.approved_bill(bill=self.bill(bill_number='CB-23'),
                                      accepted_warnings=FIVE)

        def count(profile, approval):
            client = self.client_for(profile)
            url = reverse('approval_detail', args=[approval.pk])
            client.get(url)                                  # warm the session
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(client.get(url).status_code, 200)
            return len(ctx.captured_queries)

        for profile in (self.scm, self.pm, self.se):
            with self.subTest(user=profile.user.username):
                self.assertEqual(count(profile, recorded), count(profile, plain))


# ===========================================================================
# The payments queue marker
# ===========================================================================

class QueueMarkerTests(BillPayFixture):

    MARKER = 'Raised despite warnings'

    def forward_bill(self, approval, amount='5000'):
        self.detail = ContractorBillDetail.objects.get(request=approval)
        self.form_url = reverse('approval_bill_payment', args=[approval.pk])
        return self.forwarded(amount)

    def queue(self, profile=None):
        return self.client_for(profile or self.approver).get(reverse('payment_queue'),
                                                             {'tab': 'Residential'})

    def row(self, queue, payment):
        return next(r for r in queue.context['rows'] if r['payment'].pk == payment.pk)

    def two_rounds(self, number, first=(), second=()):
        """An approved bill of two rounds, each raised over the warnings given."""
        approval = self.raise_bill(bill=self.bill(bill_number=number),
                                   accepted_warnings=first)
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_SITE_ENGINEER),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.se, note='Not done.')
        resubmit_approval_request(approval, self.scm, 'Revised.', accepted_warnings=second)
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_SITE_ENGINEER),
                                APPROVAL_STEP_APPROVED, self.se, files=[dict(SE_PHOTO)])
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_APPROVED, self.pm)
        approval.refresh_from_db()
        self.assertEqual((approval.status, approval.current_round), (APPROVAL_APPROVED, 2))
        return approval

    def test_a_bill_raised_over_warnings_is_marked_and_links_to_the_bill(self):
        approval = self.approved_bill(bill=self.bill(bill_number='CB-30'),
                                      accepted_warnings=FIVE)
        payment = self.forward_bill(approval)
        queue = self.queue()
        self.assertTrue(self.row(queue, payment)['bill']['raised_despite'])
        bill_url = reverse('approval_detail', args=[approval.pk])
        self.assertContains(
            queue, f'<a href="{bill_url}" class="badge bg-warning text-dark '
                   f'text-decoration-none">{self.MARKER}</a>', html=True)
        # The marker says THAT, never WHAT.
        content = queue.content.decode()
        for _, message in FIVE:
            self.assertNotIn(escape(message), content)

    def test_a_bill_raised_over_nothing_is_not_marked(self):
        payment = self.forward_bill(self.approval)
        queue = self.queue()
        self.assertFalse(self.row(queue, payment)['bill']['raised_despite'])
        self.assertNotContains(queue, self.MARKER)

    def test_only_the_current_round_counts(self):
        earlier = self.forward_bill(self.two_rounds('CB-31', first=FIVE))
        later = self.forward_bill(self.two_rounds('CB-32', second=FIVE[:1]))
        queue = self.queue()
        self.assertFalse(self.row(queue, earlier)['bill']['raised_despite'])
        self.assertTrue(self.row(queue, later)['bill']['raised_despite'])
        self.assertEqual(queue.content.decode().count(self.MARKER), 1)

    def test_a_bill_written_before_5c_is_not_marked(self):
        approval = self.approved_bill(bill=self.bill(bill_number='CB-33'),
                                      accepted_warnings=FIVE)
        row = ApprovalRoundSnapshot.objects.get(request=approval)
        payload = dict(row.snapshot)
        del payload['accepted_warnings']
        ApprovalRoundSnapshot.objects.filter(pk=row.pk).update(snapshot=payload)
        payment = self.forward_bill(approval)
        self.assertFalse(self.row(self.queue(), payment)['bill']['raised_despite'])

    def snapshot_queries(self):
        client = self.client_for(self.approver)
        url = reverse('payment_queue')
        client.get(url, {'tab': 'Residential'})                  # warm the session
        with CaptureQueriesContext(connection) as ctx:
            client.get(url, {'tab': 'Residential'})
        return [q['sql'] for q in ctx.captured_queries
                if 'approvalroundsnapshot' in q['sql'].lower()]

    def test_one_query_for_any_number_of_bill_rows_and_none_without_one(self):
        self.assertEqual(self.snapshot_queries(), [])            # no bill payment listed
        self.forward_bill(self.approval, '1000')
        self.assertEqual(len(self.snapshot_queries()), 1)
        self.forward_bill(self.approval, '1500')
        self.forward_bill(self.approved_bill(bill=self.bill(bill_number='CB-34'),
                                             accepted_warnings=FIVE), '2000')
        queries = self.snapshot_queries()
        self.assertEqual(len(queries), 1)
        # Ids only: the stored JSON is never fetched for the marker.
        self.assertNotIn('"snapshot"', queries[0].lower().split(' from ')[0])
