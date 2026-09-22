"""
Every responder that draws `_task_row.html` draws the same row the page does.

THE DEFECT PINNED HERE. task_add and Duplicate for locations (both through
`_render_task_add_success_hx`) and the Design Head assign (through
`_render_task_assign_design_success_hx`) built their row context by hand and left out
the two-step flags. A redrawn OPEX row then offered Done, which rung 1 of
`_apply_task_status_change()` refuses, and dropped the Submit for approval button. The
single-row swap (`_render_task_row_hx`) also dropped the delivery consignment line on a
mirror row. All four HTMX responders now take their context from `_task_row_context()`
and their counts from `_attach_delivery_consignments()`.

THE RULES, each by a test named for it
    OPEX after each response   no Done on a row that is not Done; Submit for approval
                               on an In Progress non-mirror row the viewer may submit.
    Residential                each response draws the rows project_overview draws,
                               Done still offered.
    Consignments               a single-row swap on a delivery mirror keeps the
                               "N/M consignments received" line.
    Parity (regression guard)  a Residential, a non-mirror OPEX and a mirror row come
                               out of project_overview, `_render_task_row_hx` and
                               `_render_phase_tasks_hx` byte-identical (CSRF token and
                               the oob attribute aside).

Run with:
    python manage.py test projects.tests_row_render_flags --settings=solarpms.test_settings
"""
import re
from datetime import date
from decimal import Decimal

from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from .models import DCLineItem, DeliveryChallan, ProjectPhase, Task
from .tests_phase_list_approval import PhaseListFixture
from .tests_two_step_completion import _client_for, _profile
from .utils import assign_task_to
from .views import (
    _render_phase_tasks_hx, _render_task_add_success_hx, _render_task_row_hx,
)

MIRROR_NAME     = 'Delivery — Solar Panels'
MIRROR_CATEGORY = 'Solar Modules'
SUBMIT_LABEL    = re.compile(r'>\s*Submit for approval\s*<')
CONSIGNMENTS    = re.compile(r'\d+/\d+ consignments? received')


def _norm(html):
    """One row's markup, with what legitimately differs between two renders removed:
    the per-render CSRF token and the out-of-band marker a swap adds."""
    html = re.sub(r'name="csrfmiddlewaretoken" value="[^"]+"', 'CSRF', html)
    html = html.replace(' hx-swap-oob="true"', '')
    return re.sub(r'\s+', ' ', html).strip()


def _rows(html):
    """{task pk: normalised <tr>} for every task row in a response."""
    rows = {}
    for match in re.finditer(r'id="task-row-(\d+)"', html):
        start = html.rfind('<tr', 0, match.start())
        rows[int(match.group(1))] = _norm(html[start:html.find('</tr>', start) + 5])
    return rows


def _options(row):
    return re.findall(r'<option value="([^"]+)"', row)


class RowFlagsFixture(PhaseListFixture):
    """PhaseListFixture's OPEX site (self.task = Module Installation, held by the SE,
    put In Progress here) plus a QA/QC holder on the site. The QA/QC holder is an
    independent approver, so the PM's button reads "Submit for approval" rather than
    "Complete (self-certified)"."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.qaqc = _profile('rrf_qaqc', 'Site Engineer', is_qaqc=True)

    def setUp(self):
        super().setUp()
        self._in_progress()
        self.phase = self.task.phase
        sight = (Task.objects.filter(phase__project=self.site, is_mirror=False,
                                     assigned_role=Task.SITE_ENGINEER)
                 .exclude(pk=self.task.pk).first())
        assign_task_to(sight, self.qaqc, notify=False)

    # -- helpers --------------------------------------------------------------

    def _hx_post(self, url_name, args, data, profile=None):
        response = _client_for(profile or self.pm).post(
            reverse(url_name, args=args), data, HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 200, response.content[:300])
        return response.content.decode()

    def _overview_rows(self, project, profile=None):
        response = _client_for(profile or self.pm).get(
            reverse('project_overview', args=[project.project_id]))
        self.assertEqual(response.status_code, 200)
        return _rows(response.content.decode())

    def _direct(self, responder, *args):
        """Call a responder as a view would, for a path no view can reach."""
        request = RequestFactory().post('/')
        request.user = self.pm.user
        return responder(request, *args).content.decode()

    def assertOpexRowsHonest(self, rows):
        """No row offers a Done the server would refuse, and at least one editable row
        was checked (so the assertion cannot pass on markup with no select at all)."""
        statuses = dict(Task.objects.filter(pk__in=rows).values_list('pk', 'status'))
        editable = 0
        for pk, row in rows.items():
            if '<select name="status"' not in row:
                continue
            editable += 1
            if statuses[pk] != Task.DONE:
                self.assertNotIn(Task.DONE, _options(row), f'row {pk} offers Done')
        self.assertGreater(editable, 0, 'no editable row in the response')

    def assertOffersSubmit(self, row):
        self.assertIn('openSubmitApprovalModal(this)', row)
        self.assertRegex(row, SUBMIT_LABEL)

    def assertMatchesOverview(self, rows, project):
        page = self._overview_rows(project)
        self.assertTrue(rows)
        for pk, row in rows.items():
            self.assertEqual(row, page[pk], f'row {pk} differs from project_overview')

    def _hand_design_task(self, project, status=Task.IN_PROGRESS):
        """A non-mirror Design-role task. Every Design task an OPEX site is seeded with
        is a mirror (no status control, no approval block), so the modal responder's
        two-step behaviour is only visible on one added by hand."""
        phase = ProjectPhase.objects.filter(project=project).order_by('phase_order').first()
        last = Task.objects.filter(phase=phase).order_by('-task_order').first()
        return Task.objects.create(
            phase=phase, task_name='Hand design step', task_order=last.task_order + 1,
            assigned_role=Task.DESIGN, task_type='Internal', status=status)

    def _design_head_assign(self, project, task):
        self.pm.is_design_head = True
        self.pm.save(update_fields=['is_design_head'])
        return self._hx_post('task_assign_design_head', [project.project_id, task.pk],
                             {'assigned_to': self.designer.pk})


# ---------------------------------------------------------------------------
# 1 — OPEX: the three responders that dropped the flags.
# ---------------------------------------------------------------------------

class OpexAfterResponseTests(RowFlagsFixture):

    def test_task_add_rows_offer_no_done_and_offer_submit(self):
        html = self._hx_post('task_add', [self.site.project_id], {
            'task_name': 'Extra installation step', 'phase': self.phase.pk,
            'assigned_role': Task.SITE_ENGINEER, 'assigned_to': self.se.pk,
            'due_date': timezone.localdate().isoformat()})
        rows = _rows(html)
        self.assertEqual(set(rows), set(Task.objects.filter(phase=self.phase)
                                        .values_list('pk', flat=True)))
        self.assertOpexRowsHonest(rows)
        self.assertOffersSubmit(rows[self.task.pk])
        self.assertMatchesOverview(rows, self.site)

    def test_duplicate_rows_offer_no_done_and_offer_submit(self):
        source = (Task.objects.filter(phase=self.phase, is_mirror=False,
                                      status=Task.NOT_STARTED)
                  .exclude(pk=self.task.pk).first())
        html = self._hx_post('task_duplicate_locations_create',
                             [self.site.project_id, source.pk],
                             {'existing_locations': [], 'new_locations': 'Block A',
                              'assigned_to': self.se.pk})
        self.assertIn('Block A', html)
        rows = _rows(html)
        self.assertEqual(set(rows), set(Task.objects.filter(phase=self.phase)
                                        .values_list('pk', flat=True)))
        self.assertOpexRowsHonest(rows)
        self.assertOffersSubmit(rows[self.task.pk])
        self.assertMatchesOverview(rows, self.site)

    def test_design_assign_row_offers_no_done_and_offers_submit(self):
        task = self._hand_design_task(self.site)
        rows = _rows(self._design_head_assign(self.site, task))
        self.assertEqual(list(rows), [task.pk])
        self.assertOpexRowsHonest(rows)
        self.assertOffersSubmit(rows[task.pk])
        self.assertMatchesOverview(rows, self.site)


# ---------------------------------------------------------------------------
# 2 — Residential: each response draws what the page draws, Done included.
# ---------------------------------------------------------------------------

class ResidentialUnchangedTests(RowFlagsFixture):

    def setUp(self):
        super().setUp()
        self.house = self._activated_residential()
        self.house_phase = (ProjectPhase.objects.filter(project=self.house)
                            .order_by('phase_order').first())

    def assertResidentialRows(self, rows):
        self.assertMatchesOverview(rows, self.house)
        editable = [row for row in rows.values() if '<select name="status"' in row]
        self.assertTrue(editable)
        for row in editable:
            self.assertIn(Task.DONE, _options(row))
            self.assertNotIn('task-approval-', row)

    def test_task_add(self):
        phase = Task.objects.filter(phase__project=self.house,
                                    assigned_role=Task.SITE_ENGINEER).first().phase
        html = self._hx_post('task_add', [self.house.project_id], {
            'task_name': 'Extra house step', 'phase': phase.pk,
            'assigned_role': Task.SITE_ENGINEER, 'assigned_to': self.se.pk,
            'due_date': timezone.localdate().isoformat()})
        self.assertIn(f'id="phase-tasks-{phase.pk}" hx-swap-oob="true"', html)
        self.assertIn('Extra house step', html)
        self.assertResidentialRows(_rows(html))

    def test_duplicate_is_refused_and_its_responder_draws_the_page_rows(self):
        """The duplicate view refuses Residential (v1 scope), so its responder is
        called directly: the same function, the same rows."""
        source = Task.objects.filter(phase=self.house_phase,
                                     template_task__isnull=False).first()
        count = Task.objects.filter(phase__project=self.house).count()
        response = _client_for(self.pm).post(
            reverse('task_duplicate_locations_create', args=[self.house.project_id, source.pk]),
            {'existing_locations': [], 'new_locations': 'Block A'}, HTTP_HX_REQUEST='true')
        self.assertIn(response.status_code, (403, 404))
        self.assertEqual(Task.objects.filter(phase__project=self.house).count(), count)
        self.assertResidentialRows(_rows(
            self._direct(_render_task_add_success_hx, self.house, self.house_phase)))

    def test_design_assign(self):
        task = self._hand_design_task(self.house)
        rows = _rows(self._design_head_assign(self.house, task))
        self.assertEqual(list(rows), [task.pk])
        self.assertResidentialRows(rows)


# ---------------------------------------------------------------------------
# 3 — Consignments survive a single-row swap on a delivery mirror.
# ---------------------------------------------------------------------------

class MirrorFixture(RowFlagsFixture):
    """Adds one expected challan in the Solar Panels bucket, so the mirror row shows
    "0/1 consignment received"."""

    def setUp(self):
        super().setUp()
        self.mirror = Task.objects.get(phase__project=self.site, task_name=MIRROR_NAME)
        challan = DeliveryChallan.objects.create(
            project=self.site, dc_number='DC-RRF-1', dc_date=date(2026, 9, 1),
            status=DeliveryChallan.EXPECTED)
        DCLineItem.objects.create(
            challan=challan, boq_category=MIRROR_CATEGORY, item_description='Mono 540W',
            unit='Nos', ordered_quantity=Decimal('100'))


class MirrorConsignmentTests(MirrorFixture):

    def test_status_change_keeps_the_consignment_line(self):
        """A mirror's status is nobody's to set, so the POST is refused, and the refusal
        redraws the row through `_render_task_row_hx` like any status change."""
        before = self.mirror.status
        html = self._hx_post('task_status_update', [self.site.project_id, self.mirror.pk],
                             {'status': Task.IN_PROGRESS})
        self.assertEqual(Task.objects.get(pk=self.mirror.pk).status, before)
        row = _rows(html)[self.mirror.pk]
        self.assertRegex(row, CONSIGNMENTS)
        self.assertIn('0/1 consignment received', row)
        self.assertEqual(row, self._overview_rows(self.site)[self.mirror.pk])


# ---------------------------------------------------------------------------
# 4 — Regression guard: the three responders that already worked still agree.
# ---------------------------------------------------------------------------

class ResponderParityTests(MirrorFixture):
    """project_overview, `_render_task_row_hx` and `_render_phase_tasks_hx` draw the
    same row for a Residential task, a non-mirror OPEX task and a delivery mirror.

    Checked before and after this change by snapshot: the Residential and non-mirror
    OPEX rows were byte-identical across it on all three, and the one difference was
    the consignment line `_render_task_row_hx` now adds to a mirror row, which is what
    makes it equal to the other two. This test keeps them equal."""

    def _assertParity(self, project, task):
        task = Task.objects.get(pk=task.pk)
        page = self._overview_rows(project)[task.pk]
        single = _rows(self._direct(_render_task_row_hx, project, task))[task.pk]
        phase = _rows(self._direct(_render_phase_tasks_hx, project, task.phase))[task.pk]
        self.assertEqual(single, page, '_render_task_row_hx differs from the page')
        self.assertEqual(phase, page, '_render_phase_tasks_hx differs from the page')
        return page

    def test_residential_row(self):
        house = self._activated_residential()
        task = (Task.objects.filter(phase__project=house, is_mirror=False)
                .order_by('phase__phase_order', 'task_order')[1])
        self.assertIn(Task.DONE, _options(self._assertParity(house, task)))

    def test_non_mirror_opex_row(self):
        row = self._assertParity(self.site, self.task)
        self.assertNotIn(Task.DONE, _options(row))
        self.assertOffersSubmit(row)

    def test_delivery_mirror_row(self):
        self.assertRegex(self._assertParity(self.site, self.mirror), CONSIGNMENTS)
