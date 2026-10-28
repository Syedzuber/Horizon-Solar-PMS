"""
DR-2 — the site drawings card on project_overview and the drawing open view.

WHAT IS PINNED HERE
-------------------
  access    an SE holding a task sees the card and opens a drawing; an SE with no task
            gets the bare 404 on the page and on the open view; the site's QA/QC
            engineer and Finance (the D-4 portfolio role) see and open; Residential
            draws no card (DR-4, DR-6)
  refusals  the open view 404s a superseded zip's PDF, another site's PDF, a DR-10
            hidden PDF, a non-extracted row, and any PDF on a site that is not released
  states    not_released, under_revision (ledger row out of `released`), released with
            an open change request (pending AND with_pm), DR-9 unavailable, DR-10 count
  DR-3      every BOQ dict has exactly the six keys; make is the brand label or blank,
            never Vendor.name; the card holds no price word, no ordered quantity, no
            vendor name; boq_pdf / boq_excel files never appear
  feed      one activity row per open
  queries   constant with 1 and with 10 PDFs and BOQ lines, at most 5 added (DR-5)
  DR-2c     the card starts closed; both banners and the summary sit in the header; the
            BOQ box scrolls only above BOQ_SCROLL_ROWS lines, header row sticky; the
            context's query count per state is unchanged

Storage is mocked at the signer — no test here reaches a bucket.

Run with:
    python manage.py test projects.tests_site_drawings --settings=solarpms.test_settings
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.template.loader import render_to_string
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import (
    ACTOR_ROLE_SYSTEM, CAD_PDF_EXTRACTED, CAD_PDF_NOT_A_PDF, CHANGE_REQUEST_ORIGIN_PM,
    CHANGE_REQUEST_ORIGIN_SCM, CHANGE_REQUEST_WITH_PM, DESIGN_FILE_BOQ_EXCEL,
    DESIGN_FILE_BOQ_PDF, DESIGN_FILE_CAD_ZIP, DESIGN_IN_DESIGN, DESIGN_IN_QC,
    DESIGN_RELEASED, SUBJECT_DESIGN_ASSIGNMENT, ActivityLog, ArkaSubmission, BOQ,
    BOQCorrection, BOQItem, BOQItemMaster, DesignAssignment, DesignAttempt,
    DesignChangeRequest, DesignFile, DesignFilePdf, Program, Project, ProjectPhase,
    SiteQaqcAssignment, StatusTransition, Task, Vendor, VendorBrand, VendorCategory,
)
from .permissions import user_can_view_site_drawings
from .site_drawings_views import (
    BOQ_SCROLL_ROWS, DRAWING_LINK_SECONDS, STATE_NOT_RELEASED, STATE_RELEASED,
    STATE_UNDER_REVISION, site_drawings_context,
)

SIGNED = 'https://storage.example/signed/drawing.pdf?token=abc'
SIGNER = 'projects.site_drawings_views.get_design_file_url'
BUCKET = 'Horizon-PMS-Design'
DR3_KEYS = {'code', 'description', 'category', 'unit', 'quantity', 'make'}


def _profile(username, role):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title(), last_name='Test')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.save()
    return profile


def _client(profile):
    client = Client(SERVER_NAME='localhost')
    client.force_login(profile.user)
    return client


class SiteDrawingsBase(TestCase):
    """Two released OPEX sites and one Residential site.

    site    released, attempt 1. Current zip v2 holds Layout.pdf, Electrical.pdf and
            Site BOQ.pdf (DR-10 hidden), all extracted; superseded zip v1 holds
            Old-Layout.pdf, extracted. BOQ: two lines, one with a branded make, one with
            an unbranded make; both carry ordered quantities and an ordered vendor.
    other   released, its own zip with Other.pdf.
    res     Residential.
    se      Site Engineer holding a task on `site`.   se_other: a task on `other` only.
    qaqc    Site Engineer, `site`'s QA/QC engineer, no task.   finance: portfolio (D-4).
    """

    def setUp(self):
        self.pm       = _profile('dr2_pm', 'PM')
        self.designer = _profile('dr2_des', 'Design')
        self.se       = _profile('dr2_se', 'Site Engineer')
        self.se_other = _profile('dr2_se_other', 'Site Engineer')
        self.qaqc     = _profile('dr2_qaqc', 'Site Engineer')
        self.finance  = _profile('dr2_fin', 'Finance')
        self.program = Program.objects.create(
            name='Test-DR2', program_type='OPEX', client_name='DR2Client',
            status='Active', short_tender_code='DR2')

        self.site = self._site('DR2-A')
        self.assignment, self.attempt, self.arka = self._design(self.site)
        old_zip = self._zip(self.site, self.attempt, self.arka, version=1, current=False,
                            names=['Old-Layout.pdf'])
        self.old_pdf, = self._pdfs(old_zip, ['Old-Layout.pdf'])
        self.zip = self._zip(self.site, self.attempt, self.arka, version=2,
                             names=['Layout.pdf', 'Electrical.pdf', 'Site BOQ.pdf'])
        self.layout, self.electrical, self.boq_pdf = self._pdfs(
            self.zip, ['Layout.pdf', 'Electrical.pdf', 'Site BOQ.pdf'])
        self._task(self.site, self.se)
        SiteQaqcAssignment.objects.create(project=self.site, engineer=self.qaqc,
                                          assigned_by=self.pm)

        self.other = self._site('DR2-B')
        _a, other_attempt, other_arka = self._design(self.other)
        other_zip = self._zip(self.other, other_attempt, other_arka, version=1,
                              names=['Other.pdf'])
        self.other_pdf, = self._pdfs(other_zip, ['Other.pdf'])
        self._task(self.other, self.se_other)

        self.res = self._site('DR2-R', project_type='Residential')
        self._task(self.res, self.se)

        # BOQ: one branded make, one unbranded make; ordered figures and an ordered
        # vendor on both, which the card must never show.
        category = VendorCategory.objects.create(name='Module')
        self.make_vendor = self._vendor('Makewell Industries Pvt')
        self.make_vendor.categories.add(category)
        VendorBrand.objects.create(vendor=self.make_vendor, make_brand='SunPeak',
                                   category=category)
        self.plain_vendor = self._vendor('Plainline Traders Pvt')
        self.ordered_vendor = self._vendor('Orderly Supplies Pvt')
        self.boq = BOQ.objects.create(project=self.site)
        self._boq_line(self.boq, 1, self.make_vendor)
        self._boq_line(self.boq, 2, self.plain_vendor)

        signer = patch(SIGNER, return_value=SIGNED)
        self.signer = signer.start()
        self.addCleanup(signer.stop)

    # -- builders ------------------------------------------------------------

    def _site(self, code, project_type='OPEX'):
        site = Project(
            project_id=code, customer_name='DR2Client', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type=project_type,
            program=self.program if project_type == 'OPEX' else None,
            site_code=code if project_type == 'OPEX' else None,
            dc_capacity_kw=Decimal('100.00'), status='Active', assigned_pm=self.pm)
        site.save()
        return site

    def _design(self, site, status=DESIGN_RELEASED):
        assignment = DesignAssignment.objects.create(
            project=site, status=status, assigned_to=self.designer,
            current_attempt_number=1,
            released_at=timezone.now() - timedelta(days=2))
        attempt = DesignAttempt.objects.create(assignment=assignment, attempt_number=1)
        arka = ArkaSubmission.objects.create(
            attempt=attempt, version=1, capacity_kw=Decimal('100'),
            arka_link='https://example.com/arka', submitted_by=self.designer)
        return assignment, attempt, arka

    def _zip(self, site, attempt, arka, version, names, current=True,
             carried_from=None):
        return DesignFile.objects.create(
            attempt=attempt, kind=DESIGN_FILE_CAD_ZIP, version=version,
            bucket=BUCKET, path=f'{site.project_id}/cad_zip/v{version}.zip',
            archive_listing=[{'name': f'pkg/{n}', 'size': 1000} for n in names]
                            + [{'name': 'pkg/layout.dwg', 'size': 5000}],
            derived_from_arka=arka, uploaded_by=self.designer, is_current=current,
            carried_forward_from=carried_from)

    def _pdfs(self, zip_row, names):
        return [DesignFilePdf.objects.create(
                    design_file=zip_row, entry_name=f'pkg/{n}', display_name=n,
                    bucket=BUCKET, path=f'{zip_row.path[:-4]}/pdf/{i}.pdf',
                    size_bytes=2048, outcome=CAD_PDF_EXTRACTED)
                for i, n in enumerate(names)]

    def _task(self, site, profile):
        phase = ProjectPhase.objects.create(project=site, phase_name='Install',
                                            phase_order=1)
        Task.objects.create(phase=phase, task_name='Mount structure', task_order=1,
                            assigned_role=Task.SITE_ENGINEER, assigned_to=profile)

    def _vendor(self, name):
        return Vendor.objects.create(name=name, contact_person='C', phone='9000000000')

    def _boq_line(self, boq, serial, make_vendor):
        master = BOQItemMaster.objects.create(
            code=f'DR2-{serial:03d}', description=f'Panel line {serial}', unit='Nos',
            category='Module', project_type='OPEX', sort_order=serial)
        return BOQItem.objects.create(
            boq=boq, serial_no=serial, category='Module',
            description=f'Panel line {serial}', item_master=master, uom='Nos',
            boq_quantity=Decimal('12.50'), ordered_quantity=Decimal('987.00'),
            ordered_vendor=self.ordered_vendor, make_preference=make_vendor)

    def _left_released(self, assignment, when):
        StatusTransition.objects.create(
            subject_type=SUBJECT_DESIGN_ASSIGNMENT, subject_id=assignment.pk,
            project=assignment.project, from_status=DESIGN_RELEASED,
            to_status=DESIGN_IN_DESIGN, actor_role_code=ACTOR_ROLE_SYSTEM,
            occurred_at=when)

    def _overview(self, profile, site=None):
        return _client(profile).get(
            reverse('project_overview', args=[(site or self.site).project_id]))

    def _open(self, profile, pdf):
        return _client(profile).get(reverse('site_drawing_open', args=[pdf.pk]))


class AccessTests(SiteDrawingsBase):

    def test_se_with_a_task_sees_the_card_and_opens_a_drawing(self):
        response = self._overview(self.se)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="site-drawings-card"')
        self.assertContains(response, reverse('site_drawing_open', args=[self.layout.pk]))
        opened = self._open(self.se, self.layout)
        self.assertEqual(opened.status_code, 302)
        self.assertEqual(opened['Location'], SIGNED)
        self.signer.assert_called_once_with(self.layout.bucket, self.layout.path,
                                            expires_in=DRAWING_LINK_SECONDS)
        self.assertEqual(DRAWING_LINK_SECONDS, 300)

    def test_se_without_a_task_gets_404_on_the_page_and_the_drawing(self):
        self.assertEqual(self._overview(self.se_other).status_code, 404)
        self.assertEqual(self._open(self.se_other, self.layout).status_code, 404)
        self.signer.assert_not_called()

    def test_qaqc_engineer_sees_and_opens(self):
        self.assertContains(self._overview(self.qaqc), 'id="site-drawings-card"')
        self.assertEqual(self._open(self.qaqc, self.layout).status_code, 302)

    def test_finance_portfolio_role_sees_and_opens(self):
        self.assertContains(self._overview(self.finance), 'id="site-drawings-card"')
        self.assertEqual(self._open(self.finance, self.layout).status_code, 302)

    def test_residential_draws_no_card(self):
        response = self._overview(self.se, self.res)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="site-drawings-card"')
        self.assertFalse(user_can_view_site_drawings(self.se.user, self.res))

    def test_helper_is_view_and_opex(self):
        self.assertTrue(user_can_view_site_drawings(self.se.user, self.site))
        self.assertTrue(user_can_view_site_drawings(self.qaqc.user, self.site))
        self.assertTrue(user_can_view_site_drawings(self.finance.user, self.site))
        self.assertFalse(user_can_view_site_drawings(self.se_other.user, self.site))


class OpenRefusalTests(SiteDrawingsBase):

    def assert404(self, pdf):
        self.assertEqual(self._open(self.finance, pdf).status_code, 404)

    def test_superseded_zip_pdf(self):
        self.assert404(self.old_pdf)

    def test_another_sites_pdf(self):
        self.assertEqual(self._open(self.se, self.other_pdf).status_code, 404)

    def test_dr10_hidden_pdf(self):
        self.assert404(self.boq_pdf)

    def test_non_extracted_outcome(self):
        row = DesignFilePdf.objects.create(
            design_file=self.zip, entry_name='pkg/Fake.pdf', display_name='Fake.pdf',
            outcome=CAD_PDF_NOT_A_PDF)
        self.assert404(row)

    def test_not_released_site(self):
        DesignAssignment.objects.filter(pk=self.assignment.pk).update(status=DESIGN_IN_QC)
        self.assert404(self.layout)

    def test_under_revision_site(self):
        DesignAssignment.objects.filter(pk=self.assignment.pk).update(
            status=DESIGN_IN_DESIGN, released_at=None)
        self._left_released(self.assignment, timezone.now())
        self.assert404(self.layout)

    def test_no_such_pdf(self):
        response = _client(self.finance).get(reverse('site_drawing_open', args=[999999]))
        self.assertEqual(response.status_code, 404)

    def test_none_of_the_refusals_signed_anything(self):
        for pdf in (self.old_pdf, self.boq_pdf, self.other_pdf):
            self._open(self.finance if pdf is not self.other_pdf else self.se, pdf)
        self.signer.assert_not_called()


class StateTests(SiteDrawingsBase):

    def test_released_lists_drawings_hides_boq_pdf_and_counts_it(self):
        ctx = site_drawings_context(self.site)
        self.assertEqual(ctx['state'], STATE_RELEASED)
        self.assertEqual([d.display_name for d in ctx['drawings']],
                         ['Electrical.pdf', 'Layout.pdf'])
        self.assertEqual(ctx['hidden_count'], 1)
        self.assertFalse(ctx['unavailable'])
        self.assertFalse(ctx['pending_change'])
        html = self._overview(self.se).content.decode()
        self.assertIn('1 file(s) not shown.', html)
        self.assertNotIn('Site BOQ.pdf', html)
        self.assertNotIn('Old-Layout.pdf', html)

    def test_carried_copy_reads_its_originals_pdfs(self):
        attempt2 = DesignAttempt.objects.create(assignment=self.assignment,
                                                attempt_number=2)
        arka2 = ArkaSubmission.objects.create(
            attempt=attempt2, version=1, capacity_kw=Decimal('100'),
            arka_link='https://example.com/arka', submitted_by=self.designer)
        self._zip(self.site, attempt2, arka2, version=1,
                  names=['Layout.pdf', 'Electrical.pdf', 'Site BOQ.pdf'],
                  carried_from=self.zip)
        DesignAssignment.objects.filter(pk=self.assignment.pk).update(
            current_attempt_number=2)
        ctx = site_drawings_context(self.site)
        self.assertEqual({d.pk for d in ctx['drawings']}, {self.layout.pk, self.electrical.pk})
        self.assertEqual(self._open(self.se, self.layout).status_code, 302)

    def test_not_released_without_a_ledger_row(self):
        DesignAssignment.objects.filter(pk=self.assignment.pk).update(status=DESIGN_IN_QC)
        self.assertEqual(site_drawings_context(self.site), {'state': STATE_NOT_RELEASED})
        self.assertContains(self._overview(self.se), 'Design not yet released.')

    def test_no_assignment_is_not_released(self):
        bare = self._site('DR2-C')
        self.assertEqual(site_drawings_context(bare), {'state': STATE_NOT_RELEASED})

    def test_under_revision_shows_the_red_banner_and_nothing_else(self):
        DesignAssignment.objects.filter(pk=self.assignment.pk).update(
            status=DESIGN_IN_DESIGN, released_at=None)
        left = timezone.make_aware(timezone.datetime(2026, 10, 20, 12, 0))
        self._left_released(self.assignment, left - timedelta(days=5))
        self._left_released(self.assignment, left)
        ctx = site_drawings_context(self.site)
        self.assertEqual(ctx, {'state': STATE_UNDER_REVISION,
                               'since': timezone.localtime(left).date()})
        html = self._overview(self.se).content.decode()
        self.assertIn('Design under revision since 20 Oct 2026. Check with your PM before '
                      'continuing.', html)
        self.assertIn('alert-danger', html)
        self.assertNotIn('Layout.pdf', html)
        self.assertNotIn('Panel line 1', html)

    def test_released_with_a_pending_change_request(self):
        DesignChangeRequest.objects.create(attempt=self.attempt, requested_by=self.pm,
                                           reason='Move the inverter',
                                           origin=CHANGE_REQUEST_ORIGIN_PM)
        ctx = site_drawings_context(self.site)
        self.assertTrue(ctx['pending_change'])
        self.assertTrue(ctx['drawings'])
        html = self._overview(self.se).content.decode()
        self.assertIn('A design change has been requested. Check with your PM before '
                      'starting affected work.', html)
        self.assertIn('Layout.pdf', html)

    def test_with_pm_counts_as_open(self):
        DesignChangeRequest.objects.create(attempt=self.attempt, requested_by=self.pm,
                                           reason='Quantity short',
                                           origin=CHANGE_REQUEST_ORIGIN_SCM,
                                           verdict=CHANGE_REQUEST_WITH_PM)
        self.assertTrue(site_drawings_context(self.site)['pending_change'])

    def test_unrecorded_pdf_entry_is_unavailable(self):
        DesignFile.objects.filter(pk=self.zip.pk).update(archive_listing=(
            self.zip.archive_listing + [{'name': 'pkg/SLD.pdf', 'size': 10}]))
        ctx = site_drawings_context(self.site)
        self.assertTrue(ctx['unavailable'])
        self.assertEqual(len(ctx['drawings']), 2)   # the extracted ones still show
        self.assertContains(self._overview(self.se),
                            'Drawing PDFs not available — contact Design.')

    def test_released_attempt_without_a_zip_is_unavailable(self):
        DesignFile.objects.filter(pk=self.zip.pk).update(is_current=False)
        ctx = site_drawings_context(self.site)
        self.assertEqual(ctx['drawings'], [])
        self.assertTrue(ctx['unavailable'])

    def test_corrected_on_only_after_release(self):
        correction = BOQCorrection.objects.create(
            boq=self.boq, item_code='DR2-001', action='quantity_changed',
            quantity_before=Decimal('10'), quantity_after=Decimal('12.50'),
            corrected_by=self.designer)
        self.assertEqual(site_drawings_context(self.site)['corrected_on'],
                         timezone.localdate())
        BOQCorrection.objects.filter(pk=correction.pk).update(
            corrected_at=timezone.now() - timedelta(days=3))
        self.assertIsNone(site_drawings_context(self.site)['corrected_on'])


class BoqAllowlistTests(SiteDrawingsBase):

    def test_every_row_has_exactly_the_six_keys(self):
        rows = site_drawings_context(self.site)['boq_rows']
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(set(row), DR3_KEYS)
        self.assertEqual(rows[0], {'code': 'DR2-001', 'description': 'Panel line 1',
                                   'category': 'Module', 'unit': 'Nos',
                                   'quantity': Decimal('12.50'), 'make': 'SunPeak'})
        self.assertEqual(rows[1]['make'], '')   # unbranded make: blank, never Vendor.name

    def test_rendered_card_has_no_money_no_ordered_and_no_vendor(self):
        card = render_to_string('projects/_site_drawings_card.html',
                                {'site_drawings': site_drawings_context(self.site)})
        self.assertIn('SunPeak', card)
        self.assertIn('12.5', card)
        for word in ('₹', 'Rate', 'Amount', 'ordered', 'Ordered', '987',
                     self.make_vendor.name, self.plain_vendor.name,
                     self.ordered_vendor.name):
            self.assertNotIn(word, card)

    def test_boq_files_never_appear(self):
        for kind, name in ((DESIGN_FILE_BOQ_PDF, 'Priced-BOQ-Sheet.pdf'),
                           (DESIGN_FILE_BOQ_EXCEL, 'Priced-BOQ-Sheet.xlsx')):
            DesignFile.objects.create(
                attempt=self.attempt, kind=kind, version=1, bucket=BUCKET,
                path=f'DR2-A/{kind}/x', original_filename=name,
                derived_from_arka=self.arka, uploaded_by=self.designer)
        html = self._overview(self.se).content.decode()
        self.assertNotIn('Priced-BOQ-Sheet', html)


class ActivityTests(SiteDrawingsBase):

    def test_one_activity_row_per_open(self):
        self._open(self.se, self.layout)
        self._open(self.se, self.layout)
        rows = ActivityLog.objects.filter(action_code='site_drawing_opened',
                                          project=self.site)
        self.assertEqual(rows.count(), 2)
        row = rows.first()
        self.assertEqual(row.actor, self.se)
        self.assertEqual(row.entity_type, 'DesignFilePdf')
        self.assertEqual(row.entity_id, self.layout.pk)
        self.assertEqual(row.action, 'Opened drawing Layout.pdf')

    def test_a_refused_open_writes_nothing(self):
        self._open(self.se, self.boq_pdf)
        self.assertFalse(ActivityLog.objects.filter(action_code='site_drawing_opened')
                         .exists())


class QueryCountTests(SiteDrawingsBase):
    """DR-5: constant in PDFs and BOQ lines, at most 5 added to the page."""

    def _grow(self):
        names = [f'Sheet-{i:02d}.pdf' for i in range(8)]
        DesignFile.objects.filter(pk=self.zip.pk).update(archive_listing=(
            self.zip.archive_listing + [{'name': f'pkg/{n}', 'size': 10} for n in names]))
        self._pdfs(self.zip, names)
        for serial in range(3, 11):
            self._boq_line(self.boq, serial, self.make_vendor)

    def _context_queries(self):
        with CaptureQueriesContext(connection) as ctx:
            site_drawings_context(self.site)
        return len(ctx.captured_queries)

    def _page_queries(self, card=True):
        client = _client(self.se)
        url = reverse('project_overview', args=[self.site.project_id])
        client.get(url)   # warm the session
        if card:
            with CaptureQueriesContext(connection) as ctx:
                client.get(url)
        else:
            with patch('projects.site_drawings_views.site_drawings_context',
                       return_value={'state': 'not_opex'}), \
                    CaptureQueriesContext(connection) as ctx:
                client.get(url)
        return len(ctx.captured_queries)

    def test_context_is_constant_and_within_five(self):
        small = self._context_queries()
        self.assertEqual(len(site_drawings_context(self.site)['drawings']), 2)
        self._grow()
        ctx = site_drawings_context(self.site)
        self.assertEqual(len(ctx['drawings']), 10)
        self.assertEqual(len(ctx['boq_rows']), 10)
        self.assertEqual(self._context_queries(), small)
        self.assertLessEqual(small, 5)

    def test_page_adds_at_most_five_constant(self):
        before = self._page_queries(card=False)
        small = self._page_queries() - before
        self._grow()
        large = self._page_queries() - self._page_queries(card=False)
        self.assertLessEqual(small, 5)
        self.assertEqual(small, large)


class FollowUpTests(SiteDrawingsBase):
    """DR-2 follow-up: the Make column only when a make exists; drawing opens kept out of
    the Recent activity card (in the query, before the 8-row slice)."""

    def _card(self):
        return render_to_string('projects/_site_drawings_card.html',
                                {'site_drawings': site_drawings_context(self.site)})

    def test_make_column_present_when_a_row_has_a_label(self):
        ctx = site_drawings_context(self.site)
        self.assertTrue(ctx['show_make'])
        card = self._card()
        self.assertIn('<th>Make</th>', card)
        self.assertIn('SunPeak', card)

    def test_make_column_absent_when_no_row_has_a_label(self):
        VendorBrand.objects.all().delete()
        ctx = site_drawings_context(self.site)
        self.assertFalse(ctx['show_make'])
        self.assertEqual({row['make'] for row in ctx['boq_rows']}, {''})
        for row in ctx['boq_rows']:
            self.assertEqual(set(row), DR3_KEYS)   # the allowlist is unchanged
        card = self._card()
        self.assertNotIn('Make', card)
        self.assertIn('Panel line 1', card)

    def _feed_row(self, action, code, at):
        row = ActivityLog.objects.create(project=self.site, actor=self.pm, action=action,
                                         action_code=code)
        # auto_now_add cannot be set on create; the order is what the test is about.
        ActivityLog.objects.filter(pk=row.pk).update(timestamp=at)

    def test_feed_shows_eight_other_rows_when_the_latest_are_opens(self):
        ActivityLog.objects.filter(project=self.site).delete()
        now = timezone.now()
        for i in range(8):
            self._feed_row(f'Other event {i}', 'task_status_changed',
                           now - timedelta(hours=1, minutes=i))
        for i in range(10):
            self._feed_row(f'Opened drawing Sheet-{i}.pdf', 'site_drawing_opened',
                           now - timedelta(minutes=i))
        feed = self._overview(self.se).context['recent_activity']
        self.assertEqual(len(feed), 8)
        self.assertEqual([row.action for row in feed],
                         [f'Other event {i}' for i in range(8)])
        self.assertFalse(any(row.action_code == 'site_drawing_opened' for row in feed))

    def test_an_open_still_writes_its_row_and_stays_out_of_the_card(self):
        self.assertEqual(self._open(self.se, self.layout).status_code, 302)
        self.assertTrue(ActivityLog.objects.filter(
            project=self.site, action_code='site_drawing_opened',
            entity_id=self.layout.pk).exists())
        feed = self._overview(self.se).context['recent_activity']
        self.assertNotIn('site_drawing_opened', [row.action_code for row in feed])


class ClosedCardTests(SiteDrawingsBase):
    """DR-2c: closed by default, banners and summary in the header, the BOQ's own scroll
    box above BOQ_SCROLL_ROWS lines."""

    def _card(self):
        return render_to_string('projects/_site_drawings_card.html',
                                {'site_drawings': site_drawings_context(self.site)})

    def _header(self, card):
        # The header is everything before the collapsed body; a card with no body (not
        # released, under revision) is all header.
        # Cut at the body's own div: the toggle in the header also names siteDrawingsCollapse.
        start = card.index('class="card-header')
        end = card.find('<div class="collapse"', start)
        return card[start:] if end == -1 else card[start:end]

    def _under_revision(self):
        DesignAssignment.objects.filter(pk=self.assignment.pk).update(
            status=DESIGN_IN_DESIGN, released_at=None)
        self._left_released(self.assignment, timezone.now())

    def test_card_renders_collapsed(self):
        html = self._overview(self.se).content.decode()
        self.assertIn('<div class="collapse" id="siteDrawingsCollapse">', html)
        self.assertNotIn('collapse show" id="siteDrawingsCollapse"', html)
        self.assertIn('data-bs-target="#siteDrawingsCollapse" aria-expanded="false"', html)
        # Closed, not absent: the drawings and BOQ are in the page behind the toggle.
        self.assertIn('Layout.pdf', html)
        self.assertIn('Panel line 1', html)

    def test_under_revision_banner_is_in_the_header(self):
        self._under_revision()
        header = self._header(self._card())
        self.assertIn('alert-danger', header)
        self.assertIn('Design under revision since', header)
        self.assertNotIn('siteDrawingsCollapse', header)   # nothing to open

    def test_change_requested_banner_is_in_the_header(self):
        DesignChangeRequest.objects.create(attempt=self.attempt, requested_by=self.pm,
                                           reason='Move the inverter',
                                           origin=CHANGE_REQUEST_ORIGIN_PM)
        card = self._card()
        header = self._header(card)
        self.assertIn('alert-warning', header)
        self.assertIn('A design change has been requested.', header)
        self.assertEqual(card.count('A design change has been requested.'), 1)

    def test_no_banner_on_a_released_site_without_a_change_request(self):
        header = self._header(self._card())
        self.assertNotIn('alert-', header)

    def test_summary_released(self):
        self.assertIn('2 drawings · 2 BOQ lines', self._header(self._card()))
        DesignFilePdf.objects.filter(pk=self.electrical.pk).delete()
        BOQItem.objects.filter(boq=self.boq, serial_no=2).delete()
        self.assertIn('1 drawing · 1 BOQ line<', self._header(self._card()))

    def test_summary_not_released(self):
        DesignAssignment.objects.filter(pk=self.assignment.pk).update(status=DESIGN_IN_QC)
        header = self._header(self._card())
        self.assertIn('Design not yet released.', header)
        self.assertNotIn('data-bs-toggle', header)

    def test_summary_under_revision_is_the_banner(self):
        self._under_revision()
        header = self._header(self._card())
        self.assertNotIn('id="site-drawings-summary"', header)
        self.assertIn('Check with your PM before continuing.', header)
        self.assertNotIn('data-bs-toggle', header)

    def _rows(self, total):
        for serial in range(3, total + 1):
            self._boq_line(self.boq, serial, self.make_vendor)

    def test_no_scroll_box_at_eleven_rows(self):
        self.assertEqual(BOQ_SCROLL_ROWS, 11)
        self._rows(11)
        ctx = site_drawings_context(self.site)
        self.assertEqual(ctx['boq_line_count'], 11)
        self.assertFalse(ctx['boq_scrolls'])
        card = self._card()
        self.assertNotIn('id="site-boq-scroll"', card)
        self.assertNotIn('position:sticky', card)
        self.assertIn('class="table-responsive"', card)   # sideways scroll stays

    def test_scroll_box_with_sticky_header_above_eleven_rows(self):
        self._rows(12)
        self.assertTrue(site_drawings_context(self.site)['boq_scrolls'])
        card = self._card()
        self.assertIn('id="site-boq-scroll"', card)
        self.assertIn('max-height:22.5rem; overflow-y:auto;', card)
        self.assertIn('<thead class="table-light" style="position:sticky; top:0; z-index:1;">',
                      card)
        box = card.index('id="site-boq-scroll"')
        self.assertIn('table-responsive', card[card.rindex('<div', 0, box):box])

    def test_context_query_count_per_state_unchanged(self):
        # Measured before DR-2c: released 4 (assignment, zip, PDF rows, BOQ rows), the
        # other two states 1 (the assignment). The summary and scroll flag are lengths.
        def queries():
            with CaptureQueriesContext(connection) as ctx:
                site_drawings_context(self.site)
            return len(ctx.captured_queries)
        self.assertEqual(queries(), 4)
        self._rows(15)
        self.assertEqual(queries(), 4)
        DesignAssignment.objects.filter(pk=self.assignment.pk).update(status=DESIGN_IN_QC)
        self.assertEqual(queries(), 1)
        self._under_revision()
        self.assertEqual(queries(), 1)
