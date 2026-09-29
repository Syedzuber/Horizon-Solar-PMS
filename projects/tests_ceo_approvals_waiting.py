"""
S6 — the CEO Tenders "Waiting on someone" card (approval_queries.tender_approvals_waiting).

What this file pins, and why each matters:

  * SCOPE, NOT A NEW DEFINITION. A request is in when it reaches a live tender site
    through `projects`, a site group's LIVE membership, or a program with a live site.
    Test-only links and removed memberships keep it out.
  * UNLINKED REQUESTS (ruling 6c) are out of the figures and rows and counted once each
    in the footer — only when they have a live step. A request linked to test sites is
    not "unlinked".
  * THE FIGURES EQUAL THE APPROVALS SCREENS': steps waiting is live_pending_steps()
    narrowed to the scope, and the oldest wait is the largest oldest_days aging_rows()
    reports for the scoped steps' assignees.
  * THE FIVE OLDEST, ordered activated_at then pk, as aging_rows() orders them.
  * CLOSED REQUESTS AND DECIDED STEPS never appear.
  * QUERY COUNT does not grow with the number of requests (3 vs 30).

Real fixtures throughout: every request is raised through approvals.py; timestamps are
set with QuerySet.update() afterwards, the only way to make a wait exact (the pattern
tests_approval_dashboards uses).

    python manage.py test projects.tests_ceo_approvals_waiting --settings=solarpms.test_settings
"""
import re
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .approval_queries import (
    TENDER_WAITING_ROWS, aging_rows, live_pending_steps, tender_approval_requests,
    tender_approvals_waiting,
)
from .approvals import (
    apply_approval_decision, create_approval_request, withdraw_approval_request,
)
from .models import (
    ApprovalStep, Program, Project, SiteGroup, SiteGroupMembership,
    APPROVAL_KIND_MATERIAL_PRE_ORDER, APPROVAL_STEP_APPROVED, APPROVAL_STEP_SUPERSEDED,
)
from .tests_approvals import MODULE_LINE
from .views import tender_sites_qs


def _profile(username, role, **flags):
    """A post_save signal creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.replace('_', ' ').title())
    profile = user.profile
    profile.role = role
    profile.is_active = True
    for field, value in flags.items():
        setattr(profile, field, value)
    profile.save()
    return profile


def _site(project_id, program, status='Draft', **extra):
    if status in ('Active', 'In Progress'):
        extra.setdefault('activated_at', timezone.now())
    return Project.objects.create(
        project_id=project_id, customer_name=project_id, customer_phone='9876543210',
        site_address='1 Sun Road', city='Lucknow', project_type='OPEX',
        dc_capacity_kw=Decimal('10.00'), status=status, program=program,
        target_commissioning_date=date.today() + timedelta(days=90), **extra)


def _text(response):
    """The page's visible text, tags stripped and whitespace collapsed."""
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', response.content.decode()))


class WaitingFixture(TestCase):
    """One live tender with three live sites, a test-only tender, a live site group, a
    group whose only membership was removed, and a test-only group."""

    @classmethod
    def setUpTestData(cls):
        cls.scm  = _profile('s6_scm', 'SCM')
        cls.pm   = _profile('s6_pm', 'PM')          # every scoped step is his
        cls.pm_x = _profile('s6_pm_x', 'PM')        # every out-of-scope step is his
        cls.head = _profile('s6_head', 'Design', is_design_head=True)
        cls.ceo  = _profile('s6_ceo', 'CEO')

        cls.tender = Program.objects.create(
            name='S6 Tender', program_type='OPEX', client_name='C', status='Active',
            short_tender_code='S6A')
        cls.test_tender = Program.objects.create(
            name='S6 Test Tender', program_type='OPEX', client_name='C', status='Active',
            short_tender_code='S6T')

        cls.live1 = _site('S6-LIVE1', cls.tender)
        cls.live2 = _site('S6-LIVE2', cls.tender, status='Active')
        cls.live3 = _site('S6-LIVE3', cls.tender)
        cls.test_site = _site('S6-TEST', cls.test_tender, is_test=True)
        cls.cancelled = _site('S6-CANC', cls.test_tender, status='Cancelled')

        cls.group = SiteGroup.objects.create(program=cls.tender, name='S6 Batch 1')
        for site in (cls.live2, cls.live3):
            SiteGroupMembership.objects.create(group=cls.group, project=site)
        cls.gone_group = SiteGroup.objects.create(program=cls.tender, name='S6 Batch 2')
        SiteGroupMembership.objects.create(
            group=cls.gone_group, project=cls.live1, removed_at=timezone.now(),
            removal_reason='Moved to Batch 1.')
        cls.test_group = SiteGroup.objects.create(program=cls.test_tender,
                                                  name='S6 Test Batch')
        SiteGroupMembership.objects.create(group=cls.test_group, project=cls.test_site)

    # ── helpers ─────────────────────────────────────────────────────────────

    def raise_request(self, title, pm=None, design=False, **scope):
        return create_approval_request(
            kind=APPROVAL_KIND_MATERIAL_PRE_ORDER, raised_by=self.scm,
            title=title, description='Propose Waaree 545 Wp.',
            pm_assignee=pm or self.pm, design_signoff_required=design,
            design_assignee=self.head if design else None,
            material={}, lines=[dict(MODULE_LINE)], **scope)

    def activate(self, approval, ago, now=None):
        """Put an exact activated_at on every live step of `approval`, `ago` before
        `now` — pass the same `now` the card is read at, or the wait comes out a few
        microseconds short of a whole day."""
        now = now or timezone.now()
        ApprovalStep.objects.filter(request=approval).exclude(
            verdict=APPROVAL_STEP_SUPERSEDED).update(activated_at=now - ago)

    def pm_step(self, approval):
        return ApprovalStep.objects.get(request=approval, party='pm',
                                        round=approval.current_round)

    def card(self, now=None):
        return tender_approvals_waiting(tender_sites_qs(), now=now)

    def titles(self, card):
        return [row['title'] for row in card['rows']]

    def scoped_pks(self):
        return set(tender_approval_requests(tender_sites_qs())
                   .values_list('pk', flat=True))


# ===========================================================================
# a) Scope
# ===========================================================================

class ScopeTests(WaitingFixture):

    def test_a_request_via_projects_is_in(self):
        approval = self.raise_request('Via site', projects=[self.live1])
        self.assertIn(approval.pk, self.scoped_pks())
        self.assertEqual(self.card()['rows'][0]['scope_label'], 'S6-LIVE1')

    def test_a_request_via_a_site_group_is_in(self):
        approval = self.raise_request('Via group', site_groups=[self.group])
        self.assertIn(approval.pk, self.scoped_pks())
        self.assertEqual(self.card()['rows'][0]['scope_label'], 'S6 Tender · 2 sites')

    def test_a_request_via_a_program_is_in_and_labelled_by_name_alone(self):
        approval = self.raise_request('Via program', programs=[self.tender])
        self.assertIn(approval.pk, self.scoped_pks())
        self.assertEqual(self.card()['rows'][0]['scope_label'], 'S6 Tender')

    def test_test_only_links_are_out(self):
        by_site = self.raise_request('Test site', pm=self.pm_x, projects=[self.test_site])
        by_prog = self.raise_request('Test tender', pm=self.pm_x,
                                     programs=[self.test_tender])
        by_group = self.raise_request('Test group', pm=self.pm_x,
                                      site_groups=[self.test_group])
        scoped = self.scoped_pks()
        for approval in (by_site, by_prog, by_group):
            self.assertNotIn(approval.pk, scoped)
        card = self.card()
        self.assertEqual(card['count'], 0)
        self.assertEqual(card['rows'], [])
        # Linked to something, so not "unlinked" either.
        self.assertEqual(card['unlinked'], 0)

    def test_a_removed_membership_is_not_scope(self):
        approval = self.raise_request('Removed', pm=self.pm_x,
                                      site_groups=[self.gone_group])
        self.assertNotIn(approval.pk, self.scoped_pks())

    def test_a_cancelled_site_is_not_scope(self):
        approval = self.raise_request('Cancelled', pm=self.pm_x,
                                      projects=[self.cancelled])
        self.assertNotIn(approval.pk, self.scoped_pks())

    def test_one_live_link_among_test_links_brings_a_request_in(self):
        approval = self.raise_request('Mixed', projects=[self.test_site, self.live3],
                                      site_groups=[self.test_group])
        self.assertIn(approval.pk, self.scoped_pks())
        # The label names live sites only.
        self.assertEqual(self.card()['rows'][0]['scope_label'], 'S6-LIVE3')

    def test_label_counts_a_site_once_through_projects_and_a_group(self):
        self.raise_request('Both', projects=[self.live2, self.live1],
                           site_groups=[self.group])
        self.assertEqual(self.card()['rows'][0]['scope_label'], 'S6 Tender · 3 sites')

    def test_row_names_the_assignee_and_party(self):
        self.raise_request('Parallel', design=True, programs=[self.tender])
        rows = self.card()['rows']
        self.assertEqual(sorted((r['waiting_on'], r['party_label']) for r in rows),
                         [('S6 Head', 'Design Head'), ('S6 Pm', 'PM')])
        self.assertEqual({r['kind_label'] for r in rows}, {'Material — before order'})


# ===========================================================================
# b) Requests with no scope links (ruling 6c)
# ===========================================================================

class UnlinkedTests(WaitingFixture):

    def test_unlinked_is_out_of_the_figures_and_counted_once_per_request(self):
        self.raise_request('No links', pm=self.pm_x, design=True)   # two live steps
        self.raise_request('No links either', pm=self.pm_x)
        card = self.card()
        self.assertEqual(card['count'], 0)
        self.assertEqual(card['rows'], [])
        self.assertEqual(card['unlinked'], 2)

    def test_a_closed_unlinked_request_is_not_counted(self):
        approval = self.raise_request('No links', pm=self.pm_x)
        withdraw_approval_request(approval, self.scm, 'Raised by mistake.')
        self.assertEqual(self.card()['unlinked'], 0)

    def test_empty_state_and_footer_render_together(self):
        self.raise_request('No links', pm=self.pm_x)
        self.raise_request('Nor this', pm=self.pm_x)
        client = Client(SERVER_NAME='localhost')
        client.force_login(self.ceo.user)
        text = _text(client.get(reverse('dashboard_ceo'), {'context': 'tenders'}))
        self.assertIn('Waiting on someone', text)
        self.assertIn('Nothing waiting on anyone in the tenders.', text)
        self.assertIn('+2 open requests not linked to any tender →', text)
        self.assertIn('View all approvals →', text)

    def test_no_footer_when_nothing_is_unlinked(self):
        self.raise_request('Via site', projects=[self.live1])
        client = Client(SERVER_NAME='localhost')
        client.force_login(self.ceo.user)
        text = _text(client.get(reverse('dashboard_ceo'), {'context': 'tenders'}))
        self.assertNotIn('not linked to any tender', text)
        self.assertNotIn('Nothing waiting on anyone in the tenders.', text)


# ===========================================================================
# c) The figures equal the approvals screens'
# ===========================================================================

class AgreementTests(WaitingFixture):

    def test_figures_equal_the_aging_list_on_the_same_fixture(self):
        now = timezone.now()
        a = self.raise_request('Site', projects=[self.live1])
        b = self.raise_request('Group', site_groups=[self.group], design=True)
        c = self.raise_request('Program', programs=[self.tender])
        out = self.raise_request('Out', pm=self.pm_x)                 # unlinked, older
        self.activate(a, timedelta(days=2, hours=3), now)
        self.activate(b, timedelta(days=9), now)
        self.activate(c, timedelta(hours=5), now)
        self.activate(out, timedelta(days=30), now)

        card = self.card(now=now)
        scoped = live_pending_steps().filter(
            request__in=tender_approval_requests(tender_sites_qs()))
        self.assertEqual(card['count'], scoped.count())
        self.assertEqual(card['count'], 4)

        aging = aging_rows(now - timedelta(days=90), now=now)
        scoped_assignees = set(scoped.values_list('assignee', flat=True))
        oldest = max(entry['oldest_days'] for entry in aging['assignees']
                     if entry['profile'].pk in scoped_assignees)
        self.assertEqual(card['oldest_days'], oldest)
        self.assertEqual(card['oldest_days'], 9)
        self.assertEqual(card['oldest_text'], '9 days')

    def test_days_text_matches_the_screens_wording(self):
        now = timezone.now()
        approval = self.raise_request('Fresh', projects=[self.live1])
        self.activate(approval, timedelta(hours=20), now)
        self.assertEqual(self.card(now=now)['oldest_text'], 'today')
        self.activate(approval, timedelta(days=1, hours=1), now)
        self.assertEqual(self.card(now=now)['oldest_text'], '1 day')

    def test_nothing_waiting_reads_as_a_dash(self):
        card = self.card()
        self.assertEqual((card['count'], card['oldest_days'], card['oldest_text']),
                         (0, None, '—'))


# ===========================================================================
# d) The five oldest, deterministic ties
# ===========================================================================

class OrderTests(WaitingFixture):

    def test_five_oldest_ordered_by_activation_then_pk(self):
        ages = [1, 8, 3, 8, 12, 5, 2]           # two requests tie at 8 days
        approvals = []
        for i, days in enumerate(ages):
            approval = self.raise_request(f'R{i}', projects=[self.live1])
            approvals.append(approval)
        now = timezone.now()
        for approval, days in zip(approvals, ages):
            ApprovalStep.objects.filter(request=approval).update(
                activated_at=now - timedelta(days=days))

        card = self.card(now=now)
        self.assertEqual(card['count'], 7)
        self.assertEqual(len(card['rows']), TENDER_WAITING_ROWS)
        # R1 and R3 share an activated_at; the lower step pk (R1, raised first) leads.
        self.assertEqual(self.titles(card), ['R4', 'R1', 'R3', 'R5', 'R2'])
        self.assertEqual([r['days'] for r in card['rows']], [12, 8, 8, 5, 3])


# ===========================================================================
# e) Closed requests and decided steps never appear
# ===========================================================================

class ClosedTests(WaitingFixture):

    def test_decided_steps_and_closed_requests_drop_out(self):
        approved = self.raise_request('Approved', projects=[self.live1])
        withdrawn = self.raise_request('Withdrawn', projects=[self.live1])
        changes = self.raise_request('Changes', projects=[self.live1])
        half = self.raise_request('Half', design=True, programs=[self.tender])
        waiting = self.raise_request('Waiting', projects=[self.live1])

        apply_approval_decision(self.pm_step(approved), APPROVAL_STEP_APPROVED, self.pm)
        withdraw_approval_request(withdrawn, self.scm, 'No longer needed.')
        apply_approval_decision(self.pm_step(changes), 'changes_requested', self.pm,
                                note='Quote a second make.')
        # Half: the PM approves, the Design Head's parallel step still waits.
        apply_approval_decision(self.pm_step(half), APPROVAL_STEP_APPROVED, self.pm)

        card = self.card()
        self.assertEqual(sorted(self.titles(card)), ['Half', 'Waiting'])
        self.assertEqual(card['count'], 2)
        self.assertEqual([r['party_label'] for r in card['rows']
                          if r['title'] == 'Half'], ['Design Head'])


# ===========================================================================
# f) Fixed query count
# ===========================================================================

class QueryCountTests(WaitingFixture):

    def queries_for(self, n):
        for i in range(n):
            scope = [{'projects': [self.live1]}, {'site_groups': [self.group]},
                     {'programs': [self.tender]}][i % 3]
            self.raise_request(f'Q{i}', design=bool(i % 2), **scope)
            self.raise_request(f'U{i}', pm=self.pm_x)                 # unlinked
        with CaptureQueriesContext(connection) as ctx:
            card = self.card()
        # Rows are shown, so the label prefetches ran and are in the count.
        self.assertEqual(len(card['rows']), min(card['count'], TENDER_WAITING_ROWS))
        self.assertTrue(card['rows'])
        self.assertEqual(card['unlinked'], n)
        return len(ctx.captured_queries)

    def test_three_requests(self):
        self.assertEqual(self.queries_for(3), 6)

    def test_thirty_requests(self):
        self.assertEqual(self.queries_for(30), 6)

    def test_nothing_waiting_costs_two(self):
        with CaptureQueriesContext(connection) as ctx:
            self.card()
        self.assertEqual(len(ctx.captured_queries), 2)


# ===========================================================================
# The page: Tenders only, the card's text
# ===========================================================================

class PageTests(WaitingFixture):

    def get(self, context):
        client = Client(SERVER_NAME='localhost')
        client.force_login(self.ceo.user)
        return client.get(reverse('dashboard_ceo'), {'context': context})

    def test_tenders_page_shows_the_card_with_its_rows(self):
        approval = self.raise_request('Module make', site_groups=[self.group])
        self.activate(approval, timedelta(days=4))
        response = self.get('tenders')
        text = _text(response)
        self.assertIn('Waiting on someone', text)
        self.assertIn('Steps waiting', text)
        self.assertIn('Oldest wait', text)
        self.assertIn(f'#{approval.pk} · Module make', text)
        self.assertIn('Material — before order · S6 Tender · 2 sites', text)
        self.assertIn('Waiting on S6 Pm · PM', text)
        body = response.content.decode()
        self.assertIn(reverse('approval_detail', args=[approval.pk]), body)
        self.assertIn(reverse('approval_aging'), body)
        # 4 days: red, as on the aging list (days >= 3).
        self.assertRegex(body, r'text-danger fw-semibold">4 days<')
        # The template comment stays a comment.
        self.assertNotIn('tender_approvals_waiting', body)

    def test_under_three_days_is_not_red(self):
        approval = self.raise_request('Module make', projects=[self.live1])
        self.activate(approval, timedelta(days=2, hours=1))
        self.assertRegex(self.get('tenders').content.decode(),
                         r'text-muted">2 days<')

    def test_residential_has_no_card(self):
        self.raise_request('Module make', projects=[self.live1])
        self.assertNotIn('Waiting on someone', _text(self.get('residential')))
