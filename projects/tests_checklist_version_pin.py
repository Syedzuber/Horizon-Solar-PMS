"""
Checklist version pinning — closeout step 1a: a task keeps the version it started on.

WHY THIS FILE EXISTS
--------------------
`_checklist_for_task()` used to serve the ACTIVE version of a checklist family, and
`_checklist_context()` reads completions only for the served version's items. A new
version is a new Checklist row with new ChecklistItem rows, so the moment v2 went live
every task answered under v1 rendered v2's items blank. A Done task could not answer them
again (checklist_answers_open() is False). The answers were still in the table — just not
shown. SCMPILOT01's Done Civil Work task, 11 answers on OPEX-INST-CIVIL-MMS v1, was exposed.
Evidence: docs/CLOSEOUT_MIRROR_AUDIT.md §4e.

THE RULE PINNED HERE
    Any completion on the linked family   → served the version those items belong to,
                                            whatever its status (active or archived).
    No completion                         → served the active version.
    Open and pinned                       → may finish answering the pinned version.
    Draft                                 → never served, pinned or not.
    Completions spanning two versions     → the version holding the most recent one.
    Posting another version's item        → refused with a reason, nothing written.
    Duplicate-for-locations panel         → shows the ACTIVE version, because that is what
                                            a new copy (no answers) is served.

FIXTURE SHAPE. Two OPEX sites REALLY activated through opex_site_activate, so every task
carries the template_task the link keys on. v1 is authored the way the seed authors it —
draft, items, activate() — with requires_photo=False so answering needs no upload stub.
Site A's Civil Work task answers three of v1's five items THROUGH THE REAL VIEW. Each test
publishes v2 (or v3) itself; the test transaction rolls the publish back.

Run with:
    python manage.py test projects.tests_checklist_version_pin --settings=solarpms.test_settings
"""
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import (
    Checklist, ChecklistItem, ChecklistItemCompletion, ChecklistTaskLink, Project, Task,
)
from .tests_checklist_task_link import _client_for, _profile, _seed_opex
from .views import _checklist_context, _checklist_for_task

CIVIL    = 'Civil Work and MMS Installation'
INVERTER = 'Inverter Installation'

V1_LABELS = ['Pedestal levels checked', 'Anchor bolts torqued', 'MMS alignment verified',
             'Grouting cured', 'Earthing strip bonded']
V2_LABELS = ['V2 pedestal survey recorded', 'V2 bolt torque logged']


def _activate_opex(pm, name):
    """An OPEX site activated through the product's own view, so its tasks carry
    template_task. Raises rather than asserts: setUpTestData has no TestCase instance."""
    site = Project.objects.create(
        customer_name=name, customer_phone='9876543211', site_address='1 Pin Road',
        city='Lucknow', project_type='OPEX', dc_capacity_kw=Decimal('100.00'),
        status='Draft', assigned_pm=pm,
    )
    response = _client_for(pm).post(reverse('opex_site_activate', args=[site.project_id]))
    site.refresh_from_db()
    if response.status_code != 302 or site.status != 'Active':
        raise RuntimeError(f'OPEX activation of {name} did not take')
    return site


def _task(site, name):
    task = Task.objects.get(phase__project=site, task_name=name)
    if task.template_task_id is None:
        raise RuntimeError(f'{name} on {site.project_id} has no template_task')
    return task


def _publish(code, name, version_no, labels, activate=True):
    """Author a version the R-7 way: draft, items, then activate() (unless a draft is
    wanted). activate() archives the family's previous active version."""
    checklist = Checklist.objects.create(code=code, name=name, version_no=version_no,
                                         requires_photo=False)
    items = [ChecklistItem.objects.create(checklist=checklist, label=label, order=n)
             for n, label in enumerate(labels, start=1)]
    if activate:
        checklist.activate()
    return checklist, items


class VersionPinFixture(TestCase):

    @classmethod
    def setUpTestData(cls):
        _seed_opex()
        cls.pm = _profile('pin_pm', 'PM')
        cls.site  = _activate_opex(cls.pm, 'Pin Site A')
        cls.site_b = _activate_opex(cls.pm, 'Pin Site B')
        cls.task  = _task(cls.site, CIVIL)       # answers 3 of v1's items below
        cls.fresh = _task(cls.site_b, CIVIL)     # same template task, never answered

        cls.v1, cls.v1_items = _publish('PIN-CIVIL', 'Civil Work', 1, V1_LABELS)
        ChecklistTaskLink.objects.create(checklist=cls.v1,
                                         template_task=cls.task.template_task)

        client = _client_for(cls.pm)
        for item in cls.v1_items[:3]:
            client.post(reverse('checklist_item_complete',
                                args=[cls.site.project_id, cls.task.pk, item.pk]),
                        {'answer': 'yes'})
        if ChecklistItemCompletion.objects.filter(task=cls.task, is_checked=True).count() != 3:
            raise RuntimeError('fixture: the three v1 answers were not recorded')

    # -- helpers --------------------------------------------------------------

    def _publish_v2(self):
        v2, items = _publish(self.v1.code, 'Civil Work', 2, V2_LABELS)
        self.v1.refresh_from_db()
        self.assertEqual(self.v1.status, Checklist.ARCHIVED, 'v2 did not archive v1')
        return v2, items

    def _set_status(self, task, status):
        Task.objects.filter(pk=task.pk).update(status=status)
        task.refresh_from_db()

    def _post(self, task, item, hx=False):
        headers = {'HTTP_HX_REQUEST': 'true'} if hx else {}
        return _client_for(self.pm).post(
            reverse('checklist_item_complete',
                    args=[task.phase.project.project_id, task.pk, item.pk]),
            {'answer': 'yes'}, **headers)

    def _context(self, task):
        request = RequestFactory().get('/')
        request.user = self.pm.user
        return _checklist_context(request, task.phase.project, task)

    def _page(self, task):
        return _client_for(self.pm).get(
            reverse('task_detail', args=[task.phase.project.project_id, task.pk]))

    def _answered(self, task):
        return set(ChecklistItemCompletion.objects.filter(task=task, is_checked=True)
                   .values_list('item_id', flat=True))


class AnsweredTaskKeepsItsVersionTests(VersionPinFixture):

    def test_1_v1_answers_and_snapshots_still_render_after_v2_activates(self):
        self._publish_v2()
        # Reword a v1 line behind R-7's back, so a passing render proves the SNAPSHOT is
        # what is shown for an answered row, not the live label.
        ChecklistItem.objects.filter(pk=self.v1_items[0].pk).update(label='Reworded later')

        self.assertEqual(_checklist_for_task(self.task, self.site), self.v1)
        ctx = self._context(self.task)
        self.assertEqual([r['item'].pk for r in ctx['checklist_rows']],
                         [i.pk for i in self.v1_items])
        answered = [r for r in ctx['checklist_rows']
                    if r['completion'] is not None and r['completion'].is_checked]
        self.assertEqual(len(answered), 3)
        self.assertEqual(answered[0]['label'], V1_LABELS[0])

        content = self._page(self.task).content.decode()
        for label in V1_LABELS:
            self.assertIn(label, content)
        self.assertNotIn('Reworded later', content)
        self.assertNotIn(V2_LABELS[0], content)

    def test_2_done_task_shows_its_answers_and_cannot_answer_again(self):
        self._publish_v2()
        self._set_status(self.task, Task.DONE)

        ctx = self._context(self.task)
        self.assertEqual(ctx['checklist'], self.v1)
        self.assertTrue(ctx['checklist_has_answers'])
        self.assertFalse(ctx['can_complete_items'])
        self.assertEqual(sum(1 for r in ctx['checklist_rows'] if r['completion']), 3)

        before = self._answered(self.task)
        response = self._post(self.task, self.v1_items[3])
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._answered(self.task), before, 'a closed task took an answer')

        content = self._page(self.task).content.decode()
        for label in V1_LABELS[:3]:
            self.assertIn(label, content)

    def test_3_open_task_may_finish_answering_its_pinned_version(self):
        self._publish_v2()
        for item in self.v1_items[3:]:
            response = self._post(self.task, item)
            self.assertEqual(response.status_code, 302)
        self.assertEqual(self._answered(self.task), {i.pk for i in self.v1_items})
        self.assertEqual(_checklist_for_task(self.task, self.site), self.v1)

    def test_q4_archived_with_no_successor_still_serves_answered_tasks(self):
        """Ruling Q4: the portal's archive action (status=archived, no v2) withdraws the
        checklist from unanswered tasks only. An answered task keeps it as history, and an
        open one may still finish it."""
        Checklist.objects.filter(pk=self.v1.pk).update(status=Checklist.ARCHIVED)

        self.assertEqual(_checklist_for_task(self.task, self.site), self.v1)
        self.assertIsNone(_checklist_for_task(self.fresh, self.site_b))
        self.assertEqual(self._post(self.task, self.v1_items[3]).status_code, 302)
        self.assertIn(self.v1_items[3].pk, self._answered(self.task))


class UnansweredTaskTests(VersionPinFixture):

    def test_4_a_task_with_no_answers_is_served_the_active_version(self):
        v2, v2_items = self._publish_v2()
        self.assertEqual(_checklist_for_task(self.fresh, self.site_b), v2)
        self.assertEqual([r['item'].pk for r in self._context(self.fresh)['checklist_rows']],
                         [i.pk for i in v2_items])

        # Its first answer pins it to v2, as the rule says.
        self.assertEqual(self._post(self.fresh, v2_items[0]).status_code, 302)
        self.assertEqual(_checklist_for_task(self.fresh, self.site_b), v2)

    def test_6_a_draft_version_is_never_served(self):
        v2, _ = self._publish_v2()
        v3, v3_items = _publish(self.v1.code, 'Civil Work', 3, ['V3 draft line'],
                                activate=False)

        self.assertEqual(_checklist_for_task(self.task, self.site), self.v1)
        self.assertEqual(_checklist_for_task(self.fresh, self.site_b), v2)

        # Not even to a task holding a completion on it (only reachable from the shell).
        ChecklistItemCompletion.objects.create(
            item=v3_items[0], task=self.fresh, is_checked=True, answer='yes',
            item_text_snapshot='V3 draft line', checked_at=timezone.now())
        self.assertEqual(_checklist_for_task(self.fresh, self.site_b), v2)

        # And it is not answerable: refused with a reason, nothing written.
        before = ChecklistItemCompletion.objects.count()
        response = self._post(self.fresh, v3_items[0], hx=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('version 3', response.content.decode())
        self.assertEqual(ChecklistItemCompletion.objects.count(), before)

        # With no active version at all, the draft is still not served.
        Checklist.objects.filter(pk=v2.pk).update(status=Checklist.ARCHIVED)
        ChecklistItemCompletion.objects.filter(task=self.fresh).delete()
        self.assertIsNone(_checklist_for_task(self.fresh, self.site_b))


class WritePathTests(VersionPinFixture):

    def test_5_posting_a_v2_item_to_a_task_pinned_on_v1_is_refused(self):
        _v2, v2_items = self._publish_v2()
        before = ChecklistItemCompletion.objects.count()

        response = self._post(self.task, v2_items[0], hx=True)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('version 2 of this checklist', content)
        self.assertIn('answering version 1', content)

        response = self._post(self.task, v2_items[0])
        self.assertEqual(response.status_code, 302)

        self.assertEqual(ChecklistItemCompletion.objects.count(), before)
        self.assertFalse(ChecklistItemCompletion.objects.filter(
            task=self.task, item__in=v2_items).exists())

    def test_an_unrelated_checklists_item_is_still_a_404(self):
        _other, foreign = _publish('PIN-OTHER', 'Unrelated', 1, ['Foreign line'])
        before = ChecklistItemCompletion.objects.count()
        self.assertEqual(self._post(self.task, foreign[0]).status_code, 404)
        self.assertEqual(ChecklistItemCompletion.objects.count(), before)


class SpanAndCostTests(VersionPinFixture):

    def test_7_context_query_count_does_not_grow_with_item_count(self):
        """Two pinned tasks on the same resolution path, one checklist of 5 items and one
        of 25: the context must cost the same number of queries."""
        self._publish_v2()                                 # task: pinned on archived v1
        other = _task(self.site, INVERTER)
        big, big_items = _publish('PIN-INVERTER', 'Inverter', 1,
                                  [f'Inverter line {n}' for n in range(1, 26)])
        ChecklistTaskLink.objects.create(checklist=big, template_task=other.template_task)
        self.assertEqual(self._post(other, big_items[0]).status_code, 302)

        # Warm both once: the first call also loads task.phase, phase.project and
        # user.profile, which are then cached on the instances. Measuring cold would
        # compare that one-off cost, not the per-item cost this test is about.
        self._context(self.task)
        self._context(other)
        with CaptureQueriesContext(connection) as small_run:
            self._context(self.task)
        with CaptureQueriesContext(connection) as big_run:
            self._context(other)
        self.assertEqual(len(big_run), len(small_run),
                         'checklist context cost grows with the number of items')

    def test_8_completions_spanning_two_versions_pin_to_the_most_recent(self):
        v2, v2_items = self._publish_v2()
        latest_v1 = (ChecklistItemCompletion.objects.filter(task=self.task)
                     .order_by('-checked_at').values_list('checked_at', flat=True).first())
        # A span the view can no longer create — what a task answered across a version
        # change before this rule shipped holds.
        span = ChecklistItemCompletion.objects.create(
            item=v2_items[0], task=self.task, is_checked=True, answer='yes',
            item_text_snapshot=V2_LABELS[0], checked_at=latest_v1 + timedelta(hours=1))
        self.assertEqual(_checklist_for_task(self.task, self.site), v2)

        ChecklistItemCompletion.objects.filter(pk=span.pk).update(
            checked_at=latest_v1 - timedelta(days=1))
        self.assertEqual(_checklist_for_task(self.task, self.site), self.v1)


class DuplicatePanelTests(VersionPinFixture):

    def test_q2_panel_describes_the_active_version_new_copies_get(self):
        """Ruling Q2: the source is pinned to v1 by its answers, but a copy starts with
        none and is served v2 — so the panel names v2."""
        v2, v2_items = self._publish_v2()
        response = _client_for(self.pm).get(
            reverse('task_duplicate_locations', args=[self.site.project_id, self.task.pk]),
            HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['checklist'], v2)
        self.assertEqual(response.context['checklist_item_count'], len(v2_items))
