"""
DW-8 — design screens lead back to each person's own queue.

WHY THIS FILE EXISTS
--------------------
Navigation was the design team's top day-to-day complaint (DW-A, T9): many design screens
offered only "← Project", QC and the Head had no way back to their own queue, several links
pointed at screens that refuse the person reading them (Arka review's only exit was a 403
for QC), and some refusals landed on an unrelated page. Zuber's rulings (26 Nov 2026):

  * N1 / answers 1, 4 — one "← My queue" include on every design screen, from one role rule
    (design_nav.DesignNav.queue): designer -> my sites; this site's QC -> QC queue; Head ->
    the tender's Head dashboard. It replaces a screen's own back-to-queue button for whoever
    gets one; a PM or SCM user gets none and keeps today's links.
  * N2 / N3 / answer 3 — reviewer POSTs return to the screen they came from (the Arka
    revision through `next` and SEC-2's safe_next_url); the six package-review refusals that
    landed on the QC queue land on package review; Arka verdicts stay on Arka review.
  * N4 / N6 / answer 5 — the BOQ view gives this site's reviewers "Open QC review" and
    "Download BOQ (Excel)" and shows the design stage instead of "Draft" on OPEX; every link
    on a design screen opens for the person it is shown to.
  * answers 6, 7 — no "BOQ: Draft" chip on OPEX dashboard cards; "← Design dashboard" on the
    QC queue, tender dashboard, QC dashboard and catalogue.

THE SCREEN TABLE BELOW IS THE P2 LIST. A screen that stops including _back_to_queue.html
fails ScreenIncludeTests; a link that 403s, 404s or 500s for the person it is shown to
fails EveryLinkOpensTests.
"""
import re
from decimal import Decimal
from urllib.parse import urljoin, urlsplit
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import resolve, reverse
from django.urls.exceptions import Resolver404

from .design_nav import (
    DESIGN_DASHBOARD_ROLES, PROGRAM_DETAIL_ROLES, QUEUE_DESIGNER, QUEUE_HEAD, QUEUE_QC,
    DesignNav, design_workspace_access,
)
from .models import (
    ArkaSubmission, BOQ, BOQItem, BOQItemMaster, DesignAssignment, DesignBOQGroup,
    DesignBOQGroupMembership, Program, Project, ProjectPhase, Task,
    ARKA_APPROVED, ARKA_PENDING,
    DESIGN_ARKA_SUBMITTED, DESIGN_ARTIFACTS_UPLOADED, DESIGN_AWAITING_HEAD_QC,
    DESIGN_AWAITING_PM_APPROVAL, DESIGN_IN_QC, DESIGN_RELEASED,
)
from .tests_boq_reviewer_override import ReviewerOverrideBase
from .tests_design_reviewer_cad_revisions import CadRevisionBase
from .tests_design_reviewer_revisions import _profile

#: The signed-URL views: their 302 to object storage is the "opens" answer (mocked here).
SIGNED_REDIRECT_VIEWS = {'design_file_download', 'design_survey_download'}
STORAGE_URL = 'https://storage.example.test/object'
SIGN = 'projects.design_views.get_design_file_url'

# Every <a href> on the page body (not the sidebar), and the include's anchor.
HREF = re.compile(r'<a\b[^>]*\bhref="([^"]*)"', re.I)
QUEUE_LINK = re.compile(
    r'<a href="([^"]+)" class="[^"]*js-back-to-queue[^"]*" data-queue="([a-z]+)">', re.I)
DASHBOARD_LINK = re.compile(
    r'<a href="([^"]+)"[^>]*js-back-to-design-dashboard', re.I)


def _body(response):
    """The page content between base.html's message area and its scripts: what the screen
    renders, without the sidebar every page shares."""
    html = response.content.decode()
    start = html.find('<div id="hxMessages"></div>')
    end = html.find('bootstrap.bundle.min.js', start)
    return html[start:end] if start != -1 and end != -1 else html


class DW8Base(CadRevisionBase):
    """RevisionBase's package (attempt 1 at `in_qc`, Arka v1 approved, CAD v1, BOQ stamped),
    plus the people DW-8 sorts: the site's own designer (now also its `assigned_design`, so
    it is their BOQ), a reviewer NAMED on the site (no QC flag), the open-pool QC flag
    holder (qc5, an outsider here once somebody is named), the Head, SCM and the PM. A
    design group holding the site, and one OPEX catalogue item for the catalogue screens."""

    def setUp(self):
        super().setUp()
        self.named_qc = _profile('qcn8', 'Design')
        self.scm = _profile('scm8', 'SCM')
        Project.objects.filter(pk=self.site.pk).update(assigned_design=self.designer)
        DesignAssignment.objects.filter(pk=self.a.pk).update(qc_assigned_to=self.named_qc)
        self.a.refresh_from_db()
        self.group = DesignBOQGroup.objects.create(name='DW8 group', created_by=self.head)
        DesignBOQGroupMembership.objects.create(group=self.group, project=self.site,
                                                added_by=self.head)
        self.item = BOQItemMaster.objects.create(
            code='OPX-001', description='Solar module', unit='Nos', category='Modules',
            project_type='OPEX', sort_order=1)

    def _get(self, profile, url, **kwargs):
        self._login(profile)
        return self.client.get(url, **kwargs)

    def _url(self, name, **kwargs):
        if name in ('design_qc_queue', 'design_boq_group_list', 'design_boq_catalogue',
                    'dashboard_design', 'design_my_sites'):
            return reverse(name)
        if name in ('design_tender_dashboard', 'design_qc_dashboard', 'design_head_sites'):
            return reverse(name, kwargs={'pk': self.program.pk})
        if name == 'design_boq_catalogue_edit':
            return reverse(name, kwargs={'item_id': self.item.pk})
        if name == 'design_boq_group_detail':
            return reverse(name, kwargs={'pk': self.group.pk})
        if name == 'design_boq_group_snapshot_detail':
            return reverse(name, kwargs={'pk': self.group.pk,
                                         'snapshot_pk': self._snapshot().pk})
        return reverse(name, kwargs={'project_id': self.site.project_id})

    def _snapshot(self):
        snap = self.group.snapshots.first()
        if snap is None:
            self._login(self.head)
            self.client.post(reverse('design_boq_group_snapshot_take', args=[self.group.pk]))
            snap = self.group.snapshots.get()
        return snap

    def _messages(self, response):
        return ' '.join(str(m) for m in get_messages(response.wsgi_request))

    def _tender_dashboard(self):
        return reverse('design_tender_dashboard', kwargs={'pk': self.program.pk})


# ===========================================================================
# T4.1 — the role rule (N1), every case from P3
# ===========================================================================

class RoleRuleTests(DW8Base):

    def _queue(self, profile, project='site', **kwargs):
        project = self.site if project == 'site' else project
        user = User.objects.get(pk=profile.user.pk)       # a fresh profile cache per case
        return DesignNav(user, Project.objects.get(pk=project.pk) if project else None,
                         **kwargs).queue()

    def test_the_sites_designer_goes_to_my_sites(self):
        self.assertEqual(self._queue(self.designer),
                         {'kind': QUEUE_DESIGNER, 'url': reverse('design_my_sites')})

    def test_a_designer_with_no_part_in_the_site_goes_to_my_sites(self):
        """Holds a task here, nothing else: a Design user, so their own queue."""
        phase = ProjectPhase.objects.create(project=self.site, phase_name='Design',
                                            phase_order=1)
        Task.objects.create(phase=phase, task_name='Check survey', task_order=1,
                            assigned_role=Task.DESIGN, assigned_to=self.other_designer)
        self.assertEqual(self._queue(self.other_designer)['kind'], QUEUE_DESIGNER)

    def test_the_named_qc_reviewer_goes_to_the_qc_queue(self):
        self.assertEqual(self._queue(self.named_qc),
                         {'kind': QUEUE_QC, 'url': reverse('design_qc_queue')})

    def test_a_pool_qc_not_named_here_goes_to_the_qc_queue(self):
        """qc5 holds the flag; the site is named to someone else."""
        self.assertEqual(self._queue(self.qc)['kind'], QUEUE_QC)

    def test_a_pool_qc_on_an_open_site_goes_to_the_qc_queue(self):
        DesignAssignment.objects.filter(pk=self.a.pk).update(qc_assigned_to=None)
        self.assertEqual(self._queue(self.qc)['kind'], QUEUE_QC)

    def test_the_head_goes_to_the_sites_tender_dashboard(self):
        self.assertEqual(self._queue(self.head),
                         {'kind': QUEUE_HEAD, 'url': self._tender_dashboard()})

    def test_qc_and_head_authority_named_here_goes_to_the_qc_queue(self):
        """The most specific role for THIS site wins: named as its reviewer."""
        both = _profile('both8', 'Design', is_design_qc=True, is_design_head=True)
        DesignAssignment.objects.filter(pk=self.a.pk).update(qc_assigned_to=both)
        self.assertEqual(self._queue(both)['kind'], QUEUE_QC)

    def test_qc_and_head_authority_not_named_here_goes_to_the_head_dashboard(self):
        both = _profile('both8', 'Design', is_design_qc=True, is_design_head=True)
        self.assertEqual(self._queue(both),
                         {'kind': QUEUE_HEAD, 'url': self._tender_dashboard()})

    def test_a_head_who_designs_this_site_goes_to_my_sites(self):
        DesignAssignment.objects.filter(pk=self.a.pk).update(assigned_to=self.head)
        self.assertEqual(self._queue(self.head)['kind'], QUEUE_DESIGNER)

    def test_scm_and_the_pm_get_no_queue(self):
        for profile in (self.scm, self.pm):
            with self.subTest(user=profile.user.username):
                self.assertIsNone(self._queue(profile))

    def test_the_deputy_gets_head_authority(self):
        deputy = _profile('dep8', 'Design')
        self.head.design_head_deputy = deputy
        self.head.save()
        self.assertEqual(self._queue(deputy)['kind'], QUEUE_HEAD)

    def test_off_a_site_the_head_goes_to_the_design_dashboard(self):
        self.assertEqual(self._queue(self.head, project=None),
                         {'kind': QUEUE_HEAD, 'url': reverse('dashboard_design')})

    def test_off_a_site_a_named_reviewer_without_the_flag_is_a_designer(self):
        self.assertEqual(self._queue(self.named_qc, project=None)['kind'], QUEUE_DESIGNER)

    def test_a_site_with_no_live_opex_program_sends_the_head_to_the_design_dashboard(self):
        Program.objects.filter(pk=self.program.pk).update(is_deleted=True)
        self.assertEqual(self._queue(self.head)['url'], reverse('dashboard_design'))

    def test_a_tender_screen_sends_the_head_to_that_tenders_dashboard(self):
        user = User.objects.get(pk=self.head.user.pk)
        self.assertEqual(DesignNav(user, program=self.program, head_authority=True).queue(),
                         {'kind': QUEUE_HEAD, 'url': self._tender_dashboard()})

    def test_the_role_lists_are_the_decorators_values(self):
        """The two lists moved to design_nav keep their values (no gate changed)."""
        self.assertEqual(DESIGN_DASHBOARD_ROLES, ['Design'])
        self.assertEqual(PROGRAM_DETAIL_ROLES, ['Admin', 'PM', 'CEO'])

    def test_the_workspace_gate_is_unchanged(self):
        """design_workspace_access is design_site_workspace's gate, case for case."""
        site = Project.objects.get(pk=self.site.pk)
        cases = [(self.designer, DESIGN_IN_QC, True), (self.head, DESIGN_IN_QC, True),
                 (self.named_qc, DESIGN_IN_QC, False), (self.qc, DESIGN_IN_QC, False),
                 (self.pm, DESIGN_IN_QC, False), (self.pm, DESIGN_AWAITING_PM_APPROVAL, True),
                 (self.scm, DESIGN_AWAITING_PM_APPROVAL, False)]
        for profile, status, allowed in cases:
            with self.subTest(user=profile.user.username, status=status):
                self._set_status(status)
                user = User.objects.get(pk=profile.user.pk)
                self.assertEqual(design_workspace_access(user, site, self.a)[0], allowed)
                code = self._get(profile, self._url('design_site_workspace')).status_code
                self.assertEqual(code, 200 if allowed else 403)


# ===========================================================================
# T4.2 — every screen carries the include, with the right target (table-driven)
# ===========================================================================

#: (url name, {role: expected queue kind}). Roles a screen refuses are absent. The BOQ view
#: has no designer row: it sends the site's designer to the picker (Part 11).
INCLUDE_SCREENS = [
    ('design_site_workspace', {'designer': QUEUE_DESIGNER, 'head': QUEUE_HEAD}),
    ('opex_boq_entry', {'designer': QUEUE_DESIGNER, 'qc': QUEUE_QC, 'head': QUEUE_HEAD}),
    ('boq_detail', {'qc': QUEUE_QC, 'head': QUEUE_HEAD}),
    ('design_head_review', {'qc': QUEUE_QC, 'head': QUEUE_HEAD}),
    ('design_qc_review', {'qc': QUEUE_QC, 'head': QUEUE_HEAD}),
    ('boq_correct', {'qc': QUEUE_QC, 'head': QUEUE_HEAD}),
    ('design_boq_group_list', {'designer': QUEUE_DESIGNER, 'qc': QUEUE_DESIGNER,
                               'pool': QUEUE_QC, 'head': QUEUE_HEAD}),
    ('design_boq_group_detail', {'designer': QUEUE_DESIGNER, 'qc': QUEUE_DESIGNER,
                                 'head': QUEUE_HEAD}),
    ('design_boq_group_snapshot_detail', {'designer': QUEUE_DESIGNER, 'head': QUEUE_HEAD}),
    ('design_head_sites', {'head': QUEUE_HEAD}),
]

#: The queue screens and the catalogue: "← Design dashboard" (answer 7).
DASHBOARD_LINK_SCREENS = [
    ('design_qc_queue', ('qc', 'pool', 'head')),
    ('design_tender_dashboard', ('head',)),
    ('design_qc_dashboard', ('pool', 'head')),
    ('design_boq_catalogue', ('head',)),
    ('design_boq_catalogue_edit', ('head',)),
]


class ScreenIncludeTests(DW8Base):

    def _who(self):
        return {'designer': self.designer, 'qc': self.named_qc, 'pool': self.qc,
                'head': self.head, 'scm': self.scm, 'pm': self.pm}

    def _expected_url(self, name, kind):
        if kind == QUEUE_DESIGNER:
            return reverse('design_my_sites')
        if kind == QUEUE_QC:
            return reverse('design_qc_queue')
        on_a_site = name not in ('design_boq_group_list', 'design_boq_group_detail',
                                 'design_boq_group_snapshot_detail')
        return self._tender_dashboard() if on_a_site else reverse('dashboard_design')

    def test_every_design_screen_renders_my_queue_with_the_right_target(self):
        who = self._who()
        for name, expected in INCLUDE_SCREENS:
            for role, kind in expected.items():
                with self.subTest(screen=name, role=role):
                    response = self._get(who[role], self._url(name))
                    self.assertEqual(response.status_code, 200)
                    links = QUEUE_LINK.findall(_body(response))
                    self.assertEqual(links, [(self._expected_url(name, kind), kind)])

    def test_scm_and_the_pm_get_no_my_queue_and_keep_their_links(self):
        for name in ('boq_detail', 'opex_boq_entry'):
            for profile in (self.scm, self.pm):
                with self.subTest(screen=name, user=profile.user.username):
                    body = _body(self._get(profile, self._url(name)))
                    self.assertEqual(QUEUE_LINK.findall(body), [])
        # The PM reviewing the package keeps "← Design & BOQ approvals" on the workspace.
        self._set_status(DESIGN_AWAITING_PM_APPROVAL)
        body = _body(self._get(self.pm, self._url('design_site_workspace')))
        self.assertEqual(QUEUE_LINK.findall(body), [])
        self.assertIn(reverse('design_pm_approval_queue'), body)

    def test_my_queue_replaces_the_screens_own_back_button(self):
        """Answer 4: no second "← QC queue" / "← My sites" / "Tender sites" / "Dashboard"."""
        body = _body(self._get(self.named_qc, self._url('design_qc_review')))
        self.assertNotIn('</i>QC queue', body)
        body = _body(self._get(self.designer, self._url('design_site_workspace')))
        self.assertNotIn('</i>My sites', body)
        body = _body(self._get(self.named_qc, self._url('design_head_review')))
        self.assertNotIn('Tender sites', body)
        body = _body(self._get(self.head, self._url('design_head_sites')))
        self.assertNotIn('</i>Dashboard', body)

    def test_in_flow_links_stay(self):
        body = _body(self._get(self.head, self._url('design_boq_group_detail')))
        self.assertIn('← All groups', body)
        body = _body(self._get(self.head, self._url('design_boq_group_snapshot_detail')))
        self.assertIn('← Back to the group', body)
        body = _body(self._get(self.named_qc, self._url('boq_correct')))
        self.assertIn('View BOQ', body)

    def test_queue_screens_and_the_catalogue_lead_to_the_design_dashboard(self):
        who = self._who()
        for name, roles in DASHBOARD_LINK_SCREENS:
            for role in roles:
                with self.subTest(screen=name, role=role):
                    response = self._get(who[role], self._url(name))
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(DASHBOARD_LINK.findall(_body(response)),
                                     [reverse('dashboard_design')])

    def test_the_design_dashboard_link_hides_from_a_deputy_outside_the_design_role(self):
        deputy = _profile('dep8', 'PM')
        self.head.design_head_deputy = deputy
        self.head.save()
        response = self._get(deputy, self._url('design_qc_queue'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DASHBOARD_LINK.findall(_body(response)), [])

    def test_the_groups_page_shows_the_design_dashboard_once(self):
        body = _body(self._get(self.head, self._url('design_boq_group_list')))
        self.assertEqual(body.count('href="%s"' % reverse('dashboard_design')), 1)
        body = _body(self._get(self.designer, self._url('design_boq_group_list')))
        self.assertIn('← Design dashboard', body)


# ===========================================================================
# T4.3 — every link a design screen renders opens for the person it is shown to (answer 5)
# ===========================================================================

#: Every P2 screen, and the roles that may open it, at the fixture's state (`in_qc`).
LINK_SCREENS = [
    ('dashboard_design', ('designer', 'qc', 'pool', 'head')),
    ('design_my_sites', ('designer', 'qc', 'pool', 'head', 'scm', 'pm')),
    ('design_site_workspace', ('designer', 'head')),
    ('opex_boq_entry', ('designer', 'qc', 'pool', 'head', 'scm', 'pm')),
    ('boq_detail', ('qc', 'pool', 'head', 'scm', 'pm')),
    ('design_head_review', ('qc', 'head')),
    ('design_qc_review', ('qc', 'pool', 'head')),
    ('boq_correct', ('qc', 'head')),
    ('design_boq_group_list', ('designer', 'qc', 'pool', 'head')),
    ('design_boq_group_detail', ('designer', 'qc', 'pool', 'head')),
    ('design_boq_group_snapshot_detail', ('designer', 'qc', 'pool', 'head')),
    ('design_boq_catalogue', ('head',)),
    ('design_boq_catalogue_edit', ('head',)),
    ('design_qc_queue', ('qc', 'pool', 'head')),
    ('design_tender_dashboard', ('head',)),
    ('design_head_sites', ('head',)),
    ('design_qc_dashboard', ('pool', 'head')),
]


class EveryLinkOpensTests(DW8Base):

    def _opens(self, page_url, href):
        """(ok, how) for one rendered href, as the logged-in user: 200 after following this
        site's own redirects, or a signed-URL view's redirect to object storage."""
        url = urljoin(page_url, href)
        parts = urlsplit(url)
        if parts.scheme or parts.netloc:
            return True, 'external'
        path = parts.path + (f'?{parts.query}' if parts.query else '')
        for _hop in range(4):
            try:
                view = resolve(urlsplit(path).path).url_name
            except Resolver404:
                return False, f'{path}: no such URL'
            response = self.client.get(path)
            if response.status_code == 200:
                return True, view
            if response.status_code == 302:
                location = response['Location']
                if view in SIGNED_REDIRECT_VIEWS and location == STORAGE_URL:
                    return True, view
                if urlsplit(location).netloc:
                    return False, f'{path}: off-site redirect to {location}'
                path = location
                continue
            return False, f'{path} ({view}): {response.status_code}'
        return False, f'{path}: redirect loop'

    def test_every_rendered_link_opens_for_that_user(self):
        who = {'designer': self.designer, 'qc': self.named_qc, 'pool': self.qc,
               'head': self.head, 'scm': self.scm, 'pm': self.pm}
        self._snapshot()
        checked = 0
        failures = []
        with mock.patch(SIGN, return_value=STORAGE_URL):
            for name, roles in LINK_SCREENS:
                page_url = self._url(name)
                for role in roles:
                    response = self._get(who[role], page_url)
                    if response.status_code != 200:
                        failures.append(f'{name} as {role}: the screen itself is '
                                        f'{response.status_code}')
                        continue
                    hrefs = {h for h in HREF.findall(_body(response))
                             if h and not h.startswith(('#', 'javascript:', 'mailto:'))}
                    for href in sorted(hrefs):
                        ok, how = self._opens(page_url, href.replace('&amp;', '&'))
                        checked += 1
                        if not ok:
                            failures.append(f'{name} as {role}: {how}')
        self.assertGreater(checked, 100)
        self.assertEqual(failures, [])

    def test_the_links_that_were_dead_are_hidden(self):
        """The P2 dead ends, one by one, for the person they failed."""
        workspace = self._url('design_site_workspace')
        for name in ('design_head_review', 'design_qc_review', 'opex_boq_entry'):
            with self.subTest(screen=name):
                body = _body(self._get(self.named_qc, self._url(name)))
                self.assertNotIn(f'href="{workspace}"', body)
        body = _body(self._get(self.named_qc, self._url('boq_detail')))
        self.assertNotIn(reverse('project_overview', args=[self.site.project_id]), body)
        body = _body(self._get(self.head, self._url('design_head_sites')))
        self.assertNotIn('href="%s"' % reverse('program_detail', kwargs={'pk': self.program.pk}),
                         body)
        body = _body(self._get(self.pm, self._url('design_my_sites')))
        self.assertNotIn('href="%s"' % reverse('dashboard_design'), body)
        # ... and stay for the people they open for.
        body = _body(self._get(self.head, self._url('design_qc_review')))
        self.assertIn(f'href="{workspace}"', body)
        body = _body(self._get(self.pm, self._url('boq_detail')))
        self.assertIn(reverse('project_overview', args=[self.site.project_id]), body)


# ===========================================================================
# T4.4 — where each POST lands (N2, N3)
# ===========================================================================

class ArkaReviseLandingTests(DW8Base):
    """The one form on two screens. `next` decides; a missing or unsafe one falls back to
    DW-2a's rule (Arka review at `arka_submitted`, package review otherwise)."""

    def setUp(self):
        super().setUp()
        self._set_status(DESIGN_ARKA_SUBMITTED)
        ArkaSubmission.objects.filter(pk=self.arka.pk).update(verdict=ARKA_PENDING)

    def _revise_from(self, next_value, reason='Two rows clash with the tank', who=None):
        self._login(who or self.named_qc)
        data = {'capacity_kw': '118.00', 'arka_link': self.arka.arka_link,
                'revision_reason': reason}
        if next_value is not None:
            data['next'] = next_value
        return self.client.post(self._url('design_arka_revise'), data)

    def test_the_form_sends_the_page_it_is_on(self):
        for name in ('design_head_review', 'design_qc_review'):
            with self.subTest(screen=name):
                body = _body(self._get(self.named_qc, self._url(name)))
                self.assertIn(f'name="next" value="{self._url(name)}"', body)

    def test_a_revision_from_package_review_returns_there(self):
        response = self._revise_from(self._url('design_qc_review'))
        self.assertRedirects(response, self._url('design_qc_review'),
                             fetch_redirect_response=False)
        self.assertEqual(self._current_arka().version, 2)

    def test_a_revision_from_arka_review_returns_there(self):
        response = self._revise_from(self._url('design_head_review'))
        self.assertRedirects(response, self._url('design_head_review'),
                             fetch_redirect_response=False)

    def test_a_refused_revision_returns_to_the_page_too(self):
        response = self._revise_from(self._url('design_qc_review'), reason='')
        self.assertRedirects(response, self._url('design_qc_review'),
                             fetch_redirect_response=False)
        self.assertEqual(self._current_arka().version, 1)

    def test_a_missing_or_unsafe_next_falls_back_to_the_status_rule(self):
        for bad in (None, '', '//evil.com', '/\\evil.com', 'http:evil.com',
                    'https://evil.com/x'):
            with self.subTest(next=bad):
                ArkaSubmission.objects.filter(attempt=self.attempt).exclude(
                    pk=self.arka.pk).delete()
                ArkaSubmission.objects.filter(pk=self.arka.pk).update(is_current=True)
                response = self._revise_from(bad, reason='')
                self.assertRedirects(response, self._url('design_head_review'),
                                     fetch_redirect_response=False)
        self._set_status(DESIGN_IN_QC)
        response = self._revise_from('//evil.com', reason='')
        self.assertRedirects(response, self._url('design_qc_review'),
                             fetch_redirect_response=False)

    def test_the_pool_outsider_still_lands_on_package_review(self):
        response = self._revise_from(self._url('design_head_review'), who=self.qc)
        self.assertRedirects(response, self._url('design_qc_review'),
                             fetch_redirect_response=False)


class VerdictLandingTests(DW8Base):

    def _post_as(self, who, name, **data):
        self._login(who)
        return self.client.post(self._url(name), data)

    def test_arka_verdicts_stay_on_arka_review(self):
        """Answer 3: approve and its refusals land on Arka review."""
        self._set_status(DESIGN_ARKA_SUBMITTED)
        ArkaSubmission.objects.filter(pk=self.arka.pk).update(verdict=ARKA_PENDING)
        response = self._post_as(self.named_qc, 'design_arka_approve')
        self.assertRedirects(response, self._url('design_head_review'),
                             fetch_redirect_response=False)
        response = self._post_as(self.named_qc, 'design_arka_approve')     # already ruled
        self.assertRedirects(response, self._url('design_head_review'),
                             fetch_redirect_response=False)
        response = self._post_as(self.named_qc, 'design_arka_reject', rejection_reason='')
        self.assertRedirects(response, self._url('design_head_review'),
                             fetch_redirect_response=False)

    def test_the_six_package_refusals_land_on_package_review(self):
        """N3: wrong status (and qc_start's incomplete package) no longer land on the
        queue. Each with the message, nothing written."""
        cases = [
            (self.named_qc, 'design_qc_start', DESIGN_IN_QC, {}),
            (self.named_qc, 'design_qc_pass', DESIGN_ARTIFACTS_UPLOADED, {}),
            (self.named_qc, 'design_qc_fail', DESIGN_ARTIFACTS_UPLOADED, {}),
            (self.head, 'design_head_qc_pass', DESIGN_IN_QC, {}),
            (self.head, 'design_head_qc_fail', DESIGN_IN_QC, {}),
        ]
        for who, name, status, data in cases:
            with self.subTest(view=name):
                self._set_status(status)
                response = self._post_as(who, name, **data)
                self.assertRedirects(response, self._url('design_qc_review'),
                                     fetch_redirect_response=False)
                self.assertTrue(self._messages(response))
                self.a.refresh_from_db()
                self.assertEqual(self.a.status, status)
        # qc_start at artifacts_uploaded with no BOQ stamp: the incomplete-package refusal.
        self._set_status(DESIGN_ARTIFACTS_UPLOADED)
        self.attempt.boq_submitted_at = None
        self.attempt.save()
        response = self._post_as(self.named_qc, 'design_qc_start')
        self.assertRedirects(response, self._url('design_qc_review'),
                             fetch_redirect_response=False)
        self.assertIn('the package is incomplete', self._messages(response))

    def test_a_qc_verdict_still_lands_on_package_review(self):
        response = self._post_as(self.named_qc, 'design_qc_pass')
        self.assertRedirects(response, self._url('design_qc_review'),
                             fetch_redirect_response=False)
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_AWAITING_HEAD_QC)

    def test_cad_revise_lands_on_package_review(self):
        response, _mocks = self._cad(self.named_qc)
        self.assertRedirects(response, self._url('design_qc_review'),
                             fetch_redirect_response=False)
        response, _mocks = self._cad(self.named_qc, reason='')
        self.assertRedirects(response, self._url('design_qc_review'),
                             fetch_redirect_response=False)


class CorrectionLandingTests(DW8Base):

    def test_a_correction_returns_to_the_correction_screen(self):
        line = BOQItem.objects.get(boq__project=self.site)
        self._login(self.named_qc)
        response = self.client.post(self._url('boq_correct'),
                                    {'action': 'set_quantity', 'item_id': str(line.pk),
                                     'quantity': '12'})
        self.assertRedirects(response, self._url('boq_correct'),
                             fetch_redirect_response=False)
        line.refresh_from_db()
        self.assertEqual(line.boq_quantity, 12)

    def test_a_refused_correction_lands_on_the_boq_view(self):
        self._at_head()
        self._login(self.named_qc)
        response = self.client.post(self._url('boq_correct'),
                                    {'action': 'set_quantity', 'item_id': '1',
                                     'quantity': '12'})
        self.assertRedirects(response, self._url('boq_detail'),
                             fetch_redirect_response=False)


class OverrideLandingTests(ReviewerOverrideBase):
    """B-2e's override set / clear on the correction screen: back to that screen."""

    def test_set_and_clear_return_to_the_correction_screen(self):
        self._login(self.qc)
        target = reverse('boq_correct', kwargs={'project_id': self.review.project_id})
        for response in (self._set(self.logger, 4, 'two roofs'), self._clear(self.logger)):
            self.assertEqual(response.redirect_chain[-1], (target, 302))
            self.assertEqual(response.status_code, 200)


# ===========================================================================
# T4.5 — the BOQ view: reviewer links and the badge (N4, N6); the dashboard chip (answer 6)
# ===========================================================================

class BOQViewTests(DW8Base):

    def _body(self, profile):
        response = self._get(profile, self._url('boq_detail'))
        self.assertEqual(response.status_code, 200)
        return _body(response)

    def test_reviewers_get_qc_review_and_the_download(self):
        for profile in (self.named_qc, self.head):
            with self.subTest(user=profile.user.username):
                body = self._body(profile)
                self.assertIn('Open QC review', body)
                self.assertIn(f'href="{self._url("design_qc_review")}"', body)
                self.assertIn('Download BOQ (Excel)', body)
                self.assertIn(f'href="{self._url("opex_boq_download")}"', body)
                self.assertEqual(self._get(profile, self._url('opex_boq_download'))
                                 .status_code, 200)

    def test_others_do_not(self):
        """A Design user reading by task, SCM, the PM and the pool outsider."""
        phase = ProjectPhase.objects.create(project=self.site, phase_name='Design',
                                            phase_order=1)
        Task.objects.create(phase=phase, task_name='Check survey', task_order=1,
                            assigned_role=Task.DESIGN, assigned_to=self.other_designer)
        for profile in (self.other_designer, self.scm, self.pm, self.qc):
            with self.subTest(user=profile.user.username):
                body = self._body(profile)
                self.assertNotIn('Open QC review', body)
                self.assertNotIn('Download BOQ (Excel)', body)

    def test_the_sites_designer_still_goes_to_the_picker(self):
        response = self._get(self.designer, self._url('boq_detail'))
        self.assertRedirects(response, self._url('opex_boq_entry'),
                             fetch_redirect_response=False)

    def test_the_opex_badge_is_the_design_stage(self):
        cases = [
            (DESIGN_IN_QC, None, 'Design: In QC'),
            (DESIGN_ARTIFACTS_UPLOADED, None, 'Design: Package complete'),
            (DESIGN_AWAITING_HEAD_QC, None, 'Design: QC passed — awaiting Design Head'),
            (DESIGN_RELEASED, None, 'Design: Released'),
            (DESIGN_ARKA_SUBMITTED, ARKA_APPROVED,
             'Design: Arka approved — CAD and BOQ outstanding'),
            (DESIGN_ARKA_SUBMITTED, ARKA_PENDING, 'Design: Arka awaiting Design QC'),
        ]
        for status, verdict, label in cases:
            with self.subTest(status=status, verdict=verdict):
                self._set_status(status)
                if verdict:
                    ArkaSubmission.objects.filter(pk=self.arka.pk).update(verdict=verdict)
                body = self._body(self.pm)
                self.assertIn(label, re.sub(r'\s+', ' ', body))
                self.assertNotIn('fs-6" style="background:#6b7280; color:#fff;">Draft', body)

    def test_a_residential_boq_keeps_its_badge(self):
        home = Project(project_id='DW8-RES', customer_name='Home', customer_phone='9876543210',
                       site_address='2 Sun Rd', city='Delhi', project_type='Residential',
                       status='Active', assigned_pm=self.pm)
        home.save()
        BOQ.objects.create(project=home)
        self._login(self.pm)
        body = _body(self.client.get(reverse('boq_detail', args=[home.project_id])))
        self.assertIn('style="background:#6b7280; color:#fff;">Draft</span>', body)
        self.assertNotIn('Design:', body)
        self.assertEqual(QUEUE_LINK.findall(body), [])

    def test_an_opex_site_with_no_assignment_keeps_its_badge(self):
        bare = Project(project_id='DW8-BARE', customer_name='Bare', customer_phone='9876543210',
                       site_address='3 Sun Rd', city='Delhi', project_type='OPEX',
                       program=self.program, status='Draft', assigned_pm=self.pm)
        bare.save()
        BOQ.objects.create(project=bare)
        self._login(self.pm)
        body = _body(self.client.get(reverse('boq_detail', args=[bare.project_id])))
        self.assertIn('style="background:#6b7280; color:#fff;">Draft</span>', body)


class DashboardChipTests(DW8Base):

    def test_no_boq_draft_chip_on_an_opex_card(self):
        self._login(self.designer)
        body = _body(self.client.get(reverse('dashboard_design')))
        self.assertIn(self.site.project_id, body)
        self.assertNotIn('BOQ: Draft', body)

    def test_a_residential_card_keeps_it(self):
        home = Project(project_id='DW8-RES', customer_name='Home', customer_phone='9876543210',
                       site_address='2 Sun Rd', city='Delhi', project_type='Residential',
                       status='Active', assigned_pm=self.pm, assigned_design=self.designer)
        home.save()
        self._login(self.designer)
        body = _body(self.client.get(reverse('dashboard_design')))
        self.assertIn('BOQ: Draft', body)


# ===========================================================================
# T4.6 — query counts, pinned (P7). Exact, on this fixture, so any rise fails.
# ===========================================================================

#: (url name, role, status, queries) on this fixture, measured on DW-8's code; the comment
#: is the same request on the parent commit (69d93f4), same fixture, so each row shows what
#: DW-8 added. Budget (P7, answer 5): +1 per screen, +2 on the BOQ view. The one row over it
#: (`pool` at arka_submitted, +3) is reported as a stop question, not hidden.
QUERY_PINS = [
    ('boq_detail', 'qc', DESIGN_IN_QC, 21),                    # before DW-8: 19
    ('boq_detail', 'pool', DESIGN_IN_QC, 20),                  # before DW-8: 18
    ('boq_detail', 'head', DESIGN_IN_QC, 19),                  # before DW-8: 17
    ('boq_detail', 'scm', DESIGN_IN_QC, 17),                   # before DW-8: 16
    ('boq_detail', 'pm', DESIGN_IN_QC, 15),                    # before DW-8: 15
    ('boq_detail', 'qc', DESIGN_ARKA_SUBMITTED, 21),           # before DW-8: 19
    ('boq_detail', 'pool', DESIGN_ARKA_SUBMITTED, 21),         # before DW-8: 18
    ('boq_detail', 'head', DESIGN_ARKA_SUBMITTED, 19),         # before DW-8: 17
    ('boq_detail', 'scm', DESIGN_ARKA_SUBMITTED, 18),          # before DW-8: 16
    ('boq_detail', 'pm', DESIGN_ARKA_SUBMITTED, 16),           # before DW-8: 15
    ('opex_boq_entry', 'designer', DESIGN_IN_QC, 20),          # before DW-8: 20
    ('opex_boq_entry', 'qc', DESIGN_IN_QC, 21),                # before DW-8: 20
    ('opex_boq_entry', 'pool', DESIGN_IN_QC, 21),              # before DW-8: 20
    ('opex_boq_entry', 'head', DESIGN_IN_QC, 18),              # before DW-8: 18
    ('opex_boq_entry', 'scm', DESIGN_IN_QC, 19),               # before DW-8: 18
    ('opex_boq_entry', 'pm', DESIGN_IN_QC, 18),                # before DW-8: 17
    ('design_qc_review', 'qc', DESIGN_IN_QC, 37),              # before DW-8: 37
    ('design_qc_review', 'pool', DESIGN_IN_QC, 35),            # before DW-8: 35
    ('design_qc_review', 'head', DESIGN_IN_QC, 33),            # before DW-8: 33
    ('design_head_review', 'qc', DESIGN_IN_QC, 21),            # before DW-8: 21
    ('design_head_review', 'head', DESIGN_IN_QC, 19),          # before DW-8: 19
    ('boq_correct', 'qc', DESIGN_IN_QC, 22),                   # before DW-8: 22
    ('boq_correct', 'head', DESIGN_IN_QC, 20),                 # before DW-8: 20
    ('design_site_workspace', 'designer', DESIGN_IN_QC, 20),   # before DW-8: 20
    ('design_site_workspace', 'head', DESIGN_IN_QC, 21),       # before DW-8: 21
    ('design_boq_group_list', 'designer', DESIGN_IN_QC, 10),   # before DW-8: 9
    ('design_boq_group_list', 'pool', DESIGN_IN_QC, 10),       # before DW-8: 9
    ('design_boq_group_list', 'head', DESIGN_IN_QC, 7),       # before DW-8: 7
    ('design_boq_group_detail', 'designer', DESIGN_IN_QC, 21), # before DW-8: 20
    ('design_boq_group_detail', 'head', DESIGN_IN_QC, 18),     # before DW-8: 18
    ('design_boq_group_snapshot_detail', 'designer', DESIGN_IN_QC, 9), # before DW-8: 8
    ('design_boq_group_snapshot_detail', 'head', DESIGN_IN_QC, 6), # before DW-8: 6
    ('design_head_sites', 'head', DESIGN_IN_QC, 13),           # before DW-8: 13
    ('design_tender_dashboard', 'head', DESIGN_IN_QC, 15),     # before DW-8: 15
    ('design_qc_dashboard', 'pool', DESIGN_IN_QC, 13),         # before DW-8: 13
    ('design_qc_queue', 'qc', DESIGN_IN_QC, 16),               # before DW-8: 16
    ('design_qc_queue', 'pool', DESIGN_IN_QC, 10),             # before DW-8: 10
    ('design_qc_queue', 'head', DESIGN_IN_QC, 11),             # before DW-8: 11
    ('design_boq_catalogue', 'head', DESIGN_IN_QC, 7),        # before DW-8: 7
    # before DW-8: 6. DW-4: +1, the one read of the active positions behind the position
    # field's help text (the items either side) and the used-position check.
    ('design_boq_catalogue_edit', 'head', DESIGN_IN_QC, 7),
    ('design_my_sites', 'designer', DESIGN_IN_QC, 9),         # before DW-8: 9
    ('design_my_sites', 'pm', DESIGN_IN_QC, 5),               # before DW-8: 5
    ('dashboard_design', 'designer', DESIGN_IN_QC, 19),        # before DW-8: 19
]


class QueryPinTests(DW8Base):

    def test_query_counts(self):
        who = {'designer': self.designer, 'qc': self.named_qc, 'pool': self.qc,
               'head': self.head, 'scm': self.scm, 'pm': self.pm}
        self._snapshot()
        measured = []
        for name, role, status, _pin in QUERY_PINS:
            self._set_status(status)
            self._login(who[role])
            url = self._url(name)
            self.client.get(url)
            with CaptureQueriesContext(connection) as q:
                response = self.client.get(url)
            self.assertEqual(response.status_code, 200, name)
            measured.append((name, role, status, len(q)))
        self.maxDiff = None
        self.assertEqual(measured, [p for p in QUERY_PINS])
