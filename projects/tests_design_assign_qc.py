"""design_assign_qc — the gate-1 "Save QC" action on the Design Head's sites screen, pinned
as it behaves today.

WHY THIS FILE EXISTS
--------------------
The view had no test at all, and its write is about to move into a helper that a bulk
management command will share (assign_qc_split). These tests describe the view from the
outside — what a POST changes, what it logs, what it refuses — so the extraction can be
judged by one question: does this file still pass unchanged?

Pinned on purpose, because a refactor could quietly change them:
  * QC can be named in ANY status that has a DesignAssignment, until gate 1 records a
    verdict on the current attempt — not only once the package reaches QC.
  * The reviewer needs role Design and an active profile; the is_design_qc flag is NOT
    required (assignment overrides the open pool).
  * The site's own designer is refused.
  * The write touches only the three qc_* columns and updated_at.
  * One ActivityLog row per change, no StatusTransition, no notification.
  * Saving the reviewer the site already has writes nothing at all.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.messages import constants, get_messages
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import (
    ActivityLog, DesignAssignment, DesignAttempt, NotificationLog, Program, Project,
    StatusTransition,
    DESIGN_ARKA_SUBMITTED, DESIGN_AWAITING_ALLOCATION, DESIGN_IN_DESIGN, DESIGN_IN_QC,
)
from .tests_design_status_chokepoint import _assignment_update_columns


#: Level names as the view means them. The project maps ERROR's CSS tag to 'danger', so
#: level_tag would pin a styling choice rather than the view's behaviour.
_LEVEL = {constants.SUCCESS: 'success', constants.INFO: 'info', constants.ERROR: 'error'}


def _profile(username, role, first='', last='', is_design_head=False, is_design_qc=False):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=first, last_name=last)
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.is_design_head = is_design_head
    profile.is_design_qc = is_design_qc
    profile.save()
    return profile


class DesignAssignQcTests(TestCase):

    def setUp(self):
        self.head = _profile('aq_head', 'Design', 'Hema', 'Head', is_design_head=True)
        self.designer = _profile('aq_des', 'Design', 'Dev', 'Designer')
        self.qc_a = _profile('aq_qca', 'Design', 'Asha', 'Reviewer', is_design_qc=True)
        self.qc_b = _profile('aq_qcb', 'Design', 'Bala', 'Reviewer', is_design_qc=True)
        self.program = Program.objects.create(
            name='Test-AssignQC', program_type='OPEX', client_name='AQClient',
            status='Active', short_tender_code='AQ')
        self.site, self.assignment = self._site('AQ001', DESIGN_IN_DESIGN, self.designer)
        self.client = Client()
        self.client.force_login(self.head.user)

    def _site(self, code, status, designer=None, project_type='OPEX'):
        site = Project(
            project_id=code, customer_name='AQClient', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type=project_type,
            program=self.program if project_type == 'OPEX' else None, site_code=code,
            dc_capacity_kw=Decimal('100.00'), status='Draft')
        site.save()
        assignment = DesignAssignment.objects.create(
            project=site, status=status, assigned_to=designer,
            survey_file_bucket='b', survey_file_path=f'{code}/survey/x.pdf')
        return site, assignment

    def _post(self, qc_id, site=None, client=None):
        site = site or self.site
        return (client or self.client).post(
            reverse('design_assign_qc', kwargs={'project_id': site.project_id}),
            {'qc_id': '' if qc_id is None else str(qc_id)})

    def _message(self, response):
        """The message THIS response added. Messages from an earlier POST in the same test
        are still queued (nothing rendered a page in between), so take the newest."""
        queued = list(get_messages(response.wsgi_request))
        return (_LEVEL[queued[-1].level], str(queued[-1])) if queued else None

    def _reload(self, assignment=None):
        return DesignAssignment.objects.get(pk=(assignment or self.assignment).pk)

    def _counts(self):
        return (ActivityLog.objects.count(), StatusTransition.objects.count(),
                NotificationLog.objects.count())

    def _assert_nothing_written(self, before_qc, counts):
        a = self._reload()
        self.assertEqual(a.qc_assigned_to_id, before_qc)
        self.assertEqual(self._counts(), counts)

    # -- assign / reassign / clear ----------------------------------------------------

    def test_assigning_from_the_open_pool_stamps_three_columns_and_logs_once(self):
        before = timezone.now()
        response = self._post(self.qc_a.pk)

        self.assertRedirects(response, reverse('design_head_sites', kwargs={'pk': self.program.pk}),
                             fetch_redirect_response=False)
        a = self._reload()
        self.assertEqual(a.qc_assigned_to_id, self.qc_a.pk)
        self.assertEqual(a.qc_assigned_by_id, self.head.pk)
        self.assertGreaterEqual(a.qc_assigned_at, before)
        self.assertEqual(a.status, DESIGN_IN_DESIGN)          # status untouched

        log = ActivityLog.objects.get(action_code='design_qc_assigned')
        self.assertEqual(log.action, 'Design QC reviewer assigned: Asha Reviewer')
        self.assertEqual((log.project_id, log.actor_id, log.entity_type, log.entity_id),
                         (self.site.pk, self.head.pk, 'DesignAssignment', self.assignment.pk))
        self.assertEqual(self._message(response),
                         ('success', 'AQ001: Design QC assigned to Asha Reviewer.'))

    def test_reassigning_logs_from_and_to(self):
        self._post(self.qc_a.pk)
        response = self._post(self.qc_b.pk)

        self.assertEqual(self._reload().qc_assigned_to_id, self.qc_b.pk)
        log = ActivityLog.objects.get(action_code='design_qc_reassigned')
        self.assertEqual(log.action,
                         'Design QC reviewer changed from Asha Reviewer to Bala Reviewer')
        self.assertEqual(self._message(response),
                         ('success', 'AQ001: Design QC assigned to Bala Reviewer.'))

    def test_clearing_returns_the_site_to_the_open_pool_and_still_stamps_by_and_at(self):
        self._post(self.qc_a.pk)
        response = self._post(None)

        a = self._reload()
        self.assertIsNone(a.qc_assigned_to_id)
        # Today the clear overwrites qc_assigned_by/at with the clearing actor and time,
        # rather than nulling them. Pinned so the extraction keeps it.
        self.assertEqual(a.qc_assigned_by_id, self.head.pk)
        self.assertIsNotNone(a.qc_assigned_at)
        log = ActivityLog.objects.get(action_code='design_qc_unassigned')
        self.assertEqual(log.action, 'Design QC reviewer cleared — gate 1 returns to the '
                                     'open pool (was Asha Reviewer)')
        self.assertEqual(self._message(response),
                         ('success', 'AQ001: Design QC reviewer cleared — any QC reviewer '
                                     'may now take this site.'))

    def test_saving_the_current_reviewer_again_writes_nothing(self):
        self._post(self.qc_a.pk)
        stamped_at = self._reload().qc_assigned_at
        counts = self._counts()

        response = self._post(self.qc_a.pk)

        self.assertEqual(self._reload().qc_assigned_at, stamped_at)
        self.assertEqual(self._counts(), counts)
        self.assertEqual(self._message(response),
                         ('info', 'AQ001: no change — the Design QC reviewer was already '
                                  'Asha Reviewer.'))

    def test_clearing_an_open_pool_site_writes_nothing(self):
        counts = self._counts()
        response = self._post(None)
        self.assertEqual(self._counts(), counts)
        self.assertEqual(self._message(response),
                         ('info', 'AQ001: no change — the Design QC reviewer was already '
                                  'unassigned.'))

    def test_the_update_names_only_the_qc_columns(self):
        """A concurrent writer of any other column (status, designer, dates) keeps it."""
        with CaptureQueriesContext(connection) as captured:
            self._post(self.qc_a.pk)
        self.assertEqual(_assignment_update_columns(captured),
                         [{'qc_assigned_to', 'qc_assigned_by', 'qc_assigned_at', 'updated_at'}])

    def test_no_status_transition_and_no_notification(self):
        transitions, notices = StatusTransition.objects.count(), NotificationLog.objects.count()
        self._post(self.qc_a.pk)
        self.assertEqual(StatusTransition.objects.count(), transitions)
        self.assertEqual(NotificationLog.objects.count(), notices)

    # -- which sites -----------------------------------------------------------------

    def test_any_status_with_an_assignment_is_accepted_before_a_gate1_verdict(self):
        for code, status in (('AQ010', DESIGN_AWAITING_ALLOCATION),
                             ('AQ011', DESIGN_ARKA_SUBMITTED),
                             ('AQ012', DESIGN_IN_QC)):
            with self.subTest(status=status):
                site, assignment = self._site(code, status,
                                              None if status == DESIGN_AWAITING_ALLOCATION
                                              else self.designer)
                self._post(self.qc_a.pk, site=site)
                self.assertEqual(self._reload(assignment).qc_assigned_to_id, self.qc_a.pk)

    def test_a_recorded_gate1_verdict_locks_the_reviewer(self):
        self._post(self.qc_a.pk)
        DesignAttempt.objects.create(
            assignment=self.assignment, attempt_number=1, qc_verdict='passed',
            qc_reviewed_by=self.qc_a, qc_reviewed_at=timezone.now())
        DesignAssignment.objects.filter(pk=self.assignment.pk).update(current_attempt_number=1)
        counts = self._counts()

        response = self._post(self.qc_b.pk)

        self._assert_nothing_written(self.qc_a.pk, counts)
        self.assertEqual(self._message(response),
                         ('error', 'AQ001: Design QC has already recorded a verdict on this '
                                   'attempt — the reviewer cannot be changed now.'))

    def test_an_open_attempt_without_a_verdict_does_not_lock(self):
        DesignAttempt.objects.create(assignment=self.assignment, attempt_number=1)
        DesignAssignment.objects.filter(pk=self.assignment.pk).update(current_attempt_number=1)
        self._post(self.qc_a.pk)
        self.assertEqual(self._reload().qc_assigned_to_id, self.qc_a.pk)

    def test_a_site_with_no_design_assignment_is_refused(self):
        site = Project(project_id='AQ020', customer_name='AQClient',
                       customer_phone='9876543210', site_address='1 Sun Rd', city='Delhi',
                       project_type='OPEX', program=self.program, site_code='AQ020',
                       dc_capacity_kw=Decimal('100.00'), status='Draft')
        site.save()
        counts = self._counts()
        response = self._post(self.qc_a.pk, site=site)
        self.assertEqual(self._counts(), counts)
        self.assertEqual(self._message(response),
                         ('error', 'AQ020: design has not started on this site, so there is '
                                   'no review to assign.'))

    def test_a_residential_or_deleted_site_is_404(self):
        res, _ = self._site('AQ030', DESIGN_IN_DESIGN, self.designer, project_type='Residential')
        self.assertEqual(self._post(self.qc_a.pk, site=res).status_code, 404)
        gone, _ = self._site('AQ031', DESIGN_IN_DESIGN, self.designer)
        Project.objects.filter(pk=gone.pk).update(is_deleted=True)
        self.assertEqual(self._post(self.qc_a.pk, site=gone).status_code, 404)

    # -- which reviewers -------------------------------------------------------------

    def test_the_sites_own_designer_is_refused(self):
        counts = self._counts()
        response = self._post(self.designer.pk)
        self._assert_nothing_written(None, counts)
        self.assertEqual(self._message(response),
                         ('error', 'AQ001: Dev Designer is the designer allocated to this site, '
                                   'and nobody reviews their own work at either gate.'))

    def test_the_design_qc_flag_is_not_required(self):
        plain = _profile('aq_plain', 'Design', 'Pia', 'Plain', is_design_qc=False)
        self._post(plain.pk)
        self.assertEqual(self._reload().qc_assigned_to_id, plain.pk)

    def test_a_non_design_role_or_inactive_reviewer_is_refused(self):
        se = _profile('aq_se', 'Site Engineer', 'Sam', 'Engineer')
        gone = _profile('aq_gone', 'Design', 'Gita', 'Gone')
        gone.is_active = False
        gone.save()
        for profile in (se, gone):
            with self.subTest(user=profile.user.username):
                counts = self._counts()
                response = self._post(profile.pk)
                self._assert_nothing_written(None, counts)
                # The doubled full stop is today's output: _resolve_designer's message ends
                # in '.', and the view appends another. Cosmetic, pinned as-is; fixing it
                # is not part of the helper extraction.
                self.assertEqual(self._message(response),
                                 ('error', 'AQ001: Selected user is not an active Design user..'))

    # -- who may act -----------------------------------------------------------------

    def test_only_design_head_authority_may_assign(self):
        other = Client()
        other.force_login(self.qc_a.user)
        counts = self._counts()
        response = self._post(self.qc_b.pk, client=other)
        self.assertEqual(response.status_code, 403)
        self._assert_nothing_written(None, counts)

    def test_the_heads_deputy_may_assign_and_is_the_recorded_actor(self):
        deputy = _profile('aq_dep', 'Design', 'Dina', 'Deputy')
        self.head.design_head_deputy = deputy
        self.head.save()
        client = Client()
        client.force_login(deputy.user)
        self._post(self.qc_a.pk, client=client)
        a = self._reload()
        self.assertEqual((a.qc_assigned_to_id, a.qc_assigned_by_id), (self.qc_a.pk, deputy.pk))
        self.assertEqual(ActivityLog.objects.get(action_code='design_qc_assigned').actor_id,
                         deputy.pk)

    def test_get_redirects_and_writes_nothing(self):
        counts = self._counts()
        response = self.client.get(
            reverse('design_assign_qc', kwargs={'project_id': self.site.project_id}))
        self.assertRedirects(response, reverse('design_head_sites', kwargs={'pk': self.program.pk}),
                             fetch_redirect_response=False)
        self._assert_nothing_written(None, counts)
