"""
SNAP-1 — the daily Site Dashboard snapshot (snapshot.py, send_dashboard_snapshot) and the
two additive chokepoint arguments it needed (attachments, named_recipients).

WHAT THIS PINS
    Same figures   the organisation row equals the rendered page's tiles for a CEO, label,
                   value and sub-line; a PM row equals that PM's own page when they
                   manage by assignment only; a PM who also coordinates sees more on the
                   page than their row (assigned PM only, P2)
    Scope          test, draft, deleted and cancelled sites in no figure; each live site
                   in exactly one PM row (or "No active PM") and one tender row
    Rows           Residential (even with a tender), tender, CAPEX (standalone), RESCO
                   (no tender); "No active PM" only when needed, last; sort order
    No money       no money key or text in the payload; kpi_tiles asked with
                   sees_payments False, while the CEO's page does show the payment tile
    Viewer         the lowest-pk active CEO; none -> SnapshotError -> non-zero exit
    Images         valid PNG, width 1080, split above 18 rows, names cut with '…'
    Arrows         differences vs the previous payload; none without one or when 0
    Command        dry run writes files and nothing else; a same-day re-run overwrites;
                   paused event -> 'skipped' row and exit 0; a sent email carries the
                   PNGs; a failed send exits non-zero; --to skips the named list
    Chokepoint     an existing event's ZeptoMail payload is byte-identical (no
                   'attachments' key); existing callers still get the named list; no call
                   site but the command passes named_recipients

Today is frozen at 9 Nov 2026 by SchedFixture (through DashFixture); `NOW` is that day,
20:00 IST, the cron's time.

Run with:
    python manage.py test projects.tests_dashboard_snapshot --settings=solarpms.test_settings
"""
import io
import json
import re
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from . import notify as N
from . import snapshot as S
from .models import (DashboardSnapshot, Issue, Notification, NotificationEventSetting,
                     NotificationLog, PaymentRequest, Program, SystemSettings, Task, Vendor,
                     VendorOrder, VendorOrderSite)
from .notifications import _zeptomail_post, send_notification
from .notify_admin import add_named_recipient
from .notify_registry import get_event
from .tests_sched2_scheduler import FROZEN_TODAY
from .tests_site_dashboard import DashFixture, _fresh
from .tests_two_step_completion import _profile

EVENT = 'daily_dashboard_snapshot'
URL = '/dashboard/sites/'
NOW = timezone.make_aware(datetime(2026, 11, 9, 20, 0))   # TIME_ZONE is Asia/Kolkata
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'


def _page_tiles(profile):
    """The rendered Site Dashboard's tiles for `profile`, as (label, value, detail)."""
    client = Client()
    client.force_login(profile.user)
    response = client.get(URL)
    assert response.status_code == 200
    return [(t['label'], t['value'], t['detail']) for t in response.context['kpis']]


def _org(payload):
    return [(t['label'], t['value'], t['detail']) for t in payload['org']['tiles']]


def _row(payload, table, name):
    return next(r for r in payload[table] if r['name'] == name)


def _row_tiles(row):
    """A row's values in the page's (label, value) form, for comparing with a page."""
    return [(label, row['values'][key]) for key, label, _ in S.FIGURES]


def _walk(value):
    """Every key and every string anywhere in `value`."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _walk(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk(item)
    elif isinstance(value, str):
        yield value


class SnapshotFixture(DashFixture):
    """Six live sites covering every row rule, plus sites no figure may count.

        res      Residential, self.pm, one overdue task and one open issue
        res_t    Residential WITH a tender (tender_b, set by update()), other_pm -> the
                 Residential row
        opex_a1  OPEX, tender_a, self.pm, DC 100 / AC 80, blocked task
        opex_a2  OPEX, tender_a, other_pm, DC 50.4
        capex    CAPEX, no tender, an inactive PM -> No active PM, CAPEX (standalone)
        resco    OPEX, no tender, other_pm -> RESCO (no tender)
        hidden   a test site, a deleted site and a cancelled site, each with an overdue
                 task; a draft (never in execution)
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.gone_pm = _profile('snap_gone_pm', 'PM')
        cls.gone_pm.is_active = False
        cls.gone_pm.save(update_fields=['is_active'])
        cls.tender_a = Program.objects.create(name='Tender Alpha', program_type='OPEX',
                                              client_name='C', status='Active',
                                              short_tender_code='ALPHA')
        cls.tender_b = Program.objects.create(name='Tender Beta', program_type='OPEX',
                                              client_name='C', status='Active',
                                              short_tender_code='BETA')

    def setUp(self):
        late = FROZEN_TODAY - timedelta(days=2)
        self.res = self._bare(site_name='Res')
        self._plain_task(self._phase(self.res, 'Install', 1), 1, task_type=Task.INTERNAL,
                         due_date=late)
        Issue.objects.create(project=self.res, title='Leak', raised_by=self.pm)
        self.res_t = self._bare(site_name='Res with tender', pm=self.other_pm)
        # Project.save() refuses a Residential site on a Program, so Q4's first rule is
        # defensive; .update() bypasses save() to prove the rule holds if one ever exists.
        type(self.res_t).objects.filter(pk=self.res_t.pk).update(program=self.tender_b)
        self.opex_a1 = self._bare(project_type='OPEX', program=self.tender_a,
                                  dc_capacity_kw=Decimal('100'), ac_capacity_kw=Decimal('80'))
        self._plain_task(self._phase(self.opex_a1, 'Design', 1), 1, status=Task.BLOCKED)
        self.opex_a2 = self._bare(project_type='OPEX', program=self.tender_a, pm=self.other_pm,
                                  dc_capacity_kw=Decimal('50.4'))
        self.capex = self._bare(project_type='CAPEX', pm=self.gone_pm)
        self.resco = self._bare(project_type='OPEX', pm=self.other_pm)
        self.live = [self.res, self.res_t, self.opex_a1, self.opex_a2, self.capex, self.resco]
        for hidden in (dict(is_test=True), dict(is_deleted=True), dict(status='Cancelled')):
            site = self._bare(**hidden)
            self._plain_task(self._phase(site, 'Install', 1), 1, task_type=Task.INTERNAL,
                             due_date=late)
        self.draft = self._bare(status='Draft')
        # An open payment request, so the CEO's page draws its money tile.
        vendor = Vendor.objects.create(name='Snap Vendor')
        order = VendorOrder.objects.create(vendor=vendor, project_type='Residential',
                                           created_by=self.scm, total_amount=Decimal('500000'))
        VendorOrderSite.objects.create(order=order, project=self.res)
        PaymentRequest.objects.create(
            project=self.res, vendor=vendor, vendor_order=order, amount=Decimal('250000'),
            requested_by=self.scm.user, status=PaymentRequest.PENDING_APPROVAL)

    def _collect(self):
        return S.collect_snapshot(NOW)


# ---------------------------------------------------------------------------
# 1. The same figures as the page
# ---------------------------------------------------------------------------

class SameFiguresTests(SnapshotFixture):

    def test_org_figures_equal_the_rendered_page_for_a_ceo(self):
        page = _page_tiles(self.ceo)
        payload = self._collect()
        # The page's last tile is the CEO's payment tile; the snapshot never builds it.
        self.assertEqual(page[-1][0], 'Payments pending')
        self.assertEqual(_org(payload), page[:-1])
        self.assertEqual(payload['org']['tiles'][0]['value'], len(self.live))

    def test_a_pm_row_equals_that_pms_own_page(self):
        # self.pm manages res and opex_a1 by assignment and coordinates nothing.
        payload = self._collect()
        page = {label: value for label, value, _ in _page_tiles(self.pm)}
        row = _row(payload, 'pms', self.pm.user.get_full_name() or self.pm.user.username)
        self.assertEqual(_row_tiles(row), [(label, page[label]) for _, label, _ in S.FIGURES])
        self.assertEqual(row['values']['sites'], 2)

    def test_a_coordinated_site_is_on_the_pms_page_but_not_in_their_row(self):
        self.res.coordinators.add(self.other_pm)
        payload = self._collect()
        page = {label: value for label, value, _ in _page_tiles(self.other_pm)}
        row = _row(payload, 'pms', self.other_pm.user.get_full_name()
                   or self.other_pm.user.username)
        self.assertEqual(page['Sites in execution'], 4)   # 3 assigned + res coordinated
        self.assertEqual(row['values']['sites'], 3)
        # The row is still the dashboard's own figures for exactly the assigned sites.
        cards = [c for c in S.build_site_cards(_fresh(self.ceo.user), today=FROZEN_TODAY)
                 if c.id in {self.res_t.pk, self.opex_a2.pk, self.resco.pk}]
        self.assertEqual(row['values'], {f['key']: f['value'] for f in S._figures(cards)})

    def test_the_dc_number_behind_the_tile_text(self):
        payload = self._collect()
        tile = next(t for t in payload['org']['tiles'] if t['key'] == 'dc_kwp')
        self.assertEqual(tile['value'], '150')
        self.assertEqual(payload['org']['dc_kwp_number'], 150)


# ---------------------------------------------------------------------------
# 2. Scope and rows
# ---------------------------------------------------------------------------

class RowTests(SnapshotFixture):

    def test_test_deleted_cancelled_and_draft_sites_count_nowhere(self):
        payload = self._collect()
        org = {t['key']: t['value'] for t in payload['org']['tiles']}
        self.assertEqual(org['sites'], 6)
        self.assertEqual(org['overdue'], 1)       # res only; the hidden sites' are out
        for table in ('pms', 'tenders'):
            with self.subTest(table=table):
                self.assertEqual(sum(r['values']['sites'] for r in payload[table]), 6)
                self.assertEqual(sum(r['values']['overdue'] for r in payload[table]), 1)

    def test_tender_rows_follow_the_four_rules(self):
        payload = self._collect()
        sites = {r['name']: r['values']['sites'] for r in payload['tenders']}
        self.assertEqual(sites, {'ALPHA · Tender Alpha': 2, S.RESIDENTIAL_ROW: 2,
                                 S.CAPEX_ROW: 1, S.RESCO_ROW: 1})
        # Tender Beta's only site is Residential, so Beta has no row of its own.
        self.assertNotIn('BETA · Tender Beta', sites)
        self.assertEqual([r['name'] for r in payload['tenders']][-3:],
                         [S.RESIDENTIAL_ROW, S.CAPEX_ROW, S.RESCO_ROW])
        alpha = _row(payload, 'tenders', 'ALPHA · Tender Alpha')
        self.assertEqual(alpha['id'], self.tender_a.pk)

    def test_each_site_is_in_exactly_one_tender_row(self):
        cards = [c for c in S.build_site_cards(_fresh(self.ceo.user), today=FROZEN_TODAY)
                 if not c.is_draft]
        keys = [S._tender_key(c) for c in cards]
        self.assertEqual(len(keys), len(self.live))
        self.assertEqual(sum(r['values']['sites'] for r in self._collect()['tenders']),
                         len(self.live))

    def test_inactive_pms_sites_go_to_one_no_active_pm_row_drawn_last(self):
        payload = self._collect()
        names = [r['name'] for r in payload['pms']]
        self.assertEqual(names[-1], S.NO_ACTIVE_PM)
        self.assertNotIn('Snap_Gone_Pm', names)
        self.assertEqual(_row(payload, 'pms', S.NO_ACTIVE_PM)['values']['sites'], 1)

    def test_no_active_pm_row_is_absent_when_every_site_has_one(self):
        self.capex.assigned_pm = self.pm
        self.capex.save(update_fields=['assigned_pm'])
        self.assertNotIn(S.NO_ACTIVE_PM, [r['name'] for r in self._collect()['pms']])

    def test_a_coordinator_is_never_a_row(self):
        self.res.coordinators.add(self.coord)
        names = [r['name'] for r in self._collect()['pms']]
        self.assertNotIn(self.coord.user.get_full_name() or self.coord.user.username, names)

    def test_pm_rows_sort_by_overdue_then_gone_quiet(self):
        payload = self._collect()
        rows = [r for r in payload['pms'] if r['name'] != S.NO_ACTIVE_PM]
        keys = [(-r['values']['overdue'], -r['values']['quiet']) for r in rows]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(rows[0]['id'], self.pm.user_id)   # the one overdue task is res


# ---------------------------------------------------------------------------
# 3. No money, and whose view
# ---------------------------------------------------------------------------

class MoneyAndViewerTests(SnapshotFixture):

    MONEY = re.compile(r'payment|amount|₹|lakh|contract|invoice|price', re.I)

    def test_no_money_key_or_text_in_the_payload(self):
        payload = json.loads(json.dumps(self._collect()))   # as the JSONField stores it
        self.assertEqual(set(payload), {'snapshot_date', 'snapshot_date_label', 'taken_at',
                                        'taken_at_label', 'dates_as_of', 'viewer', 'org',
                                        'pms', 'tenders'})
        self.assertEqual(set(payload['org']), {'tiles', 'dc_kwp_number'})
        for tile in payload['org']['tiles']:
            self.assertEqual(set(tile), {'key', 'label', 'value', 'detail', 'tone'})
        for row in payload['pms'] + payload['tenders']:
            self.assertEqual(set(row), {'id', 'name', 'values'})
            self.assertEqual(tuple(row['values']), S.FIGURE_KEYS)
        for text in _walk(payload):
            self.assertIsNone(self.MONEY.search(text), text)

    def test_tiles_are_always_asked_for_without_payments(self):
        with mock.patch.object(S, 'kpi_tiles', wraps=S.kpi_tiles) as spy:
            self._collect()
        self.assertTrue(spy.call_args_list)
        for call in spy.call_args_list:
            self.assertIs(call.args[2], False)

    def test_the_viewer_is_the_lowest_pk_active_ceo(self):
        later = _profile('snap_ceo2', 'CEO')
        self.assertEqual(S.snapshot_viewer().pk, self.ceo.user_id)
        self.ceo.is_active = False
        self.ceo.save(update_fields=['is_active'])
        self.assertEqual(S.snapshot_viewer().pk, later.user_id)

    def test_no_active_ceo_is_an_error(self):
        type(self.ceo).objects.filter(role='CEO').update(is_active=False)
        with self.assertRaisesRegex(S.SnapshotError, 'no active CEO'):
            S.snapshot_viewer()

    def test_labels(self):
        payload = self._collect()
        self.assertEqual(payload['snapshot_date'], '2026-11-09')
        self.assertEqual(payload['taken_at_label'], '9 Nov 2026, 8:00 PM IST')
        self.assertIn('text', payload['dates_as_of'])
        self.assertEqual(S.headline(payload),
                         '6 sites in execution · 1 overdue tasks · 0 gone quiet')


# ---------------------------------------------------------------------------
# 4. Rendering and arrows (pure: no database)
# ---------------------------------------------------------------------------

def _payload(pm_rows=3, tender_rows=2, **org):
    values = dict(sites=10, dc_kwp='1,234', behind=1, civil=2, overdue=3, blocked=0,
                  issues=4, quiet=5)
    values.update(org)
    tiles = [{'key': key, 'label': label, 'value': values[key], 'detail': 'detail',
              'tone': S.TONES.get(key, '')} for key, label, _ in S.FIGURES]

    def rows(n, prefix):
        return [{'id': i, 'name': f'{prefix} {i} with a name far too long to fit the column',
                 'values': dict(values)} for i in range(n)]
    return {'snapshot_date': '2026-11-09', 'snapshot_date_label': '9 Nov 2026',
            'taken_at': NOW.isoformat(), 'taken_at_label': '9 Nov 2026, 8:00 PM IST',
            'dates_as_of': {'text': 'Dates as of 9 Nov, 06:31', 'stale': False},
            'viewer': 'ceo', 'org': {'tiles': tiles, 'dc_kwp_number': 1234},
            'pms': rows(pm_rows, 'PM'), 'tenders': rows(tender_rows, 'Tender')}


class RenderTests(SimpleTestCase):

    def _size(self, png):
        from PIL import Image
        self.assertTrue(png.startswith(PNG_SIGNATURE))
        return Image.open(io.BytesIO(png)).size

    def test_two_valid_pngs_1080_wide(self):
        images = S.render_snapshot_images(_payload(), None)
        self.assertEqual(len(images), 2)
        for png in images:
            self.assertEqual(self._size(png)[0], S.IMAGE_WIDTH)

    def test_eighteen_rows_fit_nineteen_split(self):
        self.assertEqual(len(S.render_snapshot_images(_payload(pm_rows=18), None)), 2)
        self.assertEqual(len(S.render_snapshot_images(_payload(pm_rows=19), None)), 3)
        self.assertEqual(len(S.render_snapshot_images(_payload(pm_rows=19, tender_rows=37),
                                                      None)), 5)
        self.assertEqual(S._caption('By PM', 2, 2), 'By PM (2/2)')
        self.assertEqual(S._caption('By PM', 1, 1), 'By PM')

    def test_empty_tables_still_render(self):
        images = S.render_snapshot_images(_payload(pm_rows=0, tender_rows=0, sites=0), None)
        self.assertEqual(len(images), 2)

    def test_long_names_are_cut_with_an_ellipsis_never_wrapped(self):
        from PIL import Image, ImageDraw
        draw = ImageDraw.Draw(Image.new('RGB', (10, 10)))
        font = S._fonts().cell_bold
        cut = S.fit_text(draw, 'Rashtrapati Bhavan Estate Delhi Tender', font, 200)
        self.assertTrue(cut.endswith('…'))
        self.assertLessEqual(draw.textlength(cut, font=font), 200)
        self.assertEqual(S.fit_text(draw, 'Short', font, 200), 'Short')

    def test_rendering_reads_no_database(self):
        # SimpleTestCase refuses any query, so a renderer that touched the database
        # would fail every test in this class; this one says so out loud.
        S.render_snapshot_images(_payload(), _payload(overdue=1))

    def test_deltas(self):
        now = _payload(sites=12, overdue=3, quiet=5)
        now['org']['dc_kwp_number'] = 1300
        before = _payload(sites=10, overdue=7, quiet=5)
        deltas = S.org_deltas(now, before)
        self.assertEqual(deltas['sites'], 2)
        self.assertEqual(deltas['overdue'], -4)
        self.assertIsNone(deltas['quiet'])          # no change: no arrow
        self.assertEqual(deltas['dc_kwp'], 66)      # 1300 - 1234
        self.assertEqual(S.delta_text(2), '▲ 2')
        self.assertEqual(S.delta_text(-4), '▼ 4')
        self.assertEqual(S.delta_text(None), '')

    def test_no_previous_payload_no_arrows(self):
        self.assertEqual(set(S.org_deltas(_payload(), None).values()), {None})


# ---------------------------------------------------------------------------
# 5. The command
# ---------------------------------------------------------------------------

@override_settings(ZEPTOMAIL_API_KEY='test-key', ZEPTOMAIL_FROM_EMAIL='from@example.com')
class CommandTests(SnapshotFixture):

    def setUp(self):
        super().setUp()
        self.zuber = _profile('snap_zuber', 'Admin')
        self.zuber.user.email = 'zuber@example.com'
        self.zuber.user.save(update_fields=['email'])
        add_named_recipient(EVENT, self.zuber, self.admin, 'Daily snapshot to Zuber first')
        self.dir = tempfile.mkdtemp()

    def _call(self, *args):
        out = io.StringIO()
        with mock.patch('django.utils.timezone.now', return_value=NOW):
            call_command('send_dashboard_snapshot', *args, stdout=out)
        return out.getvalue()

    def _switch_on(self, master=True):
        NotificationEventSetting.objects.create(event_code=EVENT, channel='email',
                                                is_active=True, changed_by=self.admin,
                                                reason='switched on for the test')
        SystemSettings.objects.update_or_create(pk=1, defaults={'email_enabled': master})

    def _email_rows(self):
        return list(NotificationLog.objects.filter(event_code=EVENT, channel='email')
                    .values_list('recipient__user__username', 'status', 'error_detail'))

    def test_dry_run_writes_files_and_nothing_else(self):
        with mock.patch('projects.notifications._zeptomail_post') as post:
            out = self._call('--dry-run', '--out-dir', self.dir)
        files = sorted(Path(self.dir).glob('*.png'))
        self.assertEqual(len(files), 2)
        self.assertTrue(files[0].read_bytes().startswith(PNG_SIGNATURE))
        self.assertIn('Dry run: no row written, nothing sent.', out)
        self.assertIn('Overdue tasks', out)
        self.assertFalse(DashboardSnapshot.objects.exists())
        self.assertFalse(NotificationLog.objects.filter(event_code=EVENT).exists())
        self.assertFalse(Notification.objects.exists())
        post.assert_not_called()

    def test_dry_run_needs_an_out_dir(self):
        with self.assertRaisesRegex(CommandError, '--out-dir'):
            self._call('--dry-run')

    def test_paused_event_saves_the_row_logs_skipped_and_exits_zero(self):
        out = self._call()
        self.assertEqual(DashboardSnapshot.objects.count(), 1)
        row = DashboardSnapshot.objects.get()
        self.assertEqual((str(row.snapshot_date), row.created_by_command),
                         ('2026-11-09', 'send_dashboard_snapshot'))
        self.assertEqual(self._email_rows(), [('snap_zuber', 'skipped', 'event paused')])
        self.assertIn('skipped: event paused.', out)
        # In-app is never paused: one line, linking to the Site Dashboard.
        notice = Notification.objects.get(recipient=self.zuber)
        self.assertEqual(notice.link, URL)
        self.assertNotIn('\n', notice.message)

    def test_same_day_re_run_overwrites(self):
        self._call()
        out = self._call()
        self.assertEqual(DashboardSnapshot.objects.count(), 1)
        self.assertIn('Overwrote the snapshot row for 2026-11-09', out)

    def test_previous_day_row_is_compared(self):
        DashboardSnapshot.objects.create(snapshot_date=FROZEN_TODAY - timedelta(days=1),
                                         taken_at=NOW - timedelta(days=1),
                                         payload=_payload(), created_by_command='test')
        with mock.patch('projects.management.commands.send_dashboard_snapshot.'
                        'render_snapshot_images', wraps=S.render_snapshot_images) as render:
            out = self._call()
        self.assertIn('previous snapshot: 2026-11-08', out)
        self.assertEqual(render.call_args.args[1]['org']['dc_kwp_number'], 1234)

    def test_sent_email_carries_the_pngs(self):
        self._switch_on()
        with mock.patch('projects.notifications._zeptomail_post',
                        return_value=(True, '')) as post:
            out = self._call()
        self.assertIn('Sent 1 email(s).', out)
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'zuber@example.com')
        self.assertEqual(args[2], 'Site Dashboard – daily snapshot, 9 Nov 2026')
        files = kwargs['attachments']
        self.assertEqual([f['name'] for f in files], ['site-dashboard-2026-11-09-1.png',
                                                      'site-dashboard-2026-11-09-2.png'])
        for item in files:
            self.assertEqual(item['mime_type'], 'image/png')
            self.assertTrue(item['content'].startswith(PNG_SIGNATURE))
        self.assertEqual(self._email_rows(), [('snap_zuber', 'sent', '')])

    def test_master_switch_off_is_skipped_exit_zero(self):
        self._switch_on(master=False)
        out = self._call()
        self.assertIn('skipped: Master switch off.', out)

    def test_failed_send_exits_non_zero(self):
        self._switch_on()
        with mock.patch('projects.notifications._zeptomail_post',
                        return_value=(False, 'HTTP 500: boom')):
            with self.assertRaisesRegex(CommandError, 'failed: HTTP 500: boom'):
                self._call()

    def test_to_sends_to_that_user_only_not_the_named_list(self):
        self._switch_on()
        self.ceo.user.email = 'ceo@example.com'
        self.ceo.user.save(update_fields=['email'])
        with mock.patch('projects.notifications._zeptomail_post', return_value=(True, '')):
            self._call('--to', self.ceo.user.username)
        self.assertEqual(self._email_rows(), [(self.ceo.user.username, 'sent', '')])
        self.assertFalse(Notification.objects.filter(recipient=self.zuber).exists())

    def test_to_a_deactivated_or_unknown_user_is_refused(self):
        with self.assertRaisesRegex(CommandError, 'deactivated'):
            self._call('--to', self.gone_pm.user.username)
        with self.assertRaisesRegex(CommandError, 'no user'):
            self._call('--to', 'nobody_here')

    def test_no_active_ceo_exits_non_zero(self):
        type(self.ceo).objects.filter(role='CEO').update(is_active=False)
        with self.assertRaisesRegex(CommandError, 'snapshot not taken: no active CEO'):
            self._call()
        self.assertFalse(DashboardSnapshot.objects.exists())

    def test_an_empty_named_list_is_skipped(self):
        from .notify_admin import remove_named_recipient
        from .models import NotificationEventRecipient
        row = NotificationEventRecipient.objects.get(event_code=EVENT)
        remove_named_recipient(row, self.admin, 'No longer wanted for the test')
        out = self._call()
        self.assertIn('skipped: nobody to send to', out)


# ---------------------------------------------------------------------------
# 6. The chokepoint stays as it was for every existing caller
# ---------------------------------------------------------------------------

@override_settings(ZEPTOMAIL_API_KEY='k', ZEPTOMAIL_FROM_EMAIL='from@example.com',
                   NOTIFY_REPLY_TO='reply@example.com')
class ChokepointTests(TestCase):

    def _post(self, **kwargs):
        response = mock.Mock(status_code=200, text='')
        with mock.patch('projects.notifications.requests.post',
                        return_value=response) as post:
            ok, _ = _zeptomail_post('to@example.com', 'To', 'Subject', 'Text', '<p>Html</p>',
                                    **kwargs)
        self.assertTrue(ok)
        return post.call_args.kwargs['json']

    def test_payload_without_attachments_is_byte_identical_to_before(self):
        # The exact payload _zeptomail_post built before SNAP-1, key order included
        # (requests serialises the dict in that order).
        before = {
            'from': {'address': 'from@example.com', 'name': 'Horizon Solar'},
            'to': [{'email_address': {'address': 'to@example.com', 'name': 'To'}}],
            'subject': 'Subject',
            'textbody': 'Text',
            'reply_to': [{'address': 'reply@example.com', 'name': 'Horizon Solar PMS'}],
            'htmlbody': '<p>Html</p>',
        }
        self.assertEqual(json.dumps(self._post()), json.dumps(before))
        self.assertNotIn('attachments', self._post(attachments=None))
        self.assertNotIn('attachments', self._post(attachments=[]))

    def test_attachments_are_base64_in_zeptomails_shape(self):
        payload = self._post(attachments=[{'name': 'a.png', 'mime_type': 'image/png',
                                           'content': b'\x89PNG'}])
        self.assertEqual(payload['attachments'],
                         [{'name': 'a.png', 'mime_type': 'image/png', 'content': 'iVBORw=='}])

    def test_an_existing_send_calls_the_provider_exactly_as_before(self):
        person = _profile('snap_cp', 'SCM')
        person.user.email = 'cp@example.com'
        person.user.save(update_fields=['email'])
        SystemSettings.objects.update_or_create(pk=1, defaults={'email_enabled': True})
        with mock.patch('projects.notifications._zeptomail_post',
                        return_value=(True, '')) as post:
            send_notification(person, 'Body', channels=['email'], subject='S',
                              html_message='<p>B</p>')
        self.assertEqual(post.call_args.args,
                         ('cp@example.com', person.user.get_full_name(), 'S', 'Body',
                          '<p>B</p>'))
        self.assertEqual(post.call_args.kwargs, {})

    def test_existing_callers_still_get_the_named_list(self):
        # grn_recorded is the event that opted in before SNAP-1; its caller passes no
        # named_recipients argument, so the Admin-named person is still told.
        admin = _profile('snap_cp_admin', 'Admin')
        named = _profile('snap_cp_named', 'Finance')
        add_named_recipient('grn_recorded', named, admin, 'Finance reconciles receipts')
        values = {k: v for k, v in get_event('grn_recorded').samples().items()
                  if k not in ('link', 'recipient_name')}
        with self.captureOnCommitCallbacks(execute=True):
            N.notify('grn_recorded', recipients=[], values=values)
        self.assertTrue(Notification.objects.filter(recipient=named).exists())

    def test_named_recipients_false_skips_the_list_for_that_send_only(self):
        admin = _profile('snap_cp_admin2', 'Admin')
        named = _profile('snap_cp_named2', 'Finance')
        other = _profile('snap_cp_other', 'SCM')
        add_named_recipient(EVENT, named, admin, 'Daily snapshot recipient here')
        values = {k: v for k, v in get_event(EVENT).samples().items()
                  if k not in ('link', 'recipient_name')}
        with self.captureOnCommitCallbacks(execute=True):
            N.notify(EVENT, recipients=[other], values=values, named_recipients=False)
        self.assertFalse(Notification.objects.filter(recipient=named).exists())
        self.assertTrue(Notification.objects.filter(recipient=other).exists())
        with self.captureOnCommitCallbacks(execute=True):
            N.notify(EVENT, recipients=[], values=values)
        self.assertTrue(Notification.objects.filter(recipient=named).exists())

    def test_only_the_snapshot_command_passes_the_new_arguments(self):
        root = Path(__file__).resolve().parent
        callers = set()
        for path in root.rglob('*.py'):
            if path.name.startswith('tests') or 'migrations' in path.parts:
                continue
            text = path.read_text(encoding='utf-8')
            if re.search(r"named_recipients['\"]?\s*[:=]\s*False", text) \
                    or re.search(r'\battachments=attachments\b', text):
                callers.add(path.relative_to(root).as_posix())
        # notify.py and notifications.py hand the arguments on; the command is the only
        # place that starts them.
        self.assertEqual(callers, {'notify.py', 'notifications.py',
                                   'management/commands/send_dashboard_snapshot.py'})

    def test_the_event_is_registered_paused_not_deferred_named(self):
        event = get_event(EVENT)
        self.assertEqual((event.channels, event.default_state, event.deferred,
                          event.named_recipients), (('email',), 'paused', False, True))
