"""
QA/QC verification visits — closeout step 7 (docs/CLOSEOUT_SPEC.md v2.2, CL-5, CL-B,
CL-C; decisions V-1 to V-10; go-ahead rulings Q1–Q9).

WHAT IS PINNED HERE
-------------------
The site's assigned QA/QC engineer starts a dated visit (one per site per day, one open
per site), answers chosen checklist points — Yes / No / NA, a photo on every answer, a
remark on No, the item text snapshotted — and submits. On submit every "No" raises one
punch point on its task through punch_points.raise_punch_point(). Visits live in their
own tables and never touch the site engineer's ChecklistItemCompletion rows.

    V-1  start / one per day / next day          VisitStartTests
    V-1  who may start, answer, submit           WhoMayTests
    V-3  photo, remark, snapshot, edit, lock     AnswerRuleTests, EditTests
    V-4  site engineer's answers untouched        ExecutionAnswersUntouchedTests
    V-5  punch points at submit, COD refused      SubmitTests
    V-6  coverage                                 CoverageTests
    V-2  pinned version (1a)                      PinnedPointTests
    Q1   batch twin == _checklist_for_task        BatchResolverEquivalenceTests
    V-9  site_has_submitted_qaqc_visit            ReadinessHelperTests
    V-10 PM dashboard line, site card             DashboardAndCardTests
    V-8  notices                                  NoticeTests
         Residential / CAPEX                       NonOpexTests
    Q2   lapsed on replace / end                  LapseTests
    pre-flight 5  mirrors / NA / no version       PointRulingTests
    Q5   photo_taken_at, downscale fallback       PhotoTests
    Q6   the PunchPoint.objects.create tripwire   PunchPointCreationTripwireTests
         counts follow each save; review/submit   VisitPageCountsTests, ReviewAndSubmitTests

EVERY SITE HERE IS REALLY ACTIVATED through opex_site_activate, so its tasks carry the
template_task the checklist links key on. Visits go through the real screens; storage is
mocked at qaqc_views._client and at bill_storage's signer.

Run with:
    python manage.py test projects.tests_qaqc_visit --settings=solarpms.test_settings
"""
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import qaqc_views
from .cod_views import CodRefused, record_refusal
from .models import (
    AppendOnlyViolation, Checklist, ChecklistItem, ChecklistItemCompletion,
    ChecklistTaskLink, NotificationLog, Project, PunchPoint, QaqcVisit, QaqcVisitAnswer,
    Task, TaskTemplateTask,
)
from .punch_points import open_punch_points_for_project
from .qaqc_views import (
    assign_site_qaqc, end_site_qaqc, last_visit_lines, resolve_checklists_for_tasks,
    site_checklist_points, site_coverage, site_has_submitted_qaqc_visit,
)
from .tests_qaqc_assignment import _client_for, _messages, _profile, _seed_opex
from .utils import assign_tasks_to, resolve_residential_template
from .views import _checklist_for_task

BUCKET = 'qaqc-visit-test-private'
SIGNED = 'https://storage.example/signed/visit.jpg?token=abc'

CIVIL  = 'Civil Work and MMS Installation'
MODULE = 'Module Installation'

CIVIL_LABELS  = ['Pedestal levels checked', 'Anchor bolts torqued', 'MMS alignment verified',
                 'Grouting cured']
MODULE_LABELS = ['Module clamps torqued', 'No cracked cells', 'String labels fixed']


def _photo(name='point.jpg', content=b'\xff\xd8\xff\xe0 fake jpeg bytes',
           content_type='image/jpeg'):
    return SimpleUploadedFile(name, content, content_type=content_type)


def _template_task(label):
    return (TaskTemplateTask.objects
            .filter(label=label, phase__template__project_type='OPEX')
            .order_by('-pk').first())


def _publish(code, name, version_no, labels, activate=True):
    """Author a checklist version the R-7 way: draft, items, then activate()."""
    checklist = Checklist.objects.create(code=code, name=name, version_no=version_no,
                                         requires_photo=False)
    items = [ChecklistItem.objects.create(checklist=checklist, label=label, order=n)
             for n, label in enumerate(labels, start=1)]
    if activate:
        checklist.activate()
    return checklist, items


class VisitFixture(TestCase):
    """Two activated OPEX sites with disjoint managers.

    site:       pm + coord; QA/QC engineer `qaqc`.   other_site: pm_b; QA/QC `qaqc_b`.
    worker — Site Engineer holding every Site Engineer task on both sites
    spare  — a free Site Engineer, for replacing `qaqc`
    Checklists: Civil (4 items) and Module (3 items), linked by template task, so each
    site has 7 points.
    """

    @classmethod
    def setUpTestData(cls):
        resolve_residential_template()   # bootstraps RESIDENTIAL v1 on a virgin DB
        _seed_opex()
        cls.pm     = _profile('qv_pm', 'PM')
        cls.coord  = _profile('qv_coord', 'Project Coordinator')
        cls.pm_b   = _profile('qv_pm_b', 'PM')
        cls.worker = _profile('qv_worker', 'Site Engineer')
        cls.qaqc   = _profile('qv_qaqc', 'Site Engineer')
        cls.qaqc_b = _profile('qv_qaqc_b', 'Site Engineer')
        cls.spare  = _profile('qv_spare', 'Site Engineer')

        cls.civil_v1, cls.civil_items = _publish('QV-CIVIL', 'Civil checks', 1, CIVIL_LABELS)
        cls.module_v1, cls.module_items = _publish('QV-MODULE', 'Module checks', 1,
                                                   MODULE_LABELS)
        ChecklistTaskLink.objects.create(checklist=cls.civil_v1,
                                         template_task=_template_task(CIVIL))
        ChecklistTaskLink.objects.create(checklist=cls.module_v1,
                                         template_task=_template_task(MODULE))

    def setUp(self):
        self.site = self._activated_site('QV Site Alpha', self.pm)
        self.site.coordinators.add(self.coord)
        self.other_site = self._activated_site('QV Site Bravo', self.pm_b)
        assign_site_qaqc(self.site, self.qaqc, self.pm)
        assign_site_qaqc(self.other_site, self.qaqc_b, self.pm_b)
        self.civil = Task.objects.get(phase__project=self.site, task_name=CIVIL)
        self.module = Task.objects.get(phase__project=self.site, task_name=MODULE)

        self.storage = self.settings(SUPABASE_BILLS_BUCKET=BUCKET)
        self.storage.enable()
        self.addCleanup(self.storage.disable)
        client_patch = mock.patch('projects.qaqc_views._client')
        self.storage_client = client_patch.start()
        self.addCleanup(client_patch.stop)
        signer_patch = mock.patch('projects.bill_storage.get_design_file_url',
                                  return_value=SIGNED)
        self.signer = signer_patch.start()
        self.addCleanup(signer_patch.stop)

    # -- builders -------------------------------------------------------------

    def _site(self, name, pm, project_type='OPEX'):
        return Project.objects.create(
            customer_name=name, customer_phone='9876543210', site_address='1 Visit Road',
            city='Lucknow', project_type=project_type, dc_capacity_kw=Decimal('100.00'),
            status='Draft', assigned_pm=pm,
        )

    def _activated_site(self, name, pm):
        site = self._site(name, pm)
        response = _client_for(pm).post(reverse('opex_site_activate', args=[site.project_id]))
        self.assertEqual(response.status_code, 302, 'OPEX activation did not redirect')
        site.refresh_from_db()
        assign_tasks_to(
            Task.objects.filter(phase__project=site, assigned_role=Task.SITE_ENGINEER,
                                is_mirror=False),
            self.worker,
        )
        return site

    # -- actions through the real screens ---------------------------------------

    def _start(self, by=None, site=None):
        with self.captureOnCommitCallbacks(execute=True):
            return _client_for(by or self.qaqc).post(
                reverse('qaqc_visit_start', args=[(site or self.site).project_id]))

    def _open_visit(self, site=None):
        return QaqcVisit.objects.get(project=site or self.site, status=QaqcVisit.OPEN)

    def _answer(self, visit, task, item, answer='yes', remark='', photo=True, by=None,
                taken=None, hx=True):
        data = {'answer': answer, 'remark': remark}
        if photo is True:
            data['photo'] = _photo()
        elif photo:
            data['photo'] = photo
        if taken is not None:
            data['photo_taken_at'] = taken
        headers = {'HTTP_HX_REQUEST': 'true'} if hx else {}
        return _client_for(by or self.qaqc).post(
            reverse('qaqc_visit_point',
                    args=[visit.project.project_id, visit.pk, task.pk, item.pk]),
            data, **headers)

    def _submit(self, visit, by=None):
        with self.captureOnCommitCallbacks(execute=True):
            return _client_for(by or self.qaqc).post(
                reverse('qaqc_visit_submit', args=[visit.project.project_id, visit.pk]))

    def _uploads(self):
        return self.storage_client.return_value.storage.from_.return_value.upload.call_count

    def _removes(self):
        return self.storage_client.return_value.storage.from_.return_value.remove.call_count

    def _tomorrow(self, days=1):
        """Patch the IST date the writers read (django.utils.timezone.localdate)."""
        day = timezone.localdate() + timedelta(days=days)
        patcher = mock.patch.object(qaqc_views.timezone, 'localdate', return_value=day)
        patcher.start()
        self.addCleanup(patcher.stop)
        return day


# ---------------------------------------------------------------------------
# 1 — start; one per site per day; next day; one open per site; later-day submit
# ---------------------------------------------------------------------------

class VisitStartTests(VisitFixture):

    def test_the_assigned_engineer_starts_a_dated_open_visit(self):
        response = self._start()
        visit = self._open_visit()
        self.assertRedirects(response, reverse('qaqc_visit_detail',
                                               args=[self.site.project_id, visit.pk]))
        self.assertEqual(visit.engineer, self.qaqc)
        self.assertEqual(visit.visit_date, timezone.localdate())
        self.assertEqual(visit.assignment.engineer, self.qaqc)

    def test_a_second_visit_the_same_day_is_refused(self):
        self._start()
        visit = self._open_visit()
        self._answer(visit, self.civil, self.civil_items[0])
        self._submit(visit)
        response = self._start()
        self.assertIn(qaqc_views.VISIT_ONE_PER_DAY, _messages(response))
        self.assertEqual(QaqcVisit.objects.filter(project=self.site).count(), 1)

    def test_the_next_day_is_allowed(self):
        self._start()
        visit = self._open_visit()
        self._answer(visit, self.civil, self.civil_items[0])
        self._submit(visit)
        tomorrow = self._tomorrow()
        self._start()
        self.assertEqual(self._open_visit().visit_date, tomorrow)

    def test_one_open_visit_per_site(self):
        self._start()
        first = self._open_visit()
        self._tomorrow()
        response = self._start()
        self.assertTrue(any('already has an open visit' in m for m in _messages(response)))
        self.assertEqual(list(QaqcVisit.objects.filter(project=self.site)), [first])

    def test_continued_on_a_later_day_keeps_its_start_date(self):
        self._start()
        visit = self._open_visit()
        started_on = visit.visit_date
        self._tomorrow(days=2)
        self.assertEqual(self._answer(visit, self.civil, self.civil_items[0]).status_code, 200)
        self._submit(visit)
        visit.refresh_from_db()
        self.assertEqual(visit.status, QaqcVisit.SUBMITTED)
        self.assertEqual(visit.visit_date, started_on)

    def test_start_is_refused_while_the_photo_store_is_not_configured(self):
        with self.settings(SUPABASE_BILLS_BUCKET=''):
            response = self._start()
        self.assertIn(qaqc_views.VISIT_STORAGE_NOT_READY, _messages(response))
        self.assertFalse(QaqcVisit.objects.exists())

    def test_the_database_holds_one_per_day_and_one_open(self):
        self._start()
        visit = self._open_visit()
        with self.assertRaises(IntegrityError), transaction.atomic():
            QaqcVisit.objects.create(project=self.site, engineer=self.qaqc,
                                     assignment=visit.assignment,
                                     visit_date=visit.visit_date + timedelta(days=1))
        with self.assertRaises(IntegrityError), transaction.atomic():
            QaqcVisit.objects.create(project=self.site, engineer=self.qaqc,
                                     assignment=visit.assignment,
                                     visit_date=visit.visit_date, status=QaqcVisit.SUBMITTED,
                                     submitted_at=timezone.now())

    def test_the_state_columns_move_together(self):
        self._start()
        visit = self._open_visit()
        with self.assertRaises(IntegrityError), transaction.atomic():
            QaqcVisit.objects.filter(pk=visit.pk).update(status=QaqcVisit.SUBMITTED)
        with self.assertRaises(IntegrityError), transaction.atomic():
            QaqcVisit.objects.filter(pk=visit.pk).update(status=QaqcVisit.LAPSED,
                                                         lapsed_at=timezone.now())

    def test_a_visit_row_is_append_only(self):
        self._start()
        visit = self._open_visit()
        with self.assertRaises(AppendOnlyViolation):
            visit.save()
        with self.assertRaises(AppendOnlyViolation):
            visit.delete()


# ---------------------------------------------------------------------------
# 2 — only the assigned engineer starts, answers and submits
# ---------------------------------------------------------------------------

class WhoMayTests(VisitFixture):

    def setUp(self):
        super().setUp()
        self._start()
        self.visit = self._open_visit()
        self._answer(self.visit, self.civil, self.civil_items[0])

    def test_others_cannot_start(self):
        for profile in (self.pm, self.coord, self.worker):
            with self.subTest(who=profile.user.username):
                self.assertEqual(self._start(by=profile).status_code, 403)
        # Another site's QA/QC engineer cannot even see this site.
        self.assertEqual(self._start(by=self.qaqc_b).status_code, 404)
        self.assertEqual(QaqcVisit.objects.filter(project=self.site).count(), 1)

    def test_others_cannot_answer(self):
        # The PM and coordinator may read an open visit (V-7) but not answer it: 403.
        for profile in (self.pm, self.coord):
            with self.subTest(who=profile.user.username):
                response = self._answer(self.visit, self.civil, self.civil_items[1], by=profile)
                self.assertEqual(response.status_code, 403)
        # The site engineer with a task, and another site's engineer, may not read it: 404.
        for profile in (self.worker, self.qaqc_b):
            with self.subTest(who=profile.user.username):
                response = self._answer(self.visit, self.civil, self.civil_items[1], by=profile)
                self.assertEqual(response.status_code, 404)
        self.assertEqual(QaqcVisitAnswer.objects.count(), 1)

    def test_others_cannot_submit(self):
        for profile, code in ((self.pm, 403), (self.coord, 403), (self.worker, 404),
                              (self.qaqc_b, 404)):
            with self.subTest(who=profile.user.username):
                self.assertEqual(self._submit(self.visit, by=profile).status_code, code)
        self.visit.refresh_from_db()
        self.assertEqual(self.visit.status, QaqcVisit.OPEN)

    def test_an_ended_engineer_cannot_start_answer_or_submit(self):
        end_site_qaqc(self.site, self.pm)
        self.assertEqual(self._start().status_code, 404)   # no longer sees the site
        self.assertEqual(self._answer(self.visit, self.civil, self.civil_items[1]).status_code,
                         404)
        self.assertEqual(self._submit(self.visit).status_code, 404)
        self.assertEqual(QaqcVisitAnswer.objects.count(), 1)

    def test_a_stale_engineer_is_refused_by_the_predicate(self):
        # Role changed without the PM ending the assignment: user_is_site_qaqc() fails.
        # SCM sees every site, so the refusal is the predicate's 403, not a 404.
        self.qaqc.role = 'SCM'
        self.qaqc.save()
        self.assertEqual(self._start().status_code, 403)
        self.assertEqual(self._answer(self.visit, self.civil, self.civil_items[1]).status_code,
                         403)
        self.assertFalse(qaqc_views.user_can_conduct_qaqc_visit(self.qaqc.user, self.site))


# ---------------------------------------------------------------------------
# 3 — photo on every answer, remark on No, the snapshot
# ---------------------------------------------------------------------------

class AnswerRuleTests(VisitFixture):

    def setUp(self):
        super().setUp()
        self._start()
        self.visit = self._open_visit()

    def test_every_answer_without_a_photo_is_refused(self):
        for answer in ('yes', 'no', 'na'):
            with self.subTest(answer=answer):
                response = self._answer(self.visit, self.civil, self.civil_items[0],
                                        answer=answer, remark='Loose', photo=False)
                self.assertContains(response, qaqc_views.PHOTO_REQUIRED)
        self.assertFalse(QaqcVisitAnswer.objects.exists())
        self.assertEqual(self._uploads(), 0)

    def test_no_without_a_remark_is_refused_before_any_upload(self):
        response = self._answer(self.visit, self.civil, self.civil_items[0], answer='no')
        self.assertContains(response, 'requires a remark')
        self.assertFalse(QaqcVisitAnswer.objects.exists())
        self.assertEqual(self._uploads(), 0)

    def test_a_non_photo_file_is_refused(self):
        response = self._answer(self.visit, self.civil, self.civil_items[0],
                                photo=_photo('report.pdf', b'%PDF-1.4', 'application/pdf'))
        self.assertContains(response, 'Photo rejected')
        self.assertFalse(QaqcVisitAnswer.objects.exists())

    def test_an_answer_stores_the_snapshot_and_a_private_photo(self):
        self.assertEqual(self._answer(self.visit, self.civil, self.civil_items[0],
                                      answer='na', remark='Pedestals not cast yet').status_code,
                         200)
        row = QaqcVisitAnswer.objects.get()
        self.assertEqual(row.answer, 'na')
        self.assertEqual(row.item_text_snapshot, CIVIL_LABELS[0])
        self.assertEqual(row.photo_bucket, BUCKET)
        self.assertIn(f'/{qaqc_views.VISIT_PHOTO_KIND}/', row.photo_path)
        self.assertEqual(self._uploads(), 1)

    def test_the_database_requires_the_photo_and_the_remark_on_no(self):
        base = dict(visit=self.visit, task=self.civil, item=self.civil_items[0],
                    item_text_snapshot='x', photo_file_name='a.jpg', photo_bucket=BUCKET,
                    photo_path='p/qaqc-visit/a.jpg')
        with self.assertRaises(IntegrityError), transaction.atomic():
            QaqcVisitAnswer(**{**base, 'answer': 'no', 'remark': ''}).save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            QaqcVisitAnswer(**{**base, 'answer': 'yes', 'photo_path': ''}).save()


# ---------------------------------------------------------------------------
# 4 — editable while open (keeping the photo, Q9), immutable once submitted
# ---------------------------------------------------------------------------

class EditTests(VisitFixture):

    def setUp(self):
        super().setUp()
        self._start()
        self.visit = self._open_visit()
        self._answer(self.visit, self.civil, self.civil_items[0])
        self.row = QaqcVisitAnswer.objects.get()

    def test_an_edit_without_a_photo_keeps_the_photo(self):
        response = self._answer(self.visit, self.civil, self.civil_items[0], answer='no',
                                remark='Bolt missing', photo=False)
        self.assertEqual(response.status_code, 200)
        row = QaqcVisitAnswer.objects.get()
        self.assertEqual((row.answer, row.remark), ('no', 'Bolt missing'))
        self.assertEqual(row.photo_path, self.row.photo_path)
        self.assertEqual(self._uploads(), 1)

    def test_an_edit_with_a_photo_replaces_it_and_removes_the_old_one(self):
        self._answer(self.visit, self.civil, self.civil_items[0], photo=_photo('second.jpg'))
        row = QaqcVisitAnswer.objects.get()
        self.assertNotEqual(row.photo_path, self.row.photo_path)
        self.assertEqual(row.photo_file_name, 'second.jpg')
        self.assertEqual(self._removes(), 1)

    def test_answers_are_refused_after_submit(self):
        self._submit(self.visit)
        response = self._answer(self.visit, self.civil, self.civil_items[0], answer='na',
                                photo=False)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(QaqcVisitAnswer.objects.get().answer, 'yes')
        row = QaqcVisitAnswer.objects.get()
        row.answer = 'na'
        with self.assertRaises(AppendOnlyViolation):
            row.save()
        with self.assertRaises(AppendOnlyViolation):
            row.delete()


# ---------------------------------------------------------------------------
# 5, 7 — submit raises one punch point per No; COD then refused
# ---------------------------------------------------------------------------

class SubmitTests(VisitFixture):

    def setUp(self):
        super().setUp()
        self._start()
        self.visit = self._open_visit()
        self._answer(self.visit, self.civil, self.civil_items[0], answer='no',
                     remark='Bolt missing on pedestal 4')
        self._answer(self.visit, self.civil, self.civil_items[1], answer='yes')
        self._answer(self.visit, self.module, self.module_items[0], answer='no',
                     remark='Clamp loose')
        self._answer(self.visit, self.module, self.module_items[1], answer='na')

    def test_nothing_is_raised_before_submit(self):
        self.assertFalse(PunchPoint.objects.exists())

    def test_one_punch_point_per_no_on_the_right_task(self):
        response = self._submit(self.visit)
        self.assertRedirects(response, reverse('qaqc_visit_detail',
                                               args=[self.site.project_id, self.visit.pk]))
        points = list(PunchPoint.objects.order_by('pk'))
        self.assertEqual(len(points), 2)
        self.assertEqual([p.task for p in points], [self.civil, self.module])
        self.assertTrue(all(p.raised_by == self.qaqc and p.status == PunchPoint.OPEN
                            for p in points))
        self.assertIn(CIVIL_LABELS[0], points[0].reason)
        self.assertIn('Bolt missing on pedestal 4', points[0].reason)
        for answer in QaqcVisitAnswer.objects.all():
            self.assertEqual(answer.punch_point is not None, answer.answer == 'no')
        self.visit.refresh_from_db()
        self.assertEqual(self.visit.status, QaqcVisit.SUBMITTED)
        self.assertIsNotNone(self.visit.submitted_at)

    def test_an_empty_visit_cannot_be_submitted(self):
        # A fresh visit on the other site, with nothing answered.
        with self.captureOnCommitCallbacks(execute=True):
            _client_for(self.qaqc_b).post(reverse('qaqc_visit_start',
                                                  args=[self.other_site.project_id]))
        empty = self._open_visit(self.other_site)
        response = self._submit(empty, by=self.qaqc_b)
        self.assertIn(qaqc_views.VISIT_EMPTY, _messages(response))
        empty.refresh_from_db()
        self.assertEqual(empty.status, QaqcVisit.OPEN)

    def test_a_no_on_an_approved_task_raises_a_point_and_cod_is_refused(self):
        now = timezone.now()
        Task.objects.filter(pk=self.civil.pk).update(
            status=Task.DONE, submitted_at=now, approved_at=now, approved_by=self.pm)
        self._submit(self.visit)
        self.assertTrue(PunchPoint.objects.filter(task=self.civil, status=PunchPoint.OPEN)
                        .exists())
        self.assertTrue(open_punch_points_for_project(self.site).filter(task=self.civil)
                        .exists())
        with self.assertRaises(CodRefused) as refused:
            record_refusal(self.site)
        self.assertTrue(refused.exception.open_points)


# ---------------------------------------------------------------------------
# 6 — the site engineer's checklist answers are never touched (V-4)
# ---------------------------------------------------------------------------

class ExecutionAnswersUntouchedTests(VisitFixture):

    def test_visit_actions_leave_checklist_completions_alone(self):
        # The worker answers two Civil items through the real checklist view.
        Task.objects.filter(pk=self.civil.pk).update(status=Task.IN_PROGRESS)
        for item in self.civil_items[:2]:
            _client_for(self.worker).post(
                reverse('checklist_item_complete',
                        args=[self.site.project_id, self.civil.pk, item.pk]),
                {'answer': 'yes'})
        before = list(ChecklistItemCompletion.objects.order_by('pk').values())
        self.assertEqual(len(before), 2)

        self._start()
        visit = self._open_visit()
        self._answer(visit, self.civil, self.civil_items[0], answer='no', remark='Wrong')
        self._answer(visit, self.civil, self.civil_items[2], answer='yes')
        self._answer(visit, self.civil, self.civil_items[0], answer='na', photo=False)
        self._submit(visit)

        self.assertEqual(list(ChecklistItemCompletion.objects.order_by('pk').values()), before)


# ---------------------------------------------------------------------------
# 8 — coverage counts distinct points on submitted visits only (V-6)
# ---------------------------------------------------------------------------

class CoverageTests(VisitFixture):

    def _coverage(self):
        return site_coverage(self.site, site_checklist_points(self.site))

    def test_distinct_points_across_submitted_visits_only(self):
        self.assertEqual((self._coverage()['verified'], self._coverage()['total']), (0, 7))
        self._start()
        first = self._open_visit()
        self._answer(first, self.civil, self.civil_items[0])
        self._answer(first, self.civil, self.civil_items[1])
        self.assertEqual(self._coverage()['verified'], 0, 'an open visit verifies nothing')
        self._submit(first)
        self.assertEqual(self._coverage()['verified'], 2)

        self._tomorrow()
        self._start()
        second = self._open_visit()
        self._answer(second, self.civil, self.civil_items[1])   # already verified
        self._answer(second, self.module, self.module_items[0])
        self.assertEqual(self._coverage()['verified'], 2)
        self._submit(second)
        coverage = self._coverage()
        self.assertEqual((coverage['verified'], coverage['total'], coverage['percent']),
                         (3, 7, 42))


# ---------------------------------------------------------------------------
# 9 — points use the pinned version (closeout 1a)
# ---------------------------------------------------------------------------

class PinnedPointTests(VisitFixture):

    def test_a_task_pinned_to_v1_offers_v1_after_v2_activates(self):
        Task.objects.filter(pk=self.civil.pk).update(status=Task.IN_PROGRESS)
        _client_for(self.worker).post(
            reverse('checklist_item_complete',
                    args=[self.site.project_id, self.civil.pk, self.civil_items[0].pk]),
            {'answer': 'yes'})
        v2, v2_items = _publish('QV-CIVIL', 'Civil checks', 2, ['V2 only item'])
        other_civil = Task.objects.get(phase__project=self.other_site, task_name=CIVIL)

        points = {e['task'].pk: e for e in site_checklist_points(self.site)}
        self.assertEqual(points[self.civil.pk]['checklist'], self.civil_v1)
        self.assertEqual(points[self.civil.pk]['items'], self.civil_items)
        other = {e['task'].pk: e for e in site_checklist_points(self.other_site)}
        self.assertEqual(other[other_civil.pk]['items'], v2_items)

        self._start()
        visit = self._open_visit()
        self.assertEqual(self._answer(visit, self.civil, self.civil_items[3]).status_code, 200)
        response = self._answer(visit, self.civil, v2_items[0])
        self.assertContains(response, 'is now answering version 1')
        self.assertEqual(QaqcVisitAnswer.objects.count(), 1)


# ---------------------------------------------------------------------------
# Q1 — the batch reader agrees with _checklist_for_task for every task
# ---------------------------------------------------------------------------

class BatchResolverEquivalenceTests(VisitFixture):

    def _assert_equivalent(self, site):
        tasks = list(Task.objects.filter(phase__project=site).select_related('template_task'))
        batch = resolve_checklists_for_tasks(tasks, site)
        for task in tasks:
            single = _checklist_for_task(task, site)
            with self.subTest(task=task.task_name):
                self.assertEqual(getattr(batch[task.pk], 'pk', None),
                                 getattr(single, 'pk', None))

    def _complete(self, task, item, when):
        return ChecklistItemCompletion.objects.create(
            task=task, item=item, is_checked=True, answer='yes', checked_at=when,
            item_text_snapshot=item.label)

    def test_every_case_agrees(self):
        now = timezone.now()
        # Pinned to v1 after v2 activates.
        self._complete(self.civil, self.civil_items[0], now - timedelta(days=3))
        v2, v2_items = _publish('QV-CIVIL', 'Civil checks', 2, ['V2 a', 'V2 b'])
        # Spanning two versions: the latest completion (v2) wins on the module task.
        m2, m2_items = _publish('QV-MODULE', 'Module checks', 2, ['M2 a'])
        self._complete(self.module, self.module_items[0], now - timedelta(days=2))
        self._complete(self.module, m2_items[0], now - timedelta(days=1))
        # A draft holding a completion is never served.
        draft, draft_items = _publish('QV-CIVIL', 'Civil checks', 3, ['Draft'], activate=False)
        other_civil = Task.objects.get(phase__project=self.other_site, task_name=CIVIL)
        self._complete(other_civil, draft_items[0], now)
        # By-name fallback: a hand-added task with no template provenance.
        Task.objects.filter(pk=Task.objects.get(phase__project=self.other_site,
                                                task_name=MODULE).pk).update(template_task=None)
        self._assert_equivalent(self.site)
        self._assert_equivalent(self.other_site)

    def test_no_active_version_and_no_pin_resolves_to_none_in_both(self):
        Checklist.objects.filter(code='QV-MODULE').update(status=Checklist.ARCHIVED)
        self._assert_equivalent(self.site)
        self.assertNotIn(self.module.pk,
                         {e['task'].pk for e in site_checklist_points(self.site)})

    def test_the_batch_is_bounded(self):
        tasks = list(Task.objects.filter(phase__project=self.site)
                     .select_related('template_task'))
        self._complete(self.civil, self.civil_items[0], timezone.now())
        with self.assertNumQueries(3):
            resolve_checklists_for_tasks(tasks, self.site)


# ---------------------------------------------------------------------------
# 10 — the CL-C readiness helper (V-9)
# ---------------------------------------------------------------------------

class ReadinessHelperTests(VisitFixture):

    def test_true_only_after_a_submit(self):
        self.assertFalse(site_has_submitted_qaqc_visit(self.site))
        self._start()
        visit = self._open_visit()
        self._answer(visit, self.civil, self.civil_items[0])
        self.assertFalse(site_has_submitted_qaqc_visit(self.site))
        self._submit(visit)
        self.assertTrue(site_has_submitted_qaqc_visit(self.site))
        self.assertFalse(site_has_submitted_qaqc_visit(self.other_site))


# ---------------------------------------------------------------------------
# 11 — PM dashboard line and the site card
# ---------------------------------------------------------------------------

class DashboardAndCardTests(VisitFixture):

    def _dashboard(self, profile):
        return _client_for(profile).get(reverse('dashboard_pm'))

    def test_the_pm_dashboard_line(self):
        self.assertContains(self._dashboard(self.pm), 'No QA/QC visit yet')
        self._start()
        visit = self._open_visit()
        self._answer(visit, self.civil, self.civil_items[0])
        self._submit(visit)
        self.assertContains(self._dashboard(self.pm), 'Last QA/QC visit: today')
        QaqcVisit.objects.filter(pk=visit.pk).update(
            visit_date=timezone.localdate() - timedelta(days=4))
        self.assertContains(self._dashboard(self.pm), 'Last QA/QC visit: 4 days ago')

    def test_no_line_without_an_engineer_or_a_visit(self):
        end_site_qaqc(self.site, self.pm)
        self.assertNotContains(self._dashboard(self.pm), 'qaqc-visit-line')

    def test_the_line_costs_two_queries_whatever_the_number_of_sites(self):
        today = timezone.localdate()
        sites = [self.site, self.other_site]
        with self.assertNumQueries(2):
            lines = last_visit_lines(sites, today)
        self.assertEqual(lines, {self.site.pk: None, self.other_site.pk: None})
        more = sites + [self._activated_site(f'QV extra {n}', self.pm) for n in range(3)]
        for n, extra in enumerate(more[2:]):
            assign_site_qaqc(extra, _profile(f'qv_extra_qa_{n}', 'Site Engineer'), self.pm)
        with self.assertNumQueries(2):
            lines = last_visit_lines(more, today)
        self.assertEqual(len(lines), 5)
        residential = self._site('Res', self.pm, 'Residential')
        with self.assertNumQueries(0):
            self.assertEqual(last_visit_lines([residential], today), {})

    def test_the_site_card_renders_for_every_viewer(self):
        for profile in (self.pm, self.worker, self.qaqc):
            with self.subTest(who=profile.user.username):
                response = _client_for(profile).get(
                    reverse('project_overview', args=[self.site.project_id]))
                self.assertContains(response, 'id="qaqc-visits-card"')
                self.assertContains(response, '0 of 7 points (0%)')
        engineer_page = _client_for(self.qaqc).get(
            reverse('project_overview', args=[self.site.project_id]))
        self.assertContains(engineer_page, "Start today's visit")
        pm_page = _client_for(self.pm).get(
            reverse('project_overview', args=[self.site.project_id]))
        self.assertNotContains(pm_page, "Start today's visit")

    def test_open_since_shows_to_the_engineer_and_managers_only(self):
        self._start()
        for profile, shown in ((self.qaqc, True), (self.pm, True), (self.coord, True),
                               (self.worker, False)):
            with self.subTest(who=profile.user.username):
                response = _client_for(profile).get(
                    reverse('project_overview', args=[self.site.project_id]))
                (self.assertContains if shown else self.assertNotContains)(
                    response, 'open since')

    def test_the_se_dashboard_row_offers_start_then_continue(self):
        dashboard = reverse('dashboard_site_engineer')
        self.assertContains(_client_for(self.qaqc).get(dashboard), "Start today's visit")
        self._start()
        response = _client_for(self.qaqc).get(dashboard)
        self.assertContains(response, 'Continue visit')
        self.assertContains(response, 'Open since')


# ---------------------------------------------------------------------------
# 12 — notices to the PM and coordinators, none to the engineer (V-8)
# ---------------------------------------------------------------------------

class NoticeTests(VisitFixture):

    def test_pm_and_coordinators_hear_on_submit(self):
        self._start()
        visit = self._open_visit()
        self._answer(visit, self.civil, self.civil_items[0], answer='no', remark='Loose')
        self._answer(visit, self.civil, self.civil_items[1])
        self.assertFalse(NotificationLog.objects.filter(
            template_name=qaqc_views.T_VISIT_SUBMITTED).exists())
        self._submit(visit)
        in_app = NotificationLog.objects.filter(template_name=qaqc_views.T_VISIT_SUBMITTED,
                                                channel='in_app')
        self.assertEqual(set(in_app.values_list('recipient_id', flat=True)),
                         {self.pm.pk, self.coord.pk})
        message = in_app.first().message
        self.assertIn('2 points checked', message)
        self.assertIn('1 No', message)
        self.assertIn('1 punch point raised', message)
        self.assertTrue(NotificationLog.objects.filter(
            template_name=qaqc_views.T_VISIT_SUBMITTED, channel='email',
            recipient=self.pm).exists())
        self.assertFalse(NotificationLog.objects.filter(recipient=self.qaqc,
                                                        template_name=qaqc_views.T_VISIT_SUBMITTED)
                         .exists())


# ---------------------------------------------------------------------------
# 13 — Residential and CAPEX: no card, every URL a 404
# ---------------------------------------------------------------------------

class NonOpexTests(VisitFixture):

    def test_no_card_and_no_urls(self):
        for project_type in ('Residential', 'CAPEX'):
            with self.subTest(project_type=project_type):
                site = self._site(f'QV {project_type}', self.pm, project_type)
                client = _client_for(self.pm)
                page = client.get(reverse('project_overview', args=[site.project_id]))
                self.assertNotContains(page, 'qaqc-visits-card')
                urls = [
                    reverse('qaqc_visit_start', args=[site.project_id]),
                    reverse('qaqc_visit_detail', args=[site.project_id, 1]),
                    reverse('qaqc_visit_point', args=[site.project_id, 1, 1, 1]),
                    reverse('qaqc_visit_submit', args=[site.project_id, 1]),
                    reverse('qaqc_visit_photo', args=[site.project_id, 1]),
                ]
                for url in urls:
                    self.assertEqual(client.post(url).status_code, 404, url)
                    self.assertEqual(client.get(url).status_code, 404, url)


# ---------------------------------------------------------------------------
# V-7 — who reads a visit and its photos
# ---------------------------------------------------------------------------

class ReadTests(VisitFixture):

    def setUp(self):
        super().setUp()
        self._start()
        self.visit = self._open_visit()
        self._answer(self.visit, self.civil, self.civil_items[0])
        self.answer = QaqcVisitAnswer.objects.get()

    def _get(self, profile, name, *args):
        return _client_for(profile).get(reverse(name, args=[self.site.project_id, *args]))

    def test_an_open_visit_is_read_by_the_engineer_and_managers_only(self):
        for profile, code in ((self.qaqc, 200), (self.pm, 200), (self.coord, 200),
                              (self.worker, 404), (self.qaqc_b, 404)):
            with self.subTest(who=profile.user.username):
                self.assertEqual(self._get(profile, 'qaqc_visit_detail', self.visit.pk)
                                 .status_code, code)

    def test_a_submitted_visit_and_its_photo_are_read_by_every_viewer(self):
        self._submit(self.visit)
        page = self._get(self.worker, 'qaqc_visit_detail', self.visit.pk)
        self.assertContains(page, CIVIL_LABELS[0])
        self.assertNotContains(page, 'Check this point')
        photo = self._get(self.worker, 'qaqc_visit_photo', self.answer.pk)
        self.assertRedirects(photo, SIGNED, fetch_redirect_response=False)
        self.assertEqual(self._get(self.qaqc_b, 'qaqc_visit_photo', self.answer.pk)
                         .status_code, 404)

    def test_the_engineer_page_offers_every_point_and_hides_the_execution_answer(self):
        page = self._get(self.qaqc, 'qaqc_visit_detail', self.visit.pk)
        self.assertContains(page, 'Check this point', count=6)
        self.assertNotContains(page, 'checklist_item_complete')


# ---------------------------------------------------------------------------
# Q2 — an open visit lapses when the engineer is replaced or ended
# ---------------------------------------------------------------------------

class LapseTests(VisitFixture):

    def setUp(self):
        super().setUp()
        self._start()
        self.visit = self._open_visit()
        self._answer(self.visit, self.civil, self.civil_items[0], answer='no', remark='Gap')

    def _assert_lapsed(self, reason):
        self.visit.refresh_from_db()
        self.assertEqual((self.visit.status, self.visit.lapse_reason),
                         (QaqcVisit.LAPSED, reason))
        self.assertEqual(QaqcVisitAnswer.objects.filter(visit=self.visit).count(), 1)
        self.assertFalse(PunchPoint.objects.exists())
        self.assertFalse(site_has_submitted_qaqc_visit(self.site))
        self.assertEqual(site_coverage(self.site, site_checklist_points(self.site))['verified'],
                         0)

    def test_replace_lapses_and_the_new_engineer_starts_the_same_day(self):
        assign_site_qaqc(self.site, self.spare, self.pm)
        self._assert_lapsed(QaqcVisit.LAPSE_REPLACED)
        self._start(by=self.spare)
        fresh = self._open_visit()
        self.assertEqual((fresh.engineer, fresh.visit_date),
                         (self.spare, self.visit.visit_date))

    def test_end_lapses(self):
        end_site_qaqc(self.site, self.pm)
        self._assert_lapsed(QaqcVisit.LAPSE_ENDED)

    def test_a_lapsed_visit_stays_readable_by_the_managers(self):
        end_site_qaqc(self.site, self.pm)
        page = _client_for(self.pm).get(
            reverse('qaqc_visit_detail', args=[self.site.project_id, self.visit.pk]))
        self.assertContains(page, 'Lapsed')
        self.assertContains(page, CIVIL_LABELS[0])


# ---------------------------------------------------------------------------
# 14 — pre-flight 5 rulings: mirrors and Not Applicable tasks are not points
# ---------------------------------------------------------------------------

class PointRulingTests(VisitFixture):

    def test_mirror_tasks_are_never_points(self):
        mirror = Task.objects.filter(phase__project=self.site, is_mirror=True).first()
        mirror_list, mirror_items = _publish('QV-MIRROR', 'Mirror checks', 1, ['Never asked'])
        ChecklistTaskLink.objects.create(checklist=mirror_list,
                                         template_task=mirror.template_task)
        self.assertNotIn(mirror.pk, {e['task'].pk for e in site_checklist_points(self.site)})
        self._start()
        visit = self._open_visit()
        response = self._answer(visit, mirror, mirror_items[0])
        self.assertContains(response, 'is not checked on a QA/QC visit')
        self.assertFalse(QaqcVisitAnswer.objects.exists())

    def test_not_applicable_tasks_leave_points_and_coverage(self):
        self._start()
        visit = self._open_visit()
        self._answer(visit, self.module, self.module_items[0])
        self._submit(visit)
        self.assertEqual(site_coverage(self.site, site_checklist_points(self.site))['verified'],
                         1)
        Task.objects.filter(pk=self.module.pk).update(is_not_applicable=True)
        coverage = site_coverage(self.site, site_checklist_points(self.site))
        self.assertEqual((coverage['verified'], coverage['total']), (0, 4))
        # The answer is kept as history.
        self.assertEqual(QaqcVisitAnswer.objects.filter(task=self.module).count(), 1)


# ---------------------------------------------------------------------------
# Q5 — photo_taken_at and the downscale fallback
# ---------------------------------------------------------------------------

class PhotoTests(VisitFixture):

    def setUp(self):
        super().setUp()
        self._start()
        self.visit = self._open_visit()

    def test_photo_taken_at_is_stored_when_present(self):
        self._answer(self.visit, self.civil, self.civil_items[0], taken='2026:10:01 09:15:30')
        taken = timezone.localtime(QaqcVisitAnswer.objects.get().photo_taken_at)
        self.assertEqual((taken.year, taken.month, taken.day, taken.hour, taken.minute),
                         (2026, 10, 1, 9, 15))

    def test_photo_taken_at_is_null_when_absent_or_unusable(self):
        for n, taken in enumerate((None, '', 'not a date', '2000:01:01 00:00:00')):
            with self.subTest(taken=taken):
                self._answer(self.visit, self.civil, self.civil_items[n], taken=taken)
                self.assertIsNone(QaqcVisitAnswer.objects.get(item=self.civil_items[n])
                                  .photo_taken_at)

    def test_the_original_photo_is_accepted_when_the_browser_could_not_downscale(self):
        original = _photo('IMG_0001.png', b'\x89PNG\r\n\x1a\n original bytes', 'image/png')
        response = self._answer(self.visit, self.civil, self.civil_items[0], photo=original)
        self.assertEqual(response.status_code, 200)
        row = QaqcVisitAnswer.objects.get()
        self.assertEqual(row.photo_file_name, 'IMG_0001.png')
        self.assertIsNone(row.photo_taken_at)


# ---------------------------------------------------------------------------
# Q6 — raise_punch_point() is the only creation path
# ---------------------------------------------------------------------------

class PunchPointCreationTripwireTests(TestCase):

    def test_punch_points_are_created_only_in_punch_points_py(self):
        root = Path(__file__).resolve().parent
        pattern = re.compile(r'PunchPoint\.objects\.(create|bulk_create|get_or_create)\(')
        offenders = []
        for path in root.rglob('*.py'):
            if path.name.startswith('tests') or 'migrations' in path.parts:
                continue
            if pattern.search(path.read_text(encoding='utf-8')):
                offenders.append(path.name)
        self.assertEqual(offenders, ['punch_points.py'])


# ---------------------------------------------------------------------------
# The visit page's counts follow each save; Review and submit at any count
# ---------------------------------------------------------------------------

def _text(response):
    """The response body with runs of whitespace folded to one space."""
    return ' '.join(response.content.decode().split())


class VisitPageCountsTests(VisitFixture):
    """Saving a point swapped only its row, so the header ("N points checked, M No"),
    the Review and submit button gated on it, and the task's "N / M" kept their
    page-load values until a reload. Every point POST now carries them out of band."""

    def setUp(self):
        super().setUp()
        self._start()
        self.visit = self._open_visit()

    def _detail(self, by=None):
        return _client_for(by or self.qaqc).get(
            reverse('qaqc_visit_detail', args=[self.site.project_id, self.visit.pk]))

    def _header_oob(self, text):
        return f'<span id="visit-counts" hx-swap-oob="true">{text}</span>'

    def _task_oob(self, task, text):
        return (f'<span id="task-count-{task.pk}" class="small text-muted" '
                f'hx-swap-oob="true">{text}</span>')

    def test_review_and_submit_is_shown_at_zero_points(self):
        page = self._detail()
        self.assertContains(page, 'Review and submit')
        self.assertIn('0 points checked.', _text(page))

    def test_each_save_returns_the_header_and_task_counts(self):
        first = self._answer(self.visit, self.civil, self.civil_items[0])
        self.assertContains(first, self._header_oob('1 point checked.'), html=True)
        self.assertContains(first, self._task_oob(self.civil, '1 / 4'), html=True)
        second = self._answer(self.visit, self.civil, self.civil_items[1], answer='no',
                              remark='Bolt missing')
        self.assertContains(
            second,
            self._header_oob('2 points checked, '
                             '<span class="text-danger fw-semibold">1 No</span>.'),
            html=True)
        self.assertContains(second, self._task_oob(self.civil, '2 / 4'), html=True)
        # The other task's count is not touched by a Civil save.
        self.assertNotContains(second, f'task-count-{self.module.pk}')

    def test_a_yes_to_no_edit_updates_the_no_count(self):
        self._answer(self.visit, self.civil, self.civil_items[0])
        edit = self._answer(self.visit, self.civil, self.civil_items[0], answer='no',
                            remark='Loose on second look', photo=False)
        self.assertEqual(edit.status_code, 200)
        self.assertContains(
            edit,
            self._header_oob('1 point checked, '
                             '<span class="text-danger fw-semibold">1 No</span>.'),
            html=True)
        self.assertContains(edit, self._task_oob(self.civil, '1 / 4'), html=True)

    def test_a_refused_answer_still_returns_the_counts(self):
        self._answer(self.visit, self.module, self.module_items[0])
        refused = self._answer(self.visit, self.module, self.module_items[1], answer='no')
        self.assertContains(refused, 'requires a remark')
        self.assertContains(refused, self._header_oob('1 point checked.'), html=True)
        self.assertContains(refused, self._task_oob(self.module, '1 / 3'), html=True)

    def test_opening_a_point_form_carries_no_counts(self):
        form = _client_for(self.qaqc).get(
            reverse('qaqc_visit_point', args=[self.site.project_id, self.visit.pk,
                                              self.civil.pk, self.civil_items[0].pk]))
        self.assertNotContains(form, 'hx-swap-oob')

    def test_the_full_page_draws_the_same_ids(self):
        self._answer(self.visit, self.civil, self.civil_items[0])
        page = self._detail()
        self.assertContains(page, '<span id="visit-counts">1 point checked.</span>',
                            html=True)
        self.assertContains(page, f'<span id="task-count-{self.civil.pk}" '
                                  f'class="small text-muted">1 / 4</span>', html=True)
        self.assertNotContains(page, 'hx-swap-oob')

    def test_a_submitted_visit_shows_no_submit_button(self):
        self._answer(self.visit, self.civil, self.civil_items[0])
        self._submit(self.visit)
        for who in (self.qaqc, self.pm):
            with self.subTest(who=who.user.username):
                page = self._detail(by=who)
                self.assertNotContains(page, 'Review and submit')
                self.assertNotContains(page, '>Edit</button>')


class ReviewAndSubmitTests(VisitFixture):
    """"Review and submit" opens the review page; the final Submit visit is the one
    irreversible act, states its consequence, and is single-shot."""

    def setUp(self):
        super().setUp()
        self._start()
        self.visit = self._open_visit()

    def _review(self, by=None):
        return _client_for(by or self.qaqc).get(
            reverse('qaqc_visit_submit', args=[self.site.project_id, self.visit.pk]))

    def test_the_zero_point_review_page_explains_and_offers_no_submit(self):
        page = self._review()
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, qaqc_views.VISIT_EMPTY)
        self.assertNotContains(page, 'id="visitSubmit"')

    def test_a_zero_point_submit_is_refused_readably(self):
        response = self._submit(self.visit)
        self.assertRedirects(response, reverse('qaqc_visit_detail',
                                               args=[self.site.project_id, self.visit.pk]))
        self.assertIn(qaqc_views.VISIT_EMPTY, _messages(response))
        self.visit.refresh_from_db()
        self.assertEqual(self.visit.status, QaqcVisit.OPEN)

    def test_the_review_page_states_the_consequence(self):
        self._answer(self.visit, self.civil, self.civil_items[0], answer='no', remark='Loose')
        self._answer(self.visit, self.civil, self.civil_items[1])
        self._answer(self.visit, self.module, self.module_items[0])
        text = _text(self._review())
        self.assertIn('3 points checked, 1 No. 1 punch point will be raised. '
                      'This cannot be changed after submitting.', text)
        self.assertIn('>Submit visit</button>', text)
        self.assertIn('Back to the visit</a> </form>', text)

    def test_with_no_no_answers_the_punch_point_clause_is_left_out(self):
        self._answer(self.visit, self.civil, self.civil_items[0])
        text = _text(self._review())
        self.assertIn('<div class="alert alert-warning small mb-3" id="visitSubmitConsequence"> '
                      '1 point checked, 0 No. This cannot be changed after submitting. </div>',
                      text)

    def test_review_then_back_leaves_the_visit_open(self):
        self._answer(self.visit, self.civil, self.civil_items[0], answer='no', remark='Loose')
        notices = NotificationLog.objects.count()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self._review().status_code, 200)
            back = _client_for(self.qaqc).get(
                reverse('qaqc_visit_detail', args=[self.site.project_id, self.visit.pk]))
        self.assertContains(back, 'Review and submit')
        self.visit.refresh_from_db()
        self.assertEqual(self.visit.status, QaqcVisit.OPEN)
        self.assertEqual(QaqcVisitAnswer.objects.filter(visit=self.visit).count(), 1)
        self.assertFalse(PunchPoint.objects.exists())
        self.assertEqual(NotificationLog.objects.count(), notices)

    def test_a_second_submit_is_refused_readably_and_raises_nothing_more(self):
        self._answer(self.visit, self.civil, self.civil_items[0], answer='no', remark='Loose')
        self._submit(self.visit)
        points = PunchPoint.objects.count()
        notices = NotificationLog.objects.count()
        again = self._submit(self.visit)
        self.assertRedirects(again, reverse('qaqc_visit_detail',
                                            args=[self.site.project_id, self.visit.pk]))
        self.assertIn(qaqc_views.VISIT_NOT_OPEN, _messages(again))
        self.assertEqual(points, 1)
        self.assertEqual(PunchPoint.objects.count(), 1)
        self.assertEqual(NotificationLog.objects.count(), notices)
        self.assertEqual(NotificationLog.objects.filter(
            template_name=qaqc_views.T_VISIT_SUBMITTED, channel='in_app').count(), 2)

    def test_after_submit_everyone_else_and_every_get_keep_the_bare_403(self):
        self._answer(self.visit, self.civil, self.civil_items[0])
        self._submit(self.visit)
        for who in (self.pm, self.coord):
            with self.subTest(who=who.user.username):
                response = self._submit(self.visit, by=who)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(_messages(response), [])
        # The engineer's GET of the review page of a submitted visit: still a plain 403.
        self.assertEqual(self._review().status_code, 403)

    def test_the_final_button_is_disabled_on_submit_and_restored_on_pageshow(self):
        self._answer(self.visit, self.civil, self.civil_items[0])
        page = self._review().content.decode()
        self.assertIn("btn.disabled = true;", page)
        self.assertIn("window.addEventListener('pageshow'", page)
