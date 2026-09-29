"""
S9 — the CEO Tenders click-through site lists (views.dashboard_ceo_tender_sites).

THE HARD RULE: a link's list holds exactly the sites its count says. The list reads the
same tender_sites_qs() and site_stages() the pipeline and tender cards count from, and the
same per-site stuck verdict (tender_stages._stuck_entry) the stuck section is built from,
so "Waiting on" and the days text are the stuck section's own for a stuck site.

Real fixtures throughout, built with the S7 test builders (tests_stuck_sites.Builders).
Function-level tests pass the S7 fixed TODAY and cutoff; page tests use the real clock and
compare against the dashboard rendered on that same clock.

    python manage.py test projects.tests_ceo_tender_sites --settings=solarpms.test_settings
"""
import re
from datetime import date, timedelta
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import resolve, reverse

from . import tests_stuck_sites as s7
from . import views
from .models import (
    DESIGN_ARTIFACTS_UPLOADED, DESIGN_AWAITING_PM_APPROVAL, DesignAssignment, Program,
    Project, Task,
)
from .tender_stages import STAGES, STUCK_RULES, stage_summary, tender_cards, tender_site_list
from .views import CONTEXT_TENDERS, tender_sites_qs


def build_mixed(t):
    """Every stuck rule, a site either side of most limits, a group of five with one
    waiting-on, a group of five with two, an undated site, a programme-less site, an S0
    site, a test site and a CAPEX site. `t` is a tests_stuck_sites.Builders instance."""
    beta = s7._program('Beta', 'BET')
    for i in range(5):
        t.s1(f'G1-{i}', 20)                                  # one group, one waiting-on
    t.s1('S1-FRESH', 3)
    t.s1('S1-OLD', 12)
    t.s1('S1-BETA', 9, program=beta)
    t.s2('S2-LATE', 4)
    t.s2('S2-OK', -2)
    t.s2('S2-UP', 3, status=DESIGN_ARTIFACTS_UPLOADED)
    t.s3('S3-LATE', 2, qc_actor=t.qc)
    t.pm_rejected('PMR-OLD', 9)
    t.pm_rejected('PMR-NEW', 2)
    for i in range(5):
        t.s4(f'PMX-{i}', 12, pm=t.pm2 if i == 4 else None)  # one group, two people
    t.s4('S4-LATE', 5)
    t.s4('S4-NEW', 1, program=beta)
    undated = s7._site(t.tender, 'S4-UNDATED', assigned_pm=t.pm)
    DesignAssignment.objects.create(project=undated, status=DESIGN_AWAITING_PM_APPROVAL)
    t.s5('S5-LATE', 9)
    t.s5('S5-NEW', 2)
    t.s6('S6-LATE', 9)
    t.s6('S6-ORD', 30, ordered=True)
    t.s7('S7-BLK', [{'status': Task.BLOCKED, 'blocked_since': s7.CUTOFF - timedelta(days=2),
                     'task_name': 'Mount rails'}])
    t.s7('S7-DUE', [{'due_date': s7.TODAY - timedelta(days=3), 'task_name': 'Earthing'}])
    t.s7('S7-OK', [])
    t.s1('NOPROG', 15)
    Project.objects.filter(project_id='NOPROG').update(program=None)
    s7._site(t.tender, 'S0-NONE')
    t.s1('TEST-S1', 60, is_test=True)
    capex = s7._site(None, 'CAPEX-S1', project_type='CAPEX')
    DesignAssignment.objects.create(project=capex, status='awaiting_allocation',
                                    survey_link_added_at=s7._ago(60))
    return beta


def normalized(result):
    """stuck_sites() output with every database pk replaced by a stable name, so a golden
    copy can be written down: programme pks become tender names, URLs their route name and
    the tender name or project_id."""
    names = dict(Project.objects.values_list('program_id', 'program__name').distinct())
    out = dict(result)
    rows = []
    for row in result['rows']:
        row = dict(row)
        row['program_pk'] = names.get(row['program_pk'])
        match = resolve(row['url'])
        arg = match.kwargs.get('project_id') or names.get(match.kwargs.get('pk'))
        row['url'] = (match.url_name, arg)
        rows.append(row)
    out['rows'] = rows
    return out


# stuck_sites(build_mixed) as S7 wrote it, captured from the pre-S9 code before the
# _stuck_reads / _waiting_on / _stuck_entry split, pks replaced by normalized().
GOLDEN = {'rows': [{'program_pk': 'Alpha',
           'tender': 'Alpha',
           'site': '5 sites',
           'sites': 5,
           'stage': 'survey_on_file',
           'stage_label': 'Survey on file, not allocated',
           'rule': 'limit',
           'clock_date': date(2026, 9, 9),
           'days': 20,
           'overshoot': 13,
           'days_text': '20 days',
           'limit_text': '7 days',
           'waiting_on': 'Design Head',
           'url': ('program_detail', 'Alpha')},
          {'program_pk': 'Alpha',
           'tender': 'Alpha',
           'site': '5 sites',
           'sites': 5,
           'stage': 'awaiting_pm',
           'stage_label': 'Awaiting PM approval',
           'rule': 'limit',
           'clock_date': date(2026, 9, 17),
           'days': 12,
           'overshoot': 9,
           'days_text': '12 days',
           'limit_text': '3 days',
           'waiting_on': '2 people',
           'url': ('program_detail', 'Alpha')},
          {'program_pk': None,
           'tender': '—',
           'site': 'NOPROG',
           'sites': 1,
           'stage': 'survey_on_file',
           'stage_label': 'Survey on file, not allocated',
           'rule': 'limit',
           'clock_date': date(2026, 9, 14),
           'days': 15,
           'overshoot': 8,
           'days_text': '15 days',
           'limit_text': '7 days',
           'waiting_on': 'Design Head',
           'url': ('project_overview', 'NOPROG')},
          {'program_pk': 'Alpha',
           'tender': 'Alpha',
           'site': 'S1-OLD',
           'sites': 1,
           'stage': 'survey_on_file',
           'stage_label': 'Survey on file, not allocated',
           'rule': 'limit',
           'clock_date': date(2026, 9, 17),
           'days': 12,
           'overshoot': 5,
           'days_text': '12 days',
           'limit_text': '7 days',
           'waiting_on': 'Design Head',
           'url': ('project_overview', 'S1-OLD')},
          {'program_pk': 'Alpha',
           'tender': 'Alpha',
           'site': 'S2-LATE',
           'sites': 1,
           'stage': 'in_design',
           'stage_label': 'In design',
           'rule': 'due',
           'clock_date': date(2026, 9, 25),
           'days': 4,
           'overshoot': 4,
           'days_text': '4 days past due',
           'limit_text': 'Due 25 Sep',
           'waiting_on': 'Dev · Designer',
           'url': ('project_overview', 'S2-LATE')},
          {'program_pk': 'Alpha',
           'tender': 'Alpha',
           'site': 'S2-UP',
           'sites': 1,
           'stage': 'in_design',
           'stage_label': 'In design',
           'rule': 'due',
           'clock_date': date(2026, 9, 26),
           'days': 3,
           'overshoot': 3,
           'days_text': '3 days past due',
           'limit_text': 'Due 26 Sep',
           'waiting_on': 'Design QC',
           'url': ('project_overview', 'S2-UP')},
          {'program_pk': 'Alpha',
           'tender': 'Alpha',
           'site': 'S7-DUE',
           'sites': 1,
           'stage': 'activated',
           'stage_label': 'Activated — in execution',
           'rule': 'overdue_task',
           'clock_date': date(2026, 9, 26),
           'days': 3,
           'overshoot': 3,
           'days_text': '3 days past due',
           'limit_text': 'Due 26 Sep',
           'waiting_on': 'Pam · PM — overdue: Earthing',
           'url': ('project_overview', 'S7-DUE')},
          {'program_pk': 'Alpha',
           'tender': 'Alpha',
           'site': 'PMR-OLD',
           'sites': 1,
           'stage': 'in_qc',
           'stage_label': 'In QC / Head QC',
           'rule': 'pm_rejected',
           'clock_date': date(2026, 9, 20),
           'days': 9,
           'overshoot': 2,
           'days_text': '9 days since PM rejection',
           'limit_text': '7 days',
           'waiting_on': 'Design Head',
           'url': ('project_overview', 'PMR-OLD')},
          {'program_pk': 'Alpha',
           'tender': 'Alpha',
           'site': 'S3-LATE',
           'sites': 1,
           'stage': 'in_qc',
           'stage_label': 'In QC / Head QC',
           'rule': 'due',
           'clock_date': date(2026, 9, 27),
           'days': 2,
           'overshoot': 2,
           'days_text': '2 days past due',
           'limit_text': 'Due 27 Sep',
           'waiting_on': 'Quinn · Design QC',
           'url': ('project_overview', 'S3-LATE')},
          {'program_pk': 'Alpha',
           'tender': 'Alpha',
           'site': 'S4-LATE',
           'sites': 1,
           'stage': 'awaiting_pm',
           'stage_label': 'Awaiting PM approval',
           'rule': 'limit',
           'clock_date': date(2026, 9, 24),
           'days': 5,
           'overshoot': 2,
           'days_text': '5 days',
           'limit_text': '3 days',
           'waiting_on': 'Pam · PM',
           'url': ('project_overview', 'S4-LATE')},
          {'program_pk': 'Alpha',
           'tender': 'Alpha',
           'site': 'S5-LATE',
           'sites': 1,
           'stage': 'released',
           'stage_label': 'Released, not in a locked procurement group',
           'rule': 'limit',
           'clock_date': date(2026, 9, 20),
           'days': 9,
           'overshoot': 2,
           'days_text': '9 days',
           'limit_text': '7 days',
           'waiting_on': 'SCM',
           'url': ('project_overview', 'S5-LATE')},
          {'program_pk': 'Alpha',
           'tender': 'Alpha',
           'site': 'S6-LATE',
           'sites': 1,
           'stage': 'locked_group',
           'stage_label': 'In a locked procurement group',
           'rule': 'limit',
           'clock_date': date(2026, 9, 20),
           'days': 9,
           'overshoot': 2,
           'days_text': '9 days',
           'limit_text': '7 days',
           'waiting_on': 'Sam · SCM',
           'url': ('project_overview', 'S6-LATE')},
          {'program_pk': 'Alpha',
           'tender': 'Alpha',
           'site': 'S7-BLK',
           'sites': 1,
           'stage': 'activated',
           'stage_label': 'Activated — in execution',
           'rule': 'blocked',
           'clock_date': date(2026, 9, 20),
           'days': 9,
           'overshoot': 2,
           'days_text': '9 days blocked',
           'limit_text': 'Blocked 7 days',
           'waiting_on': 'Pam · PM — blocked: Mount rails',
           'url': ('project_overview', 'S7-BLK')},
          {'program_pk': 'Beta',
           'tender': 'Beta',
           'site': 'S1-BETA',
           'sites': 1,
           'stage': 'survey_on_file',
           'stage_label': 'Survey on file, not allocated',
           'rule': 'limit',
           'clock_date': date(2026, 9, 20),
           'days': 9,
           'overshoot': 2,
           'days_text': '9 days',
           'limit_text': '7 days',
           'waiting_on': 'Design Head',
           'url': ('project_overview', 'S1-BETA')}],
 'stuck': 22,
 'by_stage': [{'key': 'survey_on_file', 'label': 'Design backlog', 'sites': 8},
              {'key': 'in_design', 'label': 'In design', 'sites': 2},
              {'key': 'in_qc', 'label': 'In QC', 'sites': 2},
              {'key': 'awaiting_pm', 'label': 'Awaiting PM', 'sites': 6},
              {'key': 'released', 'label': 'Released, not grouped', 'sites': 1},
              {'key': 'locked_group', 'label': 'Locked, no PO/PI', 'sites': 1},
              {'key': 'activated', 'label': 'In execution', 'sites': 2}],
 'no_due_date': [{'tender': 'Alpha', 'count': 7}, {'tender': 'Beta', 'count': 1}],
 'undated': 1,
 'limits': ['Survey on file → allocated: 7 days (our limit)',
            "In design: past agreed due date (Design Head's rule)",
            "In QC / Head QC: past agreed due date (Design Head's rule); sent back by the PM: 7 "
            'days (our limit)',
            'Awaiting PM approval: 3 days (our limit)',
            'Released → in a locked procurement group: 7 days (our limit)',
            'Locked group → PO/PI recorded: 7 days (our limit)',
            'In execution: a task blocked 7 days or more, or an internal task past its due date '
            '(CEO Blocked Tasks and Tasks cards)'],
 'task_counts': {'blocked_aged': 1, 'overdue': 1}}


def list_url(stage, **params):
    """The list URL for `stage` plus params in the view's own order."""
    query = [('stage', stage)] + [(k, v) for k, v in params.items() if v is not None]
    return reverse('dashboard_ceo_tender_sites') + '?' + '&'.join(f'{k}={v}' for k, v in query)


class Page(s7.Builders):
    """A logged-in client, the Tenders dashboard and the list behind one of its links."""

    def client_for(self, profile=None):
        client = Client(SERVER_NAME='localhost')
        client.force_login((profile or self.ceo).user)
        return client

    def dashboard(self, client):
        response = client.get(reverse('dashboard_ceo'), {'context': CONTEXT_TENDERS})
        self.assertEqual(response.status_code, 200)
        return response

    def listing(self, client, url):
        response = client.get(url)
        self.assertEqual(response.status_code, 200, url)
        return response


# ---------------------------------------------------------------------------
# (a) every stage's list holds the pipeline's count, and each tender card's
# ---------------------------------------------------------------------------

class CountTests(Page):

    def test_each_pipeline_link_lists_its_count(self):
        build_mixed(self)
        client = self.client_for()
        ctx = self.dashboard(client).context
        for row in ctx['site_pipeline']['stages']:
            self.assertTrue(row['sites'], row['label'])    # build_mixed fills every stage
            self.assertEqual(self.listing(client, row['url']).context['total'], row['sites'],
                             row['label'])
        # Every STAGES key by hand, against the helper the pipeline counts with.
        summary = {s['key']: s['sites'] for s in stage_summary(tender_sites_qs())}
        for key, _ in STAGES:
            self.assertEqual(self.listing(client, list_url(key)).context['total'],
                             summary[key], key)

    def test_each_tender_card_stage_row_lists_its_count(self):
        build_mixed(self)
        client = self.client_for()
        cards = self.dashboard(client).context['tender_cards']
        self.assertEqual(sorted(c['name'] for c in cards), ['Alpha', 'Beta'])
        checked = 0
        for card in cards:
            for stage in card['stages']:
                if not stage['sites']:
                    self.assertIsNone(stage['url'])
                    continue
                response = self.listing(client, stage['url'])
                self.assertEqual(response.context['total'], stage['sites'])
                self.assertEqual({r['tender'] for r in response.context['rows']}, {card['name']})
                checked += 1
        self.assertEqual(checked, 8 + 2)                  # Alpha every stage, Beta S1 and S4
        # The same per programme against tender_cards() directly.
        for card in tender_cards(tender_sites_qs(), {}):
            for stage in card['stages']:
                url = list_url(stage['key'], program=card['program_pk'])
                self.assertEqual(self.listing(client, url).context['total'], stage['sites'])


# ---------------------------------------------------------------------------
# (b) stuck lists hold the stuck section's sites; (c) with its texts
# ---------------------------------------------------------------------------

class StuckListTests(Page):

    def test_stuck_links_list_the_stuck_sections_sites(self):
        build_mixed(self)
        client = self.client_for()
        ss = self.dashboard(client).context['stuck_sites']
        everything = self.listing(client, ss['list_url']).context
        self.assertEqual(everything['total'], ss['stuck'])
        self.assertTrue(all(r['stuck'] for r in everything['rows']))
        listed = {r['site'] for r in everything['rows']}
        for row in ss['rows']:
            if row['sites'] == 1:
                self.assertIn(row['site'], listed)
        self.assertEqual(len(ss['by_stage']), 7)
        for figure in ss['by_stage']:
            rows = self.listing(client, figure['url']).context['rows']
            self.assertEqual(len(rows), figure['sites'], figure['label'])
            self.assertEqual({(r['stage'], r['stuck']) for r in rows}, {(figure['key'], True)})

    def test_each_grouped_row_lists_exactly_its_n_sites(self):
        build_mixed(self)
        client = self.client_for()
        groups = [r for r in self.dashboard(client).context['stuck_sites']['rows']
                  if r['sites'] > 1]
        self.assertEqual(len(groups), 2)
        for group in groups:
            rows = self.listing(client, group['list_url']).context['rows']
            self.assertEqual(len(rows), group['sites'])
            self.assertEqual({(r['stage'], r['rule'], r['clock_date'], r['tender'])
                              for r in rows},
                             {(group['stage'], group['rule'], group['clock_date'],
                               group['tender'])})

    def test_same_date_groups_under_two_rules_each_list_their_own(self):
        # Both clocks start TODAY - 9: the S3 due date and the PM rejection date.
        for i in range(5):
            self.s3(f'DUE9-{i}', 9)
        for i in range(5):
            self.pm_rejected(f'PMR9-{i}', 9)
        client = self.client_for()
        groups = [r for r in self.dashboard(client).context['stuck_sites']['rows']
                  if r['sites'] > 1]
        self.assertEqual(sorted(g['rule'] for g in groups), ['due', 'pm_rejected'])
        self.assertEqual(len({(g['stage'], g['clock_date']) for g in groups}), 1)
        for group in groups:
            rows = self.listing(client, group['list_url']).context['rows']
            self.assertEqual(len(rows), 5)
            self.assertEqual({r['rule'] for r in rows}, {group['rule']})
        # Without since/rule the tender's stuck S3 list holds both groups.
        both = list_url('in_qc', program=self.tender.pk, stuck=1)
        self.assertEqual(self.listing(client, both).context['total'], 10)

    def test_waiting_on_and_days_match_the_stuck_section(self):
        build_mixed(self)
        client = self.client_for()
        ss = self.dashboard(client).context['stuck_sites']
        response = self.listing(client, ss['list_url'])
        by_site = {r['site']: r for r in response.context['rows']}
        text = s7._text(response)
        singles = [r for r in ss['rows'] if r['sites'] == 1]
        self.assertGreaterEqual(len(singles), 12)
        for row in singles:
            listed = by_site[row['site']]
            self.assertEqual((listed['in_stage'], listed['waiting_on']),
                             (row['days_text'], row['waiting_on']), row['site'])
            self.assertIn(f"{row['days_text']} 10 kWp {row['waiting_on']}", text)
        for group in (r for r in ss['rows'] if r['sites'] > 1):
            rows = self.listing(client, group['list_url']).context['rows']
            self.assertEqual({r['in_stage'] for r in rows}, {group['days_text']})
            waiting = {r['waiting_on'] for r in rows}
            if group['waiting_on'].endswith(' people'):
                self.assertEqual(f'{len(waiting)} people', group['waiting_on'])
            else:
                self.assertEqual(waiting, {group['waiting_on']})


# ---------------------------------------------------------------------------
# Rows that are not stuck, and the default order (function level, fixed TODAY)
# ---------------------------------------------------------------------------

class ListRowTests(s7.Builders):

    def rows(self):
        return tender_site_list(tender_sites_qs(), s7.TODAY, s7.CUTOFF)

    def test_sites_not_stuck_read_days_in_stage_and_the_shared_holder(self):
        build_mixed(self)
        got = {r['project_id']: (r['in_stage'], r['waiting_on'], r['stuck'])
               for r in self.rows()}
        self.assertEqual({pid: got[pid] for pid in (
            'S0-NONE', 'S1-FRESH', 'S2-OK', 'PMR-NEW', 'S4-NEW', 'S4-UNDATED', 'S5-NEW',
            'S6-ORD', 'S7-OK')}, {
            'S0-NONE':    ('—', '—', False),              # no survey: nobody's yet (D6)
            'S1-FRESH':   ('3 days', 'Design Head', False),
            'S2-OK':      ('40 days', 'Dev · Designer', False),   # from assigned_at
            'PMR-NEW':    ('2 days', 'Design Head', False),       # from the ledger
            'S4-NEW':     ('1 day', 'Pam · PM', False),
            'S4-UNDATED': ('—', 'Pam · PM', False),       # not stuck: cannot be timed
            'S5-NEW':     ('2 days', 'SCM', False),
            'S6-ORD':     ('30 days', '—', False),        # PO/PI recorded
            'S7-OK':      ('40 days', '—', False),        # in execution, nothing stuck
        })

    def test_stuck_first_by_overshoot_then_days_then_site(self):
        build_mixed(self)
        order = [r['project_id'] for r in self.rows()]
        self.assertEqual(order, [
            'G1-0', 'G1-1', 'G1-2', 'G1-3', 'G1-4',            # over 13
            'PMX-0', 'PMX-1', 'PMX-2', 'PMX-3', 'PMX-4',       # over 9
            'NOPROG',                                          # over 8
            'S1-OLD',                                          # over 5
            'S2-LATE',                                         # over 4
            'S2-UP', 'S7-DUE',                                 # over 3
            'PMR-OLD', 'S3-LATE', 'S4-LATE', 'S5-LATE', 'S6-LATE', 'S7-BLK',  # over 2, Alpha
            'S1-BETA',                                         # over 2, Beta
            'S2-OK', 'S7-OK', 'S6-ORD', 'S1-FRESH', 'PMR-NEW', 'S5-NEW', 'S4-NEW',
            'S0-NONE', 'S4-UNDATED',                           # undated last
        ])

    def test_design_status_is_the_display_label(self):
        build_mixed(self)
        got = {r['project_id']: r['design_status'] for r in self.rows()}
        self.assertEqual((got['S0-NONE'], got['S1-OLD'], got['PMR-OLD']),
                         ('—', 'Awaiting allocation', 'PM rejected — awaiting Design Head'))


# ---------------------------------------------------------------------------
# (d) scope, and unknown values are a 404
# ---------------------------------------------------------------------------

class ScopeTests(Page):

    def test_test_and_capex_sites_never_appear(self):
        build_mixed(self)
        client = self.client_for()
        for stage in [key for key, _ in STAGES] + ['stuck']:
            ids = {r['project_id'] for r in self.listing(client, list_url(stage)).context['rows']}
            self.assertFalse(ids & {'TEST-S1', 'CAPEX-S1'}, stage)

    def test_unknown_values_are_a_404(self):
        build_mixed(self)
        capex = Program.objects.create(name='Cap', program_type='CAPEX', client_name='C',
                                       status='Active')
        s7._site(capex, 'CAPEX-P1', project_type='CAPEX')
        test_only = s7._program('Testy', 'TSTY')
        s7._site(test_only, 'TESTY-1', is_test=True)
        base = reverse('dashboard_ceo_tender_sites')
        ok = f'?stage=survey_on_file&program={self.tender.pk}&stuck=1'
        client = self.client_for()
        for query in [
            '', '?stage=', '?stage=nope', '?stage=S1', '?stage=survey_on_file&stuck=0',
            '?stage=survey_on_file&stuck=true', '?stage=survey_on_file&stuck=',
            '?stage=survey_on_file&program=abc', '?stage=survey_on_file&program=-1',
            '?stage=survey_on_file&program=%C2%B2', '?stage=survey_on_file&program=999999',
            f'?stage=survey_on_file&program={capex.pk}',
            f'?stage=survey_on_file&program={test_only.pk}',
            ok + '&since=2026-09-09', ok + '&rule=limit', ok + '&since=2026-09-09&rule=nope',
            ok + '&since=20260909&rule=limit', ok + '&since=2026-02-30&rule=limit',
            '?stage=survey_on_file&since=2026-09-09&rule=limit',
            '?stage=stuck&stuck=1&since=2026-09-09&rule=limit',
        ]:
            self.assertEqual(client.get(base + query).status_code, 404, query)
        self.assertEqual(client.get(base + ok + '&since=2026-09-09&rule=limit').status_code,
                         200)

    def test_a_real_programme_with_an_empty_stage_is_zero_sites(self):
        beta = build_mixed(self)
        response = self.listing(self.client_for(), list_url('activated', program=beta.pk))
        self.assertEqual(response.context['total'], 0)
        text = s7._text(response)
        self.assertIn('Beta · Activated — in execution · 0 sites', text)
        self.assertIn('No site matches these filters.', text)


# ---------------------------------------------------------------------------
# (e) permissions
# ---------------------------------------------------------------------------

class PermissionTests(Page):

    def test_ceo_admin_and_system_admin_see_it_a_pm_gets_the_dashboards_refusal(self):
        self.s1('P-1', 10)
        url = list_url('survey_on_file')
        admin = s7._profile('adm1', 'Admin')
        system = s7._profile('sys1', 'System Admin')
        for profile in (self.ceo, admin, system):
            self.assertEqual(self.client_for(profile).get(url).status_code, 200, profile.role)
        pm = self.client_for(self.pm)
        refused = pm.get(url)
        dashboard = pm.get(reverse('dashboard_ceo'), {'context': CONTEXT_TENDERS})
        self.assertEqual((refused.status_code, dashboard.status_code), (403, 403))
        anonymous = Client(SERVER_NAME='localhost')
        self.assertEqual(anonymous.get(url).status_code, 302)
        self.assertIn(reverse('login'), anonymous.get(url)['Location'])


# ---------------------------------------------------------------------------
# (f) zero counts are plain text; links are ordinary <a> elements
# ---------------------------------------------------------------------------

class DashboardLinkTests(Page):

    def hrefs(self, body):
        return {h.replace('&amp;', '&')
                for h in re.findall(r'<a href="([^"]*tender-sites[^"]*)"', body)}

    def test_zero_count_stages_render_no_link(self):
        project = self.s1('Z-1', 0)
        # Entered just now, on the real clock the page uses, so it is never stuck.
        DesignAssignment.objects.filter(project=project).update(
            survey_link_added_at=views.timezone.now())
        response = self.dashboard(self.client_for())
        body = response.content.decode()
        self.assertEqual(self.hrefs(body), {
            list_url('survey_on_file'), list_url('survey_on_file', program=self.tender.pk)})
        # Every mention of the list is an <a href>: nothing is wired by a script.
        self.assertEqual(body.count('tender-sites'), 2 + 2)   # pipeline 2 + card row 2
        self.assertIn('No site is past its limit.', s7._text(response))

    def test_stuck_summary_figures_and_groups_link(self):
        build_mixed(self)
        body = self.dashboard(self.client_for()).content.decode()
        hrefs = self.hrefs(body)
        self.assertIn(list_url('stuck'), hrefs)
        self.assertIn(list_url('awaiting_pm', stuck=1), hrefs)
        self.assertTrue(any('since=' in h and 'rule=limit' in h for h in hrefs))
        # In the stuck section a grouped row no longer links the programme page (the tender
        # card's "Open tender" still does); single rows still link their site.
        section = body[body.index('Stuck sites</div>'):body.index('data-stuck-limits')]
        self.assertNotIn(f'href="{reverse("program_detail", args=[self.tender.pk])}"', section)
        self.assertIn(f'href="{reverse("project_overview", args=["S1-OLD"])}"', section)
        self.assertEqual(section.count('since='), 2)            # the two grouped rows


# ---------------------------------------------------------------------------
# The page: title, chips, back link, capacity, the 500-row guard
# ---------------------------------------------------------------------------

class PageTextTests(Page):

    def test_grouped_row_title_chips_and_back_link(self):
        build_mixed(self)
        client = self.client_for()
        group = next(r for r in self.dashboard(client).context['stuck_sites']['rows']
                     if r['sites'] > 1 and r['stage'] == 'survey_on_file')
        response = self.listing(client, group['list_url'])
        self.assertIn('Alpha · Survey on file, not allocated · past limit · 5 sites',
                      s7._text(response))
        chips = response.context['chips']
        self.assertEqual([c['label'] for c in chips],
                         ['Tender: Alpha', 'Past limit only', 'In stage since 9 Sep 2026'])
        for chip in chips:
            self.listing(client, chip['remove_url'])
            self.assertEqual(parse_qs(urlsplit(chip['remove_url']).query)['stage'],
                             ['survey_on_file'])
        self.assertNotIn('program', parse_qs(urlsplit(chips[0]['remove_url']).query))
        self.assertEqual(set(parse_qs(urlsplit(chips[1]['remove_url']).query)),
                         {'stage', 'program'})
        self.assertEqual(response.context['back_url'],
                         reverse('dashboard_ceo') + '?context=tenders')

    def test_stuck_title_and_capacity_cells(self):
        build_mixed(self)
        Project.objects.filter(project_id='S1-OLD').update(dc_capacity_kw=None)
        Project.objects.filter(project_id='S2-LATE').update(dc_capacity_kw=0)
        Project.objects.filter(project_id='S3-LATE').update(dc_capacity_kw='7125.50')
        client = self.client_for()
        stuck = self.dashboard(client).context['stuck_sites']['stuck']
        response = self.listing(client, list_url('stuck'))
        self.assertIn(f'Stuck sites · {stuck} sites', s7._text(response))
        self.assertEqual(response.context['chips'], [])
        cap = {r['project_id']: r['capacity'] for r in response.context['rows']}
        self.assertEqual((cap['S1-OLD'], cap['S2-LATE'], cap['S3-LATE'], cap['S4-LATE']),
                         ('—', '—', '7,125.5 kWp', '10 kWp'))

    def test_more_than_the_limit_shows_the_first_rows_and_says_so(self):
        build_mixed(self)
        client = self.client_for()
        with mock.patch.object(views, 'TENDER_SITE_LIST_LIMIT', 3):
            response = self.listing(client, list_url('survey_on_file'))
        self.assertEqual(len(response.context['rows']), 3)
        total = response.context['total']
        self.assertGreater(total, 3)
        self.assertIn(f'Showing the first 3 of {total} sites.', s7._text(response))
        self.assertIn(f'· {total} sites', s7._text(response))


# ---------------------------------------------------------------------------
# (g) a fixed query count
# ---------------------------------------------------------------------------

class QueryBudgetTests(Page):

    def count(self, client, url):
        with CaptureQueriesContext(connection) as queries:
            response = self.listing(client, url)
        return len(queries), response.context['total']

    def test_three_sites_and_thirty_cost_the_same(self):
        client = self.client_for()
        # tests_stuck_sites' own mix: every stage and rule, people named on every branch.
        s7.QueryBudgetTests.build(self, 'Q3', 3)
        small = [self.count(client, list_url(s)) for s in ('stuck', 'survey_on_file')]
        s7.QueryBudgetTests.build(self, 'Q30', 30)
        large = [self.count(client, list_url(s)) for s in ('stuck', 'survey_on_file')]
        for (few, few_rows), (many, many_rows) in zip(small, large):
            self.assertEqual(few, many)
            self.assertGreater(many_rows, few_rows)
        # stuck_sites' seven reads, plus session, user and profile for the request and
        # base.html's unread-notification count.
        self.assertEqual(small[0][0], 11)


# ---------------------------------------------------------------------------
# (h) stuck_sites is unchanged by the S9 split
# ---------------------------------------------------------------------------

class RefactorTests(s7.Builders):

    def test_stuck_sites_output_is_the_pre_s9_output(self):
        build_mixed(self)
        self.assertEqual(normalized(self.result()), GOLDEN)

    def test_the_golden_covers_every_rule_and_stuck_rules_names_them_all(self):
        self.assertEqual({row['rule'] for row in GOLDEN['rows']}, set(STUCK_RULES))
