"""Session D — `DesignAssignment` is the seventh subject type in `StatusTransition`.

WHY THIS FILE EXISTS
--------------------
Prompt 0.3 shipped the state ledger over six subject types and named the seventh it
deliberately skipped: `DesignAssignment`, "the richest workflow in the product", left out
because `DesignAssignment.status` was written in eighteen places across sixteen functions
and instrumenting it meant eighteen copies of the same call. Session C consolidated those
into `apply_design_status()`. This file pins what that consolidation bought: ONE call
site, and therefore a ledger that cannot have a hole the status writes do not have too.

THE ASSERTION THIS FILE EXISTS FOR is `LedgerFailureAbortsTheActionTests`: a ledger write
that FAILS must take the caller's whole action down with it. `record_transition()` raises
where `log_activity()` swallows, and `apply_design_status()` runs inside the caller's
`transaction.atomic()`, so the failure rolls back the status change, the `ArkaSubmission`
row and the feed line together. The alternative — a design status that moved with no
record of who moved it — is the exact defect §13 says a partly-populated ledger causes,
and it cannot be reconstructed afterwards. Everything else here is scaffolding around
that one claim.

The `IntegrityError` variant is a real DB error type raised from the real
`StatusTransition.save()` call inside the real `record_transition()` body; only the
failure itself is induced. There is no reachable input that makes that save fail on
SQLite — the suite's engine writes over-length CharFields happily — and inventing one
would test SQLite rather than the rollback.
"""
import inspect
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse

from . import design_views
from . import utils as utils_module
from .design_views import apply_design_status
from .models import (
    ActivityLog, ArkaSubmission, DesignAssignment, Program, Project,
    StatusTransition, ACTOR_ROLE_SYSTEM,
    SUBJECT_DESIGN_ASSIGNMENT, SUBJECT_TYPE_CHOICES,
    DESIGN_AWAITING_ALLOCATION, DESIGN_IN_DESIGN, DESIGN_ARKA_SUBMITTED,
    DESIGN_RELEASED,
)


def _profile(username, role, is_design_head=False, is_design_qc=False):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.is_design_head = is_design_head
    profile.is_design_qc = is_design_qc
    profile.save()
    return profile


class LedgerBase(TestCase):

    def setUp(self):
        self.head     = _profile('sd_head', 'Design', is_design_head=True)
        self.designer = _profile('sd_des',  'Design')
        self.program = Program.objects.create(
            name='Test-SessionD', program_type='OPEX', client_name='SDClient',
            status='Active', short_tender_code='SD')

    def _site(self, code, status=DESIGN_AWAITING_ALLOCATION, designer=None):
        site = Project(
            project_id=code, customer_name='SDClient', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code=code,
            dc_capacity_kw=Decimal('100.00'), status='Draft')
        site.save()
        assignment = DesignAssignment.objects.create(
            project=site, status=status, assigned_to=designer,
            survey_file_bucket='b', survey_file_path=f'{code}/survey/x.pdf')
        return site, assignment

    def _rows(self, assignment):
        return list(StatusTransition.objects
                    .filter(subject_type=SUBJECT_DESIGN_ASSIGNMENT,
                            subject_id=assignment.pk)
                    .order_by('occurred_at', 'pk'))


# ===========================================================================
# 1. Registration — the subject type exists and is DERIVED from the model
# ===========================================================================

class RegistrationTests(LedgerBase):

    def test_01_the_registry_derives_the_subject_type_from_the_model_class(self):
        registry = utils_module._subject_type_registry()
        self.assertIn(DesignAssignment, registry)
        self.assertEqual(registry[DesignAssignment], SUBJECT_DESIGN_ASSIGNMENT)

    def test_02_the_choice_is_declared_on_the_field(self):
        """A registry entry with no matching choice writes rows the field rejects."""
        self.assertIn((SUBJECT_DESIGN_ASSIGNMENT, 'Design Assignment'),
                      SUBJECT_TYPE_CHOICES)
        field = StatusTransition._meta.get_field('subject_type')
        self.assertIn(SUBJECT_DESIGN_ASSIGNMENT, dict(field.choices))
        self.assertLessEqual(len(SUBJECT_DESIGN_ASSIGNMENT), field.max_length)

    def test_03_the_project_resolver_reaches_the_owning_site(self):
        """Not the tender Program — the SITE, so a design row joins that site's timeline."""
        self.assertIn('DesignAssignment', utils_module._SUBJECT_PROJECT_RESOLVERS)
        site, assignment = self._site('SD-RES', designer=self.designer)
        self.assertEqual(
            utils_module._SUBJECT_PROJECT_RESOLVERS['DesignAssignment'](assignment),
            site)


# ===========================================================================
# 2. A real transition writes a real row
# ===========================================================================

class TransitionRowTests(LedgerBase):
    """VERIFICATION 3 — in_design -> arka_submitted produces a correct row."""

    def setUp(self):
        super().setUp()
        self.site, self.a = self._site('SD-TR', status=DESIGN_IN_DESIGN,
                                       designer=self.designer)

    def _submit_arka(self):
        self.assertTrue(self.client.login(username='sd_des', password='x'))
        return self.client.post(
            reverse('design_arka_submit', kwargs={'project_id': self.site.project_id}),
            {'capacity_kw': '95.5', 'arka_link': 'https://arka.example.com/v1'})

    def test_10_the_designers_arka_submission_writes_one_correct_row(self):
        self._submit_arka()

        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_ARKA_SUBMITTED)

        rows = self._rows(self.a)
        self.assertEqual(len(rows), 1, 'one status write, one ledger row')
        row = rows[0]
        self.assertEqual(row.subject_type, SUBJECT_DESIGN_ASSIGNMENT)
        self.assertEqual(row.subject_id,   self.a.pk)
        self.assertEqual(row.from_status,  DESIGN_IN_DESIGN)
        self.assertEqual(row.to_status,    DESIGN_ARKA_SUBMITTED)
        self.assertEqual(row.actor,        self.designer)
        self.assertEqual(row.project,      self.site,
                         'the row hangs off the OPEX SITE, not the tender')

    def test_11_the_actor_role_is_copied_not_joined(self):
        """A later role change must not rewrite what the row says they were."""
        self._submit_arka()
        row = self._rows(self.a)[0]
        self.assertEqual(row.actor_role_code, 'Design')

        self.designer.role = 'PM'
        self.designer.save(update_fields=['role'])
        row.refresh_from_db()
        self.assertEqual(row.actor_role_code, 'Design')

    def test_12_an_actorless_call_records_the_system_role(self):
        """`actor=None` is legal at the chokepoint and must not crash the ledger."""
        apply_design_status(self.a, DESIGN_ARKA_SUBMITTED, None,
                            'submitted by nobody', 'design_arka_submitted')
        row = self._rows(self.a)[0]
        self.assertIsNone(row.actor)
        self.assertEqual(row.actor_role_code, ACTOR_ROLE_SYSTEM)

    def test_13_companion_field_writes_are_not_transitions(self):
        """`new_status=None` moved nothing. A row saying so would be a lie."""
        apply_design_status(self.a, None, self.head, 'Survey file replaced',
                            'design_survey_uploaded',
                            extra_fields={'survey_file_path': 'SD-TR/survey/v2.pdf'})
        self.assertEqual(self._rows(self.a), [])
        self.a.refresh_from_db()
        self.assertEqual(self.a.survey_file_path, 'SD-TR/survey/v2.pdf')

    def test_14_rewriting_the_status_it_already_has_is_not_a_transition(self):
        """`x -> x` is a history of something that did not happen."""
        apply_design_status(self.a, DESIGN_IN_DESIGN, self.head,
                            're-stated', 'design_allocated')
        self.assertEqual(self._rows(self.a), [])

    def test_15_successive_moves_each_chain_from_the_previous_to_status(self):
        apply_design_status(self.a, DESIGN_ARKA_SUBMITTED, self.designer,
                            'arka', 'design_arka_submitted')
        apply_design_status(self.a, DESIGN_RELEASED, self.head,
                            'released', 'design_head_qc_passed')

        rows = self._rows(self.a)
        self.assertEqual([(r.from_status, r.to_status) for r in rows],
                         [(DESIGN_IN_DESIGN,      DESIGN_ARKA_SUBMITTED),
                          (DESIGN_ARKA_SUBMITTED, DESIGN_RELEASED)],
                         'the second row must start where the first ended — the '
                         'chokepoint reads from_status off the row it is about to write')


# ===========================================================================
# 3. THE POINT OF THE SESSION — a failed ledger write aborts the action
# ===========================================================================

class LedgerFailureAbortsTheActionTests(LedgerBase):
    """VERIFICATION 2. This is the class the whole session exists to produce.

    `record_transition()` raises where `log_activity()` swallows, and the chokepoint call
    is deliberately NOT wrapped in try/except. `apply_design_status()` runs inside the
    caller's `transaction.atomic()`, so the raise unwinds that block and everything the
    action did goes back with it — the status, the `ArkaSubmission` row, the feed line.

    Partial completion is the failure mode being excluded. A site sitting at
    `arka_submitted` with an Arka row and no ledger entry is indistinguishable from a
    status change nobody made, and §13 is explicit that such a gap cannot be
    reconstructed after the fact.
    """

    def setUp(self):
        super().setUp()
        self.site, self.a = self._site('SD-FAIL', status=DESIGN_IN_DESIGN,
                                       designer=self.designer)
        self.assertTrue(self.client.login(username='sd_des', password='x'))
        self.url = reverse('design_arka_submit',
                           kwargs={'project_id': self.site.project_id})
        self.post = {'capacity_kw': '95.5',
                     'arka_link': 'https://arka.example.com/v1'}

    def _assert_nothing_happened(self):
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_IN_DESIGN,
                         'the status change survived a failed ledger write — this is '
                         'exactly the untraceable move the ledger exists to prevent')
        self.assertEqual(ArkaSubmission.objects.count(), 0,
                         'the Arka row survived; the action completed partially')
        self.assertEqual(
            ActivityLog.objects.filter(entity_type='ArkaSubmission').count(), 0,
            'the feed line survived, so the feed now describes a submission that was '
            'rolled back')
        self.assertEqual(self._rows(self.a), [])

    def test_20_a_failing_ledger_write_rolls_the_whole_submission_back(self):
        """The real `record_transition()` body, failing at its real `save()`."""
        with patch.object(StatusTransition, 'save',
                          side_effect=IntegrityError('ledger unavailable')):
            with self.assertRaises(IntegrityError):
                self.client.post(self.url, self.post)

        self._assert_nothing_happened()

    def test_21_the_same_holds_when_the_helper_itself_refuses(self):
        """A `ValueError` from `record_transition()`'s own validation, not the DB.

        Patched on `design_views`, which is where the name is bound, so this pins the
        CALL SITE's behaviour: it neither catches nor degrades.
        """
        with patch('projects.design_views.record_transition',
                   side_effect=ValueError('not an instrumented subject type')):
            with self.assertRaises(ValueError):
                self.client.post(self.url, self.post)

        self._assert_nothing_happened()

    def test_22_the_call_site_carries_no_exception_handling(self):
        """Stated as source, because the two tests above pass just as well if someone
        wraps the call in `except Exception: pass` on a path they believe cannot fail."""
        source = inspect.getsource(design_views.apply_design_status)
        self.assertIn('record_transition(', source)
        self.assertNotIn('try:', source,
                         'apply_design_status() has grown a try/except. If it now wraps '
                         'the record_transition() call, a ledger failure degrades '
                         'silently and the status change commits without its row — the '
                         'defect this whole session exists to make impossible.')


# ===========================================================================
# 4. The two ledgers are separate, and both still fire
# ===========================================================================

class ActivityLogStillFiresTests(LedgerBase):
    """VERIFICATION 4 — the new ledger is ADDITIVE. The feed is not replaced by it."""

    def setUp(self):
        super().setUp()
        self.site, self.a = self._site('SD-BOTH', status=DESIGN_IN_DESIGN,
                                       designer=self.designer)

    def test_30_one_status_change_writes_both_a_feed_line_and_a_ledger_row(self):
        self.assertTrue(self.client.login(username='sd_des', password='x'))
        self.client.post(
            reverse('design_arka_submit', kwargs={'project_id': self.site.project_id}),
            {'capacity_kw': '95.5', 'arka_link': 'https://arka.example.com/v1'})

        logs = list(ActivityLog.objects.filter(
            project=self.site, action_code='design_arka_submitted'))
        self.assertEqual(len(logs), 1, 'the ActivityLog call was lost or duplicated')
        self.assertEqual(logs[0].entity_type, 'ArkaSubmission')
        self.assertEqual(logs[0].actor, self.designer)

        self.assertEqual(len(self._rows(self.a)), 1)

    def test_31_a_companion_field_write_still_logs_even_though_it_records_nothing(self):
        """The feed tracks WRITES; the ledger tracks TRANSITIONS. They are not the same
        event, and this is the call where they visibly differ."""
        apply_design_status(self.a, None, self.head, 'Survey file replaced',
                            'design_survey_uploaded',
                            extra_fields={'survey_file_path': 'SD-BOTH/v2.pdf'})

        self.assertEqual(
            ActivityLog.objects.filter(action_code='design_survey_uploaded').count(), 1)
        self.assertEqual(self._rows(self.a), [])

    def test_32_a_failing_feed_line_does_NOT_abort_the_action(self):
        """The asymmetry, asserted rather than implied (R-3).

        `log_activity()` catches its own exceptions; `record_transition()` does not. A
        lost feed line costs one row of a feed and the status change stands, WITH its
        ledger row. A lost ledger row costs the answer to "who moved this", so it takes
        the change down instead. The failure is induced at `ActivityLog.objects.create`,
        inside the real `log_activity()` body, so its real swallow is what is under test.
        """
        with patch.object(ActivityLog.objects, 'create',
                          side_effect=RuntimeError('feed unavailable')):
            apply_design_status(self.a, DESIGN_ARKA_SUBMITTED, self.designer,
                                'arka', 'design_arka_submitted')

        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_ARKA_SUBMITTED,
                         'a failed FEED write aborted the action — the two ledgers are '
                         'meant to fail differently (R-3)')
        self.assertEqual(ActivityLog.objects.count(), 0)

        rows = self._rows(self.a)
        self.assertEqual(len(rows), 1,
                         'the ledger row must still be written when only the feed failed')
        self.assertEqual(rows[0].to_status, DESIGN_ARKA_SUBMITTED)
