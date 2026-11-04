"""
NTF-DCG Session 3b — dc_created / dc_updated: WhatsApp (and in-app) to the site's Site
Engineers when SCM raises or materially edits a delivery challan.

WHAT IS PINNED HERE:
  * the registry: both events import with the approved texts verbatim, in-app +
    WhatsApp only, deferred, WhatsApp paused by default; the header may end in its
    variable (D1);
  * recipients (D3): open-task Site Engineers only (Done / Not Applicable / other
    sites' engineers / the site's QA/QC engineer / inactive logins excluded); none ⇒
    the assigned PM alone;
  * create (D4): one deferred WhatsApp per recipient, values in their slots, header =
    site code; paused ⇒ 'skipped' rows and no send; a rolled-back create tells nobody;
  * edit (D5): a line change, an added or removed line and a date change fire
    dc_updated; a header-only edit and an edit of a Received or Rejected challan do not;
  * values (D6-D8): material summary (pk order, no trailing .0, five lines then
    "+N more item(s)", whole lines moved so it never needs the mid-value cut), the
    delivery text's three forms, every site-name branch (Q6), the first name;
  * no price: the summary reads three columns, pinned by the SQL it runs;
  * the Admin WhatsApp card (D9): no email preview, screen test or draft; the Interakt
    template, the W7 command and the last test shown; a WhatsApp draft refused.

No HTTP leaves the test: requests.post in notifications is patched and Session.request
raises. NOTIFY_DEFERRED_SYNC (test_settings) runs the deferred job inline.

Run with:
    python manage.py test projects.tests_delivery_notify --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

import requests
from django.contrib.auth.models import User
from django.db import connection, transaction
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from . import delivery_notify as D
from .models import (
    DCLineItem, DeliveryChallan, NotificationEventSetting, NotificationLog,
    NotificationTemplate, Project, ProjectPhase, SiteQaqcAssignment, SystemSettings, Task,
    recalculate_dc_status,
)
from .notify_admin import WHATSAPP_TEST_MARKER
from .notify_registry import NOTIFICATION_EVENTS, _build_registry
from .whatsapp_params import BODY_VALUE_LIMIT

REASON = 'Vendor sent a corrected challan copy'

APPROVED = {
    'dc_created': (
        'Material dispatch – {{1}}',
        'Hello {{1}},\n\nDelivery challan {{2}} has been created for site {{3}}.\n\n'
        'Material: {{4}}\nExpected delivery: {{5}}\n\n'
        'Please check the challan and be ready to receive the material: {{6}}\n\nThanks.'),
    'dc_updated': (
        'Challan updated – {{1}}',
        'Hello {{1}},\n\nDelivery challan {{2}} for site {{3}} has been updated by SCM.\n\n'
        'Updated material: {{4}}\nExpected delivery: {{5}}\n\n'
        'Please refer to the latest challan before receiving the material: {{6}}.\n\nThanks'),
}


def _profile(username, role, first_name=None, phone='', active=True):
    """A post_save signal creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(
        username=username, password='x',
        first_name=username.title() if first_name is None else first_name,
        last_name='Test', email=f'{username}@example.com')
    profile = user.profile
    profile.role = role
    profile.phone_number = phone
    profile.is_active = active
    profile.save()
    return profile


def _project(project_id, pm=None, project_type='Residential', **fields):
    return Project.objects.create(
        project_id=project_id, customer_name=fields.pop('customer_name', 'Sharma Residence'),
        customer_phone='9876543210', site_address='1 Sun Road',
        city=fields.pop('city', 'Delhi'), project_type=project_type,
        dc_capacity_kw=Decimal('5.00'), status='Active', assigned_pm=pm,
        target_commissioning_date=date.today() + timedelta(days=90), **fields)


def _task(phase, holder, order, status=Task.NOT_STARTED, role=Task.SITE_ENGINEER, **extra):
    return Task.objects.create(phase=phase, task_name=f'Task {order}', task_order=order,
                               assigned_role=role, assigned_to=holder, status=status,
                               **extra)


def _line(description, qty, unit='Nos'):
    return (description, Decimal(qty), unit)


# ---------------------------------------------------------------------------
# Pure values
# ---------------------------------------------------------------------------

class MaterialSummaryTests(SimpleTestCase):

    def test_lines_in_order_with_quantities_without_trailing_zeros(self):
        self.assertEqual(
            D.material_summary([_line('DC cable', '2000.00', 'Mtr'),
                                _line('MC4 Connector', '12.50', 'Pair'),
                                _line('Ferrules', '6', 'pkt')]),
            'DC cable 2000 Mtr; MC4 Connector 12.5 Pair; Ferrules 6 pkt')

    def test_the_snapshot_string_quantity_reads_the_same(self):
        self.assertEqual(D.quantity_text('12.00'), '12')
        self.assertEqual(D.quantity_text('0.50'), '0.5')
        self.assertEqual(D.quantity_text(Decimal('100.00')), '100')

    def test_seven_lines_name_five_then_plus_two_more_items(self):
        lines = [_line(f'Item {i}', '1') for i in range(1, 8)]
        self.assertEqual(D.material_summary(lines),
                         'Item 1 1 Nos; Item 2 1 Nos; Item 3 1 Nos; Item 4 1 Nos; '
                         'Item 5 1 Nos; +2 more items')

    def test_six_lines_say_one_more_item_singular(self):
        lines = [_line(f'Item {i}', '1') for i in range(1, 7)]
        self.assertTrue(D.material_summary(lines).endswith('; +1 more item'))

    def test_five_lines_have_no_tail(self):
        lines = [_line(f'Item {i}', '1') for i in range(1, 6)]
        self.assertNotIn('more', D.material_summary(lines))

    def test_long_lines_move_whole_into_the_count_and_never_need_the_cut(self):
        # Q7: three 100-character descriptions cannot all fit in 250, so the summary
        # names fewer lines and counts the rest — never a line cut mid-way.
        lines = [_line('X' * 100, '1') for _ in range(7)]
        summary = D.material_summary(lines)
        self.assertLessEqual(len(summary), BODY_VALUE_LIMIT)
        self.assertNotIn('…', summary)
        self.assertEqual(summary, f'{"X" * 100} 1 Nos; {"X" * 100} 1 Nos; +5 more items')

    def test_one_overlong_first_line_is_left_for_the_w4_backstop(self):
        summary = D.material_summary([_line('Y' * 300, '1'), _line('Z', '1')])
        self.assertTrue(summary.startswith('Y' * 300))
        self.assertTrue(summary.endswith('+1 more item'))

    def test_newlines_in_a_description_are_cleaned_before_measuring(self):
        self.assertEqual(D.material_summary([_line('Mono\nPERC', '2')]), 'Mono PERC 2 Nos')


class DeliveryTextTests(SimpleTestCase):

    def test_expected_date(self):
        self.assertEqual(D.delivery_text(date(2026, 11, 8), date(2026, 11, 4)), '08 Nov 2026')

    def test_blank_expected_date_falls_back_to_the_dispatch_date(self):
        self.assertEqual(D.delivery_text(None, date(2026, 11, 4)),
                         'Not given (dispatched 04 Nov 2026)')

    def test_both_blank(self):
        self.assertEqual(D.delivery_text(None, None), 'Not given')


class SiteNameTests(SimpleTestCase):

    def name(self, **fields):
        base = dict(project_id='SITE01', project_type='OPEX', site_name=None,
                    customer_name='IPGCL', city='Delhi')
        base.update(fields)
        return D.site_display_name(Project(**base))

    def test_site_name_wins_on_every_type(self):
        for kind in ('Residential', 'OPEX', 'CAPEX'):
            self.assertEqual(self.name(project_type=kind, site_name=' Rashtrapati Udyan '),
                             'Rashtrapati Udyan')

    def test_residential_is_its_customer(self):
        self.assertEqual(self.name(project_type='Residential', customer_name='R. Chilkoti'),
                         'R. Chilkoti')

    def test_residential_with_no_customer_is_the_project_id(self):
        self.assertEqual(self.name(project_type='Residential', customer_name=''), 'SITE01')

    def test_tender_site_is_customer_and_city(self):
        self.assertEqual(self.name(), 'IPGCL, Delhi')
        self.assertEqual(self.name(project_type='CAPEX'), 'IPGCL, Delhi')

    def test_tender_site_with_one_part_blank_shows_the_other(self):
        self.assertEqual(self.name(city=''), 'IPGCL')
        self.assertEqual(self.name(customer_name=''), 'Delhi')

    def test_tender_site_with_both_blank_is_the_project_id(self):
        self.assertEqual(self.name(customer_name='', city=''), 'SITE01')

    def test_site_code_is_the_project_id(self):
        self.assertEqual(D.site_code(Project(project_id='UKRU001', site_code='UKRU001')),
                         'UKRU001')
        self.assertEqual(D.site_code(Project(project_id='HRP-RES-2026-012')),
                         'HRP-RES-2026-012')


class MaterialChangeTests(SimpleTestCase):

    def snap(self, lines, expected='2026-11-08', **header):
        head = {'vendor': None, 'dc_number': 'D1', 'dc_date': '2026-11-04',
                'expected_delivery_date': expected, 'issued_from_warehouse': None,
                'po_number': '', 'notes': ''}
        head.update(header)
        return {'schema': 1, 'header': head, 'lines': lines}

    def line(self, pk, qty='2.00', receipt=False, **fields):
        base = {'id': pk, 'boq_category': 'BOS', 'item_description': 'Cable',
                'ordered_quantity': qty, 'unit': 'Mtr', 'has_receipt': receipt}
        base.update(fields)
        return base

    def test_header_only_is_not_a_change(self):
        before = self.snap([self.line(1)])
        after = self.snap([self.line(1)], dc_number='D2', notes='x', po_number='PO',
                          vendor={'id': 3, 'name': 'V'}, dc_date='2026-11-03')
        self.assertFalse(D.is_material_change(before, after))

    def test_each_line_field_is_a_change(self):
        before = self.snap([self.line(1)])
        for field, value in (('boq_category', 'Inverter'), ('item_description', 'Pipe'),
                             ('ordered_quantity', '3.00'), ('unit', 'Nos')):
            with self.subTest(field=field):
                self.assertTrue(D.is_material_change(
                    before, self.snap([self.line(1, **{field: value})])))

    def test_added_or_removed_line_is_a_change(self):
        one, two = self.snap([self.line(1)]), self.snap([self.line(1), self.line(2)])
        self.assertTrue(D.is_material_change(one, two))
        self.assertTrue(D.is_material_change(two, one))

    def test_expected_date_is_a_change_both_ways(self):
        before = self.snap([self.line(1)])
        self.assertTrue(D.is_material_change(before, self.snap([self.line(1)], expected=None)))
        self.assertTrue(D.is_material_change(self.snap([self.line(1)], expected=None), before))

    def test_receipt_flag_alone_is_not_a_change(self):
        self.assertFalse(D.is_material_change(self.snap([self.line(1)]),
                                              self.snap([self.line(1, receipt=True)])))


class RegistryTests(SimpleTestCase):

    def test_both_events_carry_the_approved_texts_verbatim(self):
        for code, (header, body) in APPROVED.items():
            with self.subTest(code=code):
                spec = NOTIFICATION_EVENTS[code].whatsapp
                self.assertEqual((spec.template, spec.language), (code, 'en'))
                self.assertEqual(spec.header_text, header)
                self.assertEqual(spec.body_text, body)
                self.assertEqual(spec.param_count, 7)
                self.assertIn('–', spec.header_text)

    def test_in_app_and_whatsapp_only_deferred_and_paused_by_default(self):
        for code in APPROVED:
            event = NOTIFICATION_EVENTS[code]
            self.assertEqual(event.channels, ('whatsapp',))
            self.assertTrue(event.deferred)
            self.assertEqual(event.default_state_for('whatsapp'), 'paused')

    def test_the_registry_rebuilds_with_both_approved_texts(self):
        # The import-time check, run again on the two events alone: D1's header ending
        # in its variable passes, and every body rule still holds.
        built = _build_registry([NOTIFICATION_EVENTS['dc_created'],
                                 NOTIFICATION_EVENTS['dc_updated']])
        self.assertEqual(sorted(built), ['dc_created', 'dc_updated'])

    def test_the_builder_puts_each_value_in_its_registered_slot(self):
        values = {'site_code': 'UK1', 'recipient_first_name': 'Asha', 'dc_number': '004',
                  'site_name': 'Udyan', 'material_summary': 'Cable 2 Mtr',
                  'delivery_text': '08 Nov 2026', 'link': 'https://x/dc/'}
        spec = NOTIFICATION_EVENTS['dc_created'].whatsapp
        self.assertEqual(spec.params(values), ['UK1', 'Asha', '004', 'Udyan', 'Cable 2 Mtr',
                                               '08 Nov 2026', 'https://x/dc/'])


class NoPriceTests(TestCase):

    def test_the_summary_reads_three_columns_and_no_price_like_one(self):
        self.assertEqual(D.SUMMARY_FIELDS, ('item_description', 'ordered_quantity', 'unit'))
        for name in D.SUMMARY_FIELDS:
            for word in ('rate', 'price', 'amount', 'cost', 'value', 'total'):
                self.assertNotIn(word, name)

    def test_the_line_query_selects_only_those_columns(self):
        project = _project('NP-1')
        challan = DeliveryChallan.objects.create(project=project, dc_number='N1',
                                                 dc_date=date.today())
        DCLineItem.objects.create(challan=challan, boq_category='BOS',
                                  item_description='Cable', ordered_quantity=Decimal('2'),
                                  unit='Mtr')
        with CaptureQueriesContext(connection) as queries:
            lines = D.lines_from_db(challan)
        self.assertEqual(lines, [('Cable', Decimal('2.00'), 'Mtr')])
        [query] = queries.captured_queries
        select = query['sql'].split(' FROM ')[0].lower()
        for column in ('item_description', 'ordered_quantity', 'unit'):
            self.assertIn(column, select)
        for column in ('received_quantity', 'damaged_quantity', 'grn_', 'condition',
                       'rate', 'price', 'amount'):
            self.assertNotIn(column, select)


# ---------------------------------------------------------------------------
# Recipients
# ---------------------------------------------------------------------------

class RecipientTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.pm = _profile('dn_pm', 'PM')
        cls.site = _project('DN-SITE', pm=cls.pm)
        cls.phase = ProjectPhase.objects.create(project=cls.site, phase_name='Install',
                                                phase_order=1)

    def names(self, project=None):
        return [p.user.username for p in D.site_engineers_for_delivery(project or self.site)]

    def test_open_task_engineers_only_each_once(self):
        alice = _profile('dn_alice', 'Site Engineer')
        _task(self.phase, alice, 1, Task.IN_PROGRESS)
        _task(self.phase, alice, 2, Task.BLOCKED)
        done = _profile('dn_done', 'Site Engineer')
        _task(self.phase, done, 3, Task.DONE)
        not_applicable = _profile('dn_na', 'Site Engineer')
        _task(self.phase, not_applicable, 4, Task.NOT_STARTED, is_not_applicable=True)
        self.assertEqual(self.names(), ['dn_alice'])

    def test_a_non_engineer_holding_an_open_task_is_not_told(self):
        scm = _profile('dn_scm', 'SCM')
        _task(self.phase, scm, 1, role=Task.SCM)
        _task(self.phase, _profile('dn_se', 'Site Engineer'), 2)
        self.assertEqual(self.names(), ['dn_se'])

    def test_no_engineer_means_the_assigned_pm_alone(self):
        coordinator = _profile('dn_coord', 'Project Coordinator')
        self.site.coordinators.add(coordinator)
        _task(self.phase, _profile('dn_done2', 'Site Engineer'), 1, Task.DONE)
        self.assertEqual(self.names(), ['dn_pm'])

    def test_no_engineer_and_no_pm_is_nobody(self):
        self.assertEqual(self.names(_project('DN-NOPM')), [])

    def test_inactive_engineers_fall_back_to_the_pm(self):
        gone = _profile('dn_gone', 'Site Engineer', active=False)
        _task(self.phase, gone, 1)
        left = _profile('dn_left', 'Site Engineer')
        left.user.is_active = False
        left.user.save(update_fields=['is_active'])
        _task(self.phase, left, 2)
        self.assertEqual(self.names(), ['dn_pm'])

    def test_an_engineer_on_another_site_is_not_told(self):
        other = _project('DN-OTHER')
        other_phase = ProjectPhase.objects.create(project=other, phase_name='I', phase_order=1)
        _task(other_phase, _profile('dn_elsewhere', 'Site Engineer'), 1)
        self.assertEqual(self.names(), ['dn_pm'])

    def test_the_sites_qaqc_engineer_is_left_out(self):
        tender = _project('DN-OPEX', pm=self.pm, project_type='OPEX', site_code='DN-OPEX')
        phase = ProjectPhase.objects.create(project=tender, phase_name='I', phase_order=1)
        qaqc = _profile('dn_qaqc', 'Site Engineer')
        _task(phase, qaqc, 1, role=Task.PM)
        _task(phase, _profile('dn_works', 'Site Engineer'), 2)
        SiteQaqcAssignment.objects.create(project=tender, engineer=qaqc, assigned_by=self.pm)
        self.assertEqual(self.names(tender), ['dn_works'])


# ---------------------------------------------------------------------------
# Sending — through the real create view and the real edit save
# ---------------------------------------------------------------------------

@override_settings(INTERAKT_API_KEY='test-key')
class SendBase(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.scm = _profile('dn_scm_user', 'SCM')
        cls.admin = _profile('dn_admin', 'Admin')
        cls.pm = _profile('dn_site_pm', 'PM', phone='9876500009')
        cls.alice = _profile('dn_se_alice', 'Site Engineer', first_name='Alice Mary',
                             phone='9876500001')
        cls.bob = _profile('dn_se_bob', 'Site Engineer', first_name='', phone='9876500002')
        cls.site = _project('DNS-01', pm=cls.pm, site_name='Udyan Block A')
        cls.phase = ProjectPhase.objects.create(project=cls.site, phase_name='Install',
                                                phase_order=1)
        _task(cls.phase, cls.alice, 1, Task.IN_PROGRESS)
        _task(cls.phase, cls.bob, 2)

    def setUp(self):
        switches = SystemSettings.get()
        switches.whatsapp_enabled = True
        switches.email_enabled = True
        switches.save()
        self.posts = []

        def post(url, headers=None, json=None, timeout=None):
            self.posts.append(json)
            response = mock.MagicMock(status_code=200, text='{"id": "wamid-1"}')
            response.json.return_value = {'result': True, 'id': 'wamid-1'}
            return response

        for patch in (mock.patch('projects.notifications.requests.post', side_effect=post),
                      mock.patch('projects.notifications._zeptomail_post',
                                 side_effect=AssertionError('no email is declared')),
                      mock.patch.object(requests.sessions.Session, 'request',
                                        side_effect=AssertionError('HTTP attempted'))):
            patch.start()
            self.addCleanup(patch.stop)
        self.client = Client(SERVER_NAME='localhost')

    def activate(self, *codes):
        for code in codes:
            NotificationEventSetting.objects.create(
                event_code=code, channel='whatsapp', is_active=True, changed_by=self.admin,
                reason='test')

    def rows(self, code, channel='whatsapp'):
        return list(NotificationLog.objects.filter(event_code=code, channel=channel)
                    .order_by('pk'))

    def create(self, lines=7, expected='2026-11-08', project=None):
        project = project or self.site
        data = {'dc_number': 'DN-004', 'dc_date': '2026-11-04',
                'expected_delivery_date': expected}
        for i in range(lines):
            data.update({f'line_item_category_{i}': 'BOS',
                         f'line_item_description_{i}': f'Item {i + 1}',
                         f'line_item_qty_{i}': f'{i + 1}.00',
                         f'line_item_unit_{i}': 'Nos'})
        self.client.force_login(self.scm.user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse('create_delivery_challan', args=[project.project_id]), data)
        self.assertEqual(response.status_code, 302, response.content[:500])
        return DeliveryChallan.objects.get(project=project, dc_number='DN-004')


class CreateTests(SendBase):

    def test_one_deferred_send_per_engineer_with_values_in_their_slots(self):
        self.activate('dc_created')
        challan = self.create()
        rows = self.rows('dc_created')
        # Candidates come in name order (task_assignee_candidates), so compare as sets.
        self.assertEqual(sorted((r.recipient_id, r.status, r.template_name) for r in rows),
                         sorted([(self.alice.pk, 'sent', 'dc_created'),
                                 (self.bob.pk, 'sent', 'dc_created')]))
        self.assertTrue(all(r.finished_at for r in rows))   # went through the pending row
        by_phone = {post['phoneNumber']: post for post in self.posts}
        alice, bob = by_phone['9876500001'], by_phone['9876500002']
        template = alice['template']
        self.assertEqual(template['name'], 'dc_created')
        self.assertEqual(template['languageCode'], 'en')
        self.assertEqual(template['headerValues'], ['DNS-01'])
        url = reverse('delivery_challan_detail', args=['DNS-01', challan.pk])
        self.assertEqual(template['bodyValues'], [
            'Alice Mary', 'DN-004', 'Udyan Block A',
            'Item 1 1 Nos; Item 2 2 Nos; Item 3 3 Nos; Item 4 4 Nos; Item 5 5 Nos; '
            '+2 more items',
            '08 Nov 2026',
            'https://horizon-solar-pms-production.up.railway.app' + url])
        # A blank first name falls back to the username (D8).
        self.assertEqual(bob['template']['bodyValues'][0], 'dn_se_bob')
        self.assertEqual(alice['phoneNumber'], '9876500001')

    def test_in_app_row_for_each_engineer_never_the_creator(self):
        self.create()
        in_app = self.rows('dc_created', 'in_app')
        self.assertEqual(sorted(r.recipient_id for r in in_app),
                         sorted([self.alice.pk, self.bob.pk]))
        self.assertEqual(in_app[0].message,
                         'Delivery challan DN-004 created for DNS-01 — expected delivery: '
                         '08 Nov 2026.')
        self.assertFalse(NotificationLog.objects.filter(recipient=self.scm).exists())

    def test_paused_by_default_gives_skipped_rows_and_sends_nothing(self):
        self.create()
        self.assertEqual([(r.status, r.error_detail) for r in self.rows('dc_created')],
                         [('skipped', 'event paused'), ('skipped', 'event paused')])
        self.assertEqual(self.posts, [])
        self.assertFalse(NotificationLog.objects.filter(channel='email').exists())

    def test_blank_expected_date_says_when_it_was_dispatched(self):
        self.activate('dc_created')
        self.create(lines=1, expected='')
        self.assertEqual(self.posts[0]['template']['bodyValues'][3:5],
                         ['Item 1 1 Nos', 'Not given (dispatched 04 Nov 2026)'])

    def test_no_engineer_tells_the_pm_only(self):
        self.activate('dc_created')
        lone = _project('DNS-02', pm=self.pm)
        self.create(lines=1, project=lone)
        self.assertEqual([r.recipient_id for r in self.rows('dc_created')], [self.pm.pk])
        self.assertEqual(self.posts[0]['template']['headerValues'], ['DNS-02'])
        # Residential, no site_name: the customer names it (Q6).
        self.assertEqual(self.posts[0]['template']['bodyValues'][2], 'Sharma Residence')

    def test_a_create_that_rolls_back_tells_nobody(self):
        self.activate('dc_created')
        real = D.notify_dc_created

        def fire_then_fail(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError('a later write in the block failed')

        self.client.force_login(self.scm.user)
        self.client.raise_request_exception = False
        with mock.patch('projects.views.notify_dc_created', side_effect=fire_then_fail), \
                self.captureOnCommitCallbacks(execute=True) as callbacks:
            response = self.client.post(
                reverse('create_delivery_challan', args=['DNS-01']),
                {'dc_number': 'DN-RB', 'dc_date': '2026-11-04',
                 'line_item_category_0': 'BOS', 'line_item_description_0': 'Cable',
                 'line_item_qty_0': '2', 'line_item_unit_0': 'Mtr'})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(callbacks, [])
        self.assertFalse(DeliveryChallan.objects.filter(dc_number='DN-RB').exists())
        self.assertFalse(NotificationLog.objects.exists())
        self.assertEqual(self.posts, [])


class EditTests(SendBase):

    def setUp(self):
        super().setUp()
        self.dc = DeliveryChallan.objects.create(
            project=self.site, dc_number='DNE-1', dc_date=date(2026, 11, 4),
            expected_delivery_date=date(2026, 11, 8))
        self.cable = DCLineItem.objects.create(
            challan=self.dc, boq_category='BOS', item_description='DC cable',
            ordered_quantity=Decimal('2000'), unit='Mtr')
        self.mc4 = DCLineItem.objects.create(
            challan=self.dc, boq_category='BOS', item_description='MC4 Connector',
            ordered_quantity=Decimal('200'), unit='Pair')
        self.activate('dc_updated')

    def form(self, **overrides):
        """What the edit form posts unchanged (tests_dc_edit's shape) plus a reason."""
        self.dc.refresh_from_db()
        lines = list(self.dc.line_items.order_by('pk'))
        received = {line.pk for line in lines if line.received_quantity is not None}
        data = {
            'revision_count': str(self.dc.revisions.count()),
            'line_ids': ','.join(str(line.pk) for line in lines),
            'editable_line_ids': ','.join(str(l.pk) for l in lines if l.pk not in received),
            'vendor_id': '', 'po_number': self.dc.po_number, 'issued_from_warehouse': '',
            'dc_number': self.dc.dc_number, 'dc_date': self.dc.dc_date.isoformat(),
            'expected_delivery_date': (self.dc.expected_delivery_date.isoformat()
                                       if self.dc.expected_delivery_date else ''),
            'notes': self.dc.notes, 'reason': REASON,
        }
        for line in lines:
            prefix = f'line_{line.pk}_'
            data[prefix + 'description'] = line.item_description
            data[prefix + 'unit'] = line.unit
            if line.pk not in received:
                data[prefix + 'category'] = line.boq_category
                data[prefix + 'qty'] = str(line.ordered_quantity)
        data.update(overrides)
        return data

    def edit(self, **overrides):
        self.client.force_login(self.scm.user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse('delivery_challan_edit', args=['DNS-01', self.dc.pk]),
                self.form(**overrides))
        self.assertRedirects(response,
                             reverse('delivery_challan_detail', args=['DNS-01', self.dc.pk]),
                             fetch_redirect_response=False)
        self.assertEqual(self.dc.revisions.count(), 1)

    def receive_cable(self, received, damaged=0):
        DCLineItem.objects.filter(pk=self.cable.pk).update(
            received_quantity=Decimal(received), damaged_quantity=damaged, condition='Good',
            grn_date=date.today(), grn_confirmed_by=self.alice)
        DCLineItem.objects.filter(pk=self.mc4.pk).update(
            received_quantity=Decimal('200'), condition='Good', grn_date=date.today(),
            grn_confirmed_by=self.alice)
        recalculate_dc_status(self.dc)
        self.dc.refresh_from_db()

    def test_a_line_change_fires_dc_updated_with_the_new_material(self):
        self.edit(**{f'line_{self.mc4.pk}_qty': '250'})
        self.assertEqual([r.status for r in self.rows('dc_updated')], ['sent', 'sent'])
        template = self.posts[0]['template']
        self.assertEqual(template['name'], 'dc_updated')
        self.assertEqual(template['headerValues'], ['DNS-01'])
        self.assertEqual(template['bodyValues'][3],
                         'DC cable 2000 Mtr; MC4 Connector 250 Pair')
        self.assertEqual(self.rows('dc_updated', 'in_app')[0].message,
                         'Delivery challan DNE-1 for DNS-01 updated by SCM — expected '
                         'delivery: 08 Nov 2026.')

    def test_an_added_line_fires(self):
        self.edit(new_line_category_0='Inverter', new_line_description_0='5 kW inverter',
                  new_line_qty_0='1', new_line_unit_0='Nos')
        self.assertTrue(self.posts[0]['template']['bodyValues'][3].endswith('5 kW inverter 1 Nos'))

    def test_a_removed_line_fires(self):
        self.edit(**{f'line_{self.mc4.pk}_remove': '1'})
        self.assertEqual(self.posts[0]['template']['bodyValues'][3], 'DC cable 2000 Mtr')

    def test_a_date_change_fires(self):
        self.edit(expected_delivery_date='2026-11-12')
        self.assertEqual(self.posts[0]['template']['bodyValues'][4], '12 Nov 2026')

    def test_clearing_the_date_fires_with_the_dispatch_fallback(self):
        self.edit(expected_delivery_date='')
        self.assertEqual(self.posts[0]['template']['bodyValues'][4],
                         'Not given (dispatched 04 Nov 2026)')

    def test_a_header_only_edit_is_silent(self):
        self.edit(dc_number='DNE-1A', po_number='PO-9', notes='Second truck',
                  dc_date='2026-11-03')
        self.assertFalse(NotificationLog.objects.exists())
        self.assertEqual(self.posts, [])

    def test_an_edit_of_a_received_challan_is_silent(self):
        self.receive_cable('2000')
        self.assertEqual(self.dc.status, DeliveryChallan.RECEIVED)
        self.edit(**{f'line_{self.cable.pk}_description': 'DC solar cable 4 sqmm'})
        self.assertFalse(NotificationLog.objects.exists())

    def test_an_edit_of_a_rejected_challan_is_silent(self):
        self.receive_cable('0')
        self.assertEqual(self.dc.status, DeliveryChallan.REJECTED)
        self.edit(**{f'line_{self.cable.pk}_unit': 'Metre'})
        self.assertFalse(NotificationLog.objects.exists())

    def test_a_partially_received_challan_still_fires(self):
        self.receive_cable('1500')
        self.assertEqual(self.dc.status, DeliveryChallan.PARTIALLY_RECEIVED)
        self.edit(expected_delivery_date='2026-11-12')
        self.assertEqual(len(self.posts), 2)


# ---------------------------------------------------------------------------
# D9 — the Admin WhatsApp card
# ---------------------------------------------------------------------------

class WhatsAppCardTests(TestCase):

    def setUp(self):
        self.admin = _profile('dn_card_admin', 'Admin')
        self.client = Client(SERVER_NAME='localhost')
        self.client.force_login(self.admin.user)

    def page(self, code):
        return self.client.get(reverse('admin_notification_event', args=[code]))

    def test_the_whatsapp_card_has_no_email_only_controls(self):
        response = self.page('dc_created')
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for absent in ('Send test of live content to me', 'Create draft', '<iframe',
                       reverse('admin_notification_test_live', args=['dc_created', 'whatsapp']),
                       reverse('admin_notification_draft_create',
                               args=['dc_created', 'whatsapp']),
                       'Send a test of the live content'):
            self.assertNotIn(absent, html)
        self.assertContains(response, 'Interakt template:')
        self.assertContains(response,
                            'python manage.py test_whatsapp --event dc_created --user '
                            '&lt;your username&gt;')
        self.assertContains(response, 'Last phone test:</span>\n      never')
        # The {% comment %} block explaining the card never reaches the page.
        self.assertNotIn('NTF-DCG 3b (D9)', html)

    def test_the_last_phone_test_and_the_gate_are_shown(self):
        NotificationLog.objects.create(
            recipient=self.admin, channel='whatsapp', status='sent', event_code='dc_created',
            template_name='dc_created', message=WHATSAPP_TEST_MARKER + 'header: X')
        response = self.page('dc_created')
        self.assertContains(response, '— Sent')
        self.assertContains(response, 'so this can be switched on')

    def test_an_email_event_card_is_unchanged(self):
        response = self.page('issue_raised')
        self.assertContains(response, 'Send test of live content to me')
        self.assertContains(response, 'Create draft')
        self.assertNotContains(response, 'Interakt template:')

    def test_a_whatsapp_draft_is_refused_even_by_a_hand_made_post(self):
        response = self.client.post(
            reverse('admin_notification_draft_create', args=['dc_created', 'whatsapp']))
        self.assertRedirects(response, reverse('admin_notification_event',
                                               args=['dc_created']),
                             fetch_redirect_response=False)
        self.assertFalse(NotificationTemplate.objects.exists())
