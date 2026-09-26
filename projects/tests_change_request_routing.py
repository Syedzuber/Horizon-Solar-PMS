"""
Session B2a — an SCM change request routed through the PM, and the Head's third outcome.

WHAT IS PINNED, AND THE DECISION EACH TEST CITES (Zuber, 25 Sep 2026)
----------------------------------------------------------------------
  D-1  An SCM-raised request starts WITH THE PM, who forwards or rejects it.
  D-2  The assigned PM and ACTIVE coordinators may triage; nobody else.
  D-3  A note is mandatory on forward and on PM reject.
  D-4  Any SCM user may withdraw while the request is with the PM, with a note; not after.
  D-6  A PM- or coordinator-raised request still goes straight to the Head.
  D-7  The Head's third outcome, "corrected in the BOQ", evidenced by BOQCorrection rows,
       opening no attempt, keeping released_* and the group membership.
  A1   A request with the PM blocks a group lock and suspends a QC verdict.
  A4   Six messages gained a with-PM / SCM branch; the PM path's are byte-identical.
  Q5   A site with no assigned PM and no active coordinator sends an SCM request straight
       to the Head, and says so in the message, the activity line and the request row.
  B7   Every one of the SEVEN verdicts renders in its own branch.

FIXTURES are ChangeWindowBase's: released through design_pm_approve(), groups formed and
locked through SCM's own views, corrections made through boq_correct. The deliberate
bypasses — the lock race, and a with_pm row on an in-QC attempt that no product path can
reach (A1) — say so where they happen.
"""
from decimal import Decimal
from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone

from .design_views import NO_PM_FALLBACK_TEXT, CHANGE_REQUEST_NO_PM_ACTION_CODE
from .models import (
    ActivityLog, BOQCorrection, DesignAttempt, DesignChangeRequest, SiteGroup,
    CHANGE_REQUEST_PENDING, CHANGE_REQUEST_ACCEPTED, CHANGE_REQUEST_REJECTED,
    CHANGE_REQUEST_WITH_PM, CHANGE_REQUEST_PM_REJECTED, CHANGE_REQUEST_WITHDRAWN,
    CHANGE_REQUEST_CORRECTED, CHANGE_REQUEST_ORIGIN_PM, CHANGE_REQUEST_ORIGIN_SCM,
    DESIGN_RELEASED, QC_PENDING, SITE_GROUP_DRAFT, SITE_GROUP_LOCKED,
)
from .tests_design_change_window import ChangeWindowBase, _profile


class RoutingBase(ChangeWindowBase):

    def setUp(self):
        super().setUp()
        self.scm2 = _profile('cr_scm2', 'SCM')

    def _withdraw(self, change, profile=None, note='Raised in error — withdrawing.'):
        self._login(profile or self.scm)
        response = self.client.post(
            reverse('design_change_request_withdraw', kwargs={'pk': change.pk}),
            {'withdrawal_note': note})
        self.client.logout()
        return response

    def _correct(self, change, profile=None, note='Raised the module count to 12.'):
        self._login(profile or self.head)
        response = self.client.post(
            reverse('design_change_request_correct', kwargs={'pk': change.pk}),
            {'correction_note': note})
        self.client.logout()
        return response

    def _boq_correct(self, site, quantity, profile=None):
        """A reviewer's correction through the product view — one set_quantity POST."""
        item = site.boq.items.get()
        self._login(profile or self.head)
        self.client.post(reverse('boq_correct', kwargs={'project_id': site.project_id}),
                         {'action': 'set_quantity', 'item_id': item.pk,
                          'quantity': str(quantity)})
        self.client.logout()
        return BOQCorrection.objects.filter(boq__project=site).order_by('-pk').first()

    def _form_page(self, site, profile):
        self._login(profile)
        response = self.client.get(
            reverse('design_change_request_form', kwargs={'project_id': site.project_id}))
        self.client.logout()
        return response

    def _one(self, assignment):
        return self._requests(assignment).get()

    def _pm_less(self, code, coordinator=None):
        """A released site with no assigned PM, and only `coordinator` (or nobody)."""
        site, assignment = self._released(code, pm=None)
        site.coordinators.clear()
        if coordinator is not None:
            site.coordinators.add(coordinator)
        return site, assignment

    def _log(self, change, code):
        return ActivityLog.objects.filter(entity_type='DesignChangeRequest',
                                          entity_id=change.pk, action_code=code)


# ===========================================================================
# 1. Raise — where each request lands (D-1, D-6, Q5)
# ===========================================================================

class RaiseRoutingTests(RoutingBase):

    def test_01_scm_raise_lands_with_the_pm_and_a_pm_raise_lands_pending(self):
        scm_site, scm_a = self._released('CR-R1')
        pm_site, pm_a = self._released('CR-R2')
        self._raise(self.scm, scm_site)
        self._raise(self.pm, pm_site)
        scm_change, pm_change = self._one(scm_a), self._one(pm_a)
        self.assertEqual((scm_change.verdict, scm_change.origin),
                         (CHANGE_REQUEST_WITH_PM, CHANGE_REQUEST_ORIGIN_SCM))
        self.assertEqual((pm_change.verdict, pm_change.origin),
                         (CHANGE_REQUEST_PENDING, CHANGE_REQUEST_ORIGIN_PM))
        # The SCM raise's log line says where it went, under the ordinary raise code.
        line = self._log(scm_change, 'design_change_requested').get().action
        self.assertIn("with the site's PM", line)
        self.assertNotIn('awaiting the Design Head', line)

    def test_02_a_pm_less_site_lands_pending_and_says_so_in_three_places(self):
        site, assignment = self._pm_less('CR-R3')
        response = self._raise(self.scm, site)
        change = self._one(assignment)
        self.assertEqual((change.verdict, change.origin),
                         (CHANGE_REQUEST_PENDING, CHANGE_REQUEST_ORIGIN_SCM))
        # 1. the raise message
        message = self._messages(response)
        self.assertIn('This site has no active PM and no active coordinator, so '
                      'the request went straight to the Design Head', message)
        # 2. the activity line, under its own code so the row can find it
        line = self._log(change, CHANGE_REQUEST_NO_PM_ACTION_CODE).get().action
        self.assertIn(f'{NO_PM_FALLBACK_TEXT}, so the request went straight to the Design '
                      f'Head', line)
        self.assertFalse(self._log(change, 'design_change_requested').exists())
        # 3. the request row on change_request.html
        page = self._form_page(site, self.scm).content.decode()
        self.assertIn(f'Raised by SCM with no PM stage: {NO_PM_FALLBACK_TEXT}', page)

    def test_03_a_site_with_only_an_active_coordinator_lands_with_the_pm(self):
        site, assignment = self._pm_less('CR-R4', coordinator=self.coord)
        response = self._raise(self.scm, site)
        change = self._one(assignment)
        self.assertEqual(change.verdict, CHANGE_REQUEST_WITH_PM)
        self.assertNotIn('went straight to the Design Head', self._messages(response))
        # ...because somebody can forward it: the same list decides both.
        self._forward(change, profile=self.coord)
        change.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_PENDING)

    def test_04_an_inactive_coordinator_is_nobody_so_the_request_skips_the_pm(self):
        gone = _profile('cr_gone', 'Project Coordinator', is_active=False)
        site, assignment = self._pm_less('CR-R5', coordinator=gone)
        self._raise(self.scm, site)
        self.assertEqual(self._one(assignment).verdict, CHANGE_REQUEST_PENDING)

    def test_05_a_second_request_while_one_is_with_the_pm_is_refused_accurately(self):
        site, assignment = self._released('CR-R6')
        self._raise(self.scm, site)
        for raiser in (self.scm2, self.pm):
            with self.subTest(raiser=raiser.user.username):
                message = self._messages(self._raise(raiser, site, reason='another'))
                self.assertIn("is already with the site's PM", message)
                self.assertNotIn('with the Design Head', message)
                self.assertEqual(self._requests(assignment).count(), 1)


# ===========================================================================
# 2. The PM path's wording is byte-identical to HEAD e6da79f (A4)
# ===========================================================================

class PmPathWordingTests(RoutingBase):
    """Each expected string is copied from HEAD e6da79f, character for character — except
    where session B3b (D-18) named the kind: the raise message and log line now say
    "design change request", and the suspension sentence names no origin and adds the
    corrected outcome. Those are marked B3b below, with the e6da79f text beside them."""

    REASON = 'Client moved the array to the north shed.'

    def test_06_raise_message_log_line_and_pre_check(self):
        site, assignment = self._released('CR-W1')
        self._group('Batch W1', [site])
        response = self._raise(self.pm, site)
        # B3b: was 'CR-W1: change request raised on attempt 1 …' (e6da79f).
        self.assertEqual(
            self._messages(response),
            'CR-W1: design change request raised on attempt 1 and sent to the Design Head. '
            'No new '
            'attempt has been opened — he decides whether this becomes rework. The site '
            'stays in procurement group "Batch W1" while he decides, and the group cannot be '
            'locked until he does. It leaves the group only if he accepts.')
        change = self._one(assignment)
        # B3b: was 'PM change request raised on attempt 1 — …' (e6da79f).
        self.assertEqual(self._log(change, 'design_change_requested').get().action,
                         f'Design change request raised on attempt 1 — awaiting the Design '
                         f'Head: {self.REASON}')
        second = self._messages(self._raise(self.pm, site, reason='again'))
        self.assertEqual(
            second,
            f'CR-W1: a change request raised by cw_pm on '
            f'{timezone.localtime(change.requested_at):%d %b %Y} is already with the Design '
            f'Head. Wait for his decision before raising another.')

    def test_07_integrity_error_message_pm_path_and_scm_path(self):
        pm_site, pm_a = self._released('CR-W2')
        scm_site, scm_a = self._released('CR-W3')
        self._raise(self.pm, pm_site)
        self._raise(self.scm, scm_site)
        # THE RACE, FIXTURED: the pre-check sees nothing, so the constraint refuses.
        with patch('projects.design_views._open_change_requests',
                   side_effect=lambda attempt: DesignChangeRequest.objects.none()):
            pm_msg = self._messages(self._raise(self.pm, pm_site, reason='again'))
            scm_msg = self._messages(self._raise(self.scm2, scm_site, reason='again'))
        self.assertEqual(pm_msg, 'CR-W2: refused by the database — only one change request '
                                 'at a time may await the Design Head on an attempt.')
        self.assertEqual(scm_msg, 'CR-W3: refused by the database — only one change request '
                                  'at a time may be open on an attempt, whether it is with '
                                  'the PM or with the Design Head.')
        self.assertEqual(self._requests(pm_a).count(), 1)
        self.assertEqual(self._requests(scm_a).count(), 1)

    def test_08_already_decided_suspension_and_lock_refusals(self):
        site, assignment = self._released('CR-W4')
        group = self._group('Batch W4', [site])
        self._raise(self.pm, site)
        change = self._one(assignment)
        self.assertEqual(
            self._messages(self._lock(group)),
            '"Batch W4" cannot be locked: CR-W4 has a change request awaiting the Design '
            'Head. Wait for his decision, or remove the site from the group first.')
        self._accept(change)
        self.assertEqual(self._messages(self._accept(change)),
                         'CR-W4: that change request has already been accepted.')
        # The suspension sentence, on a pending request at gate 1.
        qc_site, qc_a = self._pre_release('CR-W5')
        self._raise(self.pm, qc_site)
        self._login(self.qc)
        response = self.client.post(reverse('design_qc_pass',
                                            kwargs={'project_id': 'CR-W5'}))
        # B3b: was "CR-W5: a PM change request on this attempt is awaiting the Design
        # Head's decision — the review is suspended until he accepts or rejects it."
        self.assertEqual(
            self._messages(response),
            "CR-W5: a change request on this attempt is awaiting the Design Head's "
            "decision — the review is suspended until he accepts it, rejects it or records "
            "it as corrected in the BOQ.")


# ===========================================================================
# 3. Forward and PM reject (D-1, D-2, D-3)
# ===========================================================================

class ForwardTests(RoutingBase):

    def _with_pm(self, code):
        site, assignment = self._released(code)
        self._raise(self.scm, site)
        return site, assignment, self._one(assignment)

    def test_09_pm_forwards_note_and_decider_stored(self):
        _, _, change = self._with_pm('CR-F1')
        self._forward(change, note='Quantities look wrong to me too.')
        change.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_PENDING)
        self.assertEqual(change.pm_note, 'Quantities look wrong to me too.')
        self.assertEqual(change.pm_decided_by, self.pm)
        self.assertIsNotNone(change.pm_decided_at)
        self.assertTrue(self._log(change, 'design_change_request_forwarded').exists())

    def test_10_coordinator_forwards(self):
        _, _, change = self._with_pm('CR-F2')
        self._forward(change, profile=self.coord)
        change.refresh_from_db()
        self.assertEqual((change.verdict, change.pm_decided_by),
                         (CHANGE_REQUEST_PENDING, self.coord))

    def test_11_forward_without_a_note_is_refused_and_writes_nothing(self):
        _, _, change = self._with_pm('CR-F3')
        response = self._forward(change, note='   ')
        self.assertIn('a note is required', self._messages(response))
        change.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_WITH_PM)
        self.assertIsNone(change.pm_decided_at)
        self.assertEqual(change.pm_note, '')
        self.assertFalse(self._log(change, 'design_change_request_forwarded').exists())

    def test_12_forward_after_the_group_was_locked_is_refused_and_writes_nothing(self):
        site, _, change = self._with_pm('CR-F4')
        group = self._group('Batch F4', [site])
        # THE RACE, FIXTURED. site_group_lock() refuses a group holding a with_pm request
        # (A1), so a lock can only land here between the two checks — written directly.
        SiteGroup.objects.filter(pk=group.pk).update(
            status=SITE_GROUP_LOCKED, locked_by=self.scm, locked_at=timezone.now())
        response = self._forward(change)
        self.assertIn('not forwarded — the BOQ was locked in procurement group "Batch F4"',
                      self._messages(response))
        change.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_WITH_PM)
        self.assertIsNone(change.pm_decided_at)
        self.assertFalse(self._log(change, 'design_change_request_forwarded').exists())
        # Not stranded: the PM can still close it.
        self._pm_reject(change)
        change.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_PM_REJECTED)

    def test_13_pm_rejects_note_stored_and_it_never_reaches_the_head(self):
        _, _, change = self._with_pm('CR-F5')
        self.assertIn('a note is required',
                      self._messages(self._pm_reject(change, note='')))
        change.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_WITH_PM)
        self._pm_reject(change, note='The BOQ matches the survey.')
        change.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_PM_REJECTED)
        self.assertEqual(change.pm_note, 'The BOQ matches the survey.')
        self.assertEqual(change.pm_decided_by, self.pm)
        self.assertIsNone(change.decided_at)
        self.assertTrue(self._log(change, 'design_change_request_pm_rejected').exists())

    def test_14_an_inactive_coordinator_may_not_forward(self):
        _, _, change = self._with_pm('CR-F6')
        self.coord.is_active = False
        self.coord.save(update_fields=['is_active'])
        self.assertEqual(self._forward(change, profile=self.coord).status_code, 403)
        change.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_WITH_PM)


# ===========================================================================
# 4. Withdraw (D-4)
# ===========================================================================

class WithdrawTests(RoutingBase):

    def test_15_scm_withdraws_while_with_the_pm(self):
        site, assignment = self._released('CR-X1')
        self._raise(self.scm, site)
        change = self._one(assignment)
        self.assertIn('a note is required', self._messages(self._withdraw(change, note='')))
        self._withdraw(change, note='Supplier confirmed the old count.')
        change.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_WITHDRAWN)
        self.assertEqual(change.withdrawn_by, self.scm)
        self.assertIsNotNone(change.withdrawn_at)
        self.assertEqual(change.withdrawal_note, 'Supplier confirmed the old count.')
        self.assertTrue(self._log(change, 'design_change_request_withdrawn').exists())

    def test_16_withdraw_after_forwarding_is_refused(self):
        site, assignment = self._released('CR-X2')
        self._raise(self.scm, site)
        change = self._one(assignment)
        self._forward(change)
        response = self._withdraw(change)
        self.assertIn('has already been forwarded to the Design Head',
                      self._messages(response))
        change.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_PENDING)
        self.assertIsNone(change.withdrawn_at)

    def test_17_a_different_scm_user_may_withdraw(self):
        site, assignment = self._released('CR-X3')
        self._raise(self.scm, site)
        change = self._one(assignment)
        self._withdraw(change, profile=self.scm2)
        change.refresh_from_db()
        self.assertEqual((change.verdict, change.withdrawn_by),
                         (CHANGE_REQUEST_WITHDRAWN, self.scm2))


# ===========================================================================
# 5. Authority — who is refused where (D-2, D-4)
# ===========================================================================

class AuthorityTests(RoutingBase):

    def test_18_designer_head_and_finance_refused_at_forward_reject_and_withdraw(self):
        site, assignment = self._released('CR-A1')
        self._raise(self.scm, site)
        change = self._one(assignment)
        for actor in (self.designer, self.head, self.finance):
            for name, call in (('forward', self._forward), ('pm_reject', self._pm_reject),
                               ('withdraw', self._withdraw)):
                with self.subTest(actor=actor.user.username, action=name):
                    self.assertEqual(call(change, profile=actor).status_code, 403)
        # SCM may withdraw but may not triage.
        self.assertEqual(self._forward(change, profile=self.scm).status_code, 403)
        self.assertEqual(self._pm_reject(change, profile=self.scm).status_code, 403)
        change.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_WITH_PM)
        self.assertIsNone(change.pm_decided_at)
        self.assertIsNone(change.withdrawn_at)

    def test_19_the_head_accepting_or_rejecting_a_with_pm_request_is_told_where_it_is(self):
        site, assignment = self._released('CR-A2')
        self._raise(self.scm, site)
        change = self._one(assignment)
        expected = ("CR-A2: that change request is still with the site's PM — it reaches "
                    "the Design Head only if the PM forwards it. Nothing was changed.")
        self.assertEqual(self._messages(self._accept(change)), expected)
        self.assertEqual(self._messages(self._reject(change)), expected)
        self.assertEqual(self._messages(self._correct(change)), expected)
        change.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_WITH_PM)
        self.assertEqual(assignment.attempts.count(), 1)
        self.assertEqual(assignment.status, DESIGN_RELEASED)


# ===========================================================================
# 6. The Head's third outcome (D-7, A2)
# ===========================================================================

class CorrectedTests(RoutingBase):

    def test_20_corrected_links_the_corrections_and_moves_nothing_else(self):
        site, assignment = self._released('CR-C1')
        group = self._group('Batch C1', [site])
        before = self._boq_correct(site, 11)          # before the raise: not evidence
        self._raise(self.scm, site)
        change = self._one(assignment)
        self._forward(change)
        released_at, released_by = assignment.released_at, assignment.released_by_id
        correction = self._boq_correct(site, 12)
        self.assertEqual(correction.quantity_after, Decimal('12'))

        self._correct(change)
        change.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_CORRECTED)
        self.assertEqual(change.corrected_by, self.head)
        self.assertIsNotNone(change.corrected_at)
        self.assertEqual(change.correction_note, 'Raised the module count to 12.')
        self.assertEqual(list(change.boq_corrections.all()), [correction])
        self.assertNotIn(before, change.boq_corrections.all())
        # No new attempt, released_* untouched, still in the group.
        self.assertEqual(assignment.attempts.count(), 1)
        self.assertEqual(assignment.current_attempt_number, 1)
        self.assertEqual(assignment.status, DESIGN_RELEASED)
        self.assertEqual((assignment.released_at, assignment.released_by_id),
                         (released_at, released_by))
        self.assertEqual(self._live(site).get().group, group)
        self.assertIsNone(change.resulting_attempt_id)
        self.assertTrue(self._log(change, 'design_change_request_corrected').exists())

        # SCM sees WHAT changed: item code, before, after, who.
        page = self._form_page(site, self.scm).content.decode()
        self.assertIn('CW-001', page)
        self.assertIn('11.00', page)
        self.assertIn('12.00', page)
        self.assertIn('cw_head', page)
        self.assertIn('matched by time only', page)

    def test_21_corrected_with_no_corrections_is_refused_and_writes_nothing(self):
        site, assignment = self._released('CR-C2')
        self._raise(self.pm, site)
        change = self._one(assignment)
        response = self._correct(change)
        self.assertIn('no BOQ correction has been made on this site since the request was '
                      'raised', self._messages(response))
        change.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_PENDING)
        self.assertIsNone(change.corrected_at)
        self.assertFalse(change.boq_corrections.exists())

    def test_22_a_correction_already_cited_by_another_request_is_not_evidence_again(self):
        site, assignment = self._released('CR-C3')
        self._raise(self.pm, site)
        first = self._one(assignment)
        self._boq_correct(site, 13)
        self._correct(first)
        first.refresh_from_db()
        self.assertEqual(first.verdict, CHANGE_REQUEST_CORRECTED)
        # A second request with no correction of its own. Its window is widened back to
        # the first request's raise (a direct write), so the first's correction falls
        # inside it by time — and is still refused, because it is already cited.
        self._raise(self.pm, site, reason='and the inverter')
        second = self._requests(assignment).get(verdict=CHANGE_REQUEST_PENDING)
        DesignChangeRequest.objects.filter(pk=second.pk).update(
            requested_at=first.requested_at)
        self.assertIn('no BOQ correction', self._messages(self._correct(second)))
        second.refresh_from_db()
        self.assertEqual(second.verdict, CHANGE_REQUEST_PENDING)

    def test_23_a_pm_raised_request_may_end_corrected_and_the_pm_may_not_record_it(self):
        site, assignment = self._released('CR-C4')
        self._raise(self.pm, site)
        change = self._one(assignment)
        self._boq_correct(site, 14)
        self.assertEqual(self._correct(change, profile=self.pm).status_code, 403)
        self.assertIn('a note is required', self._messages(self._correct(change, note='')))
        self._correct(change)
        change.refresh_from_db()
        self.assertEqual((change.verdict, change.origin),
                         (CHANGE_REQUEST_CORRECTED, CHANGE_REQUEST_ORIGIN_PM))


# ===========================================================================
# 7. A request with the PM is outstanding (A1, confirmed YES)
# ===========================================================================

class OutstandingTests(RoutingBase):

    def test_24_a_with_pm_request_blocks_the_group_lock(self):
        site, assignment = self._released('CR-O1')
        quiet, _ = self._released('CR-O2')
        group = self._group('Batch O1', [site, quiet])
        self._raise(self.scm, site)
        message = self._messages(self._lock(group))
        self.assertEqual(group.status, SITE_GROUP_DRAFT)
        self.assertIn("CR-O1 has a change request with the site's PM", message)
        self.assertNotIn('CR-O2', message)
        self._pm_reject(self._one(assignment))
        self._lock(group)
        self.assertEqual(group.status, SITE_GROUP_LOCKED)

    def test_25_a_with_pm_request_suspends_a_qc_verdict(self):
        site, assignment = self._pre_release('CR-O3')
        attempt = assignment.attempts.get()
        # UNREACHABLE THROUGH THE PRODUCT, FIXTURED ON PURPOSE: SCM raises only on a
        # released site, and nothing moves a released site back into QC while a request
        # is open. The rule is pinned anyway, because it is the rule.
        DesignChangeRequest.objects.create(
            attempt=attempt, requested_by=self.scm, reason='qty',
            verdict=CHANGE_REQUEST_WITH_PM, origin=CHANGE_REQUEST_ORIGIN_SCM)
        self._login(self.qc)
        response = self.client.post(reverse('design_qc_pass',
                                            kwargs={'project_id': 'CR-O3'}))
        self.client.logout()
        # B3b: was "an SCM change request on this attempt is with the site's PM".
        self.assertIn("a BOQ change request on this attempt is with the site's PM",
                      self._messages(response))
        attempt.refresh_from_db()
        self.assertEqual(attempt.qc_verdict, QC_PENDING)


# ===========================================================================
# 8. Seven verdicts, seven branches (B7)
# ===========================================================================

class RenderTests(RoutingBase):

    STATE_TEXT = {
        CHANGE_REQUEST_PENDING:     'Awaiting the Design Head',
        CHANGE_REQUEST_ACCEPTED:    'Accepted by the Design Head',
        CHANGE_REQUEST_REJECTED:    'Rejected by the Design Head',
        CHANGE_REQUEST_WITH_PM:     "With the site's PM",
        CHANGE_REQUEST_PM_REJECTED: 'Rejected by the PM',
        CHANGE_REQUEST_WITHDRAWN:   'Withdrawn',
        CHANGE_REQUEST_CORRECTED:   'Corrected in the BOQ',
    }

    def _in_state(self, verdict, code):
        """One site whose one request reached `verdict` through the product views."""
        site, assignment = self._released(code)
        scm_route = verdict in (CHANGE_REQUEST_WITH_PM, CHANGE_REQUEST_PM_REJECTED,
                                CHANGE_REQUEST_WITHDRAWN)
        self._raise(self.scm if scm_route else self.pm, site)
        change = self._one(assignment)
        if verdict == CHANGE_REQUEST_ACCEPTED:
            self._accept(change)
        elif verdict == CHANGE_REQUEST_REJECTED:
            self._reject(change)
        elif verdict == CHANGE_REQUEST_PM_REJECTED:
            self._pm_reject(change)
        elif verdict == CHANGE_REQUEST_WITHDRAWN:
            self._withdraw(change)
        elif verdict == CHANGE_REQUEST_CORRECTED:
            self._boq_correct(site, 15)
            self._correct(change)
        self.assertEqual(self._one(assignment).verdict, verdict)
        return site

    def test_26_each_of_the_seven_states_renders_in_its_own_branch(self):
        for n, (verdict, text) in enumerate(self.STATE_TEXT.items()):
            with self.subTest(verdict=verdict):
                site = self._in_state(verdict, f'CR-V{n}')
                page = self._form_page(site, self.pm).content.decode()
                self.assertIn(text, page)
                if verdict == CHANGE_REQUEST_PENDING:
                    self.assertIn('Awaiting the Design Head — a verdict on this attempt is '
                                  'suspended', page)
                else:
                    self.assertNotIn('Awaiting the Design Head', page)

    def test_27_the_with_pm_row_offers_each_actor_exactly_their_own_controls(self):
        site = self._in_state(CHANGE_REQUEST_WITH_PM, 'CR-V9')
        change = DesignChangeRequest.objects.get(attempt__assignment__project=site)
        forward = reverse('design_change_request_forward', kwargs={'pk': change.pk})
        withdraw = reverse('design_change_request_withdraw', kwargs={'pk': change.pk})
        pm_page = self._form_page(site, self.pm).content.decode()
        scm_page = self._form_page(site, self.scm).content.decode()
        self.assertIn(forward, pm_page)
        self.assertNotIn(withdraw, pm_page)
        self.assertIn(withdraw, scm_page)
        self.assertNotIn(forward, scm_page)
