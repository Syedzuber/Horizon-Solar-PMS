"""
SNAP-3a: the per-tender "Include in daily snapshot" switch (Program.in_daily_snapshot).

WHAT THIS PINS
    Default        on for a new Program, and for one made through ProgramForm without
                   the switch (the create form and the seed commands)
    Who            only an Admin sees the checkbox on program_edit; a PM who can open
                   the page sees none, and a PM's POST changes nothing whatever it
                   carries (rulings K2, Q1)
    Log            an Admin's change writes one program_edited line reading
                   "Include in daily snapshot: On → Off" (and back); a save that does
                   not change it writes no such line (Q3)
    Snapshot       a switched-off tender has no section, is not drawn, is not in the
                   headline or the dry run, and has no By tender row; the org tiles and
                   By PM rows are unchanged (K3, Q2)
    Back on        the tender's section returns, with no arrows that day (K3)
    Other pages    the tender page, the Site Dashboard and the Tenders Gantt show the
                   same thing whether the switch is on or off (K4)

Run with:
    python manage.py test projects.tests_snapshot_tender_switch --settings=solarpms.test_settings
"""
import io
import tempfile
from unittest import mock

from django.core.management import call_command
from django.test import Client
from django.urls import reverse

from . import snapshot as S
from .forms import SNAPSHOT_SWITCH_ROLES, ProgramForm
from .models import ActivityLog, DashboardSnapshot, Program
from .tests_dashboard_snapshot import NOW, SnapshotFixture

LABEL = 'Include in daily snapshot'


def _client(profile):
    client = Client()
    client.force_login(profile.user)
    return client


def _edit_post(program, **overrides):
    """The edit form's POST body for `program` as it stands, minus the checkbox (an
    unticked box is absent from a real POST); `in_daily_snapshot='on'` ticks it."""
    data = {'program_type': program.program_type, 'name': program.name,
            'client_name': program.client_name, 'status': program.status,
            'short_tender_code': program.short_tender_code,
            'total_capacity': '', 'planned_site_count': ''}
    data.update(overrides)
    return data


class SwitchFixture(SnapshotFixture):
    """SnapshotFixture (ALPHA with two live OPEX sites) plus a second tender, GAMMA, with
    one live OPEX site, so a switched-off ALPHA leaves one section standing."""

    def setUp(self):
        super().setUp()
        self.tender_c = Program.objects.create(name='Tender Gamma', program_type='OPEX',
                                               client_name='C', status='Active',
                                               short_tender_code='GAMMA')
        self.opex_c = self._bare(project_type='OPEX', program=self.tender_c)

    def _switch(self, program, on):
        Program.objects.filter(pk=program.pk).update(in_daily_snapshot=on)

    def _log_lines(self, program):
        return list(ActivityLog.objects.filter(entity_type='Program', entity_id=program.pk,
                                               action_code='program_edited')
                    .order_by('pk').values_list('action', flat=True))


# ---------------------------------------------------------------------------
# 1. Default and the form
# ---------------------------------------------------------------------------

class DefaultAndFormTests(SwitchFixture):

    def test_a_new_program_is_in_the_snapshot(self):
        self.assertTrue(Program.objects.create(name='New', program_type='OPEX',
                                               client_name='C').in_daily_snapshot)
        self.assertTrue(Program._meta.get_field('in_daily_snapshot').default)

    def test_the_form_has_no_switch_unless_asked(self):
        self.assertNotIn('in_daily_snapshot', ProgramForm().fields)
        self.assertNotIn('in_daily_snapshot', ProgramForm(instance=self.tender_a).fields)
        self.assertIn('in_daily_snapshot', ProgramForm(snapshot_switch=True).fields)
        self.assertEqual(SNAPSHOT_SWITCH_ROLES, frozenset({'Admin'}))

    def test_a_form_saved_without_the_switch_leaves_it_on(self):
        # The seed commands' shape: a plain dict, no checkbox key. Were the field on the
        # form it would clean to False and switch the new tender off.
        form = ProgramForm({'program_type': 'OPEX', 'name': 'Seeded', 'client_name': 'C',
                            'status': 'Draft', 'short_tender_code': 'SEEDED'})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertTrue(form.save().in_daily_snapshot)

    def test_the_create_page_has_no_switch_even_for_admin(self):
        page = _client(self.admin).get(reverse('program_create'))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, LABEL)


# ---------------------------------------------------------------------------
# 2. Who may change it, and the log
# ---------------------------------------------------------------------------

class AccessAndLogTests(SwitchFixture):

    def _url(self, program):
        return f'/programs/{program.pk}/edit/'

    def test_admin_sees_the_switch_and_a_pm_does_not(self):
        page = _client(self.admin).get(self._url(self.tender_a))
        self.assertContains(page, LABEL)
        self.assertContains(page, 'name="in_daily_snapshot"')
        # self.pm manages opex_a1, so the page opens for them, without the switch.
        page = _client(self.pm).get(self._url(self.tender_a))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, LABEL)
        self.assertNotContains(page, 'name="in_daily_snapshot"')

    def test_roles_outside_the_edit_page_are_refused(self):
        for profile in (self.ceo, self.sysadmin):
            self.assertEqual(_client(profile).get(self._url(self.tender_a)).status_code,
                             403, profile.role)

    def test_a_pm_post_cannot_switch_it_off(self):
        # No checkbox key in the body: on an Admin's form that means "unticked".
        response = _client(self.pm).post(self._url(self.tender_a),
                                         _edit_post(self.tender_a, name='Renamed'))
        self.assertEqual(response.status_code, 302)
        self.tender_a.refresh_from_db()
        self.assertEqual(self.tender_a.name, 'Renamed')     # the save did happen
        self.assertTrue(self.tender_a.in_daily_snapshot)
        self.assertFalse(any(LABEL in line for line in self._log_lines(self.tender_a)))

    def test_a_pm_post_cannot_switch_it_back_on(self):
        self._switch(self.tender_a, False)
        _client(self.pm).post(self._url(self.tender_a),
                              _edit_post(self.tender_a, in_daily_snapshot='on'))
        self.tender_a.refresh_from_db()
        self.assertFalse(self.tender_a.in_daily_snapshot)

    def test_admin_switches_it_off_and_on_and_each_change_is_logged(self):
        client = _client(self.admin)
        response = client.post(self._url(self.tender_a), _edit_post(self.tender_a))
        self.assertEqual(response.status_code, 302)
        self.tender_a.refresh_from_db()
        self.assertFalse(self.tender_a.in_daily_snapshot)
        self.assertEqual(self._log_lines(self.tender_a),
                         ["Edited Program 'Tender Alpha' Include in daily snapshot: On → Off"])

        client.post(self._url(self.tender_a), _edit_post(self.tender_a, in_daily_snapshot='on'))
        self.tender_a.refresh_from_db()
        self.assertTrue(self.tender_a.in_daily_snapshot)
        self.assertEqual(self._log_lines(self.tender_a)[-1],
                         "Edited Program 'Tender Alpha' Include in daily snapshot: Off → On")
        row = ActivityLog.objects.filter(entity_id=self.tender_a.pk).latest('pk')
        self.assertEqual((row.project_id, row.actor_id, row.entity_type),
                         (None, self.admin.pk, 'Program'))

    def test_an_admin_save_that_keeps_the_switch_logs_no_switch_line(self):
        _client(self.admin).post(self._url(self.tender_a),
                                 _edit_post(self.tender_a, name='Alpha Two',
                                            in_daily_snapshot='on'))
        self.assertEqual(self._log_lines(self.tender_a),
                         ["Edited Program 'Alpha Two' field: name"])


# ---------------------------------------------------------------------------
# 3. The snapshot
# ---------------------------------------------------------------------------

class SnapshotSwitchTests(SwitchFixture):

    def test_both_tenders_are_in_while_the_switch_is_on(self):
        payload = self._collect()
        self.assertEqual(set(payload['tender_sections']),
                         {str(self.tender_a.pk), str(self.tender_c.pk)})

    def test_a_switched_off_tender_has_no_section_headline_or_tender_row(self):
        before = self._collect()
        self._switch(self.tender_a, False)
        payload = self._collect()
        self.assertEqual(set(payload['tender_sections']), {str(self.tender_c.pk)})
        self.assertEqual(S.headline(payload), 'GAMMA 1 site')
        self.assertNotIn('ALPHA', S.headline(payload))
        names = [row['name'] for row in payload['tenders']]
        self.assertNotIn('ALPHA · Tender Alpha', names)
        self.assertIn('ALPHA · Tender Alpha', [r['name'] for r in before['tenders']])
        self.assertIn('GAMMA · Tender Gamma', names)
        # Ruling Q2: the company-wide stored figures still count ALPHA's sites.
        self.assertEqual(payload['org'], before['org'])
        self.assertEqual(payload['pms'], before['pms'])
        # The other rows (Residential, CAPEX, RESCO) are untouched too.
        self.assertEqual([r for r in payload['tenders'] if not r['name'].startswith('ALPHA')],
                         [r for r in before['tenders'] if not r['name'].startswith('ALPHA')])

    def test_a_switched_off_tender_is_not_drawn(self):
        self._switch(self.tender_a, False)
        drawn = []
        real = S._section

        # *rest: SNAP-3c added the section's "Needs action" box as a further argument.
        def record(draw, fonts, section, *rest):
            drawn.append(section['id'])
            return real(draw, fonts, section, *rest)

        with mock.patch.object(S, '_section', side_effect=record):
            images = S.render_snapshot_images(self._collect(), None)
        self.assertEqual(len(images), 1)
        self.assertEqual(drawn, [self.tender_c.pk])

    def test_every_tender_switched_off_draws_nothing(self):
        self._switch(self.tender_a, False)
        self._switch(self.tender_c, False)
        payload = self._collect()
        self.assertEqual(payload['tender_sections'], {})
        self.assertEqual(S.render_snapshot_images(payload, None), [])

    def test_the_dry_run_names_only_the_tenders_switched_on(self):
        self._switch(self.tender_a, False)
        out = io.StringIO()
        with mock.patch('django.utils.timezone.now', return_value=NOW), \
                mock.patch('projects.notifications._zeptomail_post') as post:
            call_command('send_dashboard_snapshot', '--dry-run', '--out-dir',
                         tempfile.mkdtemp(), stdout=out)
        out = out.getvalue()
        self.assertIn('1 tender(s), 1 image(s)', out)
        self.assertIn('GAMMA · Tender Gamma', out)
        self.assertNotIn('ALPHA', out)
        post.assert_not_called()
        self.assertFalse(DashboardSnapshot.objects.exists())

    def test_back_on_the_section_returns_with_no_arrows(self):
        # Yesterday's row was taken while ALPHA was off; today it is on again.
        self._switch(self.tender_a, False)
        yesterday = self._collect()
        # Give ALPHA a figure that would carry an arrow had it been compared.
        self._bare(project_type='OPEX', program=self.tender_a)
        self._switch(self.tender_a, True)
        today = self._collect()
        alpha = today['tender_sections'][str(self.tender_a.pk)]
        self.assertEqual(alpha['breakdown']['in_execution'], 3)
        deltas = S.section_deltas(alpha, yesterday)
        self.assertEqual({k: v for k, v in deltas.items() if v is not None}, {})
        # Control: against a row that had ALPHA (2 sites), the same section has an arrow.
        self._switch(self.tender_a, True)
        with_alpha = dict(yesterday, tender_sections={
            **yesterday['tender_sections'],
            str(self.tender_a.pk): dict(alpha, breakdown=dict(alpha['breakdown'],
                                                              in_execution=2))})
        self.assertEqual(S.section_deltas(alpha, with_alpha)['in_execution'], 1)


# ---------------------------------------------------------------------------
# 4. No other page reads the switch (K4)
# ---------------------------------------------------------------------------

class OtherPagesTests(SwitchFixture):

    def _pages(self):
        """What each page shows of the tenders, read as the CEO (who sees every site)."""
        client = _client(self.ceo)
        detail = client.get(f'/programs/{self.tender_a.pk}/')
        dashboard = client.get('/dashboard/sites/')
        gantt = client.get('/dashboard/tenders/gantt/')
        for page in (detail, dashboard, gantt):
            self.assertEqual(page.status_code, 200)
        return {
            'detail_breakdown': detail.context['breakdown'],
            'detail_sites': [s.pk for s in detail.context['sites']],
            'dashboard_cards': [(v.card.id, v.tender) for v in dashboard.context['cards']],
            'dashboard_kpis': [(t['label'], t['value'], t['detail'])
                               for t in dashboard.context['kpis']],
            # The tender filter's options, (Program id, label) pairs.
            'dashboard_programs': [p[0] for p in dashboard.context['programs']],
            'gantt_tenders': [t['card'].program_id for t in gantt.context['tender_cards']],
            'gantt_rows': len(gantt.context['rows']) + len(gantt.context['civil_rows']),
        }

    def test_the_tender_page_dashboard_and_gantt_ignore_the_switch(self):
        on = self._pages()
        self.assertIn(self.tender_a.pk, on['dashboard_programs'])
        self.assertIn(self.tender_a.pk, on['gantt_tenders'])
        self._switch(self.tender_a, False)
        self.assertEqual(self._pages(), on)
