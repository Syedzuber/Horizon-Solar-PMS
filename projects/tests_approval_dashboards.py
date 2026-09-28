"""Approvals 2c — the pending-approvals cards on the PM and Design dashboards, and the
aging list (approval_queries.py, approval_views.approval_aging).

What this file pins, and why each matters:

  * A CARD SHOWS ONLY THE VIEWER'S STEPS — their own, plus design steps they answer as
    the assignee Head's deputy, and those are labelled "as deputy for <Head>".
  * PENDING MEANS LIVE. A superseded step (fail-fast close, reassignment), a carried
    step and a step whose turn has not come are never shown as waiting.
  * THE ZERO STATE draws for an approver ("Nothing waiting") and nothing at all for a
    designer without Design Head authority.
  * DAYS WAITING are whole 24-hour periods since activated_at; under one reads "today".
  * THE AGING FIGURES against hand-built timestamps, including a proxy decision, a
    deputy decision, and a carried step that must stay out of turnaround.
  * WHO MAY OPEN THE AGING LIST: SCM, CEO, Admin, System Admin; a 403 WITH A BODY for
    PM, Design, Finance and Site Engineer.
  * QUERY COUNTS do not grow with the number of requests or people.

Timestamps are set with QuerySet.update() after the chokepoint has written each row —
the only way to make a turnaround exact. Nothing else here writes around approvals.py.

Run with:
    python manage.py test projects.tests_approval_dashboards --settings=solarpms.test_settings
and under the real settings (Postgres, migrations applied):
    python manage.py test projects.tests_approval_dashboards
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .approval_queries import (
    aging_rows, days_text, days_waiting, pending_approvals_card, pending_steps_for,
)
from .approvals import (
    ProxyDecision, apply_approval_decision, create_approval_request,
    reassign_approval_step, resubmit_approval_request,
)
from .models import (
    ApprovalStep, Vendor,
    APPROVAL_KIND_CONTRACTOR_BILL, APPROVAL_KIND_MATERIAL_PRE_ORDER,
    APPROVAL_PARTY_DESIGN, APPROVAL_PARTY_PM, APPROVAL_PARTY_SITE_ENGINEER,
    APPROVAL_STEP_APPROVED, APPROVAL_STEP_CHANGES_REQUESTED, APPROVAL_STEP_REJECTED,
    APPROVAL_STEP_SUPERSEDED,
)
from .tests_approvals import MODULE_LINE


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


class DashboardFixture(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.scm      = _profile('ad_scm', 'SCM')
        cls.pm       = _profile('ad_pm', 'PM')
        cls.pm_b     = _profile('ad_pm_b', 'PM')
        cls.head     = _profile('ad_head', 'Design', is_design_head=True)
        cls.deputy   = _profile('ad_deputy', 'Design')
        cls.head.design_head_deputy = cls.deputy
        cls.head.save(update_fields=['design_head_deputy'])
        cls.designer = _profile('ad_designer', 'Design')          # no Head authority
        cls.coord    = _profile('ad_coord', 'Project Coordinator')
        cls.ceo      = _profile('ad_ceo', 'CEO')
        cls.admin    = _profile('ad_admin', 'Admin')
        cls.sysadmin = _profile('ad_sysadmin', 'System Admin')
        cls.finance  = _profile('ad_finance', 'Finance')
        cls.se       = _profile('ad_se', 'Site Engineer')
        cls.vendor = Vendor.objects.create(name='AD Vendor', contact_person='R',
                                           phone='9000000003')

    # ── helpers ─────────────────────────────────────────────────────────────

    def raise_material(self, title='Module make', pm=None, design=False, vendor=None):
        return create_approval_request(
            kind=APPROVAL_KIND_MATERIAL_PRE_ORDER, raised_by=self.scm,
            title=title, description='Propose Waaree 545 Wp.',
            pm_assignee=pm or self.pm, design_signoff_required=design,
            design_assignee=self.head if design else None, vendor=vendor,
            material={}, lines=[dict(MODULE_LINE)],
        )

    def raise_bill(self, title='Civil works bill'):
        return create_approval_request(
            kind=APPROVAL_KIND_CONTRACTOR_BILL, raised_by=self.scm,
            title=title, description='Foundation work, block A.',
            pm_assignee=self.pm, site_engineer_assignee=self.se, vendor=self.vendor,
        )

    def step(self, approval, party):
        approval.refresh_from_db()
        return ApprovalStep.objects.exclude(verdict=APPROVAL_STEP_SUPERSEDED).get(
            request=approval, party=party, round=approval.current_round)

    def times(self, step, activated_ago, decided_ago=None):
        """Put exact timestamps on a step the chokepoint already wrote."""
        now = timezone.now()
        fields = {'activated_at': now - activated_ago}
        if decided_ago is not None:
            fields['decided_at'] = now - decided_ago
        ApprovalStep.objects.filter(pk=step.pk).update(**fields)

    def client_for(self, profile):
        client = Client(SERVER_NAME='localhost')
        client.force_login(profile.user)
        return client

    def titles(self, rows):
        return [row['title'] for row in rows]


# ===========================================================================
# The card: whose steps, labels, zero state, days waiting
# ===========================================================================

class CardTests(DashboardFixture):

    def test_the_card_shows_only_the_viewers_steps(self):
        self.raise_material('Mine')
        self.raise_material('Theirs', pm=self.pm_b)

        self.assertEqual(self.titles(pending_steps_for(self.pm)), ['Mine'])
        self.assertEqual(self.titles(pending_steps_for(self.pm_b)), ['Theirs'])

        response = self.client_for(self.pm).get(reverse('dashboard_pm'))
        self.assertEqual(response.status_code, 200)
        card = response.context['approvals_waiting']
        self.assertEqual((card['count'], self.titles(card['rows'])), (1, ['Mine']))
        self.assertContains(response, 'Approval waiting on you')
        self.assertContains(response, 'Mine')
        self.assertNotContains(response, 'Theirs')

    def test_a_deputy_sees_the_heads_design_steps_labelled(self):
        approval = self.raise_material('Inverter make', design=True)

        deputy_rows = pending_steps_for(self.deputy)
        self.assertEqual(self.titles(deputy_rows), ['Inverter make'])
        self.assertEqual(deputy_rows[0]['step'].party, APPROVAL_PARTY_DESIGN)
        self.assertEqual(deputy_rows[0]['as_deputy_for'], 'Ad Head')

        head_rows = pending_steps_for(self.head)
        self.assertEqual(self.titles(head_rows), ['Inverter make'])
        self.assertIsNone(head_rows[0]['as_deputy_for'])
        # The PM's own row on the same request is the PM's, not the deputy's.
        self.assertEqual([r['step'].party for r in pending_steps_for(self.pm)],
                         [APPROVAL_PARTY_PM])

        response = self.client_for(self.deputy).get(reverse('dashboard_design'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'as deputy for Ad Head')
        self.assertContains(response, reverse('approval_detail', args=[approval.pk]))
        response = self.client_for(self.head).get(reverse('dashboard_design'))
        self.assertContains(response, 'Inverter make')
        self.assertNotContains(response, 'as deputy for')

    def test_a_deputy_of_a_former_head_sees_nothing(self):
        self.raise_material('Inverter make', design=True)
        self.head.is_design_head = False
        self.head.save(update_fields=['is_design_head'])
        self.assertEqual(pending_steps_for(self.deputy), [])
        self.assertIsNone(pending_approvals_card(self.deputy.user))

    def test_a_superseded_step_is_never_pending(self):
        # Fail-fast: the PM's rejection supersedes the Head's pending step.
        approval = self.raise_material('Cable make', design=True)
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_REJECTED, self.pm, note='Wrong gauge.')
        self.assertEqual(
            ApprovalStep.objects.get(request=approval, party=APPROVAL_PARTY_DESIGN).verdict,
            APPROVAL_STEP_SUPERSEDED)
        self.assertEqual(pending_steps_for(self.head), [])
        self.assertEqual(pending_steps_for(self.deputy), [])
        self.assertEqual(pending_steps_for(self.pm), [])

        # Reassignment: the old assignee's superseded row leaves their card.
        other = self.raise_material('Module make')
        reassign_approval_step(self.step(other, APPROVAL_PARTY_PM), self.pm_b, self.scm,
                               note='PM on leave.')
        self.assertEqual(pending_steps_for(self.pm), [])
        self.assertEqual(self.titles(pending_steps_for(self.pm_b)), ['Module make'])

    def test_a_carried_step_is_never_pending(self):
        approval = self.raise_material('Module make', design=True)
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_DESIGN),
                                APPROVAL_STEP_APPROVED, self.head)
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.pm, note='Quote.')
        resubmit_approval_request(approval, self.scm, note='Added the quote.',
                                  carry={APPROVAL_PARTY_DESIGN: 'Make unchanged.'})
        kept = self.step(approval, APPROVAL_PARTY_DESIGN)
        self.assertIsNotNone(kept.carried_from_id)
        self.assertEqual(pending_steps_for(self.head), [])
        self.assertEqual(pending_steps_for(self.deputy), [])
        # The PM's fresh round-2 step is waiting.
        rows = pending_steps_for(self.pm)
        self.assertEqual([(r['title'], r['round']) for r in rows], [('Module make', 2)])

    def test_a_step_whose_turn_has_not_come_is_not_pending(self):
        # Contractor bill: the Site Engineer first, the PM only after.
        bill = self.raise_bill()
        self.assertEqual(pending_steps_for(self.pm), [])
        self.assertEqual([r['step'].party for r in pending_steps_for(self.se)],
                         [APPROVAL_PARTY_SITE_ENGINEER])
        apply_approval_decision(self.step(bill, APPROVAL_PARTY_SITE_ENGINEER),
                                APPROVAL_STEP_APPROVED, self.se)
        self.assertEqual(self.titles(pending_steps_for(self.pm)), ['Civil works bill'])

    def test_zero_state_for_an_approver(self):
        card = pending_approvals_card(self.pm.user)
        self.assertEqual((card['count'], card['rows']), (0, []))
        response = self.client_for(self.pm).get(reverse('dashboard_pm'))
        self.assertContains(response, 'Nothing waiting')
        self.assertContains(response, 'bi-check-circle-fill text-success')
        self.assertNotContains(response, 'waiting on you')

        response = self.client_for(self.head).get(reverse('dashboard_design'))
        self.assertContains(response, 'Nothing waiting')
        self.assertIsNotNone(response.context['approvals_waiting'])

    def test_no_card_for_a_non_approver(self):
        self.raise_material('Module make', design=True)
        self.assertIsNone(pending_approvals_card(self.designer.user))
        self.assertIsNone(pending_approvals_card(self.coord.user))
        response = self.client_for(self.designer).get(reverse('dashboard_design'))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['approvals_waiting'])
        self.assertNotContains(response, 'Nothing waiting')
        self.assertNotContains(response, 'waiting on you')
        response = self.client_for(self.coord).get(reverse('dashboard_pm'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Nothing waiting')

    def test_rows_oldest_first_with_days_vendor_and_kind(self):
        newer = self.raise_material('Newer', vendor=self.vendor)
        older = self.raise_material('Older')
        self.times(self.step(newer, APPROVAL_PARTY_PM), timedelta(hours=23))
        self.times(self.step(older, APPROVAL_PARTY_PM), timedelta(hours=49))

        card = pending_approvals_card(self.pm.user)
        self.assertEqual(self.titles(card['rows']), ['Older', 'Newer'])
        self.assertEqual([r['days_text'] for r in card['rows']], ['2 days', 'today'])
        self.assertEqual(card['oldest_text'], '2 days')
        self.assertEqual([r['vendor_name'] for r in card['rows']], ['—', 'AD Vendor'])
        self.assertEqual(card['rows'][0]['kind_label'], 'Material — before order')

        response = self.client_for(self.pm).get(reverse('dashboard_pm'))
        self.assertContains(response, 'Oldest: 2 days')
        self.assertContains(response, 'AD Vendor')

    def test_days_waiting_is_whole_24_hour_periods(self):
        now = timezone.now()
        for elapsed, days, text in [
            (timedelta(minutes=5),            0, 'today'),
            (timedelta(hours=23, minutes=59), 0, 'today'),
            (timedelta(hours=24),             1, '1 day'),
            (timedelta(hours=47),             1, '1 day'),
            (timedelta(hours=48),             2, '2 days'),
        ]:
            self.assertEqual(days_waiting(now - elapsed, now), days, elapsed)
            self.assertEqual(days_text(days), text)
        # A clock a moment behind never reads negative.
        self.assertEqual(days_waiting(now + timedelta(seconds=1), now), 0)

    def test_card_query_count_does_not_grow(self):
        self.raise_material('One', design=True)

        def measure(profile):
            with CaptureQueriesContext(connection) as ctx:
                pending_approvals_card(profile.user)
            return len(ctx.captured_queries)

        pm_small, deputy_small = measure(self.pm), measure(self.deputy)
        for i in range(6):
            self.raise_material(f'More {i}', design=True, vendor=self.vendor)
        self.assertEqual(measure(self.pm), pm_small)
        self.assertEqual(measure(self.deputy), deputy_small)
        self.assertLessEqual(pm_small, 2)


# ===========================================================================
# The aging figures
# ===========================================================================

class AgingFigureTests(DashboardFixture):
    """One hand-built history, every figure checked against it.

      R1  PM + Design. Round 1: PM approves (2 d), Head requests changes (1 d).
          Round 2: PM's approval CARRIED; the DEPUTY approves the Head's step (3 d).
      R2  PM only. SCM records the PM's approval by PROXY (4 d).
      R3  PM only, waiting 5 days.
      R4  PM B only, waiting 1 hour.
      R5  PM only, decided 100 days ago — outside the window.

    PM: 1 waiting (5 days), decisions R1r1 + R2 = 2, median 3.0 days (the carried step
    would make it [0, 2, 4] → 2.0 if it were counted), proxy 1 of 2, deputy 0.
    Head: 0 waiting, decisions R1r1 + R1r2 = 2, median 2.0, proxy 0 of 2, deputy 1.
    PM B: 1 waiting (today), no decisions.
    Carry rate: 1 carried ÷ (1 carried + 1 fresh round-2 approval) = 0.5.
    """

    def setUp(self):
        day = timedelta(days=1)
        r1 = self.raise_material('R1', design=True)
        pm1 = self.step(r1, APPROVAL_PARTY_PM)
        apply_approval_decision(pm1, APPROVAL_STEP_APPROVED, self.pm)
        self.times(pm1, 10 * day, 8 * day)
        head1 = self.step(r1, APPROVAL_PARTY_DESIGN)
        apply_approval_decision(head1, APPROVAL_STEP_CHANGES_REQUESTED, self.head,
                                note='Datasheet missing.')
        self.times(head1, 10 * day, 9 * day)
        resubmit_approval_request(r1, self.scm, note='Datasheet attached.',
                                  carry={APPROVAL_PARTY_PM: 'PM saw the same make.'})
        self.carried = self.step(r1, APPROVAL_PARTY_PM)
        head2 = self.step(r1, APPROVAL_PARTY_DESIGN)
        self.times(head2, 6 * day)
        apply_approval_decision(head2, APPROVAL_STEP_APPROVED, self.deputy)
        self.times(head2, 6 * day, 3 * day)

        r2 = self.raise_material('R2')
        pm2 = self.step(r2, APPROVAL_PARTY_PM)
        apply_approval_decision(pm2, APPROVAL_STEP_APPROVED, self.scm, proxy=ProxyDecision(
            decided_by=self.pm, channel='whatsapp', evidence='"Approved" on WhatsApp.'))
        self.times(pm2, 5 * day, 1 * day)

        r3 = self.raise_material('R3')
        self.times(self.step(r3, APPROVAL_PARTY_PM), 5 * day + timedelta(hours=1))

        r4 = self.raise_material('R4', pm=self.pm_b)
        self.times(self.step(r4, APPROVAL_PARTY_PM), timedelta(hours=1))

        r5 = self.raise_material('R5')
        pm5 = self.step(r5, APPROVAL_PARTY_PM)
        apply_approval_decision(pm5, APPROVAL_STEP_APPROVED, self.pm)
        self.times(pm5, 101 * day, 100 * day)

    def aging(self):
        now = timezone.now()
        return aging_rows(now - timedelta(days=90), now=now)

    def entry(self, aging, profile):
        return next(e for e in aging['assignees'] if e['profile'].pk == profile.pk)

    def test_the_carried_step_is_the_one_asserted_on(self):
        self.assertIsNotNone(self.carried.carried_from_id)
        self.assertEqual((self.carried.verdict, self.carried.round),
                         (APPROVAL_STEP_APPROVED, 2))

    def test_pm_figures(self):
        pm = self.entry(self.aging(), self.pm)
        self.assertEqual([r['title'] for r in pm['pending']], ['R3'])
        self.assertEqual((pm['pending_count'], pm['oldest_days'], pm['oldest_text']),
                         (1, 5, '5 days'))
        self.assertEqual(pm['decisions'], 2)            # R1r1 and R2; carried and R5 out
        self.assertEqual(pm['median_days'], 3.0)        # [2, 4]; with the carry, 2.0
        self.assertEqual((pm['proxies'], pm['proxy_share']), (1, 0.5))
        self.assertEqual(pm['by_deputy'], 0)

    def test_head_figures_count_the_deputys_decision(self):
        head = self.entry(self.aging(), self.head)
        self.assertEqual((head['pending_count'], head['oldest_days'], head['oldest_text']),
                         (0, None, '—'))
        self.assertEqual(head['decisions'], 2)
        self.assertEqual(head['median_days'], 2.0)      # [1, 3]
        self.assertEqual(head['proxy_share'], 0)
        self.assertEqual(head['by_deputy'], 1)
        # The deputy is never a row of their own: figures belong to the assignee.
        self.assertNotIn(self.deputy.pk, [e['profile'].pk for e in self.aging()['assignees']])

    def test_an_assignee_without_decisions(self):
        pm_b = self.entry(self.aging(), self.pm_b)
        self.assertEqual((pm_b['pending_count'], pm_b['oldest_text']), (1, 'today'))
        self.assertEqual(pm_b['decisions'], 0)
        self.assertIsNone(pm_b['median_days'])
        self.assertIsNone(pm_b['proxy_share'])
        self.assertIsNone(pm_b['by_deputy'])

    def test_order_longest_wait_first_then_the_rest(self):
        self.assertEqual([e['profile'].pk for e in self.aging()['assignees']],
                         [self.pm.pk, self.pm_b.pk, self.head.pk])

    def test_carry_rate(self):
        aging = self.aging()
        self.assertEqual((aging['carried'], aging['fresh_approved'], aging['carry_rate']),
                         (1, 1, 0.5))

    def test_superseded_and_not_yet_due_steps_are_not_pending_here(self):
        cable = self.raise_material('Cable', design=True)
        apply_approval_decision(self.step(cable, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_REJECTED, self.pm, note='Wrong gauge.')
        self.raise_bill('Bill')                          # PM step not activated yet
        titles = [r['title'] for e in self.aging()['assignees'] for r in e['pending']]
        self.assertNotIn('Cable', titles)
        self.assertEqual(titles.count('Bill'), 1)        # the Site Engineer's, only
        se = self.entry(self.aging(), self.se)
        self.assertEqual([r['party_label'] for r in se['pending']], ['Site Engineer'])

    def test_aging_rows_is_three_queries_however_much_data(self):
        with self.assertNumQueries(3):
            self.aging()
        extra_pms = [_profile(f'ad_pm_x{i}', 'PM') for i in range(4)]
        for i, profile in enumerate(extra_pms):
            approval = self.raise_material(f'Extra {i}', pm=profile, design=True,
                                           vendor=self.vendor)
            apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM),
                                    APPROVAL_STEP_APPROVED, profile)
        with self.assertNumQueries(3):
            aging = self.aging()
        self.assertEqual(len(aging['assignees']), 3 + 4)

    def test_the_page_draws_the_figures(self):
        page = self.client_for(self.scm).get(reverse('approval_aging'))
        self.assertEqual(page.status_code, 200)
        for text in ('Ad Pm', 'Ad Pm B', 'Ad Head', 'R3', '5 days',
                     '3.0 days', '50%', '(1 of 2)', 'Decided by deputy',
                     "Share of this person's decisions that SCM recorded on their behalf.",
                     'Carry rate, last 90 days:'):
            self.assertContains(page, text)
        self.assertContains(page, reverse('approval_detail', args=[
            ApprovalStep.objects.get(request__title='R3').request_id]))
        self.assertNotContains(page, 'R5')

    def test_the_page_query_count_does_not_grow(self):
        client = self.client_for(self.scm)

        def measure():
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(client.get(reverse('approval_aging')).status_code, 200)
            return len(ctx.captured_queries)

        small = measure()
        for i in range(5):
            profile = _profile(f'ad_pm_y{i}', 'PM')
            approval = self.raise_material(f'More {i}', pm=profile, vendor=self.vendor)
            apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM),
                                    APPROVAL_STEP_APPROVED, profile)
            self.raise_material(f'Waiting {i}', pm=profile)
        self.assertEqual(measure(), small)


# ===========================================================================
# Access and the list-page link
# ===========================================================================

class AgingAccessTests(DashboardFixture):

    def test_allowed_roles_open_the_page(self):
        for profile in (self.scm, self.ceo, self.admin, self.sysadmin):
            response = self.client_for(profile).get(reverse('approval_aging'))
            self.assertEqual(response.status_code, 200, profile.role)
            self.assertContains(response, 'Approval aging')

    def test_other_roles_are_refused_with_a_body(self):
        for profile in (self.pm, self.head, self.designer, self.finance, self.se):
            response = self.client_for(profile).get(reverse('approval_aging'))
            self.assertEqual(response.status_code, 403, profile.role)
            self.assertIn(b'Access denied', response.content)

    def test_anonymous_is_sent_to_login(self):
        response = Client(SERVER_NAME='localhost').get(reverse('approval_aging'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/login/')

    def test_empty_page(self):
        response = self.client_for(self.ceo).get(reverse('approval_aging'))
        self.assertContains(response, 'Nothing is waiting on anyone')

    def test_the_list_links_to_aging_only_when_allowed(self):
        aging = reverse('approval_aging')
        for profile in (self.scm, self.ceo):
            response = self.client_for(profile).get(reverse('approval_list'))
            self.assertTrue(response.context['can_view_aging'])
            self.assertContains(response, f'href="{aging}"')
        for profile in (self.pm, self.head):
            response = self.client_for(profile).get(reverse('approval_list'))
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.context['can_view_aging'])
            self.assertNotContains(response, f'href="{aging}"')
