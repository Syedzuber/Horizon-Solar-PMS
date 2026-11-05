"""
TD-B3 — tender documents: the site overview card, the open view, SE visibility.

WHAT IS PINNED HERE
-------------------
  card        PM: every current document, badges, Manage link. SE on the site: Site
              team documents only, no badges, no Manage link. SE where only Management
              only documents exist: no card. SCM / Finance / CEO: every document, no
              Manage link; Admin: with it. A manager with no documents: empty state and
              Manage link. Residential, CAPEX, Cancelled: no card. Voided and superseded
              documents never on the card. visible_tender_documents() agrees with
              user_can_view_tender_document() for every (user, document, site).
  open        an SE opens a Site team document: 302 to the signed link, one
              TenderDocumentOpen row with the site. A Management only, superseded or
              voided document for the SE: 404, nothing logged. A manager of site A opens
              a history row from site B's page in the same tender. Another Program's
              document through this site's URL: 404. Signer returns None: back to the
              card with the message, nothing logged. Residential, CAPEX, Cancelled: 404.
              A user who cannot see the site: refuse_site_view_404.
  manage      history shown to a manager of another site in the tender; open counts.
  queries     project_overview's added queries for PM, SE, SCM, Finance, CEO and Admin:
              at most 4 over the page without the card, and the same with 3 or 13 live
              sites in the tender and with 1 or 5 documents. The manage page GET is the
              same at 3 and 13 sites.
  ruling A    user_can_manage_tender_documents_on_site agrees with
              user_can_manage_tender_documents for every user x site, in <= 3 queries.
  silence     no ActivityLog row, send_notification never called (B3-g).

Storage is mocked: the signer (design_storage.get_design_file_url, as imported by
tender_documents) and the client. No test reaches a bucket.

Run with:
    python manage.py test projects.tests_tender_documents_b3 --settings=solarpms.test_settings
"""
from unittest.mock import patch

from django.contrib.messages import get_messages
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import (
    TENDER_DOC_SITE_TEAM, ActivityLog, Project, SiteQaqcAssignment, TenderDocument,
    TenderDocumentOpen,
)
from .permissions import (
    user_can_manage_tender_documents, user_can_manage_tender_documents_on_site,
    user_can_open_tender_document, user_can_view_tender_document, visible_tender_documents,
)
from .tests_tender_documents_b2 import TenderDocsB2Base, _profile

SIGNER = 'projects.tender_documents.get_design_file_url'
SIGNED = 'https://signed.example/tender-doc'
CARD_BUILDER = 'projects.views_tender_documents.tender_documents_card_context'
OPEN_FAILED = ('This document cannot be opened right now. Try again, or contact the '
               "tender's PM if it keeps failing.")

#: The go-ahead's query budget: the card may add at most this many queries to
#: project_overview, for the PM and for an SE.
QUERY_BUDGET = 4


class TenderDocsB3Base(TenderDocsB2Base):
    """The B2 fixture plus, in P1: site_doc (Site team), old_doc (Site team, superseded
    by new_doc), new_doc, void_doc (Site team, voided). pm3 manages S3 in P1
    and is S1's QA/QC engineer, so they see S1 without managing it. se2 holds a task on
    S2, whose tender (P2) has only a Management only document. P3 (OPEX) has S4 (pm2)
    and no documents."""

    def setUp(self):
        super().setUp()
        self.pm3 = _profile('b3_pm3', 'PM')
        self.se2 = _profile('b3_se2', 'Site Engineer')
        self.s3 = self._site('BTP1-S3', 'OPEX', self.p1, self.pm3)
        SiteQaqcAssignment.objects.create(project=self.s1, engineer=self.pm3,
                                          assigned_by=self.admin)
        self._task(self.s2, self.se2)
        self.p3 = self._program('B3-P3', 'OPEX', 'BTP3')
        self.s4 = self._site('BTP3-S4', 'OPEX', self.p3, self.pm2)

        now = timezone.now()
        self.site_doc = self._doc(self.p1, 'Module datasheet', TENDER_DOC_SITE_TEAM)
        self.old_doc = self._doc(self.p1, 'Layout v1', TENDER_DOC_SITE_TEAM)
        self.new_doc = self._doc(self.p1, 'Layout v2', TENDER_DOC_SITE_TEAM)
        TenderDocument.objects.filter(pk=self.old_doc.pk).update(
            superseded_by=self.new_doc, superseded_at=now)
        self.void_doc = self._doc(self.p1, 'Wrong earthing spec', TENDER_DOC_SITE_TEAM)
        TenderDocument.objects.filter(pk=self.void_doc.pk).update(
            voided_at=now, voided_by=self.pm1, void_reason='Uploaded the wrong file')
        self.old_doc.refresh_from_db()
        self.void_doc.refresh_from_db()

    # -- requests -----------------------------------------------------------------

    def _overview(self, profile, site=None):
        self.client.force_login(profile.user)
        return self.client.get(reverse('project_overview',
                                       args=[(site or self.s1).project_id]))

    def _card(self, profile, site=None):
        response = self._overview(profile, site)
        self.assertEqual(response.status_code, 200)
        return response, response.context['tender_documents_card']

    def _open(self, profile, doc, site=None, signed=SIGNED):
        self.client.force_login(profile.user)
        with patch(SIGNER, return_value=signed) as signer:
            response = self.client.get(reverse(
                'tender_documents_open', args=[(site or self.s1).project_id, doc.pk]))
        return response, signer

    @staticmethod
    def _row(content, doc):
        """The card's <li> for `doc`, or '' when it is not on the page."""
        marker = f'id="tender-doc-{doc.pk}"'
        if marker not in content:
            return ''
        return content.split(marker, 1)[1].split('</li>', 1)[0]


class CardTests(TenderDocsB3Base):
    """B3-a, B3-b."""

    def test_pm_sees_every_current_document_with_badges_and_manage(self):
        response, card = self._card(self.pm1)
        self.assertEqual({d.pk for d in card['documents']},
                         {self.current_doc.pk, self.site_doc.pk, self.new_doc.pk})
        self.assertTrue(card['show_badges'])
        content = response.content.decode()
        self.assertIn('id="tender-documents-card"', content)
        self.assertIn('Management only', self._row(content, self.current_doc))
        self.assertIn('Site team', self._row(content, self.site_doc))
        self.assertIn('id="tender-documents-manage"', content)
        self.assertIn(reverse('tender_documents_manage', args=[self.s1.project_id]), content)
        self.assertIn('Applies to all sites in B2-P1', content)

    def test_card_is_closed_by_default_and_opens_in_a_new_tab(self):
        content = self._overview(self.pm1).content.decode()
        self.assertIn('<div class="collapse" id="tenderDocumentsCollapse">', content)
        self.assertIn('data-bs-target="#tenderDocumentsCollapse" aria-expanded="false"',
                      content)
        row = self._row(content, self.site_doc)
        self.assertIn(reverse('tender_documents_open',
                              args=[self.s1.project_id, self.site_doc.pk]), row)
        self.assertIn('target="_blank" rel="noopener"', row)
        # Nothing signed on the page, and the template's comment does not leak.
        self.assertNotIn(self.site_doc.storage_path, content)
        self.assertNotIn('TD-B3 — the tender documents card', content)

    def test_row_shows_title_category_uploader_date_size(self):
        content = self._overview(self.pm1).content.decode()
        row = self._row(content, self.site_doc)
        self.assertIn('Module datasheet', row)
        self.assertIn(self.site_doc.get_category_display(), row)
        self.assertIn('b2_pm1', row)
        self.assertIn(timezone.localtime(self.site_doc.uploaded_at).strftime('%d %b %Y'), row)
        self.assertIn('1.0', row)   # 1024 bytes -> "1.0 KB"
        self.assertIn('>Open</a>', row)

    def test_se_sees_site_team_documents_only_no_badges_no_manage(self):
        response, card = self._card(self.se1)
        self.assertEqual({d.pk for d in card['documents']},
                         {self.site_doc.pk, self.new_doc.pk})
        self.assertFalse(card['show_badges'])
        self.assertIsNone(card['manage_url'])
        content = response.content.decode()
        self.assertNotIn(f'id="tender-doc-{self.current_doc.pk}"', content)
        self.assertNotIn('Approved makes', content)
        self.assertNotIn('Site team', self._row(content, self.site_doc))
        self.assertNotIn('id="tender-documents-manage"', content)

    def test_se_with_only_management_documents_gets_no_card(self):
        response, card = self._card(self.se2, self.s2)
        self.assertIsNone(card)
        self.assertNotIn('id="tender-documents-card"', response.content.decode())

    def test_portfolio_roles_see_everything_without_manage_unless_admin(self):
        for profile in (self.scm, self.finance, self.ceo):
            _response, card = self._card(profile)
            self.assertEqual({d.pk for d in card['documents']},
                             {self.current_doc.pk, self.site_doc.pk, self.new_doc.pk},
                             profile.role)
            self.assertTrue(card['show_badges'], profile.role)
            self.assertIsNone(card['manage_url'], profile.role)
        response, card = self._card(self.admin)
        self.assertTrue(card['show_badges'])
        self.assertIsNotNone(card['manage_url'])
        self.assertIn('id="tender-documents-manage"', response.content.decode())

    def test_manager_with_no_documents_gets_empty_state_and_manage(self):
        response, card = self._card(self.pm2, self.s4)
        self.assertEqual(card['documents'], [])
        content = response.content.decode()
        self.assertIn('id="tender-documents-empty"', content)
        self.assertIn('id="tender-documents-manage"', content)

    def test_no_card_on_residential_capex_or_cancelled(self):
        for site in (self.r1, self.c1, self.s_cancel):
            response, card = self._card(self.pm1, site)
            self.assertIsNone(card, site.project_id)
            self.assertNotIn('id="tender-documents-card"', response.content.decode())

    def test_voided_and_superseded_never_on_the_card(self):
        for profile in (self.pm1, self.admin, self.se1):
            content = self._overview(profile).content.decode()
            self.assertNotIn(f'id="tender-doc-{self.old_doc.pk}"', content)
            self.assertNotIn(f'id="tender-doc-{self.void_doc.pk}"', content)
            self.assertNotIn('Wrong earthing spec', content)

    def test_visible_documents_agree_with_the_per_document_helper(self):
        users = (self.pm1, self.pm2, self.pm3, self.coord, self.se1, self.se2, self.scm,
                 self.finance, self.ceo, self.admin, self.sysadmin)
        sites = (self.s1, self.s1b, self.s2, self.s3, self.s4, self.s_cancel, self.c1,
                 self.r1)
        documents = list(TenderDocument.objects.all())
        checked = 0
        for profile in users:
            for site in sites:
                visible = set(visible_tender_documents(profile.user, site)
                              .values_list('pk', flat=True))
                for doc in documents:
                    self.assertEqual(
                        doc.pk in visible,
                        user_can_view_tender_document(profile.user, doc, site),
                        f'{profile.user.username} / {site.project_id} / {doc.title}')
                    checked += 1
        self.assertEqual(checked, len(users) * len(sites) * len(documents))


class OpenTests(TenderDocsB3Base):
    """B3-c, B3-d."""

    def test_se_opens_a_site_team_document(self):
        response, signer = self._open(self.se1, self.site_doc)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], SIGNED)
        signer.assert_called_once_with(self.site_doc.storage_bucket,
                                       self.site_doc.storage_path, expires_in=300)
        row = TenderDocumentOpen.objects.get()
        self.assertEqual((row.document_id, row.user_id, row.project_id),
                         (self.site_doc.pk, self.se1.pk, self.s1.pk))

    def test_se_gets_404_for_a_management_document_and_nothing_is_logged(self):
        response, signer = self._open(self.se1, self.current_doc)
        self.assertEqual(response.status_code, 404)
        signer.assert_not_called()
        self.assertFalse(TenderDocumentOpen.objects.exists())

    def test_se_gets_404_for_superseded_and_voided_documents(self):
        for doc in (self.old_doc, self.void_doc):
            response, signer = self._open(self.se1, doc)
            self.assertEqual(response.status_code, 404, doc.title)
            signer.assert_not_called()
            self.assertFalse(user_can_open_tender_document(self.se1.user, doc, self.s1))
        self.assertFalse(TenderDocumentOpen.objects.exists())

    def test_management_of_the_site_opens_history(self):
        for doc in (self.old_doc, self.void_doc):
            response, _signer = self._open(self.pm1, doc)
            self.assertEqual(response.status_code, 302, doc.title)
        self.assertEqual(TenderDocumentOpen.objects.count(), 2)

    def test_manager_of_site_a_opens_history_from_site_b_in_the_same_tender(self):
        # pm3 manages S3, sees S1 only as its QA/QC engineer: not S1's management, but a
        # manager of the tender (B3-d's second leg).
        response, _signer = self._open(self.pm3, self.old_doc, self.s1)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(TenderDocumentOpen.objects.get().project_id, self.s1.pk)
        # Not management on S1, so a current Management only document is still refused.
        response, _signer = self._open(self.pm3, self.current_doc, self.s1)
        self.assertEqual(response.status_code, 404)

    def test_another_programs_document_through_this_site_is_404(self):
        for profile in (self.pm1, self.admin):
            response, signer = self._open(profile, self.other_doc, self.s1)
            self.assertEqual(response.status_code, 404)
            signer.assert_not_called()
        self.assertFalse(TenderDocumentOpen.objects.exists())

    def test_signer_failure_goes_back_to_the_card_and_logs_nothing(self):
        response, signer = self._open(self.se1, self.site_doc, signed=None)
        signer.assert_called_once()
        self.assertRedirects(
            response,
            reverse('project_overview', args=[self.s1.project_id]) + '#tender-documents-card',
            fetch_redirect_response=False)
        self.assertIn(OPEN_FAILED, [str(m) for m in get_messages(response.wsgi_request)])
        self.assertFalse(TenderDocumentOpen.objects.exists())

    def test_404_on_residential_capex_and_cancelled_sites(self):
        for site in (self.r1, self.c1, self.s_cancel):
            response, signer = self._open(self.pm1, self.current_doc, site)
            self.assertEqual(response.status_code, 404, site.project_id)
            signer.assert_not_called()
        self.assertFalse(TenderDocumentOpen.objects.exists())

    def test_user_who_cannot_see_the_site_gets_refuse_site_view_404(self):
        with patch('projects.views_tender_documents.refuse_site_view_404',
                   wraps=__import__('projects.site_pm', fromlist=['x'])
                   .refuse_site_view_404) as refuse:
            response, signer = self._open(self.pm2, self.current_doc, self.s1)
        self.assertEqual(response.status_code, 404)
        refuse.assert_called_once()
        signer.assert_not_called()
        self.assertFalse(TenderDocumentOpen.objects.exists())

    def test_get_only(self):
        self.client.force_login(self.se1.user)
        response = self.client.post(reverse('tender_documents_open',
                                            args=[self.s1.project_id, self.site_doc.pk]))
        self.assertEqual(response.status_code, 405)


class ManagePageTests(TenderDocsB3Base):
    """B3-f."""

    def test_history_shown_to_a_manager_of_another_site_in_the_tender(self):
        response = self._get(self.pm3, self.s1)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('id="td-history"', content)
        self.assertIn(f'id="td-hist-{self.old_doc.pk}"', content)
        hist = content.split(f'id="td-hist-{self.void_doc.pk}"', 1)[1].split('</li>', 1)[0]
        self.assertIn(reverse('tender_documents_open',
                              args=[self.s1.project_id, self.void_doc.pk]), hist)

    def test_open_counts_after_two_opens(self):
        self._open(self.se1, self.site_doc)
        self._open(self.pm1, self.site_doc)
        content = self._get(self.pm1).content.decode()
        self.assertIn(f'<div class="small text-muted" id="td-opens-{self.site_doc.pk}">'
                      f'Opened 2 times</div>', content)
        self.assertIn(f'<div class="small text-muted" id="td-opens-{self.new_doc.pk}">'
                      f'Opened 0 times</div>', content)


class SilenceTests(TenderDocsB3Base):
    """B3-g: no ActivityLog, no notification."""

    def test_no_activity_log_and_no_notification(self):
        self.client.force_login(self.pm1.user)   # its user_login row is written here
        self.client.force_login(self.se1.user)
        logs_before = ActivityLog.objects.count()
        with patch('projects.notifications.send_notification') as send, \
                patch('projects.notify.notify') as notify, \
                patch('projects.notify.notify_now') as notify_now, \
                patch(SIGNER, return_value=SIGNED):
            self.client.get(reverse('tender_documents_open',
                                    args=[self.s1.project_id, self.site_doc.pk]))
            self.client.get(reverse('project_overview', args=[self.s1.project_id]))
        self.assertEqual(TenderDocumentOpen.objects.count(), 1)
        self.assertEqual(ActivityLog.objects.count(), logs_before)
        send.assert_not_called()
        notify.assert_not_called()
        notify_now.assert_not_called()


class QueryCountTests(TenderDocsB2Base):
    """The P3c fixture exactly (the B2 base), measured the P3c way: one warm-up GET, then
    the second GET counted. The page without the card is measured by stubbing the
    builder to None, so the delta is the card's own cost on the same data."""

    def _count(self, profile, without_card=False):
        self.client.force_login(profile.user)
        url = reverse('project_overview', args=[self.s1.project_id])
        if without_card:
            with patch(CARD_BUILDER, return_value=None):
                self.client.get(url)
                with CaptureQueriesContext(connection) as ctx:
                    response = self.client.get(url)
        else:
            self.client.get(url)
            with CaptureQueriesContext(connection) as ctx:
                response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries), response.context['tender_documents_card']

    def _make_site_team(self, doc):
        TenderDocument.objects.filter(pk=doc.pk).update(
            visibility=TENDER_DOC_SITE_TEAM, no_prices_confirmed_by=self.pm1,
            no_prices_confirmed_at=timezone.now())

    def _overview_counts(self, viewers):
        """{label: (page without the card, page with it)} for each viewer on S1."""
        return {label: (self._count(profile, without_card=True)[0],
                        self._count(profile)[0])
                for label, profile in viewers}

    def _manage_count(self, profile):
        self.client.force_login(profile.user)
        url = reverse('tender_documents_manage', args=[self.s1.project_id])
        self.client.get(url)
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries)

    def test_card_cost_is_constant_and_within_budget(self):
        """Ruling A + go-ahead 2/4: the card adds at most QUERY_BUDGET queries for PM, SE,
        SCM, Finance, CEO and Admin, and the page's count is identical with 3 and 13 live
        sites in the tender and with 1 and 5 documents. The manage page GET does not grow
        with the sites either."""
        # One document every viewer sees: the P3c fixture's document, made Site team.
        self._make_site_team(self.current_doc)
        # The B2 fixture's tender has two live sites (S1, S1B); a third makes 3.
        self._site('BTP1-S3', 'OPEX', self.p1, self.pm2)
        viewers = (('PM', self.pm1), ('SE', self.se1), ('SCM', self.scm),
                   ('Finance', self.finance), ('CEO', self.ceo), ('Admin', self.admin))
        managers = (('PM', self.pm1), ('Admin', self.admin))

        three_sites = self._overview_counts(viewers)
        manage_three = {label: self._manage_count(p) for label, p in managers}
        # Ten more live sites, managed by someone else, so a site scan would show.
        for i in range(10):
            self._site(f'BTP1-X{i:02d}', 'OPEX', self.p1, self.pm2)
        thirteen_sites = self._overview_counts(viewers)
        manage_thirteen = {label: self._manage_count(p) for label, p in managers}
        # Four more documents: five in all.
        for i in range(4):
            self._doc(self.p1, f'Extra {i}', TENDER_DOC_SITE_TEAM)
        five_docs = self._overview_counts(viewers)
        for label, profile in viewers:
            self.assertEqual(len(self._count(profile)[1]['documents']), 5, label)

        print('\nTD-B3 project_overview queries, (without card, with card):')
        for label, _profile in viewers:
            print(f'  {label:8} 3 sites/1 doc {three_sites[label]}  '
                  f'13 sites/1 doc {thirteen_sites[label]}  '
                  f'13 sites/5 docs {five_docs[label]}')
        print(f'TD-B3 manage page GET queries: 3 sites {manage_three}  '
              f'13 sites {manage_thirteen}')

        for label, _profile in viewers:
            base, card = three_sites[label]
            self.assertLessEqual(card - base, QUERY_BUDGET, f'{label}: over budget')
            self.assertEqual(thirteen_sites[label][1], card,
                             f'{label}: grows with site count')
            self.assertEqual(five_docs[label][1], card,
                             f'{label}: grows with document count')
        self.assertEqual(manage_thirteen, manage_three)

    def test_management_only_fixture_counts(self):
        """The P3c fixture untouched (one Management only document): the PM gets a card
        with it, the SE gets no card. Printed for the report's table."""
        pm, pm_card = self._count(self.pm1)
        se, se_card = self._count(self.se1)
        self.assertEqual(len(pm_card['documents']), 1)
        self.assertIsNone(se_card)
        print(f'\nTD-B3 project_overview queries on the P3c fixture: PM={pm} SE={se}')
        self.assertLessEqual(pm - 57, QUERY_BUDGET)
        self.assertLessEqual(se - 55, QUERY_BUDGET)


class ManageOnSiteHelperTests(TenderDocsB3Base):
    """Ruling A: user_can_manage_tender_documents_on_site agrees with
    user_can_manage_tender_documents for every user and site, in at most 3 queries."""

    def _users(self):
        return (self.admin, self.scm, self.finance, self.ceo, self.sysadmin, self.se1,
                self.pm1,     # PM of S1, S1B, S_cancel, S_deleted, C1, R1
                self.coord,   # coordinator of S1
                self.pm3,     # PM of another site (S3) in P1, QA/QC engineer on S1
                self.pm2)     # PM of another tender (S2 in P2, S4 in P3)

    def _sites(self):
        return (self.s1, self.s1b, self.s3, self.s_cancel, self.s_deleted, self.s2,
                self.s4, self.c1, self.r1)

    def test_agrees_with_the_program_form_for_every_user_and_site(self):
        checked = 0
        for profile in self._users():
            for site in self._sites():
                fresh = Project.objects.get(pk=site.pk)
                self.assertEqual(
                    user_can_manage_tender_documents_on_site(profile.user, fresh),
                    user_can_manage_tender_documents(profile.user, fresh.program),
                    f'{profile.user.username} / {site.project_id}')
                checked += 1
        self.assertEqual(checked, 90)

    def test_expected_answers(self):
        helper = user_can_manage_tender_documents_on_site
        self.assertTrue(helper(self.admin.user, self.s1))
        self.assertTrue(helper(self.pm1.user, self.s1))
        self.assertTrue(helper(self.coord.user, self.s1))
        self.assertTrue(helper(self.pm3.user, self.s1))     # manages S3 in the tender
        self.assertTrue(helper(self.pm1.user, self.s_deleted))  # via S1, as the Program form
        for profile in (self.scm, self.finance, self.ceo, self.sysadmin, self.se1,
                        self.pm2):
            self.assertFalse(helper(profile.user, self.s1), profile.user.username)
        for site in (self.c1, self.r1):
            self.assertFalse(helper(self.admin.user, site), site.project_id)
            self.assertFalse(helper(self.pm1.user, site), site.project_id)

    def test_at_most_three_queries_with_assigned_pm_loaded(self):
        for i in range(10):
            self._site(f'BTP1-Q{i:02d}', 'OPEX', self.p1, self.pm2)
        for profile in self._users():
            for site in self._sites():
                fresh = Project.objects.select_related('assigned_pm').get(pk=site.pk)
                with CaptureQueriesContext(connection) as ctx:
                    user_can_manage_tender_documents_on_site(profile.user, fresh)
                self.assertLessEqual(len(ctx.captured_queries), 3,
                                     f'{profile.user.username} / {site.project_id}')
