"""
Closeout 4b: COD is shown from the record, and Project.status does not change
(docs/EXECUTION_MODULE_DEFERRED.md G56, closed by design; G59).

WHAT IS PINNED HERE
-------------------
    status       recording and withdrawing COD leave Project.status Active and
                 commissioned_at NULL, and write no project ledger row
    pipeline     tender_stages.activated_progress counts a site with an active CodRecord
                 in "Commissioned", stops after withdraw, counts again on re-record
    stuck        the S7 stuck rule skips a site with an active CodRecord; its task_counts
                 (held equal to the CEO cards) still count the site's open tasks
    pill         the site overview shows "COD recorded <date>" while a COD is active
    refusal      COD is refused unless the site is Active or In Progress (go-ahead Q3),
                 on the screen before any upload and again in the writer
    form         the record form says recording COD does not close the site
    Residential  no COD screen, no pill, refused by the writer's first rule

Dashboard membership is NOT pinned here. The ~30 readers that filter
['Active', 'In Progress'] are untouched, because the status never changes. Their own
tests (tests_morning_task_report, tests_tender_cards, …) keep pinning them as they were.

Every COD here is recorded through the real screen on a site activated through
opex_site_activate (CodFixture), with storage mocked as in tests_cod_record.

Run with:
    python manage.py test projects.tests_cod_commissioned --settings=solarpms.test_settings
"""
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from . import cod_views
from .cod_views import COD_OPEX_ONLY, CodRefused, record_refusal
from .models import SUBJECT_PROJECT, CodRecord, Project, StatusTransition, Task
from .tender_stages import activated_progress, stuck_sites
from .tests_cod_record import CodFixture
from .tests_qaqc_assignment import _client_for


class CodStatusUnchangedTests(CodFixture):

    def _project_rows(self):
        return StatusTransition.objects.filter(subject_type=SUBJECT_PROJECT,
                                               subject_id=self.site.pk).count()

    def test_record_and_withdraw_leave_status_and_commissioned_at_alone(self):
        rows_before = self._project_rows()
        self.assertEqual(self._record().status_code, 302)
        self.site.refresh_from_db()
        self.assertEqual(self.site.status, 'Active')
        self.assertIsNone(self.site.commissioned_at)

        self.assertEqual(self._withdraw(CodRecord.objects.get(project=self.site))
                         .status_code, 302)
        self.site.refresh_from_db()
        self.assertEqual(self.site.status, 'Active')
        self.assertIsNone(self.site.commissioned_at)
        self.assertEqual(self._project_rows(), rows_before,
                         'COD must not write a project status transition')


class CodPipelineTests(CodFixture):

    def _commissioned(self):
        return activated_progress(Project.objects.filter(pk=self.site.pk))['commissioned']

    def test_pipeline_counts_a_cod_site_and_stops_after_withdraw(self):
        self.assertEqual(self._commissioned(), 0)
        self._record()
        self.assertEqual(self._commissioned(), 1)
        self._withdraw(CodRecord.objects.get(project=self.site))
        self.assertEqual(self._commissioned(), 0)
        self._record()
        self.assertEqual(self._commissioned(), 1, 're-recording counts the site again')

    def test_status_commissioned_still_counts_without_a_record(self):
        # For when step 8 sets the status (G59). Fixture-only write: no view sets it.
        Project.objects.filter(pk=self.site.pk).update(status='Commissioned')
        self.assertEqual(self._commissioned(), 1)

    def test_only_the_cod_site_is_counted(self):
        self._record()
        both = Project.objects.filter(pk__in=[self.site.pk, self.other_site.pk])
        progress = activated_progress(both)
        self.assertEqual((progress['activated'], progress['commissioned']), (2, 1))

    def test_pipeline_stays_one_query(self):
        self._record()
        with self.assertNumQueries(1):
            activated_progress(Project.objects.filter(pk=self.site.pk))


class CodStuckTests(CodFixture):

    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.cutoff = timezone.now() - timedelta(days=7)
        # One open Internal task, human-owned and applicable, three days late: the S7
        # "overdue_task" rule's whole condition.
        self.late = (Task.objects
                     .filter(phase__project=self.site, is_mirror=False,
                             task_type=Task.INTERNAL, status=Task.NOT_STARTED)
                     .order_by('pk').first())
        Task.objects.filter(pk=self.late.pk).update(due_date=self.today - timedelta(days=3))

    def _result(self):
        return stuck_sites(Project.objects.filter(pk=self.site.pk), self.today, self.cutoff)

    def test_stuck_rule_skips_a_site_with_cod_on_record(self):
        before = self._result()
        self.assertEqual(before['stuck'], 1, 'fixture: the late task must make the site stuck')
        self.assertEqual(before['rows'][0]['rule'], 'overdue_task')

        self._record()
        after = self._result()
        self.assertEqual(after['stuck'], 0)
        self.assertEqual(after['rows'], [])
        # The site's open work is still counted where the CEO cards count it.
        self.assertEqual(after['task_counts'], before['task_counts'])

        self._withdraw(CodRecord.objects.get(project=self.site))
        self.assertEqual(self._result()['stuck'], 1, 'withdrawing COD makes it stuck again')


class CodPillTests(CodFixture):

    PILL = 'id="cod-recorded-pill"'

    def test_pill_shows_while_cod_is_active_and_hides_after_withdraw(self):
        self.assertNotContains(self._overview(self.pm), self.PILL)

        self._record()
        record = CodRecord.objects.get(project=self.site)
        page = self._overview(self.pm)
        self.assertContains(page, self.PILL)
        self.assertContains(page, f'COD recorded {record.cod_date:%d %b %Y}')
        # Beside an unchanged status badge.
        self.assertContains(page, '<span class="badge" style="background:var(--hz-green)">'
                                  'Active</span>')

        self._withdraw(record)
        self.assertNotContains(self._overview(self.pm), self.PILL)

    def test_every_viewer_sees_the_pill(self):
        self._record()
        self.assertContains(self._overview(self.ceo), self.PILL)


class CodRecordableStatusTests(CodFixture):

    def _set_status(self, status):
        # Fixture-only write: no view sets On Hold or Cancelled (B-3).
        Project.objects.filter(pk=self.site.pk).update(status=status)
        self.site.refresh_from_db()

    def _assert_refused_on_screen(self, status, message):
        self._set_status(status)
        response = self._record()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, message)
        self.assertFalse(CodRecord.objects.filter(project=self.site).exists())
        self.assertEqual(self._uploads(), 0, 'nothing is uploaded for a refused site')
        self.assertEqual(self._mirror_status(), Task.NOT_STARTED)
        self.site.refresh_from_db()
        self.assertEqual(self.site.status, status)

    def test_on_hold_site_is_refused(self):
        self._assert_refused_on_screen('On Hold', 'COD cannot be recorded on an On Hold site.')

    def test_cancelled_site_is_refused(self):
        self._assert_refused_on_screen('Cancelled', 'COD cannot be recorded on a Cancelled site.')

    def test_get_shows_the_refusal_and_no_form(self):
        self._set_status('On Hold')
        page = _client_for(self.pm).get(reverse('cod_record', args=[self.site.project_id]))
        self.assertContains(page, 'id="cod-refusal"')
        self.assertNotContains(page, 'name="cod_pdf"')

    def test_writer_refuses_under_its_lock(self):
        # The screen's pre-check passed; the status changed before the writer ran.
        self._set_status('Cancelled')
        stored = {'file_name': 'e.pdf', 'bucket': 'b', 'path': 'p/cod/x.pdf',
                  'file_size_kb': 1}
        with self.assertRaisesMessage(CodRefused, 'COD cannot be recorded on a Cancelled site.'):
            cod_views.record_cod(self.site, self.pm.user,
                                 cod_date=timezone.localdate(),
                                 evidence_type=CodRecord.EVIDENCE_DISCOM_LETTER,
                                 note='n', stored_pdf=stored)
        self.assertFalse(CodRecord.objects.filter(project=self.site).exists())

    def test_in_progress_site_may_record(self):
        self._set_status('In Progress')
        self.assertEqual(self._record().status_code, 302)
        self.assertTrue(CodRecord.objects.filter(project=self.site,
                                                 withdrawn_at__isnull=True).exists())

    def test_record_form_says_cod_does_not_close_the_site(self):
        page = _client_for(self.pm).get(reverse('cod_record', args=[self.site.project_id]))
        self.assertContains(page, 'Recording COD does not close the site. HOTO and Final '
                                  'Acceptance remain open.')


class CodResidentialUntouchedTests(CodFixture):

    def setUp(self):
        super().setUp()
        self.residential = self._site('COD Residential', self.pm, project_type='Residential')

    def test_residential_has_no_cod_screen_or_pill(self):
        self.assertEqual(_client_for(self.pm).get(
            reverse('cod_record', args=[self.residential.project_id])).status_code, 404)
        self.assertNotContains(self._overview(self.pm, site=self.residential),
                               'id="cod-recorded-pill"')

    def test_residential_is_refused_by_the_first_rule(self):
        with self.assertRaisesMessage(CodRefused, COD_OPEX_ONLY):
            record_refusal(self.residential)
