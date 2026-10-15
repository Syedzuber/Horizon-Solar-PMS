"""
Task checklist PDF — docs/CHECKLIST_PDF_SPEC.md, part 1.

WHAT IS PINNED HERE
    F-1  FINAL only when Done AND approved_at set AND no answer after approved_at;
         each condition failing alone gives NOT FINAL (the third is G63's stale approval).
    F-3  every item of the pinned version is a row; an unanswered one says "Not checked".
    F-5  an answered row prints item_text_snapshot; an unanswered one the version's label.
    1/4  the sign-off grid pre-fills submitter and approver only, "(self-certified)" on
         the flag; the approval line names approver and date and no capacity.
    6    access is task_detail's: refused without site view, allowed for the site's
         view-only QA/QC engineer (who holds no task there).
    render_pdf() raises on an exception AND on a counted error, never returning bytes.
    The view issues zero write queries.

FIXTURE SHAPE. One OPEX site activated through the real view (tasks carry
template_task), a four-item checklist in two sections linked to its Civil Work task,
and two of the four items answered THROUGH THE REAL completion view — one Yes, one No
with a remark and witness names.

Run with:
    python manage.py test projects.tests_checklist_pdf --settings=solarpms.test_settings
"""
import io
import re
from datetime import timedelta
from unittest import mock

from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from pypdf import PdfReader

from .checklist_pdf import (
    NOT_CHECKED, PdfRenderError, TASK_CHECKLIST_TEMPLATE, WITNESS_FOOTNOTE, render_pdf,
    task_checklist_document, task_checklist_filename, task_checklist_is_final,
)
from .models import (
    Checklist, ChecklistItem, ChecklistItemCompletion, ChecklistTaskLink,
    SiteQaqcAssignment, Task,
)
from .tests_checklist_task_link import _client_for, _profile, _seed_opex
from .tests_checklist_version_pin import CIVIL, INVERTER, _activate_opex, _task
from .views import _checklist_context

ITEMS = [  # (section, label)
    ('Foundation', 'Pedestal levels checked against the approved drawing'),
    ('Foundation', 'Anchor bolts torqued to the value specified by the manufacturer'),
    ('Structure',  'MMS alignment verified'),
    ('Structure',  'Earthing strip bonded'),
]


def _pdf_text(content):
    return '\n'.join(page.extract_text() for page in PdfReader(io.BytesIO(content)).pages)


class ChecklistPdfFixture(TestCase):

    @classmethod
    def setUpTestData(cls):
        _seed_opex()
        cls.pm = _profile('pdf_pm', 'PM')
        cls.site = _activate_opex(cls.pm, 'PDF Site')
        cls.task = _task(cls.site, CIVIL)
        cls.inverter = _task(cls.site, INVERTER)   # no checklist linked

        cls.checklist = Checklist.objects.create(code='PDF-CIVIL', name='Civil Work',
                                                 version_no=1, requires_photo=False)
        cls.items = [ChecklistItem.objects.create(checklist=cls.checklist, label=label,
                                                  section=section, order=n)
                     for n, (section, label) in enumerate(ITEMS, start=1)]
        cls.checklist.activate()
        ChecklistTaskLink.objects.create(checklist=cls.checklist,
                                         template_task=cls.task.template_task)

        client = _client_for(cls.pm)
        url = lambda item: reverse('checklist_item_complete',
                                   args=[cls.site.project_id, cls.task.pk, item.pk])
        client.post(url(cls.items[0]), {'answer': 'yes'})
        client.post(url(cls.items[2]), {'answer': 'no', 'remarks': 'Purlin out by 12 mm',
                                        'witness_names': 'R. Kumar (DISCOM)'})
        if ChecklistItemCompletion.objects.filter(task=cls.task, is_checked=True).count() != 2:
            raise RuntimeError('fixture: the two answers were not recorded')

        # A Site Engineer who holds no task on the site and is not its QA/QC engineer.
        cls.outsider = _profile('pdf_outsider', 'Site Engineer')
        # The site's QA/QC engineer: sees the site through the assignment alone (CL-D).
        cls.qaqc = _profile('pdf_qaqc', 'Site Engineer')
        SiteQaqcAssignment.objects.create(project=cls.site, engineer=cls.qaqc,
                                          assigned_by=cls.pm)

    def _url(self, task=None):
        task = task or self.task
        return reverse('task_checklist_pdf', args=[self.site.project_id, task.pk])

    def _set(self, **fields):
        Task.objects.filter(pk=self.task.pk).update(**fields)
        self.task.refresh_from_db()

    def _document(self):
        request = RequestFactory().get('/')
        request.user = self.pm.user
        context = _checklist_context(request, self.site, self.task)
        return task_checklist_document(self.site, self.task, context['checklist'],
                                       context['checklist_sections'], timezone.now())

    def _rows(self):
        return [row['cells'] for section in self._document()['pdf_sections']
                for row in section['rows']]


class FinalRuleTests(ChecklistPdfFixture):
    """F-1: all three conditions, each one failing on its own."""

    def _approve(self, at):
        self._set(status=Task.DONE, approved_at=at, approved_by=self.pm)

    def test_final_when_done_approved_and_no_answer_after_approval(self):
        self._approve(timezone.now() + timedelta(minutes=1))
        self.assertTrue(task_checklist_is_final(self.task))

    def test_not_final_when_not_done(self):
        self._set(status=Task.IN_PROGRESS, approved_at=timezone.now() + timedelta(minutes=1),
                  approved_by=self.pm)
        self.assertFalse(task_checklist_is_final(self.task))

    def test_not_final_when_done_without_approval(self):
        self._set(status=Task.DONE, approved_at=None)
        self.assertFalse(task_checklist_is_final(self.task))

    def test_not_final_when_an_answer_was_recorded_after_approval(self):
        """G63: approved, reopened, answered again, Done again — the approver never saw
        the later answer, so the stale approval does not make the record final."""
        approved_at = timezone.now()
        self._approve(approved_at)
        ChecklistItemCompletion.objects.filter(task=self.task, item=self.items[2]).update(
            checked_at=approved_at + timedelta(hours=2))
        self.assertFalse(task_checklist_is_final(self.task))

    def test_banner_follows_the_rule(self):
        self.assertFalse(self._document()['pdf_is_final'])
        self._approve(timezone.now() + timedelta(minutes=1))
        self.assertTrue(self._document()['pdf_is_final'])


class RowTests(ChecklistPdfFixture):
    """F-3, F-4, F-5: every item, its status, and which wording it prints."""

    def test_every_item_is_a_row_and_unanswered_ones_say_not_checked(self):
        rows = self._rows()
        self.assertEqual(len(rows), len(ITEMS))
        self.assertEqual([r[1] for r in rows], ['Yes', NOT_CHECKED, 'No', NOT_CHECKED])

    def test_rows_are_grouped_by_section_in_item_order(self):
        sections = self._document()['pdf_sections']
        self.assertEqual([s['name'] for s in sections], ['Foundation', 'Structure'])
        self.assertEqual([len(s['rows']) for s in sections], [2, 2])

    def test_answered_row_prints_the_snapshot_not_the_live_label(self):
        # The wording the engineer actually answered differs from today's label: the
        # snapshot is what the PDF must print (R-8).
        ChecklistItemCompletion.objects.filter(task=self.task, item=self.items[0]).update(
            item_text_snapshot='Pedestal levels checked (old wording)')
        rows = self._rows()
        self.assertEqual(rows[0][0], '1. Pedestal levels checked (old wording)')
        # An unanswered row has no snapshot and prints the pinned version's label.
        self.assertEqual(rows[1][0], f'2. {ITEMS[1][1]}')

    def test_answered_no_carries_witness_remark_date_and_photo_flag(self):
        completion = ChecklistItemCompletion.objects.get(task=self.task, item=self.items[2])
        _label, status, witness, photo, date, remark = self._rows()[2]
        self.assertEqual((status, witness, photo, remark),
                         ('No', 'R. Kumar (DISCOM)', 'No', 'Purlin out by 12 mm'))
        self.assertEqual(date, timezone.localtime(completion.checked_at).strftime('%d %b %Y'))

    def test_photo_proof_is_yes_only_when_a_photo_is_recorded(self):
        ChecklistItemCompletion.objects.filter(task=self.task, item=self.items[0]).update(
            photo_file_name='a.jpg', photo_url='https://example.invalid/a.jpg',
            photo_supabase_path='checklist-photos/a.jpg')
        self.assertEqual([r[3] for r in self._rows()], ['Yes', 'No', 'No', 'No'])


class SignoffAndHeaderTests(ChecklistPdfFixture):
    """Rulings 1, 2 and 4, and F-6's omissions."""

    def test_grid_prefills_submitter_and_approver_only(self):
        approver = _profile('pdf_approver', 'PM')
        self._set(submitted_by=self.pm, approved_by=approver, approved_at=timezone.now())
        names = [c['name'] for c in self._document()['pdf_signoff']]
        self.assertEqual(names, ['pdf_pm', '', '', 'pdf_approver'])

    def test_self_certified_is_named_in_grid_and_approval_line(self):
        self._set(submitted_by=self.pm, approved_by=self.pm, approved_at=timezone.now(),
                  approval_self_certified=True)
        document = self._document()
        self.assertEqual(document['pdf_signoff'][3]['name'], 'pdf_pm (self-certified)')
        approval = dict(document['pdf_header_rows'])['Approval']
        self.assertTrue(approval.startswith('Approved by pdf_pm on '))
        self.assertTrue(approval.endswith('(self-certified)'))

    def test_no_approval_line_without_an_approval(self):
        self.assertNotIn('Approval', dict(self._document()['pdf_header_rows']))

    def test_header_omits_fields_the_system_does_not_hold(self):
        labels = [label for label, _value in self._document()['pdf_header_rows']]
        self.assertEqual(labels, ['Site code', 'Client', 'Checklist', 'Generated'])
        self.assertEqual(self._document()['pdf_footnotes'], [WITNESS_FOOTNOTE])

    def test_filename_is_site_code_checklist_version_and_ist_date(self):
        moment = timezone.now()
        self.assertEqual(task_checklist_filename(self.site, self.checklist, moment),
                         f'{self.site.project_id}_PDF-CIVIL_v1_'
                         f'{timezone.localtime(moment):%Y%m%d}.pdf')


class DownloadViewTests(ChecklistPdfFixture):
    """The view end to end: access (ruling 6), the file itself, and no writes."""

    def test_pm_downloads_a_complete_pdf_attachment(self):
        response = _client_for(self.pm).get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertRegex(response['Content-Disposition'],
                         rf'^attachment; filename="{self.site.project_id}_PDF-CIVIL_v1_\d{{8}}\.pdf"$')
        text = _pdf_text(response.content)
        self.assertEqual(text.count(NOT_CHECKED), 2)
        self.assertIn('NOT FINAL', text)
        for number in range(1, len(ITEMS) + 1):
            self.assertRegex(text, rf'(?m)^{number}\. ')

    def test_user_without_site_view_is_refused(self):
        response = _client_for(self.outsider).get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_view_only_qaqc_engineer_may_download(self):
        self.assertFalse(Task.objects.filter(phase__project=self.site,
                                             assigned_to=self.qaqc).exists())
        response = _client_for(self.qaqc).get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_task_without_a_checklist_is_404(self):
        response = _client_for(self.pm).get(self._url(self.inverter))
        self.assertEqual(response.status_code, 404)

    def test_get_only(self):
        response = _client_for(self.pm).post(self._url())
        self.assertEqual(response.status_code, 405)

    def test_view_issues_no_write_query(self):
        client = _client_for(self.pm)
        with CaptureQueriesContext(connection) as queries:
            response = client.get(self._url())
        self.assertEqual(response.status_code, 200)
        writes = [q['sql'] for q in queries.captured_queries
                  if re.match(r'\s*(INSERT|UPDATE|DELETE)\b', q['sql'], re.IGNORECASE)]
        self.assertEqual(writes, [])

    def test_task_page_shows_the_button_only_with_a_checklist(self):
        client = _client_for(self.pm)
        page = client.get(reverse('task_detail', args=[self.site.project_id, self.task.pk]))
        self.assertContains(page, self._url())
        page = client.get(reverse('task_detail', args=[self.site.project_id, self.inverter.pk]))
        self.assertNotContains(page, 'Download checklist (PDF)')


class RendererTests(TestCase):
    """render_pdf() never hands back a partial document."""

    CONTEXT = {'pdf_title': 'T', 'pdf_is_final': False, 'pdf_generated_at': 'now',
               'pdf_header_rows': [('Site code', 'S1')], 'pdf_footer_left': '',
               'pdf_columns': [{'label': 'Checking point', 'width': 100}],
               'pdf_sections': [{'name': '', 'rows': [{'cells': ['1. A point']}]}],
               'pdf_signoff': [], 'pdf_footnotes': []}

    def test_renders_a_pdf(self):
        self.assertTrue(render_pdf(TASK_CHECKLIST_TEMPLATE, self.CONTEXT).startswith(b'%PDF'))

    def test_counted_error_raises_even_though_bytes_were_written(self):
        def partial(html, dest, **kwargs):
            dest.write(b'%PDF-1.4 truncated')
            return mock.Mock(err=1, log=[(40, 1, 'image could not be read', '')])
        with mock.patch('xhtml2pdf.pisa.CreatePDF', side_effect=partial):
            with self.assertRaisesMessage(PdfRenderError, 'image could not be read'):
                render_pdf(TASK_CHECKLIST_TEMPLATE, self.CONTEXT)

    def test_exception_inside_the_converter_raises(self):
        def broken(html, dest, **kwargs):
            dest.write(b'%PDF-1.4 truncated')
            raise ValueError('layout failed')
        with mock.patch('xhtml2pdf.pisa.CreatePDF', side_effect=broken):
            with self.assertRaisesMessage(PdfRenderError, 'layout failed'):
                render_pdf(TASK_CHECKLIST_TEMPLATE, self.CONTEXT)
