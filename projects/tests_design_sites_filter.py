"""DA-1 — the Design Head's site list: search, Pending-at chips, and the way back to the row.

WHAT IS PINNED HERE
-------------------
  (a) Every one of the eight action views on head_sites.html — success AND refusal —
      redirects to `?q=&status=#site-<code>`, with the Head's search and chip kept, a
      tampered chip dropped, a long search capped, and the anchor taken from the site the
      view loaded, never from anything posted.
  (b) A message about one site renders inside that site's <tbody>, not at the top; a
      page-level message still renders at the top; a bulk result is both; a refresh shows
      nothing; and a site with a message is flagged so the filter cannot hide it.
  (c) The chips: every PENDING_AT_* label in models.py maps to one (discovered by NAME, so
      a new label fails here), the counts match the rows, and a released site whose SCM
      change request waits on the PM counts under PM.
  (d) base.html's new {% block messages %} renders other pages' messages exactly as the
      loop it wraps always did.
"""
import re
from datetime import timedelta
from decimal import Decimal
from unittest import mock
from urllib.parse import urlencode

from django.contrib import messages as django_messages
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template import Context, Template
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from . import models as models_module
from . import design_views
from .design_views import (
    CHIP_DESIGN_HEAD, CHIP_DESIGNER, CHIP_ON_HOLD, CHIP_PM, CHIP_QC, CHIP_RELEASED,
    CHIP_SURVEY, HEAD_SITES_CHIP_KEYS, HEAD_SITES_Q_MAX, PENDING_AT_CHIP, _head_sites_filter,
)
from .models import (
    DesignAssignment, DesignAttempt, DesignChangeRequest, DueDateCommitment, Program, Project,
    ATTEMPT_REASON_INITIAL, CHANGE_REQUEST_ORIGIN_SCM, CHANGE_REQUEST_WITH_PM,
    DESIGN_AWAITING_ALLOCATION, DESIGN_AWAITING_PM_APPROVAL, DESIGN_IN_DESIGN, DESIGN_IN_QC,
    DESIGN_RELEASED, DESIGN_SURVEY_RETURNED,
    PENDING_AT_CHANGE_REQUEST_WITH_PM,
)


def _profile(username, role='Design', first='', last='', **flags):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=first, last_name=last)
    profile = user.profile
    profile.role = role
    profile.is_active = True
    for field, value in flags.items():
        setattr(profile, field, value)
    profile.save()
    return profile


#: The filter every action below is posted with, and the query string it must come back as.
FILTER = {'q': 'mb', 'status': CHIP_DESIGNER}
#: A tampered post: an unknown chip, an over-long search, and three ways a crafted form
#: might try to choose the anchor or the destination. None of them may reach the Location.
TAMPERED = {'q': 'x' * 80, 'status': '<script>', 'project_id': 'EVIL',
            'site': 'EVIL', 'next': 'https://evil.example/'}


class SitesFilterBase(TestCase):

    def setUp(self):
        self.head = _profile('sf_head', first='Hema', last='Head', is_design_head=True)
        self.designer = _profile('sf_des', first='Dev', last='Designer')
        self.designer2 = _profile('sf_des2', first='Dina', last='Designer')
        self.reviewer = _profile('sf_qc', first='Asha', last='Reviewer', is_design_qc=True)
        self.program = Program.objects.create(
            name='Test-SitesFilter', program_type='OPEX', client_name='SFClient',
            status='Active', short_tender_code='SF')
        self.client = Client()
        self.client.force_login(self.head.user)

    # -- fixtures ---------------------------------------------------------------------

    def _project(self, code, city='Delhi'):
        site = Project(
            project_id=code, customer_name='SFClient', customer_phone='9876543210',
            site_address='1 Sun Rd', city=city, project_type='OPEX', program=self.program,
            site_code=code, dc_capacity_kw=Decimal('100.00'), status='Draft')
        site.save()
        return site

    def _site(self, code, status=DESIGN_AWAITING_ALLOCATION, designer=None, qc=None,
              city='Delhi'):
        site = self._project(code, city)
        assignment = DesignAssignment.objects.create(
            project=site, status=status, assigned_to=designer, qc_assigned_to=qc,
            survey_file_bucket='b', survey_file_path=f'{code}/survey/x.pdf')
        return site, assignment

    def _agreed(self, assignment, days=7, current=True):
        return DueDateCommitment.objects.create(
            assignment=assignment, proposed_date=timezone.localdate() + timedelta(days=days),
            proposed_by=self.head, approved_by=self.head, approved_at=timezone.now(),
            is_current=current)

    def _with_extension(self, code):
        """An in-design site with an agreed date and an extension request pending on it."""
        site, assignment = self._site(code, DESIGN_IN_DESIGN, self.designer)
        self._agreed(assignment, current=False)
        DueDateCommitment.objects.create(
            assignment=assignment, proposed_date=timezone.localdate() + timedelta(days=14),
            proposed_by=self.designer, change_reason='needs longer', is_current=True)
        return site

    # -- helpers ----------------------------------------------------------------------

    def _sites_url(self, q='', status=''):
        return (reverse('design_head_sites', kwargs={'pk': self.program.pk})
                + '?' + urlencode({'q': q, 'status': status}))

    @staticmethod
    def _tbody(html, code):
        """The whole <tbody> for one site, or '' if the page has none."""
        match = re.search(r'<tbody id="site-%s".*?</tbody>' % re.escape(code), html, re.S)
        return match.group(0) if match else ''

    @staticmethod
    def _above_table(html):
        return html.split('<table', 1)[0]


# ===========================================================================
# (a) Every action view comes back to the row, filter kept
# ===========================================================================

class RedirectToRowTests(SitesFilterBase):
    """One case per (view, outcome). Each builder makes a FRESH site, so a success that
    writes cannot leak into the next subtest, and returns (url, data, files, anchor_code)."""

    def _cases(self):
        def upload_ok(code):
            self._site(code)
            return ('design_survey_upload', code,
                    {'survey_file': SimpleUploadedFile('s.pdf', b'%PDF-1.4 x')}, code)

        def upload_refused(code):
            self._site(code)
            return 'design_survey_upload', code, {}, code          # no file chosen

        def link_ok(code):
            self._site(code)
            return ('design_survey_link_set', code,
                    {'survey_folder_url': 'https://drive.google.com/drive/folders/x'}, code)

        def link_refused(code):
            self._site(code)
            return ('design_survey_link_set', code,
                    {'survey_folder_url': 'https://evil.example/folder'}, code)

        def allocate_ok(code):
            self._site(code)
            return 'design_allocate', code, {'designer_id': str(self.designer.pk)}, code

        def allocate_refused(code):
            self._site(code)
            return 'design_allocate', code, {'designer_id': '999999'}, code

        def qc_ok(code):
            self._site(code, DESIGN_IN_DESIGN, self.designer)
            return 'design_assign_qc', code, {'qc_id': str(self.reviewer.pk)}, code

        def qc_refused(code):
            self._site(code, DESIGN_IN_DESIGN, self.designer)
            return 'design_assign_qc', code, {'qc_id': str(self.designer.pk)}, code

        def approve_ok(code):
            self._with_extension(code)
            return 'design_due_date_approve', code, {}, code

        def approve_refused(code):
            self._site(code, DESIGN_IN_DESIGN, self.designer)    # nothing pending
            return 'design_due_date_approve', code, {}, code

        def reject_ok(code):
            self._with_extension(code)
            return 'design_due_date_reject', code, {'reason': 'no'}, code

        def reject_refused(code):
            self._site(code, DESIGN_IN_DESIGN, self.designer)
            return 'design_due_date_reject', code, {}, code

        def change_ok(code):
            _, assignment = self._site(code, DESIGN_IN_DESIGN, self.designer)
            self._agreed(assignment)
            return ('design_due_date_change', code,
                    {'proposed_date': (timezone.localdate() + timedelta(days=10)).isoformat(),
                     'change_reason': 'client asked'}, code)

        def change_refused(code):
            _, assignment = self._site(code, DESIGN_IN_DESIGN, self.designer)
            self._agreed(assignment)
            return ('design_due_date_change', code,
                    {'proposed_date': (timezone.localdate() + timedelta(days=10)).isoformat(),
                     'change_reason': ''}, code)                  # reason is required

        def bulk_ok(code):
            self._site(code)
            self._site(code + 'B')
            return ('design_bulk_allocate', None,
                    {'designer_id': str(self.designer.pk), 'site_ids': [code, code + 'B']},
                    code)                                          # first allocated site

        def bulk_refused(code):
            self._site(code)
            self._site(code + 'Q', DESIGN_IN_QC, self.designer2)
            return ('design_bulk_allocate', None,
                    {'designer_id': str(self.designer.pk), 'site_ids': [code, code + 'Q']},
                    code + 'Q')                                    # the failing site

        return [('upload', upload_ok, upload_refused), ('link', link_ok, link_refused),
                ('allocate', allocate_ok, allocate_refused), ('qc', qc_ok, qc_refused),
                ('approve', approve_ok, approve_refused), ('reject', reject_ok, reject_refused),
                ('change', change_ok, change_refused), ('bulk', bulk_ok, bulk_refused)]

    def _post(self, name, code, data):
        if code is None:
            url = reverse(name, kwargs={'pk': self.program.pk})
        else:
            url = reverse(name, kwargs={'project_id': code})
        return self.client.post(url, data)

    def test_every_view_success_and_refusal_lands_on_the_row_with_the_filter_kept(self):
        upload = mock.patch.object(design_views, 'upload_design_file',
                                   return_value=('b', 'stub/survey/new.pdf'))
        n = 0
        with upload:
            for label, ok, refused in self._cases():
                for outcome, build in (('success', ok), ('refusal', refused)):
                    for tampered in (False, True):
                        n += 1
                        code = f'SF{n:03d}'
                        with self.subTest(view=label, outcome=outcome, tampered=tampered):
                            name, site_code, data, anchor = build(code)
                            posted = dict(data, **(TAMPERED if tampered else FILTER))
                            response = self._post(name, site_code, posted)

                            self.assertEqual(response.status_code, 302)
                            expected = (self._sites_url('x' * HEAD_SITES_Q_MAX, '')
                                        if tampered else
                                        self._sites_url(FILTER['q'], FILTER['status']))
                            self.assertEqual(response['Location'], f'{expected}#site-{anchor}')
                            self.assertNotIn('EVIL', response['Location'])
                            self.assertNotIn('evil.example', response['Location'])

                            queued = list(get_messages(response.wsgi_request))
                            self.assertTrue(queued, 'every outcome says what happened')
                            self.assertIn(f'site:{anchor}', queued[-1].extra_tags)
                            # The outcome really was the one this case claims.
                            self.assertEqual(
                                queued[-1].level_tag,
                                'success' if outcome == 'success' else 'danger',
                                str(queued[-1]))

    def test_bulk_refusals_before_any_site_is_loaded_have_no_anchor(self):
        site, _ = self._site('SFNOSEL')
        url = reverse('design_bulk_allocate', kwargs={'pk': self.program.pk})
        for data in ({'designer_id': str(self.designer.pk)},          # nothing selected
                     {'designer_id': '', 'site_ids': ['SFNOSEL']}):    # no designer
            with self.subTest(data=data):
                response = self.client.post(url, dict(data, **FILTER))
                self.assertEqual(response['Location'],
                                 self._sites_url(FILTER['q'], FILTER['status']))
                message = list(get_messages(response.wsgi_request))[-1]
                self.assertNotIn('site:', message.extra_tags or '')

    def test_a_get_to_an_action_view_also_keeps_the_filter(self):
        self._site('SFGET', DESIGN_IN_DESIGN, self.designer)
        response = self.client.get(
            reverse('design_assign_qc', kwargs={'project_id': 'SFGET'}), FILTER)
        self.assertEqual(response['Location'],
                         self._sites_url(FILTER['q'], FILTER['status']) + '#site-SFGET')


class FilterValidationTests(TestCase):

    def test_status_must_be_a_chip_key(self):
        for key in HEAD_SITES_CHIP_KEYS:
            self.assertEqual(_head_sites_filter({'status': key}), ('', key))
        for bad in ('all', 'DESIGNER', 'designer ', '<b>', 'released;'):
            with self.subTest(bad=bad):
                self.assertEqual(_head_sites_filter({'status': bad})[1],
                                 'designer' if bad == 'designer ' else '')

    def test_q_is_trimmed_and_capped(self):
        self.assertEqual(_head_sites_filter({'q': '  MB0001  '})[0], 'MB0001')
        self.assertEqual(len(_head_sites_filter({'q': 'y' * 500})[0]), HEAD_SITES_Q_MAX)

    def test_the_screen_seeds_alpine_from_the_validated_values(self):
        head = _profile('sfv_head', is_design_head=True)
        program = Program.objects.create(name='T-SFV', program_type='OPEX', client_name='C',
                                         status='Active', short_tender_code='SFV')
        client = Client()
        client.force_login(head.user)
        response = client.get(reverse('design_head_sites', kwargs={'pk': program.pk}),
                              {'q': 'Mb', 'status': 'bogus'})
        self.assertEqual(response.context['filter_state'], {'q': 'Mb', 'status': ''})
        self.assertContains(response, 'id="headSitesFilter"')


# ===========================================================================
# (b) Where messages render
# ===========================================================================

class RowMessageTests(SitesFilterBase):

    def _save_link(self, code, **filter_):
        return self.client.post(
            reverse('design_survey_link_set', kwargs={'project_id': code}),
            dict({'survey_folder_url': 'https://drive.google.com/drive/folders/x'}, **filter_))

    def test_a_site_message_renders_inside_that_sites_tbody_not_at_the_top(self):
        self._site('SFM1')
        self._site('SFM2')
        response = self._save_link('SFM2', **FILTER)
        html = self.client.get(response['Location']).content.decode()

        text = 'SFM2: survey folder link added — ready to allocate.'
        self.assertIn(text, self._tbody(html, 'SFM2'))
        self.assertNotIn(text, self._tbody(html, 'SFM1'))
        self.assertNotIn(text, self._above_table(html))
        self.assertIn('alert alert-success', self._tbody(html, 'SFM2'))
        self.assertIn('data-pinned="1"', self._tbody(html, 'SFM2'))
        self.assertIn('data-pinned="0"', self._tbody(html, 'SFM1'))

    def test_a_pinned_site_stays_flagged_when_its_chip_is_not_the_one_selected(self):
        """SFP is awaiting allocation (Design Head chip); the Head is on the QC chip."""
        self._site('SFP')
        response = self._save_link('SFP', q='', status=CHIP_QC)
        self.assertTrue(response['Location'].endswith('status=qc#site-SFP'))
        page = self.client.get(response['Location'])
        tbody = self._tbody(page.content.decode(), 'SFP')
        self.assertIn(f'data-chip="{CHIP_DESIGN_HEAD}"', tbody)
        self.assertIn('data-pinned="1"', tbody)
        self.assertEqual(page.context['status_filter'], CHIP_QC)

    def test_a_refresh_after_the_redirect_shows_no_message(self):
        self._site('SFR')
        response = self._save_link('SFR', **FILTER)
        text = 'SFR: survey folder link added'
        self.assertContains(self.client.get(response['Location']), text)
        again = self.client.get(response['Location'])
        self.assertNotContains(again, text)
        self.assertIn('data-pinned="0"', self._tbody(again.content.decode(), 'SFR'))

    def test_a_page_level_message_still_renders_at_the_top(self):
        self._site('SFT')
        response = self.client.post(
            reverse('design_bulk_allocate', kwargs={'pk': self.program.pk}),
            dict({'designer_id': str(self.designer.pk)}, **FILTER))
        html = self.client.get(response['Location']).content.decode()
        text = 'Select at least one site to allocate.'
        self.assertIn(text, self._above_table(html))
        self.assertNotIn(text, self._tbody(html, 'SFT'))

    def test_a_bulk_result_is_at_the_top_and_on_the_first_site(self):
        self._site('SFB1')
        self._site('SFB2')
        response = self.client.post(
            reverse('design_bulk_allocate', kwargs={'pk': self.program.pk}),
            dict({'designer_id': str(self.designer.pk), 'site_ids': ['SFB1', 'SFB2']},
                 **FILTER))
        html = self.client.get(response['Location']).content.decode()
        text = '2 site(s) allocated to Dev Designer'
        self.assertIn(text, self._above_table(html))
        self.assertIn(text, self._tbody(html, 'SFB1'))
        self.assertNotIn(text, self._tbody(html, 'SFB2'))
        # The page-level copy keeps base.html's markup: level_tag, not the raw tag string.
        self.assertNotIn('alert-site:', html)

    def test_a_message_for_a_site_not_on_this_screen_falls_back_to_the_top(self):
        site, _ = self._site('SFGONE')
        response = self._save_link('SFGONE', **FILTER)
        Project.objects.filter(pk=site.pk).update(is_deleted=True)
        html = self.client.get(response['Location']).content.decode()
        self.assertIn('SFGONE: survey folder link added', self._above_table(html))


class OtherPagesMessageMarkupTests(SitesFilterBase):
    """base.html's message loop now sits in {% block messages %}. Pages that do not
    override the block must render it exactly as the unwrapped loop did — the loop below
    is base.html's markup as it stood before DA-1, rendered with the same messages."""

    ORIGINAL_LOOP = (
        "  {% if messages %}\n"
        "    {% for message in messages %}\n"
        "      <div class=\"alert alert-{{ message.tags|default:'info' }} alert-dismissible "
        "fade show\" role=\"alert\">\n"
        "        {{ message }}\n"
        "        <button type=\"button\" class=\"btn-close\" data-bs-dismiss=\"alert\"></button>\n"
        "      </div>\n"
        "    {% endfor %}\n"
        "  {% endif %}\n")

    def _render(self, view, user, **kwargs):
        request = RequestFactory().get('/')
        request.user = user
        request.session = SessionStore()
        request._messages = FallbackStorage(request)
        django_messages.success(request, 'Saved — page level.')
        django_messages.error(request, 'Something failed.')
        html = view(request, **kwargs).content.decode()
        # Iterating the storage again yields the same two messages: the render moved them
        # from "queued" to "loaded", it did not discard them within this request.
        shown = list(request._messages)
        self.assertEqual(len(shown), 2)
        expected = Template(self.ORIGINAL_LOOP).render(Context({'messages': shown}))
        return html, expected

    def test_design_my_sites_renders_messages_unchanged(self):
        html, expected = self._render(design_views.design_my_sites, self.designer.user)
        self.assertIn('<div class="container mt-4">\n\n' + expected + '\n', html)

    def test_the_tender_dashboard_renders_messages_unchanged(self):
        html, expected = self._render(design_views.design_tender_dashboard, self.head.user,
                                      pk=self.program.pk)
        self.assertIn('<div class="container mt-4">\n\n' + expected + '\n', html)


# ===========================================================================
# (c) The chips
# ===========================================================================

class ChipMappingTests(TestCase):

    def test_every_pending_at_constant_in_models_maps_to_a_chip(self):
        """DISCOVERED BY NAME. A hand-copied list would agree with a label somebody adds to
        models.py and forgets to map; reading the module means the new constant arrives
        in this loop the moment it is declared."""
        labels = {name: value for name, value in vars(models_module).items()
                  if name.startswith('PENDING_AT_') and isinstance(value, str)}
        self.assertGreaterEqual(len(labels), 10, 'discovery found too few constants')
        unmapped = sorted(name for name, value in labels.items()
                          if value not in PENDING_AT_CHIP)
        self.assertEqual(unmapped, [],
                         'These PENDING_AT_* labels have no chip in '
                         'design_views.PENDING_AT_CHIP: %s' % unmapped)

    def test_the_mapping_names_only_real_chips_and_every_chip_is_reachable(self):
        self.assertEqual(set(PENDING_AT_CHIP.values()), set(HEAD_SITES_CHIP_KEYS))


class ChipCountTests(SitesFilterBase):

    def test_counts_match_the_rows_pending_at_on_a_fixture_tender(self):
        scm = _profile('sf_scm', role='SCM')
        self._project('SFC-SURVEY')                                     # no assignment
        self._site('SFC-ALLOC')                                         # Design Head
        self._site('SFC-DESIGN', DESIGN_IN_DESIGN, self.designer)       # Designer
        self._site('SFC-QC', DESIGN_IN_QC, self.designer, self.reviewer)  # QC
        self._site('SFC-POOL', DESIGN_IN_QC, self.designer)             # QC (pool) -> QC
        self._site('SFC-PM', DESIGN_AWAITING_PM_APPROVAL, self.designer)  # PM
        self._site('SFC-REL', DESIGN_RELEASED, self.designer)           # With SCM -> Released
        self._site('SFC-HOLD', DESIGN_SURVEY_RETURNED, self.designer)   # On hold
        # Released, with an SCM change request still waiting on the PM -> PM, not Released.
        _, released = self._site('SFC-REL-CR', DESIGN_RELEASED, self.designer)
        attempt = DesignAttempt.objects.create(assignment=released, attempt_number=1,
                                               opened_reason=ATTEMPT_REASON_INITIAL)
        DesignAssignment.objects.filter(pk=released.pk).update(current_attempt_number=1)
        DesignChangeRequest.objects.create(
            attempt=attempt, requested_by=scm, reason='qty',
            verdict=CHANGE_REQUEST_WITH_PM, origin=CHANGE_REQUEST_ORIGIN_SCM)

        response = self.client.get(reverse('design_head_sites', kwargs={'pk': self.program.pk}))
        rows = {row['site'].project_id: row for row in response.context['rows']}
        counts = {chip['key']: chip['count'] for chip in response.context['chips']}

        self.assertEqual(rows['SFC-REL-CR']['pending_at'], PENDING_AT_CHANGE_REQUEST_WITH_PM)
        self.assertEqual(rows['SFC-REL-CR']['chip'], CHIP_PM)
        self.assertEqual(rows['SFC-POOL']['chip'], CHIP_QC)
        self.assertEqual(rows['SFC-REL']['chip'], CHIP_RELEASED)

        # The counts are the rows' own pending_at, grouped — not a second derivation.
        expected = dict.fromkeys(HEAD_SITES_CHIP_KEYS, 0)
        for row in rows.values():
            expected[PENDING_AT_CHIP[row['pending_at']]] += 1
        self.assertEqual(counts, expected)
        self.assertEqual(counts, {CHIP_SURVEY: 1, CHIP_DESIGN_HEAD: 1, CHIP_DESIGNER: 1,
                                  CHIP_QC: 2, CHIP_PM: 2, CHIP_RELEASED: 1, CHIP_ON_HOLD: 1})

        html = response.content.decode()
        self.assertIn(f'data-chip="{CHIP_PM}"', self._tbody(html, 'SFC-REL-CR'))
        # The row badge keeps the exact pending_at text; only the chip groups it.
        self.assertIn('PM (change request)', self._tbody(html, 'SFC-REL-CR'))
        self.assertIn('data-search="sfc-pm delhi"', self._tbody(html, 'SFC-PM'))

    def test_the_eight_forms_each_carry_the_filter(self):
        site, assignment = self._site('SFF', DESIGN_IN_DESIGN, self.designer)
        self._agreed(assignment, current=False)
        DueDateCommitment.objects.create(
            assignment=assignment, proposed_date=timezone.localdate() + timedelta(days=14),
            proposed_by=self.designer, change_reason='needs longer', is_current=True)
        html = self.client.get(self._sites_url('mb', CHIP_DESIGNER)).content.decode()
        forms = re.findall(r'<form method="post".*?</form>', html, re.S)
        action_forms = [f for f in forms if 'blockTaskModalForm' not in f]
        # 7, not 8, since DA-2: on an allocatable row Save QC is part of the combined form.
        self.assertEqual(len(action_forms), 7)
        for form in action_forms:
            self.assertIn('<input type="hidden" name="q" value="mb"', form)
            self.assertIn(f'<input type="hidden" name="status" value="{CHIP_DESIGNER}"', form)

        # A non-allocatable row (in_qc) still renders the separate Save QC form.
        self._site('SFF-QC', DESIGN_IN_QC, self.designer)
        html = self.client.get(self._sites_url('mb', CHIP_DESIGNER)).content.decode()
        self.assertIn(reverse('design_assign_qc', kwargs={'project_id': 'SFF-QC'}),
                      self._tbody(html, 'SFF-QC'))
