"""
QA/QC visit PDF — docs/CHECKLIST_PDF_SPEC.md §3 (v1.4), parts 2 and 2b.

WHAT IS PINNED HERE
    V-PDF-1  open and lapsed visits are refused (404) and show no button; submitted
             visits download, from the visit page and the site overview's table.
    V-PDF-2  access is qaqc_visit_detail's: the engineer, the PM and another viewer of
             the site read a submitted visit's PDF; someone who cannot see the site gets
             404. The PDF agrees with user_can_read_visit for every one of them.
    V-PDF-3  rows are this visit's answers only, grouped by task, then section, in
             checklist order, each numbered by its place in its checklist version.
    V-PDF-4  Witness "—" with its footnote, Photo proof "Yes", the snapshot wording, and
             "Punch point raised · now <status> (at generation)" on a No (V-PDF-13).
    V-PDF-5  "SUBMITTED <date>" marker, counts line, coverage line, engineer.
    V-PDF-6  sign-off grid: the engineer's name under QA/QC Engineer, the rest blank.
    V-PDF-7  <site code>_QAQC-visit_<YYYYMMDD>_<visit id>.pdf
    V-PDF-8  "□" for undrawable characters; a render failure redirects to the visit page.
    The view issues zero write queries, and is GET only.
  v1.4 (part 2b):
    V-PDF-10 drifted answers print last, in their own section, split by the visit
             page's own predicate (answer_left_site) — the two lists are equal.
    V-PDF-11 a checklist named as its task is not named twice; the version stays.
    V-PDF-12 coverage is as of this visit's submit, and differs from today's once a
             later visit has verified more points.

FIXTURE SHAPE. tests_qaqc_visit.VisitFixture: two really activated OPEX sites, a 4-item
Civil and a 3-item Module checklist, the site's QA/QC engineer answering through the
real screens with storage mocked. Each test here submits one visit on the first site:
Civil items 1 (Yes) and 3 (No, remark), Module item 2 (Not Applicable).

Run with:
    python manage.py test projects.tests_qaqc_visit_pdf --settings=solarpms.test_settings
"""
import io
import re
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from pypdf import PdfReader

from .checklist_pdf import (
    COVERAGE_LABEL, DRIFTED_SECTION, NO_WITNESS, UNDRAWABLE, VISIT_SIGNOFF_HEADINGS,
    VISIT_WITNESS_FOOTNOTE, qaqc_visit_filename,
)
from .models import Checklist, ChecklistItem, PunchPoint, QaqcVisit, QaqcVisitAnswer, Task
from .qaqc_views import (
    end_site_qaqc, site_checklist_points, site_coverage, user_can_read_visit,
    visit_pdf_document,
)
from .tests_qaqc_assignment import _client_for, _messages
from .tests_qaqc_visit import CIVIL, CIVIL_LABELS, MODULE, MODULE_LABELS, VisitFixture


def _pdf_text(content):
    return '\n'.join(page.extract_text() for page in PdfReader(io.BytesIO(content)).pages)


class VisitPdfFixture(VisitFixture):

    def setUp(self):
        super().setUp()
        self._start()
        self.visit = self._open_visit()
        self._answer(self.visit, self.civil, self.civil_items[0], 'yes')
        self._answer(self.visit, self.civil, self.civil_items[2], 'no',
                     remark='Purlin out by 12 mm')
        self._answer(self.visit, self.module, self.module_items[1], 'na')

    def _submit_visit(self):
        self._submit(self.visit)
        self.visit.refresh_from_db()
        self.assertEqual(self.visit.status, QaqcVisit.SUBMITTED)

    def _url(self, visit=None):
        visit = visit or self.visit
        return reverse('qaqc_visit_pdf', args=[visit.project.project_id, visit.pk])

    def _document(self, visit=None):
        visit = visit or self.visit
        visit = QaqcVisit.objects.select_related('engineer__user').get(pk=visit.pk)
        # The view's own wiring: as-of coverage and the visit page's drift predicate.
        return visit_pdf_document(self.site, visit, timezone.now())

    def _rows(self, document=None):
        document = document or self._document()
        return [row['cells'] for section in document['pdf_sections']
                for row in section['rows'] if 'cells' in row]


class StateTests(VisitPdfFixture):
    """V-PDF-1: a submitted visit is a document; an open or lapsed one is not."""

    def test_open_visit_is_refused_even_to_its_engineer_and_the_pm(self):
        for profile in (self.qaqc, self.pm):
            with self.subTest(who=profile.user.username):
                self.assertEqual(_client_for(profile).get(self._url()).status_code, 404)

    def test_open_visit_page_has_no_button(self):
        page = _client_for(self.qaqc).get(
            reverse('qaqc_visit_detail', args=[self.site.project_id, self.visit.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'Download visit (PDF)')
        self.assertNotContains(page, self._url())

    def test_lapsed_visit_is_refused_and_has_no_button(self):
        end_site_qaqc(self.site, self.pm)
        self.visit.refresh_from_db()
        self.assertEqual(self.visit.status, QaqcVisit.LAPSED)
        client = _client_for(self.pm)
        self.assertEqual(client.get(self._url()).status_code, 404)
        page = client.get(reverse('qaqc_visit_detail',
                                  args=[self.site.project_id, self.visit.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'Download visit (PDF)')

    def test_submitted_visit_page_and_overview_row_link_the_pdf(self):
        self._submit_visit()
        client = _client_for(self.worker)
        page = client.get(reverse('qaqc_visit_detail',
                                  args=[self.site.project_id, self.visit.pk]))
        self.assertContains(page, 'Download visit (PDF)')
        self.assertContains(page, self._url())
        overview = client.get(reverse('project_overview', args=[self.site.project_id]))
        self.assertContains(overview, self._url(), count=1)

    def test_overview_links_no_open_visit(self):
        overview = _client_for(self.pm).get(
            reverse('project_overview', args=[self.site.project_id]))
        self.assertContains(overview, 'Open visit by')
        self.assertNotContains(overview, self._url())


class AccessTests(VisitPdfFixture):
    """V-PDF-2: the visit page's gates, and nothing of the PDF's own."""

    def test_access_matches_user_can_read_visit_and_site_view(self):
        self._submit_visit()
        # qaqc: the engineer; pm / coord: managers; worker: another viewer of the site
        # (holds its tasks, manages nothing); qaqc_b / pm_b: cannot see the site at all.
        for profile, code in ((self.qaqc, 200), (self.pm, 200), (self.coord, 200),
                              (self.worker, 200), (self.qaqc_b, 404), (self.pm_b, 404)):
            with self.subTest(who=profile.user.username):
                response = _client_for(profile).get(self._url())
                self.assertEqual(response.status_code, code)
                page = _client_for(profile).get(
                    reverse('qaqc_visit_detail', args=[self.site.project_id, self.visit.pk]))
                self.assertEqual(page.status_code, code)
                if code == 200:
                    self.assertTrue(user_can_read_visit(profile.user, self.visit, self.site))

    def test_visit_of_another_site_is_404(self):
        self._submit_visit()
        url = reverse('qaqc_visit_pdf', args=[self.other_site.project_id, self.visit.pk])
        self.assertEqual(_client_for(self.pm_b).get(url).status_code, 404)

    def test_get_only(self):
        self._submit_visit()
        self.assertEqual(_client_for(self.pm).post(self._url()).status_code, 405)


class RowTests(VisitPdfFixture):
    """V-PDF-3 and V-PDF-4."""

    def setUp(self):
        super().setUp()
        self._submit_visit()

    def test_rows_are_this_visits_answers_grouped_by_task_in_checklist_order(self):
        document = self._document()
        self.assertEqual(
            [s['name'] for s in document['pdf_sections']],
            [f'{CIVIL} — Civil checks v1', f'{MODULE} — Module checks v1'])
        self.assertEqual(
            [(r[0], r[1]) for r in self._rows(document)],
            [(f'1. {CIVIL_LABELS[0]}', 'Yes'), (f'3. {CIVIL_LABELS[2]}', 'No'),
             (f'2. {MODULE_LABELS[1]}', 'Not Applicable')])

    def test_another_visits_answers_are_not_rows(self):
        # A second visit the next day checks a different point; neither PDF shows the
        # other's answers.
        self._tomorrow()
        self._start()
        second = self._open_visit()
        self._answer(second, self.civil, self.civil_items[1], 'yes')
        self._submit(second)
        self.assertEqual([r[0] for r in self._rows(self._document(second))],
                         [f'2. {CIVIL_LABELS[1]}'])
        self.assertEqual(len(self._rows()), 3)

    def test_checklist_sections_become_sub_headings_in_item_order(self):
        # .update() skips ChecklistItem.save()'s R-7 draft guard: test data only.
        ChecklistItem.objects.filter(pk__in=[self.civil_items[0].pk,
                                             self.civil_items[1].pk]).update(section='Foundation')
        ChecklistItem.objects.filter(pk__in=[self.civil_items[2].pk,
                                             self.civil_items[3].pk]).update(section='Structure')
        civil = self._document()['pdf_sections'][0]['rows']
        self.assertEqual(
            [row.get('subheading') or row['cells'][0] for row in civil],
            ['Foundation', f'1. {CIVIL_LABELS[0]}', 'Structure', f'3. {CIVIL_LABELS[2]}'])

    def test_cells_witness_photo_date_and_snapshot(self):
        answer = QaqcVisitAnswer.objects.get(visit=self.visit, item=self.civil_items[0])
        QaqcVisitAnswer.objects.filter(pk=answer.pk).update(
            item_text_snapshot='Pedestal levels checked (old wording)')
        label, status, witness, photo, date, remark = self._rows()[0]
        self.assertEqual((label, status, witness, photo, remark),
                         ('1. Pedestal levels checked (old wording)', 'Yes', NO_WITNESS,
                          'Yes', ''))
        self.assertEqual(date, timezone.localtime(answer.answered_at).strftime('%d %b %Y'))
        self.assertEqual({r[3] for r in self._rows()}, {'Yes'})
        self.assertEqual({r[2] for r in self._rows()}, {NO_WITNESS})

    def test_a_no_carries_its_punch_point_and_current_status(self):
        answer = QaqcVisitAnswer.objects.get(visit=self.visit, item=self.civil_items[2])
        self.assertIsNotNone(answer.punch_point_id)
        self.assertEqual(self._rows()[1][5],
                         'Purlin out by 12 mm\nPunch point raised · now Open (at generation)')
        PunchPoint.objects.filter(pk=answer.punch_point_id).update(status=PunchPoint.RECTIFIED)
        self.assertEqual(
            self._rows()[1][5],
            'Purlin out by 12 mm\nPunch point raised · now Rectified (at generation)')


class HeaderTests(VisitPdfFixture):
    """V-PDF-5, V-PDF-6, V-PDF-7."""

    def setUp(self):
        super().setUp()
        self._submit_visit()

    def test_marker_counts_coverage_and_engineer(self):
        document = self._document()
        rows = dict(document['pdf_header_rows'])
        self.assertEqual(document['pdf_title'], 'QA/QC verification visit')
        self.assertEqual(document['pdf_marker'],
                         f'SUBMITTED {timezone.localtime(self.visit.submitted_at):%d %b %Y}')
        self.assertEqual(rows['Checked'], '3 points checked, 1 No, 1 punch point raised')
        self.assertEqual(rows[COVERAGE_LABEL],
                         '3 of 7 points verified on submitted visits (42%)')
        self.assertEqual(rows['QA/QC engineer'], self.qaqc.user.get_full_name())
        self.assertEqual(rows['Visit date'], self.visit.visit_date.strftime('%d %b %Y'))
        self.assertEqual(rows['Site code'], self.site.project_id)
        self.assertEqual(document['pdf_footnotes'], [VISIT_WITNESS_FOOTNOTE])

    def test_signoff_grid_names_the_engineer_only(self):
        signoff = self._document()['pdf_signoff']
        self.assertEqual([c['heading'] for c in signoff], VISIT_SIGNOFF_HEADINGS)
        self.assertEqual([c['name'] for c in signoff],
                         [self.qaqc.user.get_full_name(), '', '', ''])

    def test_filename(self):
        self.assertEqual(
            qaqc_visit_filename(self.site, self.visit),
            f'{self.site.project_id}_QAQC-visit_{self.visit.visit_date:%Y%m%d}_'
            f'{self.visit.pk}.pdf')


class DownloadViewTests(VisitPdfFixture):
    """The view end to end: the file, no writes, F-7 and F-9."""

    def setUp(self):
        super().setUp()
        self._submit_visit()

    def test_downloads_a_complete_pdf_attachment(self):
        response = _client_for(self.worker).get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertEqual(response['Content-Disposition'],
                         f'attachment; filename="{qaqc_visit_filename(self.site, self.visit)}"')
        # Whitespace collapsed: the narrow Remark column wraps "(Open)" onto a line.
        text = ' '.join(_pdf_text(response.content).split())
        self.assertIn('SUBMITTED', text)
        self.assertIn('3 points checked, 1 No, 1 punch point raised', text)
        # pypdf extracts the middle dot as a replacement character, so V-PDF-13's wording
        # is checked either side of it.
        self.assertIn('Punch point raised', text)
        self.assertIn('now Open (at generation)', text)
        self.assertIn(COVERAGE_LABEL, text)
        self.assertIn(VISIT_WITNESS_FOOTNOTE, text)
        self.assertIn('Reviewed By (PM/Coordinator)', text)
        self.assertIn(self.qaqc.user.get_full_name(), text)

    def test_view_issues_no_write_query(self):
        client = _client_for(self.pm)
        with CaptureQueriesContext(connection) as queries:
            response = client.get(self._url())
        self.assertEqual(response.status_code, 200)
        writes = [q['sql'] for q in queries.captured_queries
                  if re.match(r'\s*(INSERT|UPDATE|DELETE)\b', q['sql'], re.IGNORECASE)]
        self.assertEqual(writes, [])

    def test_undrawable_characters_become_boxes_and_one_warning_names_the_visit(self):
        QaqcVisitAnswer.objects.filter(visit=self.visit, item=self.civil_items[2]).update(
            remark='Earth Ω 2.5 — राम')
        with self.assertLogs('projects.checklist_pdf', level='WARNING') as logs:
            response = _client_for(self.pm).get(self._url())
        self.assertIn(f'Earth Ω 2.5 — {UNDRAWABLE * 3}', _pdf_text(response.content))
        self.assertEqual(len(logs.records), 1)
        self.assertIn(f'visit {self.visit.pk}', logs.output[0])

    def test_render_failure_redirects_to_the_visit_page_with_a_message(self):
        def broken(html, dest, **kwargs):
            dest.write(b'%PDF-1.4 truncated')
            raise ValueError('layout failed')
        client = _client_for(self.pm)
        with mock.patch('xhtml2pdf.pisa.CreatePDF', side_effect=broken), \
                self.assertLogs('projects.qaqc_views', level='ERROR') as logs:
            response = client.get(self._url())
        visit_page = reverse('qaqc_visit_detail', args=[self.site.project_id, self.visit.pk])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], visit_page)
        self.assertNotIn('Content-Disposition', response)
        self.assertNotIn(b'%PDF', response.content)
        self.assertIn('layout failed', '\n'.join(logs.output))
        self.assertIn('The PDF could not be generated. The error has been logged.',
                      _messages(client.get(visit_page)))


class PartTwoBRulingTests(VisitPdfFixture):
    """v1.4: V-PDF-10, V-PDF-11 and V-PDF-12 (V-PDF-13's wording is in RowTests)."""

    def setUp(self):
        super().setUp()
        self._submit_visit()

    def _cells_by_section(self, document=None):
        document = document or self._document()
        return [(section['name'], [row.get('subheading') or row['cells'][0]
                                   for row in section['rows']])
                for section in document['pdf_sections']]

    def test_no_drifted_section_while_every_point_is_on_the_site(self):
        self.assertNotIn(DRIFTED_SECTION, [name for name, _ in self._cells_by_section()])

    def test_drifted_answers_print_last_in_their_own_section_and_only_there(self):
        # The Civil task becomes Not Applicable after the visit: its two points leave the
        # site (pre-flight 5), the Module answer stays.
        Task.objects.filter(pk=self.civil.pk).update(is_not_applicable=True)
        self.assertEqual(self._cells_by_section(), [
            (f'{MODULE} — Module checks v1', [f'2. {MODULE_LABELS[1]}']),
            (DRIFTED_SECTION, [f'{CIVIL} — Civil checks v1',
                               f'1. {CIVIL_LABELS[0]}', f'3. {CIVIL_LABELS[2]}']),
        ])
        # The counts still cover every answer on the visit, as the visit page's do.
        self.assertEqual(dict(self._document()['pdf_header_rows'])['Checked'],
                         '3 points checked, 1 No, 1 punch point raised')

    def test_drifted_answers_are_exactly_the_visit_pages_stale_answers(self):
        Task.objects.filter(pk=self.civil.pk).update(is_not_applicable=True)
        page = _client_for(self.pm).get(
            reverse('qaqc_visit_detail', args=[self.site.project_id, self.visit.pk]))
        page_stale = sorted(a.item_text_snapshot for a in page.context['stale_answers'])
        drifted = dict(self._cells_by_section())[DRIFTED_SECTION][1:]
        self.assertEqual(sorted(cell.split('. ', 1)[1] for cell in drifted), page_stale)

    def test_heading_names_a_checklist_named_as_its_task_once(self):
        # .update() skips Checklist.save()'s R-7 guard on an active version: test data only.
        Checklist.objects.filter(pk=self.civil_v1.pk).update(name=CIVIL)
        names = [name for name, _ in self._cells_by_section()]
        self.assertEqual(names, [f'{CIVIL} — v1', f'{MODULE} — Module checks v1'])

    def test_coverage_is_as_of_this_visit_not_today(self):
        # A later visit verifies a fourth point. Today's coverage moves; this visit's
        # document keeps the coverage it reached.
        self._tomorrow()
        self._start()
        later = self._open_visit()
        self._answer(later, self.civil, self.civil_items[1], 'yes')
        self._submit(later)
        today = site_coverage(self.site, site_checklist_points(self.site))
        self.assertEqual((today['verified'], today['total']), (4, 7))
        self.assertEqual(dict(self._document()['pdf_header_rows'])[COVERAGE_LABEL],
                         '3 of 7 points verified on submitted visits (42%)')
        self.assertEqual(dict(self._document(later)['pdf_header_rows'])[COVERAGE_LABEL],
                         '4 of 7 points verified on submitted visits (57%)')
        self.assertNotIn('Site coverage', dict(self._document()['pdf_header_rows']))
