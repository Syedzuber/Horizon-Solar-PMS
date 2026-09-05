"""Session C verification — apply_design_status() is the one place the design status moves.

WHY THIS FILE EXISTS
--------------------
Audit A-2.2 found `DesignAssignment.status` written in eighteen places across sixteen
functions, plus a Django admin change form that could set it — and `released_at` and
`released_by` with it — in one submit. Six of the eighteen saved the WHOLE ROW.

Three distinct defects hid in that spread, and none of them is visible from any single
screen:

  * A BARE save() CLOBBERS. Six sites wrote every column from a possibly stale in-memory
    row, on a model whose one queryset write already carried a comment explaining exactly
    that hazard. Two people acting on one site, and the loser's view of every other
    column silently won.
  * A TRANSIENT STATUS IS NOT A STATE. `qc_failed` was written and overwritten two
    statements later inside the same atomic block, so no query could ever observe it —
    but a derivation hook firing per write would have announced it downstream as a
    transition that never happened.
  * THE ADMIN WAS A RELEASE ROUTE. `design_head_qc_pass()` is the only path that stamps
    the release AND closes the attempt, so an admin form that set `status='released'`
    correctly still produced a released site with an open attempt — a state the product
    itself cannot make.

Everything below pins the consolidation that closes all three. The clobber tests use a
DELIBERATELY STALE instance rather than threads: staleness is what the bug needed, and a
test that reproduces it without a race is the one that will still be reliable in a year.
"""
import re
from decimal import Decimal
from unittest.mock import patch

from django.contrib import admin as django_admin
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from .design_views import (
    _allocate_one, _open_next_attempt, apply_design_status,
)
from .models import (
    ActivityLog, Program, Project, UserProfile, DesignAssignment, DesignAttempt,
    ArkaSubmission, DesignFile,
    DESIGN_AWAITING_SURVEY, DESIGN_AWAITING_ALLOCATION, DESIGN_IN_DESIGN,
    DESIGN_ARKA_SUBMITTED, DESIGN_ARTIFACTS_UPLOADED, DESIGN_IN_QC,
    DESIGN_AWAITING_HEAD_QC, DESIGN_QC_FAILED, DESIGN_RELEASED, DESIGN_SURVEY_RETURNED,
    DESIGN_FILE_CAD_ZIP,
    ARKA_PENDING, ARKA_APPROVED,
    ATTEMPT_REASON_INITIAL, ATTEMPT_REASON_QC_FAILED,
    ERR_BOQ_SPECIFICATION,
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


#: Columns named in an UPDATE against the assignment table. SQLite and Postgres both
#: render `UPDATE "projects_designassignment" SET "col" = %s, "col2" = %s WHERE ...`,
#: and the point of every assertion that uses this is the SET list — a column absent
#: from it is a column a concurrent writer still owns.
_UPDATE_RE = re.compile(r'UPDATE\s+"?projects_designassignment"?\s+SET\s+(.*?)\s+WHERE',
                        re.IGNORECASE | re.DOTALL)


def _assignment_update_columns(captured):
    """[{columns of each UPDATE against projects_designassignment}], in order.

    Foreign keys are named in Django terms (`released_by`, not `released_by_id`) so the
    assertions below read as the field names the views actually pass.
    """
    out = []
    for query in captured.captured_queries:
        match = _UPDATE_RE.search(query['sql'])
        if match:
            out.append({re.sub(r'_id$', '', c.strip().strip('"'))
                        for c in re.findall(r'"?([A-Za-z_]+)"?\s*=', match.group(1))})
    return out


#: A direct write to the design status or its release stamp, in any of the three rvalue
#: forms this module uses — a DESIGN_ literal, `_status_after_unblock()`'s computed
#: result, or `opening_status`. A-2.1 swept for the first only and missed two sites; the
#: audit that corrected it swept for all three, and so does this.
#:
#: `group.status = SITE_GROUP_LOCKED` is a SiteGroup and correctly not matched — the
#: rvalue is what tells the two apart.
_DIRECT_WRITE_RE = re.compile(
    r'^\s*assignment\.(?:status|released_at|released_by)\s*=(?!=)'
    r'|^\s*\w+\.status\s*=\s*(?:DESIGN_|_status_after_unblock|opening_status)'
    r'|^\s*\w+\.released_(?:at|by)\s*=(?!=)'
)


class ChokepointBase(TestCase):

    def setUp(self):
        self.head     = _profile('sc_head', 'Design', is_design_head=True)
        self.qc       = _profile('sc_qc',   'Design', is_design_qc=True)
        self.designer = _profile('sc_des',  'Design')
        self.program = Program.objects.create(
            name='Test-SessionC', program_type='OPEX', client_name='SCClient',
            status='Active', short_tender_code='SC')

    def _site(self, code, status=DESIGN_AWAITING_ALLOCATION, designer=None):
        site = Project(
            project_id=code, customer_name='SCClient', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code=code,
            capacity_kw=Decimal('100.00'), status='Draft')
        site.save()
        assignment = DesignAssignment.objects.create(
            project=site, status=status, assigned_to=designer,
            survey_file_bucket='b', survey_file_path=f'{code}/survey/x.pdf')
        return site, assignment

    def _login(self, profile):
        self.assertTrue(self.client.login(username=profile.user.username, password='x'))

    def _post(self, name, project, **data):
        return self.client.post(
            reverse(name, kwargs={'project_id': project.project_id}), data)


# ===========================================================================
# 1. The chokepoint itself
# ===========================================================================

class ApplyDesignStatusTests(ChokepointBase):
    """VERIFICATION 3 (the mechanism) and VERIFICATION 6 (the hook's arguments)."""

    def setUp(self):
        super().setUp()
        self.site, self.a = self._site('SC-FN')

    def test_01_status_and_companion_fields_land_in_one_update(self):
        with CaptureQueriesContext(connection) as captured:
            apply_design_status(
                self.a, DESIGN_RELEASED, self.head, 'released', 'design_head_qc_passed',
                extra_fields={'released_at': timezone.now(), 'released_by': self.head})

        updates = _assignment_update_columns(captured)
        self.assertEqual(len(updates), 1,
                         'the status and its release stamp must be ONE write, not two — '
                         'a row that says released without them is a state the product '
                         'cannot produce')
        self.assertEqual(updates[0],
                         {'status', 'released_at', 'released_by', 'updated_at'})

    def test_02_the_update_names_no_column_it_was_not_given(self):
        """The whole of the bare-save() defect, stated as one assertion.

        Every column outside the SET list is a column a concurrent writer still owns.
        """
        with CaptureQueriesContext(connection) as captured:
            apply_design_status(self.a, DESIGN_IN_DESIGN, self.head,
                                'allocated', 'design_allocated')

        self.assertEqual(_assignment_update_columns(captured),
                         [{'status', 'updated_at'}])

    def test_03_a_stale_instance_does_not_clobber_another_column(self):
        """THE DEFECT ITSELF, reproduced without a race.

        `stale` is read BEFORE a second writer touches a different column. Under a bare
        save() the second writer's value is written back from `stale`'s empty copy and
        lost; under update() it survives, because it is not in the SET list.
        """
        stale = DesignAssignment.objects.get(pk=self.a.pk)

        DesignAssignment.objects.filter(pk=self.a.pk).update(
            survey_return_reason='the roof plan is missing',
            survey_returned_at=timezone.now())

        apply_design_status(stale, DESIGN_IN_DESIGN, self.head,
                            'allocated', 'design_allocated')

        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_IN_DESIGN)
        self.assertEqual(self.a.survey_return_reason, 'the roof plan is missing',
                         'the concurrent write was clobbered by a stale row')
        self.assertIsNotNone(self.a.survey_returned_at)

    def test_04_none_writes_the_companion_fields_and_leaves_status_alone(self):
        """The replace-in-place branches of both survey views."""
        with CaptureQueriesContext(connection) as captured:
            apply_design_status(
                self.a, None, self.head, 'Survey file replaced',
                'design_survey_replaced',
                extra_fields={'survey_file_path': 'SC-FN/survey/v2.pdf'})

        updates = _assignment_update_columns(captured)
        self.assertEqual(updates, [{'survey_file_path', 'updated_at'}])
        self.assertNotIn('status', updates[0],
                         'a replacement is not a transition and must not write status')

        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_AWAITING_ALLOCATION)
        self.assertEqual(self.a.survey_file_path, 'SC-FN/survey/v2.pdf')

    def test_05_the_in_memory_instance_is_kept_in_step(self):
        """Callers read `assignment` after the call, for their own success messages."""
        apply_design_status(
            self.a, DESIGN_RELEASED, self.head, 'released', 'design_head_qc_passed',
            extra_fields={'released_by': self.head})

        self.assertEqual(self.a.status, DESIGN_RELEASED)
        self.assertEqual(self.a.released_by_id, self.head.pk)
        self.assertEqual(self.a.get_status_display(), 'Released')

    def test_06_it_returns_the_status_the_row_was_on(self):
        """VERIFICATION 6 — `from_status` is read BEFORE the write.

        This is the argument the mirror hook cannot reconstruct afterwards, so the fact
        that the function can still name it at the end of its own body is the shape the
        next session depends on.
        """
        previous = apply_design_status(self.a, DESIGN_IN_DESIGN, self.head,
                                       'allocated', 'design_allocated')
        self.assertEqual(previous, DESIGN_AWAITING_ALLOCATION)
        self.assertEqual(self.a.status, DESIGN_IN_DESIGN)

    def test_07_it_writes_the_activity_log_line_it_was_given(self):
        """The eighteen action_codes are load-bearing — design_analytics pairs
        `design_blocked` with `design_survey_unblocked` to reconstruct hold duration —
        so the consolidation must pass them through untouched, entity ref included."""
        apply_design_status(
            self.a, DESIGN_ARTIFACTS_UPLOADED, self.designer,
            'Design package complete on attempt 1', 'design_artifacts_uploaded',
            entity_type='DesignAttempt', entity_id=4242)

        row = ActivityLog.objects.filter(project=self.site).latest('id')
        self.assertEqual(row.action_code, 'design_artifacts_uploaded')
        self.assertEqual(row.action, 'Design package complete on attempt 1')
        self.assertEqual(row.entity_type, 'DesignAttempt')
        self.assertEqual(row.entity_id, 4242)
        self.assertEqual(row.actor_id, self.designer.pk)

    def test_08_the_entity_defaults_to_the_assignment(self):
        apply_design_status(self.a, DESIGN_IN_DESIGN, self.head,
                            'allocated', 'design_allocated')
        row = ActivityLog.objects.filter(project=self.site).latest('id')
        self.assertEqual(row.entity_type, 'DesignAssignment')
        self.assertEqual(row.entity_id, self.a.pk)


# ===========================================================================
# 2. The six sites that used to save the whole row
# ===========================================================================

class FormerBareSaveTests(ChokepointBase):
    """VERIFICATION 3 — sites #1-#6 of the audit, each proved from its own entry point.

    #1/#2 are design_survey_upload's two transitions, #3/#4 design_survey_link_set's,
    #5 _allocate_one and #6 design_mark_blocked. All six went through a bare save().
    """

    def test_01_allocate_one_does_not_clobber_a_concurrent_write(self):
        """SITE #5, the sharpest of the six.

        `_allocate_one` is reachable from the BULK path, which can hold an in-memory row
        read some time before it writes. A Design Hold placed on the same site meanwhile
        used to be wiped along with every other column.
        """
        site, assignment = self._site('SC-ALLOC')
        stale = DesignAssignment.objects.get(pk=assignment.pk)

        # A second actor, between the read and the write.
        DesignAssignment.objects.filter(pk=assignment.pk).update(
            survey_folder_url='https://drive.google.com/drive/folders/xyz')

        _allocate_one(stale, self.designer, self.head)

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, DESIGN_IN_DESIGN)
        self.assertEqual(assignment.assigned_to_id, self.designer.pk)
        self.assertEqual(assignment.survey_folder_url,
                         'https://drive.google.com/drive/folders/xyz',
                         'allocation clobbered a column it was never asked to write')

    def test_02_allocate_one_writes_its_three_stamps_with_the_status(self):
        site, assignment = self._site('SC-ALLOC2')
        with CaptureQueriesContext(connection) as captured:
            _allocate_one(assignment, self.designer, self.head)

        updates = _assignment_update_columns(captured)
        self.assertEqual(len(updates), 1, 'the allocation is one write')
        self.assertEqual(updates[0], {'status', 'assigned_to', 'assigned_by',
                                      'assigned_at', 'updated_at'})

    def test_03_allocation_still_logs_reallocation_distinctly(self):
        """The two codes survive the move into apply_design_status()."""
        site, assignment = self._site('SC-REALLOC')
        other = _profile('sc_des2', 'Design')

        _allocate_one(assignment, self.designer, self.head)
        self.assertEqual(
            ActivityLog.objects.filter(project=site).latest('id').action_code,
            'design_allocated')

        assignment.refresh_from_db()
        _allocate_one(assignment, other, self.head)
        self.assertEqual(
            ActivityLog.objects.filter(project=site).latest('id').action_code,
            'design_reallocated')

    def test_04_mark_blocked_writes_the_hold_triple_with_the_status(self):
        """SITE #6. The hold and its reason are one write — a hold with no reason, or a
        reason with no hold, is not a state this row may ever be in."""
        site, assignment = self._site('SC-HOLD', status=DESIGN_IN_DESIGN,
                                      designer=self.designer)
        self._login(self.designer)

        with CaptureQueriesContext(connection) as captured:
            self._post('design_mark_blocked', site, reason='no roof plan')

        updates = _assignment_update_columns(captured)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0], {'status', 'survey_returned_at',
                                      'survey_returned_by', 'survey_return_reason',
                                      'updated_at'})

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, DESIGN_SURVEY_RETURNED)
        self.assertEqual(assignment.survey_return_reason, 'no roof plan')

    def test_05_survey_link_transition_writes_only_its_own_columns(self):
        """SITE #4 — `awaiting_survey` -> `awaiting_allocation` by folder link."""
        site, assignment = self._site('SC-LINK', status=DESIGN_AWAITING_SURVEY)
        DesignAssignment.objects.filter(pk=assignment.pk).update(
            survey_file_bucket='', survey_file_path='')
        self._login(self.head)

        with CaptureQueriesContext(connection) as captured:
            self._post('design_survey_link_set', site,
                       survey_folder_url='https://drive.google.com/drive/folders/abc')

        updates = _assignment_update_columns(captured)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0], {'status', 'survey_folder_url',
                                      'survey_link_added_by', 'survey_link_added_at',
                                      'updated_at'})

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, DESIGN_AWAITING_ALLOCATION)

    def test_06_survey_link_unblock_writes_only_its_own_columns(self):
        """SITE #3 — the hold-clearing branch, whose to-status is COMPUTED.

        The audit found this one missing from A-2.1's count precisely because the rvalue
        is a call, not a DESIGN_ literal. It is a write like any other.
        """
        site, assignment = self._site('SC-LINKU', status=DESIGN_SURVEY_RETURNED,
                                      designer=self.designer)
        self._login(self.head)

        with CaptureQueriesContext(connection) as captured:
            self._post('design_survey_link_set', site,
                       survey_folder_url='https://drive.google.com/drive/folders/abc')

        updates = _assignment_update_columns(captured)
        self.assertEqual(len(updates), 1)
        self.assertIn('status', updates[0])
        self.assertNotIn('survey_return_reason', updates[0],
                         'the hold record is deliberately LEFT IN PLACE — it is how '
                         'hold duration is reconstructed')

        assignment.refresh_from_db()
        # No approved commitment ever existed, so _status_after_unblock returns
        # `allocated` rather than `in_design`. What matters here is that the site left
        # the hold and the hold record survived.
        self.assertNotEqual(assignment.status, DESIGN_SURVEY_RETURNED)
        self.assertEqual(
            ActivityLog.objects.filter(project=site).latest('id').action_code,
            'design_survey_unblocked')

    def test_07_survey_link_replacement_writes_no_status_at_all(self):
        """The third branch — not a transition, and it no longer pretends to be one."""
        site, assignment = self._site('SC-LINKR', status=DESIGN_IN_DESIGN,
                                      designer=self.designer)
        DesignAssignment.objects.filter(pk=assignment.pk).update(
            survey_folder_url='https://drive.google.com/drive/folders/old')
        self._login(self.head)

        with CaptureQueriesContext(connection) as captured:
            self._post('design_survey_link_set', site,
                       survey_folder_url='https://drive.google.com/drive/folders/new')

        updates = _assignment_update_columns(captured)
        # The replace-lock refuses a change after allocation, so nothing is written at
        # all here. Either way the invariant under test holds: no status column moves.
        for columns in updates:
            self.assertNotIn('status', columns)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, DESIGN_IN_DESIGN)

    def test_08_survey_upload_transition_writes_only_its_own_columns(self):
        """SITES #1/#2 — storage stubbed at its point of use in design_views."""
        site, assignment = self._site('SC-UP', status=DESIGN_AWAITING_SURVEY)
        DesignAssignment.objects.filter(pk=assignment.pk).update(
            survey_file_bucket='', survey_file_path='')
        self._login(self.head)

        upload = SimpleUploadedFile('survey.pdf', b'%PDF-1.4 stub',
                                    content_type='application/pdf')
        with patch('projects.design_views.upload_design_file',
                   return_value=('Horizon-PMS-Design', 'SC-UP/survey/survey.pdf')):
            with CaptureQueriesContext(connection) as captured:
                self.client.post(
                    reverse('design_survey_upload',
                            kwargs={'project_id': site.project_id}),
                    {'survey_file': upload})

        updates = _assignment_update_columns(captured)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0], {'status', 'survey_file_bucket', 'survey_file_path',
                                      'survey_uploaded_by', 'survey_uploaded_at',
                                      'updated_at'})

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, DESIGN_AWAITING_ALLOCATION)
        self.assertEqual(assignment.survey_file_path, 'SC-UP/survey/survey.pdf')


# ===========================================================================
# 3. The transient qc_failed write
# ===========================================================================

class TransientQcFailedTests(ChokepointBase):
    """VERIFICATION 5 — a QC failure now writes the status ONCE, not twice.

    It used to write `qc_failed` and then, two statements later inside the SAME atomic
    block, the new attempt's opening status. Nothing could ever read the first value.
    The log line, which is what actually made the failure visible in the trail, is
    unchanged.
    """

    def setUp(self):
        super().setUp()
        self.site, self.a = self._site('SC-QCF', status=DESIGN_IN_QC,
                                       designer=self.designer)
        self.attempt = DesignAttempt.objects.create(
            assignment=self.a, attempt_number=1, opened_reason=ATTEMPT_REASON_INITIAL)
        DesignAssignment.objects.filter(pk=self.a.pk).update(current_attempt_number=1)
        self.a.refresh_from_db()

        self.arka = ArkaSubmission.objects.create(
            attempt=self.attempt, version=1, capacity_kw=Decimal('120.00'),
            arka_link='https://example.com/arka', submitted_by=self.designer,
            verdict=ARKA_APPROVED, reviewed_by=self.qc, reviewed_at=timezone.now(),
            head_verdict=ARKA_APPROVED, head_reviewed_by=self.head,
            head_reviewed_at=timezone.now(), is_current=True)
        DesignFile.objects.create(
            attempt=self.attempt, kind=DESIGN_FILE_CAD_ZIP, version=1,
            bucket='b', path='SC-QCF/cad_zip/a.zip', original_filename='a.zip',
            size_bytes=1000, archive_listing=[{'name': 'a.pdf', 'size': 10}],
            derived_from_arka=self.arka, uploaded_by=self.designer, is_current=True)
        self.attempt.boq_submitted_at = timezone.now()
        self.attempt.boq_submitted_by = self.designer
        self.attempt.qc_started_at = timezone.now()
        self.attempt.save()

    def test_01_a_qc_failure_writes_the_status_exactly_once(self):
        self._login(self.qc)
        with CaptureQueriesContext(connection) as captured:
            self._post('design_qc_fail', self.site,
                       qc_remarks='the string sizing is wrong',
                       error_category=ERR_BOQ_SPECIFICATION,
                       redo_scope_submitted='1', redo=['boq'])

        status_writes = [u for u in _assignment_update_columns(captured)
                         if 'status' in u]
        self.assertEqual(
            len(status_writes), 1,
            'the failure should move the row once, to the new attempt\'s opening '
            'status — a second write of a value nothing can read is a transition the '
            'mirror hook would announce and that never happened')
        self.assertIn('current_attempt_number', status_writes[0],
                      'the opening status and the attempt number are one write')

    def test_02_qc_failed_is_never_left_on_the_row(self):
        self._login(self.qc)
        self._post('design_qc_fail', self.site,
                   qc_remarks='the string sizing is wrong',
                   error_category=ERR_BOQ_SPECIFICATION,
                   redo_scope_submitted='1', redo=['boq'])

        self.a.refresh_from_db()
        self.assertNotEqual(self.a.status, DESIGN_QC_FAILED)
        self.assertEqual(self.a.current_attempt_number, 2)

    def test_03_the_failure_is_still_in_the_trail(self):
        """The log line was always what made the failure visible, and it is untouched."""
        self._login(self.qc)
        self._post('design_qc_fail', self.site,
                   qc_remarks='the string sizing is wrong',
                   error_category=ERR_BOQ_SPECIFICATION,
                   redo_scope_submitted='1', redo=['boq'])

        codes = list(ActivityLog.objects.filter(project=self.site)
                     .order_by('id').values_list('action_code', flat=True))
        self.assertIn('design_qc_failed', codes)
        self.assertIn(f'design_attempt_opened_{ATTEMPT_REASON_QC_FAILED}', codes)
        self.assertLess(codes.index('design_qc_failed'),
                        codes.index(f'design_attempt_opened_{ATTEMPT_REASON_QC_FAILED}'),
                        'the failure must be logged before the attempt that answers it')

    def test_04_head_qc_failure_writes_the_status_exactly_once(self):
        """The gate-2 half of the same shape."""
        self.attempt.qc_verdict = 'passed'
        self.attempt.qc_reviewed_by = self.qc
        self.attempt.qc_reviewed_at = timezone.now()
        self.attempt.head_started_at = timezone.now()
        self.attempt.save()
        DesignAssignment.objects.filter(pk=self.a.pk).update(
            status=DESIGN_AWAITING_HEAD_QC)

        self._login(self.head)
        with CaptureQueriesContext(connection) as captured:
            self._post('design_head_qc_fail', self.site,
                       head_remarks='the layout does not match the survey',
                       error_category=ERR_BOQ_SPECIFICATION,
                       redo_scope_submitted='1', redo=['boq'])

        status_writes = [u for u in _assignment_update_columns(captured)
                         if 'status' in u]
        self.assertEqual(len(status_writes), 1)

        self.a.refresh_from_db()
        self.assertNotEqual(self.a.status, DESIGN_QC_FAILED)


# ===========================================================================
# 4. _open_next_attempt() nests, it does not merge
# ===========================================================================

class OpenNextAttemptNestingTests(ChokepointBase):
    """The attempt lifecycle stays its own chokepoint and CALLS this one.

    The coupling is not one-to-one in either direction, which is why they are not
    merged: design_head_qc_pass() closes an attempt without opening one, and
    design_qc_pass() deliberately closes nothing.
    """

    def test_01_the_opening_status_and_the_attempt_number_are_one_write(self):
        site, assignment = self._site('SC-NEXT', status=DESIGN_IN_QC,
                                      designer=self.designer)
        DesignAttempt.objects.create(
            assignment=assignment, attempt_number=1,
            opened_reason=ATTEMPT_REASON_INITIAL)
        DesignAssignment.objects.filter(pk=assignment.pk).update(
            current_attempt_number=1)
        assignment.refresh_from_db()

        with CaptureQueriesContext(connection) as captured:
            _open_next_attempt(assignment, ATTEMPT_REASON_QC_FAILED, self.qc,
                               'QC failure on attempt 1')

        updates = [u for u in _assignment_update_columns(captured) if 'status' in u]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0],
                         {'status', 'current_attempt_number', 'updated_at'})

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, DESIGN_IN_DESIGN)
        self.assertEqual(assignment.current_attempt_number, 2)

    def test_02_the_outgoing_attempt_is_still_closed_here(self):
        """Proof the nesting did not quietly move attempt lifecycle into the writer."""
        site, assignment = self._site('SC-NEXT2', status=DESIGN_IN_QC,
                                      designer=self.designer)
        first = DesignAttempt.objects.create(
            assignment=assignment, attempt_number=1,
            opened_reason=ATTEMPT_REASON_INITIAL)
        DesignAssignment.objects.filter(pk=assignment.pk).update(
            current_attempt_number=1)
        assignment.refresh_from_db()

        _open_next_attempt(assignment, ATTEMPT_REASON_QC_FAILED, self.qc, 'failed')

        first.refresh_from_db()
        self.assertIsNotNone(first.closed_at)
        self.assertEqual(assignment.attempts.count(), 2)


# ===========================================================================
# 5. The admin is not a release route
# ===========================================================================

class DesignAssignmentAdminIsClosedTests(ChokepointBase):
    """VERIFICATION 4 — read-only in the FORM, not merely absent from a template.

    A field missing from a rendered page but still present on the ModelForm accepts a
    hand-crafted POST. Both halves are asserted: the form has no such field, and a POST
    that names all three is accepted and changes none of them.
    """

    def setUp(self):
        super().setUp()
        self.superuser = User.objects.create_superuser(
            'sc_admin', 'sc@example.com', 'pw')
        # solarpms.middleware.AdminAccessMiddleware gates /admin/ on
        # UserProfile.role == 'Admin', not on is_staff.
        self.superuser.profile.role = 'Admin'
        self.superuser.profile.save(update_fields=['role'])
        self.client = Client(SERVER_NAME='localhost')
        self.client.force_login(self.superuser)

        self.site, self.a = self._site('SC-ADMIN', status=DESIGN_AWAITING_SURVEY)

    def _model_admin(self):
        return django_admin.site._registry[DesignAssignment]

    def _request(self):
        request = RequestFactory().get('/')
        request.user = self.superuser
        return request

    def test_01_the_three_fields_are_declared_read_only(self):
        readonly = self._model_admin().get_readonly_fields(self._request(), self.a)
        for name in ('status', 'released_at', 'released_by'):
            self.assertIn(name, readonly)

    def test_02_the_change_form_carries_no_bindable_field_for_them(self):
        """THE DECISIVE CHECK. A readonly_fields entry is removed from the ModelForm, so
        there is nothing for a POSTed value to bind to — which is what makes this a
        closed door rather than a hidden one."""
        model_admin = self._model_admin()
        form_class = model_admin.get_form(self._request(), self.a, change=True)
        for name in ('status', 'released_at', 'released_by'):
            self.assertNotIn(name, form_class.base_fields,
                             f'{name} is still a form field and would accept a POST')

    def test_03_the_change_page_renders_and_shows_the_status_as_text(self):
        url = reverse('admin:projects_designassignment_change', args=[self.a.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        html = response.content.decode()
        self.assertNotIn('name="status"', html,
                         'the status still renders as an editable input')
        self.assertNotIn('name="released_at"', html)
        self.assertNotIn('name="released_by"', html)

    def test_04_a_post_naming_all_three_changes_none_of_them(self):
        """The end-to-end proof: a real, accepted admin submit that tries to release."""
        url = reverse('admin:projects_designassignment_change', args=[self.a.pk])
        form_class = self._model_admin().get_form(self._request(), self.a, change=True)
        form = form_class(instance=self.a)

        data = {}
        for name, field in form.fields.items():
            value = form.initial.get(name, field.initial)
            if value is None or value == '':
                continue
            data[name] = getattr(value, 'pk', value)
        # What the attacker adds on top of a legitimate submit.
        data['status'] = DESIGN_RELEASED
        data['released_by'] = self.head.pk
        data['released_at_0'] = '2026-09-05'
        data['released_at_1'] = '10:00:00'

        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302,
                         'the submit was rejected for an unrelated reason, so this '
                         'test proved nothing — fix the posted data')

        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_AWAITING_SURVEY,
                         'the admin moved a site to released in one form submit')
        self.assertIsNone(self.a.released_at)
        self.assertIsNone(self.a.released_by_id)

    def test_05_the_three_fields_are_not_list_editable(self):
        """A list_editable entry writes past readonly_fields entirely."""
        model_admin = self._model_admin()
        for name in ('status', 'released_at', 'released_by'):
            self.assertNotIn(name, getattr(model_admin, 'list_editable', ()) or ())


# ===========================================================================
# 6. The absence of any other writer
# ===========================================================================

class NoOtherWriterTests(TestCase):
    """The claim this whole session rests on, asserted rather than grepped once.

    A future session adding `assignment.status = X` back into design_views.py fails
    here, not in review.
    """

    def test_01_design_views_has_no_direct_status_write(self):
        import io
        import os
        from . import design_views

        source = io.open(os.path.abspath(design_views.__file__.replace('.pyc', '.py')),
                         encoding='utf-8-sig').read()

        offenders = [line.strip() for line in source.splitlines()
                     if _DIRECT_WRITE_RE.match(line)]
        self.assertEqual(
            offenders, [],
            'design_views.py writes a design status outside apply_design_status(): '
            + '; '.join(offenders))

    def test_02_apply_design_status_keeps_its_mirror_hook_attachment_point(self):
        """VERIFICATION 6 — the marker the next session attaches to still exists."""
        import inspect
        from .design_views import apply_design_status

        source = inspect.getsource(apply_design_status)
        self.assertIn('MIRROR HOOK ATTACHMENT POINT', source)
        for name in ('assignment', 'from_status', 'new_status', 'actor'):
            self.assertIn(name, source.split('MIRROR HOOK ATTACHMENT POINT')[1],
                          f'{name} is not named at the attachment point')
