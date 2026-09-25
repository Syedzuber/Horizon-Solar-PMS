"""
Session B1 verification — the SCM change-request routing SCHEMA, with no behaviour.

WHY THIS FILE EXISTS
--------------------
Migration 0102 adds four verdict values (with_pm, pm_rejected, withdrawn, corrected), an
`origin` column, the PM-stage / withdrawal / correction fields, and the constraints that
keep them honest. Nothing in the product writes the new values until B2, so the only way
to prove the constraints hold is to write rows straight through the ORM — which is also
the only way to prove they hold against admin edits and imports.

The two exceptions are the raise view, which B1 teaches to write `origin` (it is a
required column), and the admin, which must not offer the new fields for editing. Both are
pinned at the bottom.

SQLITE AND CONSTRAINT NAMES. The suite runs on SQLite (solarpms.test_settings). SQLite
names a violated CHECK constraint in its error ("CHECK constraint failed: <name>"), so
those assertions check the name. It does NOT name a violated partial UNIQUE index — it
reports the column ("UNIQUE constraint failed: ...attempt_id") — so the uniqueness tests
assert the exception type there, and the name only on PostgreSQL. That is the same gap
tests_design_part46 test_02 documents and fails on.
"""
from django.contrib.admin.sites import AdminSite
from django.db import IntegrityError, connection, transaction
from django.test import RequestFactory
from django.utils import timezone

from .admin import DesignChangeRequestAdmin
from .models import (
    BOQ, BOQCorrection, DesignChangeRequest, ActivityLog,
    BOQ_CORRECTION_QUANTITY_CHANGED, DESIGN_RELEASED,
    CHANGE_REQUEST_PENDING, CHANGE_REQUEST_ACCEPTED, CHANGE_REQUEST_REJECTED,
    CHANGE_REQUEST_WITH_PM, CHANGE_REQUEST_PM_REJECTED, CHANGE_REQUEST_WITHDRAWN,
    CHANGE_REQUEST_CORRECTED, CHANGE_REQUEST_ORIGIN_PM, CHANGE_REQUEST_ORIGIN_SCM,
)
from .tests_design_part46 import Part46Base

UNIQUE_NAME = 'uniq_pending_change_request_per_attempt'


class SchemaBase(Part46Base):
    """Part 4.6's site-with-a-package fixture, plus a row factory with SCM defaults."""

    def _row(self, **fields):
        values = dict(attempt=self.attempt, requested_by=self.scm, reason='x',
                      origin=CHANGE_REQUEST_ORIGIN_SCM)
        values.update(fields)
        return DesignChangeRequest.objects.create(**values)

    def _refused(self, constraint_name=None, **fields):
        """Create the row inside a savepoint; it must be refused by the database."""
        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                self._row(**fields)
        if constraint_name:
            self.assertIn(constraint_name, str(caught.exception))
        return caught.exception

    def _withdrawal(self, **overrides):
        values = dict(verdict=CHANGE_REQUEST_WITHDRAWN, withdrawn_by=self.scm,
                      withdrawn_at=timezone.now(), withdrawal_note='no longer needed')
        values.update(overrides)
        return values

    def _correction(self, **overrides):
        values = dict(verdict=CHANGE_REQUEST_CORRECTED, corrected_by=self.head,
                      corrected_at=timezone.now(), correction_note='fixed OPX-042 in place')
        values.update(overrides)
        return values


# ===========================================================================
# Each new value can be stored
# ===========================================================================

class NewVerdictValuesTests(SchemaBase):

    def test_01_every_new_verdict_can_be_stored(self):
        now = timezone.now()
        rows = [
            self._row(verdict=CHANGE_REQUEST_WITH_PM),
            self._row(verdict=CHANGE_REQUEST_PM_REJECTED, pm_decided_by=self.pm,
                      pm_decided_at=now, pm_note='the site does not need it'),
            self._row(**self._withdrawal()),
            self._row(**self._correction()),
        ]
        stored = sorted(DesignChangeRequest.objects.filter(pk__in=[r.pk for r in rows])
                        .values_list('verdict', flat=True))
        self.assertEqual(stored, sorted([CHANGE_REQUEST_WITH_PM, CHANGE_REQUEST_PM_REJECTED,
                                         CHANGE_REQUEST_WITHDRAWN, CHANGE_REQUEST_CORRECTED]))

    def test_02_the_longest_value_fits_the_column(self):
        field = DesignChangeRequest._meta.get_field('verdict')
        self.assertEqual(field.max_length, 20)
        self.assertLessEqual(max(len(v) for v, _ in field.choices), field.max_length)

    def test_03_a_corrected_row_carries_its_evidence(self):
        boq = BOQ.objects.create(project=self.site)
        fix = BOQCorrection.objects.create(
            boq=boq, action=BOQ_CORRECTION_QUANTITY_CHANGED, corrected_by=self.head)
        row = self._row(**self._correction())
        row.boq_corrections.add(fix)
        self.assertEqual(list(row.boq_corrections.all()), [fix])


# ===========================================================================
# The one-open-request rule, widened to with_pm under the SAME name
# ===========================================================================

class OpenRequestUniquenessTests(SchemaBase):

    def _assert_unique_refusal(self, exc):
        # SQLite names the column, not the partial index; only PostgreSQL names it. See
        # the module docstring — the exception type is the assertion that holds on both.
        if connection.vendor == 'postgresql':
            self.assertIn(UNIQUE_NAME, str(exc))

    def test_01_with_pm_then_pending_on_one_attempt_is_refused(self):
        self._row(verdict=CHANGE_REQUEST_WITH_PM)
        exc = self._refused(verdict=CHANGE_REQUEST_PENDING, origin=CHANGE_REQUEST_ORIGIN_PM,
                            requested_by=self.pm)
        self._assert_unique_refusal(exc)

    def test_02_pending_then_with_pm_on_one_attempt_is_refused(self):
        self._row(verdict=CHANGE_REQUEST_PENDING, origin=CHANGE_REQUEST_ORIGIN_PM,
                  requested_by=self.pm)
        exc = self._refused(verdict=CHANGE_REQUEST_WITH_PM)
        self._assert_unique_refusal(exc)

    def test_03_two_with_pm_on_one_attempt_are_refused(self):
        self._row(verdict=CHANGE_REQUEST_WITH_PM)
        self._assert_unique_refusal(self._refused(verdict=CHANGE_REQUEST_WITH_PM))

    def test_04_with_pm_beside_accepted_is_allowed(self):
        self._row(verdict=CHANGE_REQUEST_ACCEPTED, origin=CHANGE_REQUEST_ORIGIN_PM,
                  requested_by=self.pm)
        self._row(verdict=CHANGE_REQUEST_WITH_PM)
        self.assertEqual(DesignChangeRequest.objects.filter(attempt=self.attempt).count(), 2)

    def test_05_the_constraint_kept_its_name(self):
        names = [c.name for c in DesignChangeRequest._meta.constraints]
        self.assertIn(UNIQUE_NAME, names)


# ===========================================================================
# The PM's note, keyed on pm_decided_at
# ===========================================================================

class PMDecisionNoteTests(SchemaBase):

    def test_01_a_pm_decision_with_no_note_is_refused(self):
        self._refused('cr_pm_note_required_when_pm_decided',
                      verdict=CHANGE_REQUEST_PENDING, pm_decided_by=self.pm,
                      pm_decided_at=timezone.now(), pm_note='')

    def test_02_a_forwarded_request_with_a_note_is_allowed(self):
        row = self._row(verdict=CHANGE_REQUEST_PENDING, pm_decided_by=self.pm,
                        pm_decided_at=timezone.now(), pm_note='agreed, over to Design')
        self.assertEqual(row.verdict, CHANGE_REQUEST_PENDING)


# ===========================================================================
# Withdrawn and corrected each need all three of their fields
# ===========================================================================

class WithdrawalTests(SchemaBase):

    def test_01_withdrawn_without_each_field_is_refused(self):
        for missing in ({'withdrawn_by': None}, {'withdrawn_at': None},
                        {'withdrawal_note': ''}):
            with self.subTest(missing=missing):
                self._refused('cr_withdrawal_fields_required_when_withdrawn',
                              **self._withdrawal(**missing))

    def test_02_withdrawn_with_all_three_is_allowed(self):
        self.assertEqual(self._row(**self._withdrawal()).verdict, CHANGE_REQUEST_WITHDRAWN)


class CorrectionTests(SchemaBase):

    def test_01_corrected_without_each_field_is_refused(self):
        for missing in ({'corrected_by': None}, {'corrected_at': None},
                        {'correction_note': ''}):
            with self.subTest(missing=missing):
                self._refused('cr_correction_fields_required_when_corrected',
                              **self._correction(**missing))

    def test_02_corrected_with_all_three_is_allowed(self):
        self.assertEqual(self._row(**self._correction()).verdict, CHANGE_REQUEST_CORRECTED)

    def test_03_a_pm_raised_request_may_end_corrected(self):
        row = self._row(origin=CHANGE_REQUEST_ORIGIN_PM, requested_by=self.pm,
                        **self._correction())
        self.assertEqual((row.origin, row.verdict),
                         (CHANGE_REQUEST_ORIGIN_PM, CHANGE_REQUEST_CORRECTED))


# ===========================================================================
# Origin: required, valid, and the PM path has no PM stage
# ===========================================================================

class OriginTests(SchemaBase):

    def test_01_a_pm_raised_request_may_not_hold_an_scm_only_verdict(self):
        now = timezone.now()
        cases = {
            CHANGE_REQUEST_WITH_PM: {},
            CHANGE_REQUEST_PM_REJECTED: dict(pm_decided_by=self.pm, pm_decided_at=now,
                                             pm_note='no'),
            CHANGE_REQUEST_WITHDRAWN: self._withdrawal(),
        }
        for verdict, extra in cases.items():
            with self.subTest(verdict=verdict):
                fields = dict(extra, verdict=verdict, origin=CHANGE_REQUEST_ORIGIN_PM,
                              requested_by=self.pm)
                self._refused('cr_pm_origin_excludes_scm_stages', **fields)

    def test_02_origin_has_no_model_default(self):
        self.assertFalse(DesignChangeRequest._meta.get_field('origin').has_default())

    def test_03_a_row_that_omits_origin_is_refused(self):
        # A CharField with no default stores '' rather than raising; cr_origin_valid is
        # what turns the omission into an IntegrityError.
        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                DesignChangeRequest.objects.create(
                    attempt=self.attempt, requested_by=self.pm, reason='x',
                    verdict=CHANGE_REQUEST_PENDING)
        self.assertIn('cr_origin_valid', str(caught.exception))

    def test_04_an_unknown_origin_is_refused(self):
        self._refused('cr_origin_valid', origin='ceo')


# ===========================================================================
# Pre-B1 rows behave exactly as before
# ===========================================================================

class LegacyVerdictTests(SchemaBase):

    def _pm_row(self, **fields):
        return self._row(origin=CHANGE_REQUEST_ORIGIN_PM, requested_by=self.pm, **fields)

    def test_01_two_pending_on_one_attempt_are_still_refused(self):
        self._pm_row(verdict=CHANGE_REQUEST_PENDING)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._pm_row(verdict=CHANGE_REQUEST_PENDING)

    def test_02_decided_rows_still_coexist_with_one_pending(self):
        self._pm_row(verdict=CHANGE_REQUEST_ACCEPTED)
        self._pm_row(verdict=CHANGE_REQUEST_ACCEPTED)
        self._pm_row(verdict=CHANGE_REQUEST_REJECTED, rejection_reason='stands')
        self._pm_row(verdict=CHANGE_REQUEST_PENDING)
        self.assertEqual(DesignChangeRequest.objects.filter(attempt=self.attempt).count(), 4)

    def test_03_a_rejection_still_needs_its_reason(self):
        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                self._pm_row(verdict=CHANGE_REQUEST_REJECTED, rejection_reason='')
        self.assertIn('cr_rejection_reason_required_when_rejected', str(caught.exception))

    def test_04_the_default_verdict_is_still_pending(self):
        row = self._pm_row()
        self.assertEqual(row.verdict, CHANGE_REQUEST_PENDING)


# ===========================================================================
# The raise view writes origin from raised_as's own predicate
# ===========================================================================

class RaiseViewOriginTests(SchemaBase):

    def _raise_log(self, change):
        return ActivityLog.objects.get(action_code='design_change_requested',
                                       entity_type='DesignChangeRequest',
                                       entity_id=change.pk).action

    def test_01_a_pm_raise_is_origin_pm_and_says_so_in_the_log(self):
        self._into_qc()
        self._login(self.pm)
        self._raise()
        change = self._the_request()
        self.assertEqual(change.origin, CHANGE_REQUEST_ORIGIN_PM)
        self.assertTrue(self._raise_log(change).startswith('PM change request raised'))

    def test_02_an_scm_raise_is_origin_scm_and_says_so_in_the_log(self):
        self.a.status = DESIGN_RELEASED
        self.a.save(update_fields=['status'])
        self._login(self.scm)
        self._raise()
        change = self._the_request()
        self.assertEqual(change.origin, CHANGE_REQUEST_ORIGIN_SCM)
        self.assertTrue(self._raise_log(change).startswith('Change request raised by SCM'))


# ===========================================================================
# Admin: nothing new is editable, origin only on the add form
# ===========================================================================

class AdminReadonlyTests(SchemaBase):

    NEW_FIELDS = {'pm_decided_by', 'pm_decided_at', 'pm_note',
                  'withdrawn_by', 'withdrawn_at', 'withdrawal_note',
                  'corrected_by', 'corrected_at', 'correction_note', 'boq_corrections'}

    def setUp(self):
        super().setUp()
        self.admin = DesignChangeRequestAdmin(DesignChangeRequest, AdminSite())
        self.request = RequestFactory().get('/')

    def test_01_the_add_form_offers_origin_and_none_of_the_new_fields(self):
        readonly = set(self.admin.get_readonly_fields(self.request, obj=None))
        self.assertTrue(self.NEW_FIELDS <= readonly)
        self.assertNotIn('origin', readonly)

    def test_02_the_change_form_freezes_origin_too(self):
        row = self._row(verdict=CHANGE_REQUEST_WITH_PM)
        readonly = set(self.admin.get_readonly_fields(self.request, obj=row))
        self.assertTrue(self.NEW_FIELDS | {'origin'} <= readonly)

    def test_03_what_was_editable_before_still_is(self):
        row = self._row(verdict=CHANGE_REQUEST_WITH_PM)
        for obj in (None, row):
            readonly = set(self.admin.get_readonly_fields(self.request, obj=obj))
            self.assertEqual(readonly & {'verdict', 'decided_by', 'decided_at',
                                         'rejection_reason', 'reason', 'attempt',
                                         'requested_by', 'resulting_attempt'}, set())
            self.assertIn('requested_at', readonly)
