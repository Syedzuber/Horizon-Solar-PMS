"""
B-5b verification — design BOQ group snapshots (Zuber Q3/Q4/Q7, S1-S8, answers A0-A9,
24 Nov 2026).

WHAT THIS FILE PROVES
---------------------
  * ONE SHAPE. hydrate(group_boq_data(consolidate(x))) reads exactly like consolidate(x) for
    every row kind; every key consolidate() returns is either stored or named as ignored;
    an unknown data version is refused; the stored keys are exactly the fixed list (no
    prices).
  * STORED EQUALS LIVE. A snapshot taken now renders and downloads exactly like the live tab
    and the live Excel (titles and dates aside).
  * FROZEN. After quantity, status, membership, retirement and catalogue-wording changes, a
    snapshot's page and Excel are unchanged.
  * IMMUTABLE. save() of an existing row, delete(), QuerySet update/delete/bulk_update all
    raise; no route edits or deletes one; a group with a snapshot can't be deleted, by the
    view, by Django (PROTECT) and by the database (FK check). Deleting the taker's user only
    nulls taken_by.
  * FINGERPRINT and CHANGE LIST (A2). Unchanged by a counted-to-counted status move, a
    reworded description or a retirement; changed by quantity, split, unit, ACCB, unlinked
    and counted-set changes; the change list is empty exactly when the fingerprints match.
  * TAKE / REFUSAL / ACCESS (S4, S6, A7) and GROUP DELETE (S7, A6).
  * QUERY COUNTS, pinned and flat in sites and snapshots.

Run with:
    python manage.py test projects.tests_design_boq_group_snapshots --settings=solarpms.test_settings
"""
import io
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from openpyxl import load_workbook

from . import models as models_module
from . import urls as project_urls
from .design_boq_group_views import (
    DELETE_NAME_MISMATCH, GROUP_BOQ_DATA_KEYS, GROUP_BOQ_KEYS_IGNORED, GROUP_BOQ_KEYS_READ,
    GROUP_HAS_SNAPSHOTS, NOTHING_TO_SNAPSHOT, SNAPSHOT_NOT_OPEN, _canon, _live_members,
    _member_rows, consolidate, group_boq_data, hydrate, snapshot_changes,
    snapshot_fingerprint, warning_lines, SUMMARY_SHEET,
)
from .models import (
    ActivityLog, BOQ, BOQItem, BOQItemGroup, BOQItemGroupMember, BOQItemMaster, DesignAssignment, DesignBOQGroup,
    DesignBOQGroupMembership, DesignBOQGroupSnapshot, Project, SnapshotImmutableError,
)
from .tests_accb_config import EX_TYPE1, _longest_config
from .tests_design_boq_groups import (
    EX_TYPE2, EX_TYPE2_REORDERED, EXCEL_QUERIES, PRICE_WORDS, DesignGroupFixture, _client,
    _profile,
)

#: S1, written out by hand so a change to GROUP_BOQ_DATA_KEYS fails here rather than passing.
STORED_KEYS = {
    'top':          {'version', 'group_name', 'sites', 'lines', 'accb_lines', 'unlinked',
                     'blanks', 'unit_warnings', 'content_gaps'},
    'site':         {'id', 'code', 'name', 'tender', 'designer', 'status', 'counted'},
    'line':         {'code', 'description', 'unit', 'site_count', 'total', 'retired', 'per_site'},
    'accb':         {'config_key', 'description', 'unit', 'site_count', 'total', 'retired',
                     'per_site'},
    'unlinked':     {'site', 'description', 'unit', 'quantity'},
    'blank':        {'site', 'code', 'description'},
    'unit_warning': {'site', 'code', 'quantity', 'line_unit', 'catalogue_unit', 'reasons'},
    'content_gap':  {'site', 'gaps'},
}

#: Measured at build time and pinned, for a PLAIN DESIGNER (the costliest reader: the
#: deputy lookup, once per request). Each is the same at 5 and 20 sites, and the group
#: screen is the same with 1 and 30 snapshots. Counts include the session, user and
#: profile loads; the take POST includes its savepoints and the group-row lock. With
#: snapshots the group screen adds the readable check on their stored sites and the newest
#: one's data (the snapshot list itself is already in the 17).
#: DW-1b: +1 each on the screen and the take, for the formula-group members the content-gap
#: warning reads (one query, whatever the number of sites). Snapshot view/Excel unchanged.
#: DW-8: +1 on the group screen and the snapshot view for "← My queue" (a designer's deputy
#: lookup). The take and both Excel files are unchanged.
DETAIL_NO_SNAPSHOTS = 19
DETAIL_WITH_SNAPSHOTS = 21
TAKE_QUERIES = 19
SNAPSHOT_VIEW_QUERIES = 9
SNAPSHOT_EXCEL_QUERIES = 6


def _rows(ws):
    return [list(r) for r in ws.iter_rows(values_only=True)]


class SnapshotFixture(DesignGroupFixture):

    def live_data(self, group):
        rows = _member_rows(_live_members(group))
        return group_boq_data(group.name, rows, consolidate(rows))

    def take(self, profile, group, note='', follow=True):
        return _client(profile).post(reverse('design_boq_group_snapshot_take', args=[group.pk]),
                                     {'note': note}, follow=follow)

    def orm_snapshot(self, group, site_ids=None, by=None):
        """A snapshot written straight to the table (no access check) for access tests."""
        data = self.live_data(group)
        counted = sum(1 for s in data['sites'] if s['counted'])
        return DesignBOQGroupSnapshot.objects.create(
            group=group, taken_by=by or self.head, taken_by_name='Head', data=data,
            fingerprint=snapshot_fingerprint(data),
            site_ids=site_ids if site_ids is not None else [s['id'] for s in data['sites']],
            sites_counted=counted, sites_not_counted=len(data['sites']) - counted)

    def load_content_groups(self):
        """DW-1b: the three B-2c formula groups the content-gap warning reads, each with one
        item of its own, as load_formula_rules would make them. Returns the masters."""
        masters = {}
        for code, name, description in (('OPX-010', 'all inverters', 'String inverter'),
                                         ('OPX-020', 'all DCDBs', 'DCDB'),
                                         ('OPX-030', 'all ACDBs', 'ACDB')):
            master = BOQItemMaster.objects.create(
                code=code, description=description, unit='Nos', category='Electrical',
                project_type='OPEX', sort_order=int(code[4:]))
            group = BOQItemGroup.objects.create(name=name, label=name.split(' ', 1)[1])
            BOQItemGroupMember(group=group, item=master).save()
            masters[name] = master
        return masters

    def snapshot_page(self, profile, snapshot):
        return _client(profile).get(reverse('design_boq_group_snapshot_detail',
                                            args=[snapshot.group_id, snapshot.pk]))

    def snapshot_xlsx(self, profile, snapshot):
        response = _client(profile).get(reverse('design_boq_group_snapshot_excel',
                                                args=[snapshot.group_id, snapshot.pk]))
        self.assertEqual(response.status_code, 200)
        return response, load_workbook(io.BytesIO(response.content))

    def live_xlsx(self, profile, group):
        response = _client(profile).get(reverse('design_boq_group_excel', args=[group.pk]))
        return load_workbook(io.BytesIO(response.content))

    def detail(self, profile, group):
        return _client(profile).get(reverse('design_boq_group_detail', args=[group.pk]))

    @staticmethod
    def boq_html(html):
        """The consolidated BOQ tables as rendered — lines, ACCBs, lines with no catalogue
        item — each from its id to its closing tag; absent tables are left out."""
        parts = []
        for table_id in ('boqGroupLines', 'boqGroupAccbs', 'boqGroupUnlinked'):
            marker = f'id="{table_id}"'
            if marker in html:
                start = html.index(marker)
                parts.append(html[start:html.index('</table>', start)])
        return parts


# ═════════════════════════════════════════════════════════════════════════════
# One shape: the plain data and its round trip
# ═════════════════════════════════════════════════════════════════════════════

class PlainDataTests(SnapshotFixture):

    def every_kind(self):
        """A group holding every row kind: counted lines with a per-site split, a retired
        item, an ACCB, a line with no catalogue item, a blank, a fractional Tin Shade, a
        unit mismatch, a not-counted site and (DW-1b) counted sites missing an inverter."""
        self.load_content_groups()
        self.accb(self.a1, EX_TYPE2)
        self.accb(self.a2, EX_TYPE2_REORDERED)
        group = self.group('Every kind', [self.a1, self.a2, self.a3, self.a4])
        rows = _member_rows(_live_members(group))
        boq = consolidate(rows)
        # The fixture really holds every kind.
        self.assertTrue(any(len(l['per_site_list']) > 1 for l in boq['lines']))
        self.assertTrue(any(l['retired'] for l in boq['lines']))
        self.assertTrue(boq['accb_lines'] and boq['unlinked'] and boq['blanks'] and boq['not_counted'])
        self.assertTrue(boq['content_gaps'])
        self.assertEqual({tuple(w['reasons']) for w in boq['unit_warnings']},
                         {('not a whole number',), ('unit differs from the catalogue',)})
        return group, rows, boq

    def test_hydrated_data_reads_exactly_like_consolidate(self):
        group, rows, boq = self.every_kind()
        view = hydrate(group_boq_data(group.name, rows, boq))
        out = view['boq']
        line_keys = ('item_master__code', 'item_master__description', 'item_master__unit',
                     'site_count', 'total_quantity', 'retired', 'per_site_list', 'per_site')
        self.assertEqual([{k: l[k] for k in line_keys} for l in out['lines']],
                         [{k: l[k] for k in line_keys} for l in boq['lines']])
        accb_keys = ('config_key', 'description', 'unit', 'site_count', 'total_quantity',
                     'retired', 'per_site_list', 'per_site', 'code')
        self.assertEqual([{k: a[k] for k in accb_keys} for a in out['accb_lines']],
                         [{k: a[k] for k in accb_keys} for a in boq['accb_lines']])
        # Unlinked rows: BOQItem objects compared by the field the template and Excel read.
        self.assertEqual([(u['site'], u['description'], u['unit'], u['quantity']) for u in out['unlinked']],
                         [(i.boq.project.project_id, i.description, i.uom, i.boq_quantity)
                          for i in boq['unlinked']])
        self.assertEqual(out['counted_codes'], boq['counted_codes'])
        self.assertEqual([(r['code'], r['status']) for r in out['not_counted']],
                         [(r['code'], r['status']) for r in boq['not_counted']])
        self.assertEqual(out['blanks'], boq['blanks'])
        self.assertEqual(out['unit_warnings'], boq['unit_warnings'])
        self.assertEqual(warning_lines(out), warning_lines(boq))
        # Sites: what the Sites sheet reads, against the member rows it used to read.
        self.assertEqual([(s['code'], s['name'], s['tender'], s['designer'], s['status'], s['counted'])
                          for s in view['sites']],
                         [(r['code'], r['project'].site_name or '', r['tender'], r['designer'],
                           r['status'], r['counted']) for r in rows])

    def test_every_key_consolidate_returns_is_stored_or_named_as_ignored(self):
        _group, rows, boq = self.every_kind()
        levels = {
            'boq':          [boq],
            'line':         boq['lines'],
            'accb':         boq['accb_lines'],
            'blank':        boq['blanks'],
            'unit_warning': boq['unit_warnings'],
            'content_gap':  boq['content_gaps'],
            'member_row':   rows,
        }
        for level, dicts in levels.items():
            handled = GROUP_BOQ_KEYS_READ[level] | set(GROUP_BOQ_KEYS_IGNORED[level])
            for d in dicts:
                with self.subTest(level=level):
                    self.assertEqual(set(d), handled)

    def test_hydrate_refuses_an_unknown_version(self):
        data = self.live_data(self.group('G', [self.a1]))
        for bad in (0, 3, None, '1', '2', True):
            with self.subTest(version=bad):
                data['version'] = bad
                with self.assertRaisesRegex(ValueError, 'is not one this code can read'):
                    hydrate(data)
        del data['version']
        with self.assertRaisesRegex(ValueError, 'is not one this code can read'):
            hydrate(data)

    def test_stored_keys_are_exactly_the_fixed_list_and_hold_no_price(self):
        group, _rows, _boq = self.every_kind()
        self.take(self.head, group)
        data = DesignBOQGroupSnapshot.objects.get().data
        self.assertEqual({k: set(v) for k, v in GROUP_BOQ_DATA_KEYS.items()}, STORED_KEYS)
        self.assertEqual(set(data), STORED_KEYS['top'])
        for level, key in (('site', 'sites'), ('line', 'lines'), ('accb', 'accb_lines'),
                           ('unlinked', 'unlinked'), ('blank', 'blanks'),
                           ('unit_warning', 'unit_warnings'), ('content_gap', 'content_gaps')):
            self.assertTrue(data[key], key)
            for row in data[key]:
                with self.subTest(level=level):
                    self.assertEqual(set(row), STORED_KEYS[level])
        every_key = set().union(*STORED_KEYS.values())
        self.assertFalse([k for k in every_key if any(w in k.lower() for w in PRICE_WORDS)])

    def test_canonical_quantities(self):
        for raw in ('2340', '2340.00', '2340.000', '2.34E+3'):
            self.assertEqual(_canon(Decimal(raw)), '2340')
        self.assertEqual((_canon(Decimal('38.40')), _canon(Decimal('0.00')), _canon(Decimal('-0')),
                          _canon(None)), ('38.4', '0', '0', None))


# ═════════════════════════════════════════════════════════════════════════════
# Taking a snapshot
# ═════════════════════════════════════════════════════════════════════════════

class TakeTests(SnapshotFixture):

    def test_a_snapshot_stores_the_live_data_who_when_and_the_note(self):
        group = self.group('G', [self.a1, self.a2, self.a3], by=self.des_a)
        live = self.live_data(group)
        response = self.take(self.des_a, group, note='  Sent for vendor RFQ  ')
        snap = DesignBOQGroupSnapshot.objects.get()
        self.assertEqual(snap.data, live)
        self.assertEqual((snap.group, snap.taken_by, snap.taken_by_name, snap.note),
                         (group, self.des_a, 'Desa', 'Sent for vendor RFQ'))
        self.assertEqual((snap.sites_counted, snap.sites_not_counted), (2, 1))
        self.assertEqual(snap.site_ids, [self.a1.pk, self.a2.pk, self.a3.pk])
        self.assertEqual(snap.fingerprint, snapshot_fingerprint(live))
        self.assertIn('Snapshot saved: 2 sites counted.', self.msgs(response))
        self.assertEqual(response.redirect_chain[-1][0],
                         reverse('design_boq_group_detail', args=[group.pk]) + '?tab=snapshots')
        log = ActivityLog.objects.get(action_code='design_boq_group_snapshot_taken')
        self.assertEqual(log.action, 'Desa took a snapshot of design BOQ group "G": 2 sites counted')
        self.assertEqual((log.entity_type, log.entity_id), ('DesignBOQGroup', group.pk))
        # It shows in the Snapshots tab.
        html = self.detail(self.des_a, group).content.decode()
        table = html.split('id="boqGroupSnapshots"', 1)[1].split('</table>', 1)[0]
        for text in ('Desa', '2 counted, 1 not counted', 'Sent for vendor RFQ', '>View<', '>Excel<'):
            self.assertIn(text, table)

    def test_a_group_with_no_counted_site_is_refused(self):
        group = self.group('G', [self.a3])
        response = self.take(self.head, group)
        self.assertIn(NOTHING_TO_SNAPSHOT, self.msgs(response))
        self.assertFalse(DesignBOQGroupSnapshot.objects.exists())
        response = self.take(self.head, self.group('Empty'))
        self.assertIn(NOTHING_TO_SNAPSHOT, self.msgs(response))
        self.assertFalse(DesignBOQGroupSnapshot.objects.exists())

    def test_a_note_over_200_characters_is_refused_and_two_in_a_minute_are_allowed(self):
        group = self.group('G', [self.a1])
        self.assertIn('Keep the note to 200 characters.', self.msgs(self.take(self.head, group, 'x' * 201)))
        self.assertFalse(DesignBOQGroupSnapshot.objects.exists())
        self.take(self.head, group, 'x' * 200)
        self.take(self.head, group)
        self.assertEqual(group.snapshots.count(), 2)

    def test_a_get_takes_nothing(self):
        group = self.group('G', [self.a1])
        _client(self.head).get(reverse('design_boq_group_snapshot_take', args=[group.pk]))
        self.assertFalse(DesignBOQGroupSnapshot.objects.exists())

    def test_the_group_list_counts_snapshots(self):
        group = self.group('G', [self.a1])
        self.group('Other', [self.a2])
        self.take(self.head, group)
        self.take(self.head, group)
        groups = {g.name: g for g in _client(self.head).get(reverse('design_boq_group_list')).context['groups']}
        self.assertEqual((groups['G'].snapshot_count, groups['G'].site_count), (2, 1))
        self.assertEqual(groups['Other'].snapshot_count, 0)


# ═════════════════════════════════════════════════════════════════════════════
# Stored equals live, and frozen
# ═════════════════════════════════════════════════════════════════════════════

class StoredEqualsLiveTests(SnapshotFixture):

    def setUp(self):
        self.accb(self.a1, EX_TYPE2)
        self.grp = self.group('Lot 2', [self.a1, self.a2, self.a3, self.a4])

    def test_a_snapshot_taken_now_reads_and_downloads_exactly_like_the_live_view(self):
        live_page = self.detail(self.head, self.grp)
        live_wb = self.live_xlsx(self.head, self.grp)
        self.take(self.head, self.grp)
        snap = DesignBOQGroupSnapshot.objects.get()
        page = self.snapshot_page(self.head, snap)
        self.assertEqual(page.status_code, 200)
        # The values the page renders from, field by field.
        self.assertEqual(page.context['boq'], live_page.context['boq'])
        self.assertEqual(page.context['warnings'], live_page.context['warnings'])
        # And the rendered tables.
        self.assertEqual(self.boq_html(page.content.decode()), self.boq_html(live_page.content.decode()))
        # The Excel, every cell of every sheet except the Summary's title and intro line.
        response, wb = self.snapshot_xlsx(self.head, snap)
        self.assertEqual(wb.sheetnames, live_wb.sheetnames)
        for name in wb.sheetnames:
            ours, theirs = _rows(wb[name]), _rows(live_wb[name])
            if name == 'Summary':
                self.assertTrue(ours[0][0].startswith('Lot 2 — snapshot of '))
                self.assertTrue(ours[1][0].startswith('Snapshot taken '))
                ours, theirs = ours[2:], theirs[2:]
            self.assertEqual(ours, theirs, name)
        self.assertRegex(response['Content-Disposition'],
                         r'^attachment; filename="Lot 2_snapshot_\d{4}-\d{2}-\d{2}_\d{4}\.xlsx"$')

    def test_a_snapshot_is_frozen(self):
        self.take(self.head, self.grp, note='Before the changes')
        snap = DesignBOQGroupSnapshot.objects.get()
        page_before = self.snapshot_page(self.head, snap)
        _r, wb_before = self.snapshot_xlsx(self.head, snap)
        before = {n: _rows(wb_before[n]) for n in wb_before.sheetnames}
        live_before = self.detail(self.head, self.grp).context['boq']

        # A counted site's quantity, a site out of the counted set, a site out of the group,
        # an item retired and a catalogue description reworded — and the group renamed.
        BOQItem.objects.filter(boq__project=self.a1, item_master=self.m_module).update(boq_quantity=Decimal('12'))
        DesignAssignment.objects.filter(project=self.a2).update(status='in_design')
        self.grp.memberships.filter(project=self.a4).delete()
        BOQItemMaster.objects.filter(pk=self.m_module.pk).update(is_active=False)
        BOQItemMaster.objects.filter(pk=self.m_cable.pk).update(description='DC cable, reworded')
        DesignBOQGroup.objects.filter(pk=self.grp.pk).update(name='Renamed')

        self.assertNotEqual(self.detail(self.head, self.grp).context['boq'], live_before)   # live moved
        page_after = self.snapshot_page(self.head, snap)
        self.assertEqual(page_after.context['boq'], page_before.context['boq'])
        self.assertEqual(page_after.context['view'], page_before.context['view'])
        self.assertEqual(self.boq_html(page_after.content.decode()), self.boq_html(page_before.content.decode()))
        response, wb_after = self.snapshot_xlsx(self.head, snap)
        self.assertEqual({n: _rows(wb_after[n]) for n in wb_after.sheetnames}, before)
        # The stored name, in the title and the file name (A3); statuses at the time (S5).
        self.assertIn('filename="Lot 2_snapshot_', response['Content-Disposition'])
        sites = {r[0]: r[4] for r in before['Sites'][1:]}
        self.assertEqual((sites['B5A-A2'], sites['B5A-A4']), ('In QC', 'Awaiting PM approval'))
        self.assertIn('DC cable', [r[1] for r in before['Summary']])


# ═════════════════════════════════════════════════════════════════════════════
# Immutable
# ═════════════════════════════════════════════════════════════════════════════

class ImmutableTests(SnapshotFixture):

    def setUp(self):
        self.grp = self.group('G', [self.a1])
        self.take(self.head, self.grp)
        self.snap = DesignBOQGroupSnapshot.objects.get()

    def test_no_write_changes_or_removes_a_snapshot(self):
        self.snap.note = 'edited'
        with self.assertRaises(SnapshotImmutableError):
            self.snap.save()
        with self.assertRaises(SnapshotImmutableError):
            self.snap.delete()
        qs = DesignBOQGroupSnapshot.objects.filter(pk=self.snap.pk)
        with self.assertRaises(SnapshotImmutableError):
            qs.update(note='edited')
        with self.assertRaises(SnapshotImmutableError):
            qs.update(taken_by=None, note='edited')        # only a bare taken_by=None passes
        with self.assertRaises(SnapshotImmutableError):
            qs.delete()
        with self.assertRaises(SnapshotImmutableError):
            DesignBOQGroupSnapshot.objects.bulk_update([self.snap], ['note'])
        fresh = DesignBOQGroupSnapshot.objects.get()
        self.assertEqual((fresh.note, fresh.data), ('', self.live_data(self.grp)))

    def test_no_route_edits_or_deletes_a_snapshot(self):
        names = {p.name for p in project_urls.urlpatterns if getattr(p, 'name', None)
                 and 'snapshot' in p.name and p.name.startswith('design_boq_group')}
        self.assertEqual(names, {'design_boq_group_snapshot_take', 'design_boq_group_snapshot_detail',
                                 'design_boq_group_snapshot_excel'})
        client = _client(self.head)
        for name in ('design_boq_group_snapshot_detail', 'design_boq_group_snapshot_excel'):
            client.post(reverse(name, args=[self.grp.pk, self.snap.pk]), {'note': 'x', 'data': '{}'})
        fresh = DesignBOQGroupSnapshot.objects.get()
        self.assertEqual((fresh.note, fresh.data, DesignBOQGroupSnapshot.objects.count()),
                         ('', self.snap.data, 1))

    def test_a_group_with_a_snapshot_cant_be_deleted_by_the_view_django_or_the_database(self):
        response = _client(self.head).post(reverse('design_boq_group_delete', args=[self.grp.pk]),
                                           {'confirm_name': 'G'}, follow=True)
        self.assertIn(GROUP_HAS_SNAPSHOTS, self.msgs(response))
        self.assertTrue(DesignBOQGroup.objects.filter(pk=self.grp.pk).exists())
        with self.assertRaises(ProtectedError):
            self.grp.delete()
        # Under Django's collector: a raw DELETE fails the snapshot's FK check.
        self.grp.memberships.all().delete()
        with self.assertRaises(IntegrityError), transaction.atomic():
            DesignBOQGroup.objects.filter(pk=self.grp.pk)._raw_delete(connection.alias)
            connection.check_constraints(table_names=['projects_designboqgroupsnapshot'])
        self.assertTrue(DesignBOQGroup.objects.filter(pk=self.grp.pk).exists())

    def test_deleting_the_taker_only_clears_taken_by(self):
        temp = _profile('b5b_temp', 'Design', is_design_qc=True)
        snap = self.orm_snapshot(self.grp, by=temp)
        temp.user.delete()
        snap = DesignBOQGroupSnapshot.objects.get(pk=snap.pk)
        self.assertIsNone(snap.taken_by)
        self.assertEqual(snap.taken_by_name, 'Head')


# ═════════════════════════════════════════════════════════════════════════════
# Fingerprint and change list
# ═════════════════════════════════════════════════════════════════════════════

class FingerprintTests(SnapshotFixture):

    def setUp(self):
        self.accb(self.a1, EX_TYPE2)
        self.grp = self.group('G', [self.a1, self.a2, self.a3, self.a4])
        self.old = self.live_data(self.grp)

    def compare(self):
        new = self.live_data(self.grp)
        changes = snapshot_changes(self.old, new)
        same = snapshot_fingerprint(self.old) == snapshot_fingerprint(new)
        # THE invariant: the change list is empty exactly when the fingerprints are equal.
        self.assertEqual(changes['any'], not same)
        return same, changes

    def test_unchanged_by_status_moves_within_counted_rewording_or_retiring(self):
        DesignAssignment.objects.filter(project=self.a2).update(status='awaiting_head_qc')
        BOQItemMaster.objects.filter(pk=self.m_module.pk).update(description='Reworded', is_active=False)
        same, _changes = self.compare()
        self.assertTrue(same)

    def test_a_quantity_change_lists_old_and_new(self):
        BOQItem.objects.filter(boq__project=self.a1, item_master=self.m_module).update(boq_quantity=Decimal('2302'))
        same, changes = self.compare()
        self.assertFalse(same)
        self.assertEqual(changes['totals'], [{'label': 'OPX-001', 'old': '18', 'new': '2,310'}])
        self.assertEqual((changes['units'], changes['split'], changes['sites_added']), ([], [], []))

    def test_a_split_change_with_the_same_total(self):
        BOQItem.objects.filter(boq__project=self.a1, item_master=self.m_module).update(boq_quantity=Decimal('9'))
        BOQItem.objects.filter(boq__project=self.a4, item_master=self.m_module).update(boq_quantity=Decimal('4'))
        same, changes = self.compare()
        self.assertFalse(same)
        self.assertEqual((changes['split'], changes['totals']), (['OPX-001'], []))

    def test_a_unit_change(self):
        BOQItemMaster.objects.filter(pk=self.m_tin.pk).update(unit='KWp')
        same, changes = self.compare()
        self.assertFalse(same)
        self.assertEqual(changes['units'], [{'label': 'OPX-038', 'old': '38.4 Nos', 'new': '38.4 KWp'}])

    def test_a_site_joining_and_a_site_leaving_the_totals(self):
        DesignAssignment.objects.filter(project=self.a3).update(status='released')
        DesignAssignment.objects.filter(project=self.a4).update(status='in_design')
        same, changes = self.compare()
        self.assertFalse(same)
        self.assertEqual((changes['sites_added'], changes['sites_dropped']), (['B5A-A3'], ['B5A-A4']))
        # A4's retired clamp and unlinked line went with it.
        self.assertIn({'label': 'OPX-099', 'old': '2', 'new': None}, changes['totals'])
        self.assertIn({'label': 'B5A-A4 Ad-hoc fitting A4 (no catalogue item)', 'old': '4', 'new': None},
                      changes['totals'])

    def test_a_site_leaving_the_group(self):
        self.grp.memberships.filter(project=self.a1).delete()
        same, changes = self.compare()
        self.assertFalse(same)
        self.assertEqual(changes['sites_dropped'], ['B5A-A1'])

    def test_an_accb_change_is_labelled_by_its_full_text(self):
        config = self.accb(self.a2, _longest_config())
        text = BOQItem.objects.get(pk=config.boq_item_id).description
        self.assertGreater(len(text), 255)
        same, changes = self.compare()
        self.assertFalse(same)
        self.assertEqual(changes['totals'], [{'label': text, 'old': None, 'new': '1'}])
        # The same configuration on another site: total moves, labelled the same way.
        self.old = self.live_data(self.grp)
        self.accb(self.a4, EX_TYPE1)
        self.accb(self.a2, EX_TYPE2_REORDERED)
        same, changes = self.compare()
        type2 = next(a['description'] for a in self.old['accb_lines'] if a['total'] == '1'
                     and a['per_site'] == [['B5A-A1', '1']])
        self.assertIn({'label': type2, 'old': '1', 'new': '2'}, changes['totals'])

    def test_a_line_with_no_catalogue_item_added(self):
        BOQItem.objects.create(boq=BOQ.objects.get(project=self.a1), serial_no=20, category='Other',
                               item_master=None, description='Ad-hoc bracket', uom='Nos',
                               boq_quantity=Decimal('6'))
        same, changes = self.compare()
        self.assertFalse(same)
        self.assertEqual(changes['totals'], [{'label': 'B5A-A1 Ad-hoc bracket (no catalogue item)',
                                              'old': None, 'new': '6'}])

    def test_the_banner_shows_the_list_and_only_when_it_differs(self):
        html = self.detail(self.head, self.grp).content.decode()
        self.assertNotIn('id="boqGroupDiffers"', html)                    # no snapshot yet
        self.take(self.head, self.grp)
        self.assertNotIn('id="boqGroupDiffers"', self.detail(self.head, self.grp).content.decode())
        DesignAssignment.objects.filter(project=self.a2).update(status='awaiting_head_qc')
        self.assertNotIn('id="boqGroupDiffers"', self.detail(self.head, self.grp).content.decode())
        BOQItem.objects.filter(boq__project=self.a1, item_master=self.m_module).update(boq_quantity=Decimal('12'))
        DesignAssignment.objects.filter(project=self.a3).update(status='released')
        response = self.detail(self.head, self.grp)
        banner = response.content.decode().split('id="boqGroupDiffers"', 1)[1].split('</ul>', 1)[0]
        self.assertIn('Live totals differ from the last snapshot', banner)
        self.assertIn('Added to the totals: B5A-A3', banner)
        self.assertIn('OPX-001: 18 → 27', banner)


# ═════════════════════════════════════════════════════════════════════════════
# Access
# ═════════════════════════════════════════════════════════════════════════════

class SnapshotAccessTests(SnapshotFixture):

    def test_non_design_users_are_refused_every_snapshot_route(self):
        group = self.group('G', [self.a1])
        snap = self.orm_snapshot(group)
        routes = [('post', reverse('design_boq_group_snapshot_take', args=[group.pk]), {}),
                  ('get', reverse('design_boq_group_snapshot_detail', args=[group.pk, snap.pk]), {}),
                  ('get', reverse('design_boq_group_snapshot_excel', args=[group.pk, snap.pk]), {}),
                  ('post', reverse('design_boq_group_delete', args=[group.pk]), {'confirm_name': 'G'})]
        for profile in (self.scm, self.pm, self.coord, self.admin, self.ceo, self.finance, self.se):
            client = _client(profile)
            for method, url, data in routes:
                with self.subTest(role=profile.role, url=url):
                    self.assertEqual(getattr(client, method)(url, data).status_code, 403)
        self.assertEqual(DesignBOQGroupSnapshot.objects.count(), 1)
        self.assertTrue(DesignBOQGroup.objects.filter(pk=group.pk).exists())

    def test_a_site_that_left_the_group_still_guards_its_snapshot(self):
        group = self.group('Mixed', [self.a1, self.b1])
        self.take(self.head, group)
        snap = DesignBOQGroupSnapshot.objects.get()
        group.memberships.filter(project=self.b1).delete()
        # The designer can open the group now (only A1 is left) ...
        response = self.detail(self.des_a, group)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        table = html.split('id="boqGroupSnapshots"', 1)[1].split('</table>', 1)[0]
        self.assertIn(SNAPSHOT_NOT_OPEN.replace("'", '&#x27;'), table)
        self.assertNotIn(reverse('design_boq_group_snapshot_detail', args=[group.pk, snap.pk]), html)
        # ... and isn't shown a comparison built from a site they can't open.
        self.assertNotIn('id="boqGroupDiffers"', html)
        self.assertIn('id="boqGroupNotCompared"', html)
        # ... but the snapshot itself, page and Excel, is refused.
        for name in ('design_boq_group_snapshot_detail', 'design_boq_group_snapshot_excel'):
            response = _client(self.des_a).get(reverse(name, args=[group.pk, snap.pk]))
            self.assertEqual((response.status_code, response.content.decode()), (403, SNAPSHOT_NOT_OPEN))
        for profile in (self.head, self.qc, self.deputy):
            self.assertEqual(self.snapshot_page(profile, snap).status_code, 200)

    def test_a_snapshot_the_designer_can_read_opens_and_a_soft_deleted_site_still_guards(self):
        mine = self.group('Mine', [self.a1, self.a2])
        self.take(self.des_a, mine)
        self.assertEqual(self.snapshot_page(self.des_a, mine.snapshots.get()).status_code, 200)
        theirs = self.group('Theirs', [self.b1])
        snap = self.orm_snapshot(theirs)
        Project.objects.filter(pk=self.b1.pk).update(is_deleted=True)
        self.assertEqual(self.snapshot_page(self.des_a, snap).status_code, 403)
        self.assertEqual(self.snapshot_page(self.head, snap).status_code, 200)

    def test_a_stored_site_that_no_longer_exists(self):
        """Zuber A7: Head, QC and the deputy (who read every site) still open it; nobody else."""
        group = self.group('G', [self.a1])
        snap = self.orm_snapshot(group, site_ids=[self.a1.pk, 999999])
        for profile, status in ((self.head, 200), (self.qc, 200), (self.deputy, 200), (self.des_a, 403)):
            with self.subTest(user=profile.user.username):
                self.assertEqual(self.snapshot_page(profile, snap).status_code, status)

    def test_another_groups_snapshot_is_a_404(self):
        snap = self.orm_snapshot(self.group('G', [self.a1]))
        other = self.group('Other', [self.a2])
        response = _client(self.head).get(reverse('design_boq_group_snapshot_detail', args=[other.pk, snap.pk]))
        self.assertEqual(response.status_code, 404)

    def test_a_designer_cant_snapshot_a_group_they_cant_open(self):
        group = self.group('Mixed', [self.a1, self.b1])
        response = self.take(self.des_a, group, follow=False)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(DesignBOQGroupSnapshot.objects.exists())


# ═════════════════════════════════════════════════════════════════════════════
# Group delete (S7)
# ═════════════════════════════════════════════════════════════════════════════

class GroupDeleteTests(SnapshotFixture):

    def delete(self, profile, group, typed, follow=True):
        return _client(profile).post(reverse('design_boq_group_delete', args=[group.pk]),
                                     {'confirm_name': typed}, follow=follow)

    def test_a_group_with_no_snapshots_is_deleted_once_its_name_is_typed(self):
        group = self.group('Lot 2', [self.a1, self.a2])
        lines = BOQItem.objects.filter(boq__project__in=[self.a1, self.a2]).count()
        response = self.delete(self.head, group, '  Lot 2  ')
        self.assertFalse(DesignBOQGroup.objects.filter(pk=group.pk).exists())
        self.assertFalse(DesignBOQGroupMembership.objects.filter(group_id=group.pk).exists())
        self.assertEqual(BOQItem.objects.filter(boq__project__in=[self.a1, self.a2]).count(), lines)
        self.assertIn('Deleted "Lot 2". The sites\' BOQs are unchanged.', self.msgs(response))
        log = ActivityLog.objects.get(action_code='design_boq_group_deleted')
        self.assertEqual(log.action, 'Head deleted design BOQ group "Lot 2" with 2 sites: B5A-A1, B5A-A2')
        self.assertEqual(log.entity_id, group.pk)

    def test_the_name_must_match_exactly(self):
        group = self.group('Lot 2', [self.a1])
        for typed in ('lot 2', 'Lot  2', '', 'Lot 2x'):
            with self.subTest(typed=typed):
                self.assertIn(DELETE_NAME_MISMATCH, self.msgs(self.delete(self.head, group, typed)))
                self.assertTrue(DesignBOQGroup.objects.filter(pk=group.pk).exists())
        # An empty group goes too, logged with no sites.
        empty = self.group('Empty')
        self.delete(self.des_a, empty, 'Empty')
        self.assertFalse(DesignBOQGroup.objects.filter(pk=empty.pk).exists())
        self.assertEqual(ActivityLog.objects.get(action_code='design_boq_group_deleted').action,
                         'Desa deleted design BOQ group "Empty" with no sites')

    def test_delete_shows_only_without_snapshots(self):
        group = self.group('G', [self.a1])
        self.assertIn('Delete group', self.detail(self.head, group).content.decode())
        self.take(self.head, group)
        html = self.detail(self.head, group).content.decode()
        self.assertNotIn('Delete group', html)
        self.assertNotIn(reverse('design_boq_group_delete', args=[group.pk]), html)
        self.assertIn(GROUP_HAS_SNAPSHOTS, self.msgs(self.delete(self.head, group, 'G')))
        self.assertTrue(DesignBOQGroup.objects.filter(pk=group.pk).exists())

    def test_refused_for_a_user_who_cant_open_every_member_and_on_get(self):
        group = self.group('Mixed', [self.a1, self.b1])
        self.assertEqual(self.delete(self.des_a, group, 'Mixed', follow=False).status_code, 403)
        _client(self.head).get(reverse('design_boq_group_delete', args=[group.pk]))
        self.assertTrue(DesignBOQGroup.objects.filter(pk=group.pk).exists())


# ═════════════════════════════════════════════════════════════════════════════
# Query counts
# ═════════════════════════════════════════════════════════════════════════════

class SnapshotQueryCountTests(SnapshotFixture):

    def make_sites(self, n, prefix):
        return [self.site(f'{prefix}{i}', self.t1, self.des_a, 'released',
                          [(self.m_module, '2'), (self.m_cable, '3')]) for i in range(n)]

    def count(self, client, method, url, data=None):
        client.get(reverse('design_boq_group_list'))                  # warm the session
        with CaptureQueriesContext(connection) as ctx:
            response = getattr(client, method)(url, data or {})
        self.assertIn(response.status_code, (200, 302))
        return len(ctx.captured_queries)

    def test_take_view_and_excel_stay_flat_in_sites(self):
        client = _client(self.des_a)
        seen = {}
        for n in (5, 20):
            group = self.group(f'Size {n}', self.make_sites(n, f'S{n}-'), by=self.des_a)
            take = self.count(client, 'post', reverse('design_boq_group_snapshot_take', args=[group.pk]))
            snap = group.snapshots.get()
            seen[n] = (take,
                       self.count(client, 'get', reverse('design_boq_group_snapshot_detail', args=[group.pk, snap.pk])),
                       self.count(client, 'get', reverse('design_boq_group_snapshot_excel', args=[group.pk, snap.pk])))
        self.assertEqual(seen[5], seen[20])
        self.assertEqual(seen[5], (TAKE_QUERIES, SNAPSHOT_VIEW_QUERIES, SNAPSHOT_EXCEL_QUERIES))

    def test_group_screen_is_flat_in_snapshots(self):
        client = _client(self.des_a)
        group = self.group('G', self.make_sites(5, 'G-'), by=self.des_a)
        url = reverse('design_boq_group_detail', args=[group.pk])
        seen = {0: self.count(client, 'get', url)}
        self.orm_snapshot(group, by=self.des_a)
        seen[1] = self.count(client, 'get', url)
        for _ in range(29):
            self.orm_snapshot(group, by=self.des_a)
        BOQItem.objects.filter(boq__project__in=group.memberships.values('project')[:1]).update(
            boq_quantity=Decimal('9'))                                  # so the banner is built too
        seen[30] = self.count(client, 'get', url)
        self.assertIn('id="boqGroupDiffers"', client.get(url).content.decode())
        self.assertEqual(seen, {0: DETAIL_NO_SNAPSHOTS, 1: DETAIL_WITH_SNAPSHOTS,
                                30: DETAIL_WITH_SNAPSHOTS})


# ═════════════════════════════════════════════════════════════════════════════
# DW-1b — counted sites missing an inverter or a DCDB, ACDB or ACCB
# ═════════════════════════════════════════════════════════════════════════════

#: The group-screen words for a site with both gaps (BOQ_GAP_WORDS, Zuber DW-1 Q5).
BOTH_GAPS = ('no inverter line with a quantity; no DCDB, ACDB or ACCB line with a quantity. '
             'Still counted in the totals.')
BOTH_KEYS = ['no_inverter', 'no_dcdb_acdb_accb']


class ContentGapTests(SnapshotFixture):

    def setUp(self):
        super().setUp()
        models_module._MINIMUM_CONTENT_LOGGED.clear()
        self.masters = self.load_content_groups()
        self.inv = self.masters['all inverters']
        self.dcdb = self.masters['all DCDBs']
        self.acdb = self.masters['all ACDBs']

    def add_line(self, site, master, qty):
        BOQItem.objects.create(
            boq=BOQ.objects.get(project=site), serial_no=50, category='Electrical',
            item_master=master, description=master.description, uom=master.unit,
            boq_quantity=None if qty is None else Decimal(qty))

    @staticmethod
    def gap_lines(lines):
        return [line for line in lines if 'with a quantity' in line]

    @staticmethod
    def cells(workbook):
        return [row[0] for row in _rows(workbook[SUMMARY_SHEET]) if row and row[0]]

    def test_failing_counted_sites_warn_and_others_do_not(self):
        self.add_line(self.a1, self.inv, '2')        # A1 passes: inverter + ACDB
        self.add_line(self.a1, self.acdb, '1')
        self.add_line(self.a2, self.inv, '1')        # A2: the board gap only
        self.add_line(self.a2, self.dcdb, None)      # a blank DCDB does not count
        self.add_line(self.a3, self.inv, '0')        # A3 is not counted: never checked
        group = self.group('G', [self.a1, self.a2, self.a3, self.a4])   # A4: module only
        boq = self.boq_of(group)
        self.assertEqual(boq['content_gaps'], [
            {'site': 'B5A-A2', 'gaps': ['no_dcdb_acdb_accb']},
            {'site': 'B5A-A4', 'gaps': BOTH_KEYS}])
        expected = ['B5A-A2: no DCDB, ACDB or ACCB line with a quantity. Still counted in '
                    'the totals.', f'B5A-A4: {BOTH_GAPS}']
        self.assertEqual(self.gap_lines(warning_lines(boq)), expected)

        html = self.detail(self.head, group).content.decode()
        for line in expected:
            self.assertIn(line, html)
        self.assertNotIn('B5A-A1: no', html)
        self.assertNotIn('B5A-A3: no', html)
        self.assertEqual(self.gap_lines(self.cells(self.live_xlsx(self.head, group))), expected)

    def test_an_accb_satisfies_the_board_test(self):
        self.add_line(self.a1, self.inv, '1')
        self.accb(self.a1, EX_TYPE2)
        self.assertEqual(self.boq_of(self.group('G', [self.a1]))['content_gaps'], [])

    def test_totals_and_fingerprint_do_not_depend_on_the_check(self):
        group = self.group('G', [self.a1, self.a2, self.a4])
        checked = self.live_data(group)
        self.assertEqual(len(checked['content_gaps']), 3)
        BOQItemGroup.objects.all().delete()          # a database with no rules loaded
        with self.assertLogs('projects.models', 'WARNING'):
            unchecked = self.live_data(group)
        self.assertEqual(unchecked['content_gaps'], [])
        for key in ('sites', 'lines', 'accb_lines', 'unlinked', 'blanks', 'unit_warnings'):
            self.assertEqual(checked[key], unchecked[key], key)
        self.assertEqual(snapshot_fingerprint(checked), snapshot_fingerprint(unchecked))

    def test_snapshot_stores_the_keys_and_shows_the_same_words_frozen(self):
        group = self.group('G', [self.a1, self.a4])
        self.take(self.head, group)
        snap = DesignBOQGroupSnapshot.objects.get()
        self.assertEqual(snap.data['version'], 2)
        self.assertEqual(snap.data['content_gaps'], [{'site': 'B5A-A1', 'gaps': BOTH_KEYS},
                                                     {'site': 'B5A-A4', 'gaps': BOTH_KEYS}])
        expected = [f'B5A-A1: {BOTH_GAPS}', f'B5A-A4: {BOTH_GAPS}']
        self.assertEqual(warning_lines(hydrate(snap.data)['boq'])[-2:], expected)

        # A1 is fixed afterwards: live stops warning, the snapshot still says what it saw.
        self.add_line(self.a1, self.inv, '1')
        self.add_line(self.a1, self.dcdb, '1')
        html = self.snapshot_page(self.head, snap).content.decode()
        for line in expected:
            self.assertIn(line, html)
        _response, workbook = self.snapshot_xlsx(self.head, snap)
        self.assertEqual(self.gap_lines(self.cells(workbook)), expected)
        live = self.detail(self.head, group).content.decode()
        self.assertNotIn('B5A-A1: no', live)
        self.assertIn(f'B5A-A4: {BOTH_GAPS}', live)

    def test_a_version_1_snapshot_opens_with_no_content_gap_line(self):
        group = self.group('G', [self.a1, self.a4])
        data = self.live_data(group)
        self.assertTrue(data['content_gaps'])
        old = {k: v for k, v in data.items() if k != 'content_gaps'}
        old['version'] = 1                                           # B-5b's shape exactly
        self.assertEqual(set(old), STORED_KEYS['top'] - {'content_gaps'})
        counted = sum(1 for s in old['sites'] if s['counted'])
        snap = DesignBOQGroupSnapshot.objects.create(
            group=group, taken_by=self.head, taken_by_name='Head', data=old,
            fingerprint=snapshot_fingerprint(old), site_ids=[s['id'] for s in old['sites']],
            sites_counted=counted, sites_not_counted=len(old['sites']) - counted)
        self.assertEqual(hydrate(old)['boq']['content_gaps'], [])

        page = self.snapshot_page(self.head, snap)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('with a quantity', page.content.decode())
        _response, workbook = self.snapshot_xlsx(self.head, snap)
        self.assertEqual(self.gap_lines(self.cells(workbook)), [])

        # The fingerprint ignores the version and the warnings, so the group screen, whose
        # newest snapshot is this one, opens with no "differs" banner and the live warnings.
        self.assertEqual(snapshot_fingerprint(old), snapshot_fingerprint(data))
        screen = self.detail(self.head, group)
        self.assertEqual(screen.status_code, 200)
        html = screen.content.decode()
        self.assertNotIn('id="boqGroupDiffers"', html)
        self.assertIn(f'B5A-A1: {BOTH_GAPS}', html)

    def test_query_counts_are_the_pinned_ones_with_failing_sites(self):
        """The groups are loaded and every site fails: still one query for the rule."""
        client = _client(self.des_a)
        for n in (5, 20):
            sites = [self.site(f'W{n}-{i}', self.t1, self.des_a, 'released',
                               [(self.m_module, '2')]) for i in range(n)]
            group = self.group(f'Gaps {n}', sites, by=self.des_a)
            self.assertEqual(len(self.boq_of(group)['content_gaps']), n)
            for name, pin in (('design_boq_group_detail', DETAIL_NO_SNAPSHOTS),
                              ('design_boq_group_excel', EXCEL_QUERIES)):
                url = reverse(name, args=[group.pk])
                client.get(reverse('design_boq_group_list'))         # warm the session
                with CaptureQueriesContext(connection) as ctx:
                    self.assertEqual(client.get(url).status_code, 200)
                with self.subTest(view=name, sites=n):
                    self.assertEqual(len(ctx.captured_queries), pin)
