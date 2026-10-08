"""
COD record — closeout step 4 (docs/CLOSEOUT_SPEC.md CL-2, CL-3).

WHAT IS PINNED HERE
-------------------
A PM or coordinator records an OPEX site's COD — date, one evidence type, one PDF in the
private bills bucket, a mandatory note — and the active record drives the site's COD
mirror to Done through the single mirror writer. Withdrawing (reason required) drives it
back to Not Started; records are append-only with one active per site.

    CL-2   refused while any punch point on the site is Open; the refusal names them
    Q1     refused on a site with no COD mirror (not activated)
    Q4     Testing & Commissioning not Done is a warning on the form, never a refusal
    Q5     PM, coordinators and the CEO hear about both events, minus the actor
    Q7/Q10 the PDF opens through a redirect signed at the click, for every viewer
    orphan  a PDF uploaded for a COD the writer then refuses is deleted

The caller discipline (sync_cod_mirror called only by cod_views' two writers, and the
writer only by named compositions) is pinned in tests_design_mirror_derivation.

EVERY SITE HERE IS REALLY ACTIVATED through opex_site_activate, and every COD is recorded
through the real screen, so the tests exercise the writer, its lock and its notices.
Storage is mocked at cod_views._client (upload / remove) and at the signer.

Run with:
    python manage.py test projects.tests_cod_record --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from . import cod_views
from .cod_views import (
    COD_NOT_ACTIVATED, COD_STORAGE_OFF, TC_NOT_DONE_WARNING, discard_unrecorded_cod_pdf,
)
from .models import (
    ACTOR_ROLE_SYSTEM, ActivityLog, AppendOnlyViolation, CodRecord, NotificationLog, Project,
    PunchPoint,
    REASON_MIRROR_DERIVED, SUBJECT_TASK, StatusTransition, Task,
)
from .tests_qaqc_assignment import _client_for, _profile, _seed_opex
from .utils import (
    RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL, assign_tasks_to, resolve_residential_template,
)

BUCKET = 'cod-test-private'
SIGNED = 'https://storage.example/signed/cod.pdf?token=abc'
PDF_BYTES = b'%PDF-1.4\n% COD evidence\n'

EVIDENCE_TYPES = [
    CodRecord.EVIDENCE_DISCOM_LETTER,
    CodRecord.EVIDENCE_NET_METER,
    CodRecord.EVIDENCE_COMMISSIONING_REPORT,
]


def _pdf(name='evidence.pdf', content=PDF_BYTES, content_type='application/pdf'):
    return SimpleUploadedFile(name, content, content_type=content_type)


class CodFixture(TestCase):
    """Two activated OPEX sites with disjoint managers, a Draft OPEX site, a Residential
    and a CAPEX site, and the people around them.

    site:  pm + coord.   other_site: pm_b.   draft_site: pm (never activated).
    worker — Site Engineer holding every Site Engineer task on `site`
    qaqc   — Site Engineer assigned as `site`'s QA/QC engineer (through the real screen)
    """

    @classmethod
    def setUpTestData(cls):
        resolve_residential_template()   # bootstraps RESIDENTIAL v1 on a virgin DB
        _seed_opex()

        cls.pm      = _profile('cod_pm', 'PM')
        cls.coord   = _profile('cod_coord', 'Project Coordinator')
        cls.pm_b    = _profile('cod_pm_b', 'PM')
        cls.worker  = _profile('cod_worker', 'Site Engineer')
        cls.qaqc    = _profile('cod_qaqc', 'Site Engineer')
        cls.scm     = _profile('cod_scm', 'SCM')
        cls.design  = _profile('cod_design', 'Design')
        cls.finance = _profile('cod_fin', 'Finance', email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)
        cls.ceo     = _profile('cod_ceo', 'CEO', email='ceo@example.com')

    def setUp(self):
        self.site = self._activated_site('COD Site Alpha', self.pm)
        self.site.coordinators.add(self.coord)
        assign_tasks_to(
            Task.objects.filter(phase__project=self.site, assigned_role=Task.SITE_ENGINEER,
                                is_mirror=False),
            self.worker,
        )
        with self.captureOnCommitCallbacks(execute=True):
            response = _client_for(self.pm).post(
                reverse('site_qaqc', args=[self.site.project_id]),
                {'action': 'assign', 'engineer_id': self.qaqc.pk})
        self.assertEqual(response.status_code, 302, 'fixture: QA/QC not assigned')
        self.other_site = self._activated_site('COD Site Bravo', self.pm_b)
        self.draft_site = self._site('COD Site Draft', self.pm)
        self.mirror = self._mirror(self.site)

        self.storage = self.settings(SUPABASE_BILLS_BUCKET=BUCKET)
        self.storage.enable()
        self.addCleanup(self.storage.disable)
        client_patch = mock.patch('projects.cod_views._client')
        self.storage_client = client_patch.start()
        self.addCleanup(client_patch.stop)
        signer_patch = mock.patch('projects.bill_storage.get_design_file_url',
                                  return_value=SIGNED)
        self.signer = signer_patch.start()
        self.addCleanup(signer_patch.stop)

    # -- builders ------------------------------------------------------------

    def _site(self, name, pm, project_type='OPEX'):
        return Project.objects.create(
            customer_name=name, customer_phone='9876543210', site_address='1 COD Road',
            city='Lucknow', project_type=project_type, dc_capacity_kw=Decimal('100.00'),
            status='Draft', assigned_pm=pm,
        )

    def _activated_site(self, name, pm):
        site = self._site(name, pm)
        response = _client_for(pm).post(reverse('opex_site_activate', args=[site.project_id]))
        self.assertEqual(response.status_code, 302, 'OPEX activation did not redirect')
        site.refresh_from_db()
        self.assertEqual(site.status, 'Active')
        return site

    def _mirror(self, site):
        return Task.objects.get(phase__project=site, is_mirror=True,
                                template_task__code='COD')

    def _tc_task(self, site):
        return Task.objects.get(phase__project=site,
                                template_task__code='TESTING_COMMISSIONING')

    def _punch(self, site=None, reason='Earthing clamp loose on string 3'):
        task = Task.objects.filter(phase__project=site or self.site,
                                   is_mirror=False).order_by('pk').first()
        return PunchPoint.objects.create(task=task, reason=reason, raised_by=self.pm)

    # -- actions through the real screens --------------------------------------

    def _record(self, by=None, site=None, **overrides):
        site = site or self.site
        data = {
            'cod_date': (timezone.localdate() - timedelta(days=1)).isoformat(),
            'evidence_type': CodRecord.EVIDENCE_DISCOM_LETTER,
            'note': 'DISCOM letter received; plant exporting since yesterday.',
            'cod_pdf': _pdf(),
        }
        data.update(overrides)
        data = {k: v for k, v in data.items() if v is not None}
        with self.captureOnCommitCallbacks(execute=True):
            return _client_for(by or self.pm).post(
                reverse('cod_record', args=[site.project_id]), data)

    def _withdraw(self, record, by=None, reason='DISCOM letter was for the wrong meter.'):
        with self.captureOnCommitCallbacks(execute=True):
            return _client_for(by or self.pm).post(
                reverse('cod_withdraw', args=[record.project.project_id, record.pk]),
                {'reason': reason})

    def _overview(self, profile, site=None):
        return _client_for(profile).get(
            reverse('project_overview', args=[(site or self.site).project_id]))

    def _mirror_status(self):
        self.mirror.refresh_from_db()
        return self.mirror.status

    def _uploads(self):
        return self.storage_client.return_value.storage.from_.return_value.upload.call_count

    def _transitions(self):
        return StatusTransition.objects.filter(
            subject_type=SUBJECT_TASK, subject_id=self.mirror.pk).order_by('pk')


# ---------------------------------------------------------------------------
# 1, 2 — who records, and what recording does
# ---------------------------------------------------------------------------

class CodRecordWriteTests(CodFixture):

    def test_pm_records_each_evidence_type_and_the_mirror_follows(self):
        for evidence in EVIDENCE_TYPES:
            with self.subTest(evidence=evidence):
                response = self._record(evidence_type=evidence)
                self.assertEqual(response.status_code, 302, response.content[:500])
                record = CodRecord.objects.get(project=self.site, withdrawn_at__isnull=True)
                self.assertEqual(record.evidence_type, evidence)
                self.assertEqual(record.recorded_by, self.pm)
                self.assertEqual(record.pdf_bucket, BUCKET)
                self.assertIn('/cod/', record.pdf_path)
                self.assertEqual(self._mirror_status(), Task.DONE)
                self.assertIsNotNone(self.mirror.completed_at)

                done = self._transitions().filter(to_status=Task.DONE).last()
                self.assertEqual(done.actor, self.pm)
                self.assertEqual(done.reason_code, REASON_MIRROR_DERIVED)
                self.assertNotEqual(done.actor_role_code, ACTOR_ROLE_SYSTEM)

                # Withdraw so the next evidence type can be recorded as a new record.
                self.assertEqual(self._withdraw(record).status_code, 302)
                self.assertEqual(self._mirror_status(), Task.NOT_STARTED)

        self.assertEqual(CodRecord.objects.filter(project=self.site).count(), 3)

    def test_note_is_stored_stripped_and_activity_is_logged(self):
        self._record(note='   Net meter installed and sealed.   ')
        record = CodRecord.objects.get(project=self.site)
        self.assertEqual(record.note, 'Net meter installed and sealed.')
        self.assertTrue(ActivityLog.objects.filter(
            project=self.site, action_code='cod_recorded', entity_id=record.pk).exists())

    def test_coordinator_can_record(self):
        response = self._record(by=self.coord)
        self.assertEqual(response.status_code, 302)
        record = CodRecord.objects.get(project=self.site)
        self.assertEqual(record.recorded_by, self.coord)
        self.assertEqual(self._mirror_status(), Task.DONE)
        self.assertEqual(self._transitions().last().actor, self.coord)

    def test_everyone_else_is_refused_and_nothing_is_written(self):
        # 403 for those who can see the site; 404 for another site's PM, who cannot.
        expected = {
            'site engineer':   (self.worker, 403),
            'QA/QC engineer':  (self.qaqc, 403),
            'SCM':             (self.scm, 403),
            'Finance':         (self.finance, 403),
            'CEO':             (self.ceo, 403),
            'another site PM': (self.pm_b, 404),
        }
        for label, (profile, status) in expected.items():
            with self.subTest(who=label):
                self.assertEqual(self._record(by=profile).status_code, status)
                get = _client_for(profile).get(
                    reverse('cod_record', args=[self.site.project_id]))
                self.assertEqual(get.status_code, status)
        # Design holds no task and is not assigned_design here: it cannot see the site.
        self.assertIn(self._record(by=self.design).status_code, (403, 404))
        self.assertFalse(CodRecord.objects.exists())
        self.assertEqual(self._mirror_status(), Task.NOT_STARTED)
        self.assertEqual(self._uploads(), 0)

    def test_only_managers_withdraw(self):
        self._record()
        record = CodRecord.objects.get(project=self.site)
        for profile, status in ((self.worker, 403), (self.qaqc, 403), (self.scm, 403),
                                (self.ceo, 403), (self.pm_b, 404)):
            with self.subTest(who=profile.user.username):
                self.assertEqual(self._withdraw(record, by=profile).status_code, status)
        record = CodRecord.objects.get(pk=record.pk)
        self.assertIsNone(record.withdrawn_at)
        self.assertEqual(self._mirror_status(), Task.DONE)
        self.assertEqual(self._withdraw(record, by=self.coord).status_code, 302)
        self.assertEqual(self._mirror_status(), Task.NOT_STARTED)


# ---------------------------------------------------------------------------
# 3 — CL-2: open punch points refuse; closed or waived ones do not
# ---------------------------------------------------------------------------

class CodPunchPointGateTests(CodFixture):

    def test_refused_with_an_open_point_which_the_page_names(self):
        point = self._punch()
        response = self._record()
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('COD cannot be recorded while 1 punch point is open', body)
        self.assertIn(point.task.task_name, body)
        self.assertIn('Earthing clamp loose on string 3', body)
        self.assertFalse(CodRecord.objects.exists())
        self.assertEqual(self._mirror_status(), Task.NOT_STARTED)
        self.assertFalse(self._transitions().exists())
        self.assertEqual(self._uploads(), 0, 'a PDF was uploaded for a refused COD')

    def test_get_shows_the_refusal_before_the_form_is_filled(self):
        self._punch()
        self._punch(reason='Inverter label missing')
        body = _client_for(self.pm).get(
            reverse('cod_record', args=[self.site.project_id])).content.decode()
        self.assertIn('2 punch points are open', body)
        self.assertIn('Inverter label missing', body)
        self.assertNotIn('name="cod_pdf"', body)

    def test_allowed_once_the_point_is_closed(self):
        point = self._punch()
        PunchPoint.objects.filter(pk=point.pk).update(
            status=PunchPoint.CLOSED, closed_by=self.pm, closed_at=timezone.now(),
            closure_method=PunchPoint.CLOSED_ON_APPROVAL)
        self.assertEqual(self._record().status_code, 302)
        self.assertEqual(self._mirror_status(), Task.DONE)

    def test_allowed_once_the_point_is_waived(self):
        point = self._punch()
        response = _client_for(self.pm).post(
            reverse('punch_point_waive', args=[self.site.project_id, point.pk]),
            {'waiver_reason': 'Accepted by client; cosmetic only.'})
        self.assertIn(response.status_code, (200, 302))
        point.refresh_from_db()
        self.assertEqual(point.status, PunchPoint.WAIVED, 'fixture: point not waived')
        self.assertEqual(self._record().status_code, 302)
        self.assertEqual(self._mirror_status(), Task.DONE)

    def test_a_point_raised_after_cod_leaves_cod_standing(self):
        # Go-ahead Q2: COD is a dated fact; a later point still blocks HOTO (step 8).
        self._record()
        self._punch()
        self.assertEqual(self._mirror_status(), Task.DONE)
        self.assertTrue(CodRecord.objects.filter(project=self.site,
                                                 withdrawn_at__isnull=True).exists())

    def test_withdraw_is_never_blocked_by_open_points(self):
        # Go-ahead Q13.
        self._record()
        self._punch()
        record = CodRecord.objects.get(project=self.site)
        self.assertEqual(self._withdraw(record).status_code, 302)
        self.assertEqual(self._mirror_status(), Task.NOT_STARTED)

    def test_another_sites_point_does_not_block(self):
        self._punch(site=self.other_site)
        self.assertEqual(self._record().status_code, 302)


# ---------------------------------------------------------------------------
# 4 — form refusals; Q1 not activated; Q4 the T&C warning; storage off
# ---------------------------------------------------------------------------

class CodFormTests(CodFixture):

    def _refused(self, response, text):
        self.assertEqual(response.status_code, 200)
        self.assertIn(text, response.content.decode())
        self.assertFalse(CodRecord.objects.exists())
        self.assertEqual(self._mirror_status(), Task.NOT_STARTED)
        self.assertEqual(self._uploads(), 0)

    def test_blank_note_is_refused(self):
        self._refused(self._record(note='    '), 'Write a note')

    def test_missing_pdf_is_refused(self):
        self._refused(self._record(cod_pdf=None), 'Attach the evidence as a PDF.')

    def test_non_pdf_is_refused(self):
        self._refused(self._record(cod_pdf=_pdf('letter.docx', b'PK\x03\x04',
                                                'application/msword')),
                      'The COD evidence must be a PDF.')

    def test_pdf_name_without_pdf_bytes_is_refused(self):
        self._refused(self._record(cod_pdf=_pdf('letter.pdf', b'not a pdf')),
                      'That file is not a PDF')

    def test_future_cod_date_is_refused(self):
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        self._refused(self._record(cod_date=tomorrow), 'cannot be in the future')

    def test_missing_evidence_type_is_refused(self):
        self._refused(self._record(evidence_type=''), 'Choose the evidence')

    def test_today_is_accepted(self):
        response = self._record(cod_date=timezone.localdate().isoformat())
        self.assertEqual(response.status_code, 302)

    def test_q1_refused_on_a_site_that_is_not_activated(self):
        response = self._record(site=self.draft_site)
        self.assertEqual(response.status_code, 200)
        self.assertIn(COD_NOT_ACTIVATED, response.content.decode())
        self.assertFalse(CodRecord.objects.exists())
        self.assertEqual(self._uploads(), 0)
        # The card says so too, with no Record button.
        body = self._overview(self.pm, self.draft_site).content.decode()
        self.assertIn('id="cod-card"', body)
        self.assertIn(COD_NOT_ACTIVATED, body)
        self.assertNotIn(reverse('cod_record', args=[self.draft_site.project_id]), body)

    def test_q4_tc_warning_shows_and_does_not_block(self):
        self.assertNotEqual(self._tc_task(self.site).status, Task.DONE)
        body = _client_for(self.pm).get(
            reverse('cod_record', args=[self.site.project_id])).content.decode()
        self.assertIn(escape(TC_NOT_DONE_WARNING), body)
        self.assertIn('name="cod_pdf"', body)
        self.assertEqual(self._record().status_code, 302)
        self.assertEqual(self._mirror_status(), Task.DONE)

    def test_q4_no_warning_once_tc_is_done(self):
        Task.objects.filter(pk=self._tc_task(self.site).pk).update(status=Task.DONE)
        body = _client_for(self.pm).get(
            reverse('cod_record', args=[self.site.project_id])).content.decode()
        self.assertNotIn(escape(TC_NOT_DONE_WARNING), body)

    def test_storage_off_fails_closed_with_a_message(self):
        with self.settings(SUPABASE_BILLS_BUCKET=''):
            get = _client_for(self.pm).get(
                reverse('cod_record', args=[self.site.project_id])).content.decode()
            self.assertIn('not configured', get)
            self.assertNotIn('name="cod_pdf"', get)
            self._refused(self._record(), COD_STORAGE_OFF)

    def test_second_record_while_one_is_active_is_refused(self):
        self._record()
        response = self._record()
        self.assertEqual(response.status_code, 200)
        self.assertIn('already has a COD on record', response.content.decode())
        self.assertEqual(CodRecord.objects.filter(project=self.site).count(), 1)


# ---------------------------------------------------------------------------
# 5, 6 — withdraw, history, re-record; the database backstops
# ---------------------------------------------------------------------------

class CodWithdrawAndHistoryTests(CodFixture):

    def test_reason_is_required(self):
        self._record()
        record = CodRecord.objects.get(project=self.site)
        response = self._withdraw(record, reason='   ')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Give a reason', response.content.decode())
        self.assertIsNone(CodRecord.objects.get(pk=record.pk).withdrawn_at)
        self.assertEqual(self._mirror_status(), Task.DONE)

    def test_withdraw_then_re_record_keeps_both_rows_in_history(self):
        self._record(evidence_type=CodRecord.EVIDENCE_DISCOM_LETTER)
        first = CodRecord.objects.get(project=self.site)
        self._withdraw(first, by=self.coord, reason='Wrong meter number on the letter.')
        first = CodRecord.objects.get(pk=first.pk)
        self.assertEqual(first.withdrawn_by, self.coord)
        self.assertEqual(first.withdraw_reason, 'Wrong meter number on the letter.')
        self.assertEqual(self._mirror_status(), Task.NOT_STARTED)
        self.assertIsNone(self.mirror.completed_at)
        back = self._transitions().filter(to_status=Task.NOT_STARTED).last()
        self.assertEqual(back.actor, self.coord)
        self.assertEqual(back.reason_code, REASON_MIRROR_DERIVED)

        self.assertEqual(self._record(evidence_type=CodRecord.EVIDENCE_NET_METER)
                         .status_code, 302)
        self.assertEqual(self._mirror_status(), Task.DONE)
        self.assertEqual(CodRecord.objects.filter(project=self.site).count(), 2)

        body = self._overview(self.worker).content.decode()
        self.assertIn('Wrong meter number on the letter.', body)
        self.assertIn('DISCOM letter', body)
        self.assertIn('Net-meter installation', body)

    def test_withdrawing_twice_is_refused(self):
        self._record()
        record = CodRecord.objects.get(project=self.site)
        self._withdraw(record)
        response = self._withdraw(record)
        self.assertEqual(response.status_code, 200)
        self.assertIn('already withdrawn', response.content.decode())

    def test_a_record_from_another_site_is_404(self):
        self._record()
        record = CodRecord.objects.get(project=self.site)
        response = _client_for(self.pm_b).post(
            reverse('cod_withdraw', args=[self.other_site.project_id, record.pk]),
            {'reason': 'not mine'})
        self.assertEqual(response.status_code, 404)

    def test_database_refuses_a_second_active_record(self):
        fields = dict(project=self.site, cod_date=date(2026, 10, 1),
                      evidence_type=CodRecord.EVIDENCE_DISCOM_LETTER,
                      pdf_file_name='a.pdf', pdf_bucket=BUCKET, pdf_path='x/cod/a.pdf',
                      note='First.', recorded_by=self.pm)
        CodRecord.objects.create(**fields)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CodRecord.objects.create(**fields)

    def test_database_refuses_a_blank_note_and_half_a_withdrawal(self):
        base = dict(project=self.site, cod_date=date(2026, 10, 1),
                    evidence_type=CodRecord.EVIDENCE_DISCOM_LETTER,
                    pdf_file_name='a.pdf', pdf_bucket=BUCKET, pdf_path='x/cod/a.pdf',
                    recorded_by=self.pm)
        for extra in ({'note': ''},
                      {'note': 'ok', 'withdrawn_at': timezone.now()},
                      {'note': 'ok', 'evidence_type': 'photo'},
                      {'note': 'ok', 'pdf_path': ''}):
            with self.subTest(extra=extra):
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        CodRecord.objects.create(**{**base, **extra})

    def test_rows_are_append_only(self):
        self._record()
        record = CodRecord.objects.get(project=self.site)
        record.note = 'edited'
        with self.assertRaises(AppendOnlyViolation):
            record.save()
        with self.assertRaises(AppendOnlyViolation):
            record.delete()


# ---------------------------------------------------------------------------
# 7 — the PDF: signed at the click, private bucket, viewers only
# ---------------------------------------------------------------------------

class CodPdfLinkTests(CodFixture):

    def setUp(self):
        super().setUp()
        self._record()
        self.record = CodRecord.objects.get(project=self.site)
        self.url = reverse('cod_record_pdf', args=[self.site.project_id, self.record.pk])

    def test_every_viewer_is_redirected_to_a_freshly_signed_link(self):
        for profile in (self.pm, self.coord, self.worker, self.qaqc, self.scm,
                        self.finance, self.ceo):
            with self.subTest(who=profile.user.username):
                self.signer.reset_mock()
                response = _client_for(profile).get(self.url)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response['Location'], SIGNED)
                self.signer.assert_called_once()
                bucket, path = self.signer.call_args.args[:2]
                self.assertEqual((bucket, path), (BUCKET, self.record.pdf_path))

    def test_a_non_viewer_gets_404_and_nothing_is_signed(self):
        self.signer.reset_mock()
        self.assertEqual(_client_for(self.pm_b).get(self.url).status_code, 404)
        self.signer.assert_not_called()

    def test_the_overview_signs_nothing_and_links_to_the_redirect(self):
        self.signer.reset_mock()
        body = self._overview(self.worker).content.decode()
        self.signer.assert_not_called()
        self.assertIn(self.url, body)
        self.assertNotIn(SIGNED, body)

    def test_storage_off_sends_the_viewer_back_not_to_a_url(self):
        with self.settings(SUPABASE_BILLS_BUCKET=''):
            response = _client_for(self.pm).get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('project_overview', args=[self.site.project_id]),
                      response['Location'])

    def test_a_withdrawn_records_pdf_still_opens(self):
        self._withdraw(self.record)
        self.assertEqual(_client_for(self.worker).get(self.url)['Location'], SIGNED)


# ---------------------------------------------------------------------------
# The card on the site overview
# ---------------------------------------------------------------------------

class CodCardTests(CodFixture):

    def test_manager_sees_record_button_viewer_does_not(self):
        record_url = reverse('cod_record', args=[self.site.project_id])
        self.assertIn(record_url, self._overview(self.pm).content.decode())
        for profile in (self.worker, self.qaqc, self.scm, self.ceo):
            with self.subTest(who=profile.user.username):
                body = self._overview(profile).content.decode()
                self.assertIn('id="cod-card"', body)
                self.assertIn('Not recorded', body)
                self.assertNotIn(record_url, body)

    def test_card_shows_the_active_record(self):
        self._record(note='Commissioning report signed by HRPPL.',
                     evidence_type=CodRecord.EVIDENCE_COMMISSIONING_REPORT)
        record = CodRecord.objects.get(project=self.site)
        body = self._overview(self.scm).content.decode()
        self.assertIn('Commissioning report signed by HRPPL.', body)
        self.assertIn('Commissioning report', body)
        self.assertIn(record.cod_date.strftime('%d %b %Y'), body)
        self.assertNotIn(reverse('cod_withdraw', args=[self.site.project_id, record.pk]), body)
        self.assertIn(reverse('cod_withdraw', args=[self.site.project_id, record.pk]),
                      self._overview(self.pm).content.decode())

    def test_mirror_tooltip_names_the_cod_record(self):
        body = self._overview(self.pm).content.decode()
        self.assertIn('Updates from the COD record. Its status cannot be set here.', body)


# ---------------------------------------------------------------------------
# 8 — Residential and CAPEX: no card, no URL
# ---------------------------------------------------------------------------

class CodNotOnOtherSiteTypesTests(CodFixture):

    def test_no_card_and_every_url_404s(self):
        self._record()
        record = CodRecord.objects.get(project=self.site)
        admin = _profile('cod_admin', 'Admin')
        for project_type in ('Residential', 'CAPEX'):
            with self.subTest(project_type=project_type):
                site = self._site(f'COD {project_type}', self.pm, project_type)
                self.assertNotIn('id="cod-card"',
                                 self._overview(self.pm, site).content.decode())
                for url in (reverse('cod_record', args=[site.project_id]),
                            reverse('cod_withdraw', args=[site.project_id, record.pk]),
                            reverse('cod_record_pdf', args=[site.project_id, record.pk])):
                    for profile in (self.pm, admin):
                        self.assertEqual(_client_for(profile).get(url).status_code, 404, url)
                        self.assertEqual(_client_for(profile).post(url, {}).status_code,
                                         404, url)


# ---------------------------------------------------------------------------
# 9 — rung 0: the COD mirror still refuses every human status write
# ---------------------------------------------------------------------------

class CodMirrorRungZeroTests(CodFixture):

    def _human_write(self, target):
        future = (date.today() + timedelta(days=7)).isoformat()
        for name in ('task_status_update', 'task_detail_status_update'):
            _client_for(self.pm).post(
                reverse(name, args=[self.site.project_id, self.mirror.pk]),
                {'status': target, 'due_date': future})

    def test_humans_cannot_move_it_before_or_after_a_record(self):
        for target in (Task.IN_PROGRESS, Task.DONE, Task.BLOCKED):
            self._human_write(target)
        self.assertEqual(self._mirror_status(), Task.NOT_STARTED)
        self.assertFalse(self._transitions().exists())

        self._record()
        for target in (Task.NOT_STARTED, Task.IN_PROGRESS, Task.BLOCKED):
            self._human_write(target)
        self.assertEqual(self._mirror_status(), Task.DONE)
        self.assertEqual(self._transitions().count(), 1)


# ---------------------------------------------------------------------------
# Q5 notices; the orphan PDF
# ---------------------------------------------------------------------------

class CodNoticeTests(CodFixture):

    def _notices(self, template):
        return set(NotificationLog.objects.filter(template_name=template, channel='in_app')
                   .values_list('recipient_id', flat=True))

    def test_pm_coordinators_and_ceo_hear_both_events_minus_the_actor(self):
        self._record(by=self.coord)
        self.assertEqual(self._notices(cod_views.T_RECORDED),
                         {self.pm.pk, self.ceo.pk})
        record = CodRecord.objects.get(project=self.site)
        self._withdraw(record, by=self.pm)
        self.assertEqual(self._notices(cod_views.T_WITHDRAWN),
                         {self.coord.pk, self.ceo.pk})
        # Email goes to the same people (logged whatever the master switch says).
        self.assertTrue(NotificationLog.objects.filter(
            template_name=cod_views.T_RECORDED, channel='email',
            recipient=self.ceo).exists())

    def test_a_refused_record_tells_nobody(self):
        self._punch()
        self._record()
        self.assertFalse(NotificationLog.objects.filter(
            template_name=cod_views.T_RECORDED).exists())


class CodOrphanPdfTests(CodFixture):

    def _remove(self):
        return self.storage_client.return_value.storage.from_.return_value.remove

    def test_pdf_uploaded_then_refused_under_the_lock_is_deleted(self):
        # The race the lock exists for: no open point when the screen pre-checks, one
        # by the time the writer re-checks under the lock.
        point = self._punch()
        real = cod_views.open_punch_points_for_project
        calls = []

        def racing(project):
            calls.append(project.pk)
            if len(calls) == 1:
                return PunchPoint.objects.none()
            return real(project)

        with mock.patch('projects.cod_views.open_punch_points_for_project', side_effect=racing):
            response = self._record()
        self.assertEqual(response.status_code, 200)
        self.assertIn(point.reason, response.content.decode())
        self.assertEqual(self._uploads(), 1)
        uploaded_path = (self.storage_client.return_value.storage.from_.return_value
                         .upload.call_args.kwargs['path'])
        self._remove().assert_called_once_with([uploaded_path])
        self.assertFalse(CodRecord.objects.exists())
        self.assertEqual(self._mirror_status(), Task.NOT_STARTED)

    def test_discard_never_removes_a_recorded_or_non_cod_file(self):
        self._record()
        record = CodRecord.objects.get(project=self.site)
        self._remove().reset_mock()
        self.assertFalse(discard_unrecorded_cod_pdf(
            {'bucket': BUCKET, 'path': record.pdf_path}))
        self.assertFalse(discard_unrecorded_cod_pdf(
            {'bucket': BUCKET, 'path': f'{self.site.project_id}/bill/x.pdf'}))
        self.assertFalse(discard_unrecorded_cod_pdf(
            {'bucket': 'public-bucket', 'path': f'{self.site.project_id}/cod/x.pdf'}))
        self._remove().assert_not_called()
