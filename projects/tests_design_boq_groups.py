"""
B-5a verification — design BOQ groups (rulings 33-35; Zuber Q1-Q8 and Q-A..Q-F, 23 Nov 2026).

WHAT THIS FILE PROVES
---------------------
  * MEMBERSHIP. A site is in one design group (form refusal AND database constraint); sites
    are added several at a time, all or none, each refused site with its own reason; a site
    the user can't open is refused in the same words as one that doesn't exist; remove,
    rename and a case-insensitive duplicate name behave; every add and removal is logged.
  * COUNTED. Each Q1 status counts, every other status doesn't, and a QC fail (the real
    _open_next_attempt path) drops the site out of the totals.
  * TOTALS. Equal to the counted sites' lines summed by hand; a blank counts as 0 and is
    listed, a 0 is not; retired items are summed and marked; OPX-038 fractions and unit
    mismatches are listed and still counted; unlinked lines are listed, never summed;
    ACCBs are one row per configuration (feeder order ignored, a different SPD apart).
  * SCM UNTOUCHED. aggregate_group_boq() and SCM's group screen give identical totals with
    design groups around the same sites; a locked SCM group blocks no design-group change.
  * ACCESS (Zuber Q-A, Q-B). Non-design users refused on every route; boq_readable_projects_q
    equals user_can_view_project_boq for every user and site here; a plain designer sees
    only the sites they can open in the picker and is refused a group holding any other.
  * EXCEL. Three sheets, their headers, totals equal to the screen, no price column, the
    full ACCB text past 255 characters, Retired marks, warnings and the unlinked block.
  * QUERY COUNTS, pinned and equal at 5 and 20 sites.

Run with:
    python manage.py test projects.tests_design_boq_groups --settings=solarpms.test_settings
"""
import io
import re
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from openpyxl import load_workbook

from .accb import ACCB_CONFIGURED_CODE
from .design_boq_group_views import (
    GROUP_NOT_OPEN, SITE_NOT_FOUND, SUMMARY_HEADERS, SITES_HEADERS, UNLINKED_TITLE,
    consolidate, design_boq_counts, _member_rows, _live_members,
)
from .design_views import _open_next_attempt, aggregate_group_boq
from .models import (
    ActivityLog, BOQ, BOQItem, BOQItemMaster, DesignAssignment, DesignBOQGroup,
    DesignBOQGroupMembership, Program, Project, ProjectPhase, SiteGroup,
    SiteGroupMembership, Task, ATTEMPT_REASON_QC_FAILED, DESIGN_ASSIGNMENT_STATUS_CHOICES,
    DESIGN_BOQ_COUNTED_STATUSES, SITE_GROUP_LOCKED,
)
from .permissions import (
    boq_readable_project_ids, user_can_use_design_boq_groups, user_can_view_project_boq,
)
from .tests_accb_config import EX_TYPE1, EX_TYPE2, _longest_config, cfg
from .views import _accb_create

#: EX_TYPE2 with its incomers the other way round: the same thing to buy (B-3c ruling 4).
EX_TYPE2_REORDERED = cfg('Type-2', [('MCB', 63), ('MCCB', 100)], [('MCCB', 200, True)])
#: EX_TYPE2 with the SPD taken off its outgoing feeder: a different thing to buy.
EX_TYPE2_NO_SPD = cfg('Type-2', [('MCCB', 100), ('MCB', 63)], [('MCCB', 200, False)])

#: Q1, written out by hand so a change to the constant fails here rather than passing.
Q1_COUNTED = {'artifacts_uploaded', 'in_qc', 'awaiting_head_qc', 'awaiting_pm_approval',
              'released'}

PRICE_WORDS = ('price', 'rate', 'amount', 'cost', 'value', '₹', 'rs.')


def _profile(username, role, **flags):
    user = User.objects.create_user(username=username, password='pw12345',
                                    first_name=username.split('_')[-1].title())
    profile = user.profile          # auto-created by the post_save signal
    profile.role = role
    for field, value in flags.items():
        setattr(profile, field, value)
    profile.save()
    return profile


def _client(profile):
    client = Client(SERVER_NAME='localhost')
    client.force_login(profile.user)
    return client


class DesignGroupFixture(TestCase):
    """Two tenders; designer A's four sites (two counted, one not, one counted with every
    warning shape) and designer B's one counted site, which A cannot open."""

    @classmethod
    def setUpTestData(cls):
        cls.head = _profile('b5a_head', 'Design', is_design_head=True)
        cls.qc = _profile('b5a_qc', 'Design', is_design_qc=True)
        cls.des_a = _profile('b5a_desa', 'Design')
        cls.des_b = _profile('b5a_desb', 'Design')
        cls.des_task = _profile('b5a_destask', 'Design')
        cls.des_rev = _profile('b5a_desrev', 'Design')
        cls.deputy = _profile('b5a_deputy', 'Site Engineer')
        cls.head.design_head_deputy = cls.deputy
        cls.head.save(update_fields=['design_head_deputy'])
        cls.scm = _profile('b5a_scm', 'SCM')
        cls.pm = _profile('b5a_pm', 'PM')
        cls.coord = _profile('b5a_coord', 'Project Coordinator')
        cls.admin = _profile('b5a_admin', 'Admin')
        cls.ceo = _profile('b5a_ceo', 'CEO')
        cls.finance = _profile('b5a_fin', 'Finance')
        cls.se = _profile('b5a_se', 'Site Engineer')

        cls.t1 = Program.objects.create(program_type='OPEX', name='Tender One',
                                        client_name='C1', status='Active',
                                        short_tender_code='TONE')
        cls.t2 = Program.objects.create(program_type='OPEX', name='Tender Two',
                                        client_name='C2', status='Active',
                                        short_tender_code='TTWO')

        master = BOQItemMaster.objects.create
        cls.m_module = master(code='OPX-001', description='Solar module', unit='Nos',
                              category='Modules', project_type='OPEX', sort_order=1)
        cls.m_cable = master(code='OPX-002', description='DC cable', unit='Mtr',
                             category='Cable', project_type='OPEX', sort_order=2)
        cls.m_tin = master(code='OPX-038', description='Tin Shade', unit='Nos',
                           category='Structure', project_type='OPEX', sort_order=38)
        cls.m_retired = master(code='OPX-099', description='Old clamp', unit='Nos',
                               category='Structure', project_type='OPEX', sort_order=99,
                               is_active=False)
        cls.m_accb = master(code=ACCB_CONFIGURED_CODE, description='ACCB (configured)',
                            unit='Nos', category='ACCB', project_type='OPEX', sort_order=273)

        cls.a1 = cls.site('A1', cls.t1, cls.des_a, 'released',
                          [(cls.m_module, '10'), (cls.m_cable, '100.5')])
        cls.a2 = cls.site('A2', cls.t1, cls.des_a, 'in_qc',
                          [(cls.m_module, '5'), (cls.m_cable, None), (cls.m_tin, '38.4')])
        cls.a3 = cls.site('A3', cls.t2, cls.des_a, 'in_design', [(cls.m_module, '7')])
        cls.a4 = cls.site('A4', cls.t2, cls.des_a, 'awaiting_pm_approval',
                          [(cls.m_module, '3'), (cls.m_retired, '2'),
                           (cls.m_cable, '10', 'Nos'), (cls.m_tin, '0'), (None, '4')])
        cls.b1 = cls.site('B1', cls.t1, cls.des_b, 'released', [(cls.m_module, '20')])

        # Per-site read sources other than the designer, for the parity test.
        cls.b1.assigned_pm = cls.pm
        cls.b1.save(update_fields=['assigned_pm'])
        cls.a3.coordinators.add(cls.coord)
        phase = ProjectPhase.objects.create(project=cls.a3, phase_name='Design', phase_order=1)
        Task.objects.create(phase=phase, task_name='Check drawings', task_order=1,
                            assigned_role=Task.DESIGN, assigned_to=cls.des_task)
        DesignAssignment.objects.filter(project=cls.a4).update(qc_assigned_to=cls.des_rev)

    @classmethod
    def site(cls, code, tender, designer, status, lines):
        site = Project.objects.create(
            project_id=f'B5A-{code}', customer_name='C', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX', program=tender,
            site_code=code, dc_capacity_kw=Decimal('100.00'), status='Draft',
            assigned_design=designer, site_name=f'Site {code}')
        DesignAssignment.objects.create(project=site, assigned_to=designer, status=status)
        boq = BOQ.objects.create(project=site)
        for serial, line in enumerate(lines, 1):
            item, qty = line[0], line[1]
            uom = line[2] if len(line) > 2 else (item.unit if item else 'Nos')
            BOQItem.objects.create(
                boq=boq, serial_no=serial, category='Other', item_master=item,
                description=item.description if item else f'Ad-hoc fitting {code}',
                uom=uom, boq_quantity=Decimal(qty) if qty is not None else None)
        return site

    # ── helpers ───────────────────────────────────────────────────────────────
    def group(self, name='Group G', sites=(), by=None):
        group = DesignBOQGroup.objects.create(name=name, created_by=by or self.head)
        for site in sites:
            DesignBOQGroupMembership.objects.create(group=group, project=site, added_by=group.created_by)
        return group

    def boq_of(self, group):
        return consolidate(_member_rows(_live_members(group)))

    def add(self, profile, group, sites, follow=True):
        return _client(profile).post(
            reverse('design_boq_group_add_sites', args=[group.pk]),
            {'project_ids': [s.pk if hasattr(s, 'pk') else s for s in sites]}, follow=follow)

    def accb(self, site, config):
        return _accb_create(BOQ.objects.get(project=site), self.m_accb, config, self.des_a)

    @staticmethod
    def msgs(response):
        return [str(m) for m in response.context['messages']]


# ═════════════════════════════════════════════════════════════════════════════
# Membership
# ═════════════════════════════════════════════════════════════════════════════

class MembershipTests(DesignGroupFixture):

    def test_create_opens_the_new_group_into_the_picker(self):
        response = _client(self.des_a).post(reverse('design_boq_group_create'),
                                            {'name': '  Lot 2  '}, follow=True)
        group = DesignBOQGroup.objects.get()
        self.assertEqual(group.name, 'Lot 2')
        self.assertEqual(group.created_by, self.des_a)
        self.assertTrue(response.context['open_picker'])
        self.assertTrue(ActivityLog.objects.filter(action_code='design_boq_group_created',
                                                   entity_id=group.pk).exists())

    def test_a_duplicate_name_ignoring_case_is_refused(self):
        self.group('Lot 2')
        response = _client(self.des_a).post(reverse('design_boq_group_create'),
                                            {'name': 'LOT 2'}, follow=True)
        self.assertEqual(DesignBOQGroup.objects.count(), 1)
        self.assertIn('A group named "Lot 2" already exists. Choose another name.',
                      self.msgs(response))

    def test_the_database_refuses_a_duplicate_name_ignoring_case(self):
        self.group('Lot 2')
        with self.assertRaises(IntegrityError), transaction.atomic():
            DesignBOQGroup.objects.create(name='lot 2')

    def test_rename_and_a_rename_onto_another_name_is_refused(self):
        group = self.group('Old')
        self.group('Taken')
        client = _client(self.des_a)
        client.post(reverse('design_boq_group_rename', args=[group.pk]), {'name': 'New'})
        group.refresh_from_db()
        self.assertEqual(group.name, 'New')
        self.assertTrue(ActivityLog.objects.filter(action_code='design_boq_group_renamed').exists())
        response = client.post(reverse('design_boq_group_rename', args=[group.pk]),
                               {'name': 'taken'}, follow=True)
        group.refresh_from_db()
        self.assertEqual(group.name, 'New')
        self.assertIn('A group named "Taken" already exists. Choose another name.',
                      self.msgs(response))

    def test_several_sites_are_added_at_once_and_logged_once(self):
        group = self.group()
        response = self.add(self.des_a, group, [self.a1, self.a2, self.a3])
        self.assertEqual(set(group.memberships.values_list('project_id', flat=True)),
                         {self.a1.pk, self.a2.pk, self.a3.pk})
        self.assertIn('3 sites added: B5A-A1, B5A-A2, B5A-A3. 1 shows as not counted yet '
                      'until the BOQ is submitted.', self.msgs(response))
        logs = ActivityLog.objects.filter(action_code='design_boq_group_sites_added')
        self.assertEqual(logs.count(), 1)
        self.assertEqual(logs.get().action,
                         'Desa added 3 sites to design BOQ group "Group G": '
                         'B5A-A1, B5A-A2, B5A-A3')
        self.assertIsNone(logs.get().project)

    def test_a_long_add_is_logged_in_the_short_form(self):
        sites = [self.site(f'L{n}', self.t1, self.des_a, 'released', []) for n in range(8)]
        group = self.group()
        self.add(self.des_a, group, sites)
        log = ActivityLog.objects.get(action_code='design_boq_group_sites_added')
        self.assertTrue(log.action.endswith('B5A-L0, B5A-L1, B5A-L2, B5A-L3, B5A-L4 and 3 more'))

    def test_a_site_in_another_group_is_refused_and_nothing_is_added(self):
        self.group('First', [self.a1])
        group = self.group('Second')
        response = self.add(self.des_a, group, [self.a2, self.a1])
        self.assertFalse(group.memberships.exists())
        messages = self.msgs(response)
        self.assertIn('No sites were added. Fix these and try again:', messages)
        self.assertIn('B5A-A1: already in "First". A site can be in one design group only.',
                      messages)

    def test_the_database_refuses_a_site_in_two_groups(self):
        self.group('First', [self.a1])
        second = self.group('Second')
        with self.assertRaises(IntegrityError), transaction.atomic():
            DesignBOQGroupMembership.objects.create(group=second, project=self.a1)

    def test_each_refusal_reason(self):
        group = self.group('Here', [self.a1])
        residential = Project.objects.create(
            project_id='B5A-RES', customer_name='C', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='Residential',
            dc_capacity_kw=Decimal('5.00'), status='Draft')
        bare = Project.objects.create(
            project_id='B5A-BARE', customer_name='C', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX', program=self.t1,
            site_code='BARE', dc_capacity_kw=Decimal('100.00'), status='Draft')
        deleted = self.site('DEL', self.t1, self.des_a, 'released', [])
        Project.objects.filter(pk=deleted.pk).update(is_deleted=True)
        response = self.add(self.head, group,
                            [self.a1, residential, bare, deleted, 999999, 'x', self.a2])
        self.assertEqual(list(group.memberships.values_list('project_id', flat=True)),
                         [self.a1.pk])
        messages = self.msgs(response)
        for line in ('B5A-A1: already in this group.', 'B5A-RES: not a RESCO site.',
                     'B5A-BARE: has no design assignment.', SITE_NOT_FOUND):
            self.assertIn(line, messages)
        # Each distinct reason once: the deleted site, the bogus pk and 'x' share one line.
        self.assertEqual(messages.count(SITE_NOT_FOUND), 1)

    def test_a_site_the_designer_cannot_open_reads_as_not_found(self):
        group = self.group('Mine', [self.a1], by=self.des_a)
        response = self.add(self.des_a, group, [self.b1])
        self.assertFalse(group.memberships.filter(project=self.b1).exists())
        self.assertIn(SITE_NOT_FOUND, self.msgs(response))
        self.assertNotIn('B5A-B1', ' '.join(self.msgs(response)))

    def test_remove_deletes_the_row_logs_it_and_leaves_the_boq(self):
        group = self.group('G', [self.a1, self.a2])
        membership = group.memberships.get(project=self.a1)
        lines_before = BOQItem.objects.filter(boq__project=self.a1).count()
        response = _client(self.des_a).post(
            reverse('design_boq_group_remove_site', args=[group.pk]),
            {'membership_id': membership.pk}, follow=True)
        self.assertFalse(DesignBOQGroupMembership.objects.filter(pk=membership.pk).exists())
        self.assertEqual(BOQItem.objects.filter(boq__project=self.a1).count(), lines_before)
        self.assertIn('B5A-A1 removed from "G". Its BOQ is unchanged.', self.msgs(response))
        self.assertEqual(ActivityLog.objects.get(action_code='design_boq_group_site_removed').action,
                         'Desa removed B5A-A1 from design BOQ group "G"')

    def test_remove_is_scoped_to_the_group(self):
        other = self.group('Other', [self.a1])
        group = self.group('G', [self.a2])
        response = _client(self.des_a).post(
            reverse('design_boq_group_remove_site', args=[group.pk]),
            {'membership_id': other.memberships.get().pk})
        self.assertEqual(response.status_code, 404)
        self.assertTrue(other.memberships.exists())

    def test_a_soft_deleted_site_is_hidden_but_keeps_its_row(self):
        group = self.group('G', [self.a1, self.a2])
        Project.objects.filter(pk=self.a1.pk).update(is_deleted=True)
        response = _client(self.head).get(reverse('design_boq_group_detail', args=[group.pk]))
        self.assertEqual([r['code'] for r in response.context['member_rows']], ['B5A-A2'])
        self.assertEqual(response.context['boq']['counted_codes'], ['B5A-A2'])
        listed = _client(self.head).get(reverse('design_boq_group_list')).context['groups'][0]
        self.assertEqual((listed.site_count, listed.counted_count), (1, 1))
        self.assertEqual(group.memberships.count(), 2)


# ═════════════════════════════════════════════════════════════════════════════
# What counts
# ═════════════════════════════════════════════════════════════════════════════

class CountedTests(DesignGroupFixture):

    def test_the_counted_set_is_q1(self):
        self.assertEqual(set(DESIGN_BOQ_COUNTED_STATUSES), Q1_COUNTED)

    def test_each_status_counts_exactly_when_q1_says_so(self):
        group = self.group('G', [self.a1])
        for status, _label in DESIGN_ASSIGNMENT_STATUS_CHOICES:
            with self.subTest(status=status):
                DesignAssignment.objects.filter(project=self.a1).update(status=status)
                boq = self.boq_of(group)
                expected = status in Q1_COUNTED
                self.assertEqual(design_boq_counts(DesignAssignment.objects.get(project=self.a1)),
                                 expected)
                self.assertEqual(boq['counted_codes'], ['B5A-A1'] if expected else [])
                self.assertEqual(bool(boq['lines']), expected)

    def test_pm_rejected_shows_as_not_counted_yet(self):
        DesignAssignment.objects.filter(project=self.a1).update(status='pm_rejected')
        group = self.group('G', [self.a1])
        html = _client(self.head).get(
            reverse('design_boq_group_detail', args=[group.pk])).content.decode()
        sites = html.split('id="boqGroupSites"', 1)[1].split('</table>', 1)[0]
        self.assertIn('Not counted yet', sites)

    def test_a_qc_fail_drops_the_site_out_of_the_totals(self):
        group = self.group('G', [self.a1, self.a2])
        totals = {l['item_master__code']: l['total_quantity'] for l in self.boq_of(group)['lines']}
        self.assertEqual(totals['OPX-001'], Decimal('15'))
        # The real path a QC fail and a Head fail take (design_qc_fail / design_head_qc_fail).
        assignment = DesignAssignment.objects.get(project=self.a2)
        _open_next_attempt(assignment, ATTEMPT_REASON_QC_FAILED, self.qc, 'QC failed')
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, 'in_design')
        boq = self.boq_of(group)
        totals = {l['item_master__code']: l['total_quantity'] for l in boq['lines']}
        self.assertEqual(totals['OPX-001'], Decimal('10'))
        self.assertEqual([r['code'] for r in boq['not_counted']], ['B5A-A2'])


# ═════════════════════════════════════════════════════════════════════════════
# Totals and warnings
# ═════════════════════════════════════════════════════════════════════════════

class TotalsTests(DesignGroupFixture):

    def test_totals_equal_the_counted_sites_lines_summed_by_hand(self):
        group = self.group('G', [self.a1, self.a2, self.a3, self.a4])
        boq = self.boq_of(group)
        expected = {}
        for item in BOQItem.objects.filter(
                boq__project__in=[self.a1, self.a2, self.a4], item_master__isnull=False,
                boq_quantity__gt=0):
            code = item.item_master.code
            expected[code] = expected.get(code, Decimal('0')) + item.boq_quantity
        self.assertEqual({l['item_master__code']: l['total_quantity'] for l in boq['lines']},
                         expected)
        module = next(l for l in boq['lines'] if l['item_master__code'] == 'OPX-001')
        self.assertEqual(module['per_site_list'],
                         [('B5A-A1', Decimal('10')), ('B5A-A2', Decimal('5')),
                          ('B5A-A4', Decimal('3'))])
        self.assertEqual(module['site_count'], 3)
        # A3 is not counted: its 7 modules are nowhere in the totals.
        self.assertEqual(module['total_quantity'], Decimal('18'))

    def test_two_lines_on_one_item_at_one_site_are_one_site_in_the_split(self):
        BOQItem.objects.create(boq=BOQ.objects.get(project=self.a1), serial_no=9,
                               category='Other', item_master=self.m_module,
                               description='Solar module', uom='Nos', boq_quantity=Decimal('1'))
        boq = self.boq_of(self.group('G', [self.a1]))
        module = next(l for l in boq['lines'] if l['item_master__code'] == 'OPX-001')
        self.assertEqual(module['per_site_list'], [('B5A-A1', Decimal('11'))])

    def test_a_blank_counts_as_0_and_is_listed_and_a_0_is_not(self):
        boq = self.boq_of(self.group('G', [self.a2, self.a4]))
        self.assertEqual([(b['site'], b['code']) for b in boq['blanks']], [('B5A-A2', 'OPX-002')])
        cable = next(l for l in boq['lines'] if l['item_master__code'] == 'OPX-002')
        self.assertEqual(cable['per_site_list'], [('B5A-A4', Decimal('10'))])   # A2's blank adds 0

    def test_blanks_on_a_not_counted_site_are_not_listed(self):
        DesignAssignment.objects.filter(project=self.a2).update(status='in_design')
        self.assertEqual(self.boq_of(self.group('G', [self.a2]))['blanks'], [])

    def test_a_retired_item_is_summed_and_marked(self):
        boq = self.boq_of(self.group('G', [self.a4]))
        retired = next(l for l in boq['lines'] if l['item_master__code'] == 'OPX-099')
        self.assertTrue(retired['retired'])
        self.assertEqual(retired['total_quantity'], Decimal('2'))
        self.assertFalse(next(l for l in boq['lines'] if l['item_master__code'] == 'OPX-001')['retired'])

    def test_tin_shade_fractions_and_unit_mismatches_are_listed_and_still_counted(self):
        boq = self.boq_of(self.group('G', [self.a2, self.a4]))
        found = {(w['site'], w['code']): w for w in boq['unit_warnings']}
        self.assertEqual(set(found), {('B5A-A2', 'OPX-038'), ('B5A-A4', 'OPX-002')})
        tin = found[('B5A-A2', 'OPX-038')]
        self.assertEqual((tin['quantity'], tin['reasons']), (Decimal('38.4'), ['not a whole number']))
        unit = found[('B5A-A4', 'OPX-002')]
        self.assertEqual((unit['line_unit'], unit['catalogue_unit'], unit['reasons']),
                         ('Nos', 'Mtr', ['unit differs from the catalogue']))
        totals = {l['item_master__code']: l['total_quantity'] for l in boq['lines']}
        self.assertEqual(totals['OPX-038'], Decimal('38.4'))
        self.assertEqual(totals['OPX-002'], Decimal('10'))

    def test_unlinked_lines_are_listed_never_summed(self):
        group = self.group('G', [self.a4])
        boq = self.boq_of(group)
        self.assertEqual([(i.boq.project.project_id, i.description, i.boq_quantity)
                          for i in boq['unlinked']], [('B5A-A4', 'Ad-hoc fitting A4', Decimal('4'))])
        self.assertNotIn(None, [l['item_master'] for l in boq['lines']])
        html = _client(self.head).get(
            reverse('design_boq_group_detail', args=[group.pk])).content.decode()
        self.assertIn('Lines with no catalogue item', html)
        self.assertIn('Ad-hoc fitting A4', html)

    def test_the_screen_lists_every_warning(self):
        group = self.group('G', [self.a2, self.a3, self.a4])
        response = _client(self.head).get(reverse('design_boq_group_detail', args=[group.pk]))
        warnings = response.context['warnings']
        self.assertIn('Not counted yet: B5A-A3 (In design).', warnings)
        self.assertIn('Blank quantities counted as 0: B5A-A2 OPX-002.', warnings)
        self.assertIn('B5A-A2 OPX-038: quantity 38.4, line unit Nos, catalogue unit Nos — not '
                      'a whole number. Still counted in the total.', warnings)
        html = response.content.decode()
        for line in warnings:
            self.assertIn(line.replace('"', '&quot;'), html)

    def test_accbs_one_row_per_configuration(self):
        self.accb(self.a1, EX_TYPE2)
        self.accb(self.a2, EX_TYPE2_REORDERED)
        self.accb(self.a2, EX_TYPE2_NO_SPD)
        self.accb(self.a4, EX_TYPE1)
        boq = self.boq_of(self.group('G', [self.a1, self.a2, self.a4]))
        rows = boq['accb_lines']
        self.assertEqual(len(rows), 3)
        by_sites = {tuple(r['per_site_list']): r for r in rows}
        same = by_sites[(('B5A-A1', Decimal('1')), ('B5A-A2', Decimal('1')))]
        self.assertEqual((same['total_quantity'], same['site_count']), (Decimal('2'), 2))
        self.assertIn((('B5A-A2', Decimal('1')),), by_sites)          # the no-SPD one, apart
        self.assertTrue(all(r['code'] == ACCB_CONFIGURED_CODE for r in rows))
        # The ACCB catalogue row never becomes one meaningless catalogue line.
        self.assertNotIn(ACCB_CONFIGURED_CODE, [l['item_master__code'] for l in boq['lines']])


# ═════════════════════════════════════════════════════════════════════════════
# SCM untouched
# ═════════════════════════════════════════════════════════════════════════════

class SCMUntouchedTests(DesignGroupFixture):

    def scm_group(self, status='draft'):
        group = SiteGroup.objects.create(program=self.t1, name='SCM batch', status=status,
                                         created_by=self.scm)
        for site in (self.a1, self.a2):
            SiteGroupMembership.objects.create(group=group, project=site, added_by=self.scm)
        return group

    def test_scm_totals_are_identical_with_a_design_group_on_the_same_sites(self):
        scm_group = self.scm_group()
        url = reverse('site_group_detail', args=[scm_group.pk])
        before_agg = repr(aggregate_group_boq([self.a1.pk, self.a2.pk]))
        before_page = repr(_client(self.scm).get(url).context['agg'])
        design = self.group('Design', [self.a1])
        self.add(self.head, design, [self.a2])
        self.assertEqual(repr(aggregate_group_boq([self.a1.pk, self.a2.pk])), before_agg)
        self.assertEqual(repr(_client(self.scm).get(url).context['agg']), before_page)
        # And the site sits in both, each group seeing it.
        self.assertTrue(SiteGroupMembership.objects.filter(project=self.a2, removed_at__isnull=True).exists())
        self.assertTrue(design.memberships.filter(project=self.a2).exists())

    def test_a_locked_scm_group_blocks_no_design_group_change(self):
        self.scm_group(status=SITE_GROUP_LOCKED)
        group = self.group('Design')
        self.add(self.head, group, [self.a1, self.a2])
        self.assertEqual(group.memberships.count(), 2)
        _client(self.head).post(reverse('design_boq_group_remove_site', args=[group.pk]),
                                {'membership_id': group.memberships.get(project=self.a1).pk})
        self.assertEqual(list(group.memberships.values_list('project_id', flat=True)), [self.a2.pk])
        self.assertEqual(SiteGroup.objects.get().status, SITE_GROUP_LOCKED)


# ═════════════════════════════════════════════════════════════════════════════
# Access
# ═════════════════════════════════════════════════════════════════════════════

class AccessTests(DesignGroupFixture):

    def routes(self, group):
        return [
            ('get',  reverse('design_boq_group_list'), {}),
            ('post', reverse('design_boq_group_create'), {'name': 'X'}),
            ('get',  reverse('design_boq_group_detail', args=[group.pk]), {}),
            ('post', reverse('design_boq_group_rename', args=[group.pk]), {'name': 'Y'}),
            ('post', reverse('design_boq_group_add_sites', args=[group.pk]), {'project_ids': [self.a2.pk]}),
            ('post', reverse('design_boq_group_remove_site', args=[group.pk]),
             {'membership_id': group.memberships.get().pk}),
            ('get',  reverse('design_boq_group_excel', args=[group.pk]), {}),
        ]

    def test_non_design_users_are_refused_on_every_route(self):
        group = self.group('G', [self.a1])
        for profile in (self.scm, self.pm, self.coord, self.admin, self.ceo, self.finance, self.se):
            client = _client(profile)
            for method, url, data in self.routes(group):
                with self.subTest(role=profile.role, url=url):
                    self.assertEqual(getattr(client, method)(url, data).status_code, 403)
        self.assertEqual(DesignBOQGroup.objects.count(), 1)
        self.assertEqual(group.memberships.count(), 1)

    def test_design_users_head_qc_and_a_deputy_of_any_role_are_admitted(self):
        for profile in (self.head, self.qc, self.des_a, self.deputy):
            with self.subTest(user=profile.user.username):
                self.assertTrue(user_can_use_design_boq_groups(profile.user))
                self.assertEqual(_client(profile).get(reverse('design_boq_group_list')).status_code, 200)

    def test_the_batched_read_gate_equals_the_predicate_for_every_user_and_site(self):
        users = [self.head, self.qc, self.des_a, self.des_b, self.des_task, self.des_rev,
                 self.deputy, self.scm, self.pm, self.coord, self.admin, self.ceo,
                 self.finance, self.se]
        sites = list(Project.objects.all())
        for profile in users:
            readable = boq_readable_project_ids(profile.user, Project.objects.all())
            for site in sites:
                with self.subTest(user=profile.user.username, site=site.project_id):
                    self.assertEqual(site.pk in readable,
                                     user_can_view_project_boq(profile.user, site))
        # The fixture exercises every per-site branch, not just the portfolio ones.
        self.assertEqual(boq_readable_project_ids(self.des_task.user, Project.objects.all()), {self.a3.pk})
        self.assertEqual(boq_readable_project_ids(self.des_rev.user, Project.objects.all()), {self.a4.pk})
        self.assertEqual(boq_readable_project_ids(self.coord.user, Project.objects.all()), {self.a3.pk})
        self.assertEqual(boq_readable_project_ids(self.pm.user, Project.objects.all()), {self.b1.pk})

    def test_a_plain_designer_is_refused_a_group_holding_a_site_they_cannot_open(self):
        group = self.group('Mixed', [self.a1, self.b1])
        client = _client(self.des_a)
        for url in (reverse('design_boq_group_detail', args=[group.pk]),
                    reverse('design_boq_group_excel', args=[group.pk])):
            response = client.get(url)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.content.decode(), GROUP_NOT_OPEN)
        response = client.post(reverse('design_boq_group_add_sites', args=[group.pk]),
                               {'project_ids': [self.a2.pk]})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(group.memberships.count(), 2)
        # The list shows it without a link.
        html = client.get(reverse('design_boq_group_list')).content.decode()
        self.assertIn('Mixed', html)
        self.assertNotIn(reverse('design_boq_group_detail', args=[group.pk]), html)
        self.assertIn(GROUP_NOT_OPEN.replace("'", '&#x27;'), html)
        # Head and QC open it: they can read every site.
        for profile in (self.head, self.qc):
            self.assertEqual(_client(profile).get(
                reverse('design_boq_group_detail', args=[group.pk])).status_code, 200)

    def test_the_picker_lists_only_sites_the_user_can_open(self):
        group = self.group('Mine', by=self.des_a)
        rows = _client(self.des_a).get(
            reverse('design_boq_group_detail', args=[group.pk])).context['picker_rows']
        self.assertEqual([r['code'] for r in rows], ['B5A-A1', 'B5A-A2', 'B5A-A3', 'B5A-A4'])
        head_rows = _client(self.head).get(
            reverse('design_boq_group_detail', args=[group.pk])).context['picker_rows']
        self.assertIn('B5A-B1', [r['code'] for r in head_rows])

    def test_picker_rows_name_another_group_and_flag_this_one(self):
        self.group('Other', [self.a1])
        group = self.group('Mine', [self.a2])
        rows = {r['code']: r for r in _client(self.head).get(
            reverse('design_boq_group_detail', args=[group.pk])).context['picker_rows']}
        self.assertEqual((rows['B5A-A1']['group'], rows['B5A-A1']['here']), ('Other', False))
        self.assertEqual((rows['B5A-A2']['group'], rows['B5A-A2']['here']), ('', True))
        self.assertEqual((rows['B5A-A3']['group'], rows['B5A-A3']['counted']), ('', False))

    def test_the_group_screen_links_each_site_to_its_own_boq(self):
        group = self.group('G', [self.a1])
        html = _client(self.des_a).get(reverse('design_boq_group_detail', args=[group.pk])).content.decode()
        self.assertIn(reverse('boq_detail', args=['B5A-A1']), html)
        # The site's own BOQ page still refuses a designer who cannot open it.
        self.assertFalse(user_can_view_project_boq(self.des_a.user, self.b1))
        self.assertIn(_client(self.des_a).get(reverse('boq_detail', args=['B5A-B1'])).status_code,
                      (403, 404))


# ═════════════════════════════════════════════════════════════════════════════
# Excel
# ═════════════════════════════════════════════════════════════════════════════

class ExcelTests(DesignGroupFixture):

    def workbook(self, group, profile=None):
        response = _client(profile or self.head).get(reverse('design_boq_group_excel', args=[group.pk]))
        self.assertEqual(response.status_code, 200)
        return response, load_workbook(io.BytesIO(response.content))

    @staticmethod
    def table(ws, first_header):
        """Rows from the header row whose first cell is `first_header` to the next blank."""
        rows = list(ws.iter_rows(values_only=True))
        start = next(i for i, r in enumerate(rows) if r[0] == first_header)
        out = []
        for row in rows[start:]:
            if row[0] is None:
                break
            out.append(row)
        return out

    def test_three_sheets_with_their_headers_and_the_file_name(self):
        group = self.group('Lot/2 "A"', [self.a1, self.a3])
        response, wb = self.workbook(group)
        self.assertEqual(wb.sheetnames, ['Summary', 'By site', 'Sites'])
        self.assertEqual(list(self.table(wb['Summary'], 'Code')[0]), SUMMARY_HEADERS)
        self.assertEqual(list(next(wb['By site'].iter_rows(values_only=True))),
                         ['Code', 'Description', 'Unit', 'Note', 'B5A-A1', 'Total'])
        self.assertEqual(list(next(wb['Sites'].iter_rows(values_only=True))), SITES_HEADERS)
        self.assertRegex(response['Content-Disposition'],
                         r'^attachment; filename="Lot_2 _A__live_\d{4}-\d{2}-\d{2}\.xlsx"$')

    def test_summary_totals_equal_the_screen_and_by_site_adds_up(self):
        self.accb(self.a1, EX_TYPE2)
        group = self.group('G', [self.a1, self.a2, self.a4])
        screen = _client(self.head).get(reverse('design_boq_group_detail', args=[group.pk])).context['boq']
        _response, wb = self.workbook(group)
        summary = {row[0] if row[0] != ACCB_CONFIGURED_CODE else row[1]: row[4]
                   for row in self.table(wb['Summary'], 'Code')[1:]}
        expected = {l['item_master__code']: float(l['total_quantity']) for l in screen['lines']}
        expected.update({a['description']: float(a['total_quantity']) for a in screen['accb_lines']})
        self.assertEqual({k: float(v) for k, v in summary.items()}, expected)
        for row in list(wb['By site'].iter_rows(values_only=True))[1:]:
            self.assertAlmostEqual(sum(float(v) for v in row[4:-1] if v is not None), float(row[-1]))

    def test_sites_sheet_says_who_counts(self):
        _response, wb = self.workbook(self.group('G', [self.a1, self.a3]))
        rows = list(wb['Sites'].iter_rows(values_only=True))[1:]
        self.assertEqual([(r[0], r[2], r[3], r[4], r[5]) for r in rows],
                         [('B5A-A1', 'Tender One', 'Desa', 'Released', 'Yes'),
                          ('B5A-A3', 'Tender Two', 'Desa', 'In design', 'No')])

    def test_no_price_column_anywhere(self):
        _response, wb = self.workbook(self.group('G', [self.a1, self.a2, self.a4]))
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                for cell in row:
                    if isinstance(cell, str) and len(cell) < 40:    # headers, not sentences
                        with self.subTest(sheet=ws.title, cell=cell):
                            self.assertFalse(any(w in cell.lower() for w in PRICE_WORDS))

    def test_a_full_accb_description_over_255_characters(self):
        config = self.accb(self.a1, _longest_config())
        text = BOQItem.objects.get(pk=config.boq_item_id).description
        self.assertGreater(len(text), 255)
        _response, wb = self.workbook(self.group('G', [self.a1]))
        summary = [r[1] for r in self.table(wb['Summary'], 'Code')[1:] if r[0] == ACCB_CONFIGURED_CODE]
        by_site = [r[1] for r in wb['By site'].iter_rows(values_only=True) if r[0] == ACCB_CONFIGURED_CODE]
        self.assertEqual(summary, [text])
        self.assertEqual(by_site, [text])

    def test_retired_marks_warnings_and_the_unlinked_block(self):
        _response, wb = self.workbook(self.group('G', [self.a2, self.a3, self.a4]))
        summary = wb['Summary']
        retired = [r for r in self.table(summary, 'Code')[1:] if r[0] == 'OPX-099']
        self.assertEqual(retired[0][5], 'Retired')
        by_site = [r for r in wb['By site'].iter_rows(values_only=True) if r[0] == 'OPX-099']
        self.assertEqual(by_site[0][3], 'Retired')
        cells = [r[0] for r in summary.iter_rows(values_only=True) if r[0]]
        self.assertIn('Blank quantities counted as 0: B5A-A2 OPX-002.', cells)
        self.assertIn('Not counted yet: B5A-A3 (In design).', cells)
        self.assertIn(UNLINKED_TITLE, cells)
        unlinked = self.table(summary, 'Site')
        self.assertEqual([tuple(r[:4]) for r in unlinked[1:]], [('B5A-A4', 'Ad-hoc fitting A4', 'Nos', 4)])
        # The unlinked line is in no catalogue row.
        self.assertNotIn('Ad-hoc fitting A4', [r[1] for r in self.table(summary, 'Code')])


# ═════════════════════════════════════════════════════════════════════════════
# Query counts
# ═════════════════════════════════════════════════════════════════════════════

#: Measured at build time and pinned, for a PLAIN DESIGNER — the costliest reader, since
#: their read gate needs the deputy lookup (once per request, design_boq_group_views.
#: _readable_q) and base.html adds its own. Each is the same at 5 and at 20 sites. Counts
#: include the session, user and profile loads; the add POST includes its savepoints.
DETAIL_QUERIES = 19     # B-5b: +1 for the Snapshots tab's list (Zuber, 24 Nov); DW-1b: +1 formula groups; DW-8: +1 "← My queue" (a designer's deputy lookup)
EXCEL_QUERIES = 12      # DW-1b: +1 for the formula groups the content-gap warning reads
LIST_QUERIES = 10       # DW-8: +1 "← My queue" (a designer's deputy lookup)
ADD_QUERIES = 15


class QueryCountTests(DesignGroupFixture):

    def make_sites(self, n, prefix):
        return [self.site(f'{prefix}{i}', self.t1, self.des_a, 'released',
                          [(self.m_module, '2'), (self.m_cable, '3')]) for i in range(n)]

    def count(self, client, method, url, data=None):
        getattr(client, method)(url, data or {})            # warm the session
        with CaptureQueriesContext(connection) as ctx:
            response = getattr(client, method)(url, data or {})
        self.assertIn(response.status_code, (200, 302))
        return len(ctx.captured_queries)

    def test_group_screen_excel_and_list_stay_flat(self):
        client = _client(self.des_a)
        seen = {}
        for n in (5, 20):
            group = self.group(f'Size {n}', self.make_sites(n, f'Q{n}-'), by=self.des_a)
            seen[n] = (self.count(client, 'get', reverse('design_boq_group_detail', args=[group.pk])),
                       self.count(client, 'get', reverse('design_boq_group_excel', args=[group.pk])),
                       self.count(client, 'get', reverse('design_boq_group_list')))
        self.assertEqual(seen[5], seen[20])
        self.assertEqual(seen[5], (DETAIL_QUERIES, EXCEL_QUERIES, LIST_QUERIES))

    def test_the_add_post_stays_flat(self):
        client = _client(self.des_a)
        seen = {}
        for n in (5, 20):
            group = self.group(f'Add {n}', by=self.des_a)
            ids = [s.pk for s in self.make_sites(n, f'P{n}-')]
            url = reverse('design_boq_group_add_sites', args=[group.pk])
            client.get(reverse('design_boq_group_list'))            # warm the session
            with CaptureQueriesContext(connection) as ctx:
                client.post(url, {'project_ids': ids})
            self.assertEqual(group.memberships.count(), n)
            seen[n] = len(ctx.captured_queries)
        self.assertEqual(seen[5], seen[20])
        self.assertEqual(seen[5], ADD_QUERIES)
