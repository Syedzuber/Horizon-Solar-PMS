"""
B-5a — design BOQ groups (rulings 33-35; Zuber's decisions Q1-Q8 and Q-A..Q-F, 23 Nov 2026).

Design users put sites into named groups (a site in one group at most) and read one LIVE
consolidated BOQ per group, on screen and as a three-sheet Excel. Separate from SCM's
procurement groups in every respect: own tables, no lock taken or checked, and SCM never
reads these.

WHAT COUNTS (Q1). Only sites whose design status is in DESIGN_BOQ_COUNTED_STATUSES —
handed in, in either review, with the PM, or released — contribute to the totals. Every
other member shows as "Not counted yet". The one predicate is design_boq_counts().

NO READ IS WIDENED (Q-B). The picker lists only sites the user can already open (the BOQ
read gate, batched as permissions.boq_readable_projects_q); the add POST refuses any other
site with the same words as a site that does not exist; and a group — its screen, its
Excel, and every write on it — is open only to a user who can open EVERY live site in it.
So every per-site figure on these pages is one the user could already read on that site's
own BOQ.

THE TOTALS ARE SCM'S FUNCTION, UNCHANGED. design_views.aggregate_group_boq() takes project
pks and filters on quantity alone (no BOQ status, lock or SCM state), so it is called here
with the counted sites and is not modified. What it leaves out on purpose — blank
quantities — is fetched by one more query and reported, never summed silently (Q5).

NO PRICES anywhere: nothing on these pages, in their context or in the Excel reads a rate,
amount or value.

B-5b — SNAPSHOTS (Zuber Q3/Q4/Q7, S1-S8 and the pre-flight answers A1-A9, 24 Nov 2026).
A snapshot is the group's consolidated BOQ frozen as plain data (group_boq_data()), never
edited or deleted. ONE SHAPE FOR EVERYTHING: the live screen and the live Excel also render
from hydrate(group_boq_data(...)), so the only difference between live and a snapshot is
where the plain data came from — computed now, or read from the snapshot row. consolidate()
itself is unchanged. A snapshot is opened only by someone who can open every site it STORES
(S4), and a group with snapshots can't be deleted (S7).
"""
import hashlib
import json
import re
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Count, F, IntegerField, OuterRef, ProtectedError, Q, Subquery
from django.db.models.functions import Coalesce, Lower
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font

from .accb import ACCB_CONFIGURED_CODE
from .design_views import aggregate_group_boq
from .models import (
    BOQItem, DesignAssignment, DesignBOQGroup, DesignBOQGroupMembership,
    DesignBOQGroupSnapshot, Project, DESIGN_BOQ_COUNTED_STATUSES, log_activity,
)
from .permissions import (
    boq_readable_project_ids, boq_readable_projects_q, user_can_use_design_boq_groups,
)

#: Refusal for anyone who is not a design user (permissions.user_can_use_design_boq_groups).
NOT_A_DESIGN_USER = 'BOQ groups are for design users.'

#: Zuber Q-B: a group holding even one site the user cannot open is refused whole.
GROUP_NOT_OPEN = "This group includes sites you can't open."

#: Zuber Q-B: a site the user can't open gets the SAME words as one that doesn't exist, so
#: the add POST cannot be used to find out which site codes exist.
SITE_NOT_FOUND = "A selected site doesn't exist."

#: Ballast Type Structure and Tin Shade are counted in whole numbers. A fraction on either
#: is almost always a kWp or area typed into a count (Zuber, 23 Nov), so it is warned about.
WHOLE_NUMBER_CODES = ('OPX-037', 'OPX-038')

#: Activity entries name at most this many sites, then "and N more": ActivityLog.action is
#: 255 characters and log_activity() cuts anything longer mid-word (Zuber Q-E).
LOG_SITES_SHOWN = 5

GROUP_NAME_MAX = DesignBOQGroup._meta.get_field('name').max_length

#: B-5b S6: a snapshot of a group with no counted site would be an empty record.
NOTHING_TO_SNAPSHOT = ('Nothing to snapshot: no site in this group has a submitted or '
                       'released BOQ.')

#: B-5b S4: a snapshot is refused whole when it stores any site the user can't open.
SNAPSHOT_NOT_OPEN = "This snapshot includes sites you can't open."

#: B-5b S7: a group with snapshots is kept, because its snapshots point at it.
GROUP_HAS_SNAPSHOTS = "This group has snapshots, so it can't be deleted."

#: B-5b S7 / Zuber A6: the typed name must match exactly (case too) after trimming.
DELETE_NAME_MISMATCH = "Type the group's name exactly as shown to delete it."

NOTE_MAX = DesignBOQGroupSnapshot._meta.get_field('note').max_length

#: The group screen's tabs; ?tab= picks one (the snapshot POST returns to its own tab).
TABS = ('sites', 'boq', 'snapshots')

XLSX_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


# ---------------------------------------------------------------------------
# Predicates and small helpers
# ---------------------------------------------------------------------------

def design_boq_counts(assignment):
    """THE ONE PLACE "does this site's BOQ count in a design group's totals" is answered.

    Zuber Q1: by the design status, never BOQ.status (which is 'Draft' on every OPEX BOQ).
    A site with no design assignment never counts.
    """
    return assignment is not None and assignment.status in DESIGN_BOQ_COUNTED_STATUSES


def _assignment(project):
    """The site's design assignment, or None. getattr because the reverse one-to-one
    raises (an AttributeError subclass) when the row is missing."""
    return getattr(project, 'design_assignment', None)


def _person(profile):
    """A UserProfile's display name, or '' for none."""
    if profile is None:
        return ''
    return profile.user.get_full_name() or profile.user.username


def _short_list(codes):
    """'A, B, C' — or the first LOG_SITES_SHOWN codes and 'and N more' (Zuber Q-E)."""
    if len(codes) <= LOG_SITES_SHOWN:
        return ', '.join(codes)
    return f"{', '.join(codes[:LOG_SITES_SHOWN])} and {len(codes) - LOG_SITES_SHOWN} more"


def _clean_group_name(raw, exclude_pk=None):
    """(name, '') or (None, the refusal). Unique ignoring case, as the constraint says.

    The pre-check is for a readable message; uniq_design_boq_group_name_ci is what makes it
    true under two concurrent saves, and both callers catch its IntegrityError.
    """
    name = (raw or '').strip()
    if not name:
        return None, 'Give the group a name.'
    if len(name) > GROUP_NAME_MAX:
        return None, f'Keep the group name to {GROUP_NAME_MAX} characters.'
    # Any group whose name equals this one ignoring case, other than the one being renamed.
    clash = (DesignBOQGroup.objects.filter(name__iexact=name)
             .exclude(pk=exclude_pk).values_list('name', flat=True).first())
    if clash is not None:
        return None, f'A group named "{clash}" already exists. Choose another name.'
    return name, ''


def _live_members(group):
    """The group's memberships whose site is not soft-deleted, with everything the screen,
    the Excel and the access check read — one query.

    A soft-deleted site keeps its row (so it cannot join another group) but is hidden from
    the member list, the counts and the totals (pre-flight P5).
    """
    return list(group.memberships
                .filter(project__is_deleted=False)
                .select_related('project__program',
                                'project__design_assignment__assigned_to__user',
                                'added_by__user')
                .order_by('project__project_id'))


def _readable_q(request):
    """boq_readable_projects_q() for this request's user, built ONCE per request.

    The group screen asks it twice (the group gate and the picker) and the add POST twice
    (the gate and the posted sites). Building it can cost a query (the deputy lookup), so
    it is kept on the request rather than rebuilt; the answer cannot change mid-request.
    """
    if not hasattr(request, '_design_boq_readable_q'):
        request._design_boq_readable_q = boq_readable_projects_q(request.user)
    return request._design_boq_readable_q


def _readable_ids(request, projects):
    """The pks among `projects` whose BOQ this request's user may read. One query."""
    return boq_readable_project_ids(request.user, projects, readable_q=_readable_q(request))


def _group_open_to(request, members):
    """True if the user can open every live site in the group (Zuber Q-B).

    An empty group is open to every design user: there is nothing in it to read.
    One query when the group has members.
    """
    ids = {m.project_id for m in members}
    if not ids:
        return True
    return ids <= _readable_ids(request, Project.objects.filter(pk__in=ids))


def _open_group_or_refuse(request, pk):
    """(group, members, None) for a design user who can open the group, else
    (None, None, a 403 response). Every per-group route starts here, so the screen, the
    Excel and every write on a group answer the same question the same way."""
    # Design users only (ruling 33, Zuber Q-A): SCM has its own groups, nobody else groups.
    if not user_can_use_design_boq_groups(request.user):
        return None, None, HttpResponseForbidden(NOT_A_DESIGN_USER)
    # The creator rides with the group: the screen's header names them.
    group = get_object_or_404(DesignBOQGroup.objects.select_related('created_by__user'), pk=pk)
    members = _live_members(group)
    # The whole group, or nothing (Zuber Q-B): every per-site figure on the group's pages
    # must be one the user could already read on that site's own BOQ.
    if not _group_open_to(request, members):
        return None, None, HttpResponseForbidden(GROUP_NOT_OPEN)
    return group, members, None


# ---------------------------------------------------------------------------
# The consolidated BOQ
# ---------------------------------------------------------------------------

def _member_rows(members):
    """One dict per live member: what the Sites tab, the warnings and the Sites sheet show."""
    rows = []
    for membership in members:
        project = membership.project
        assignment = _assignment(project)
        rows.append({
            'membership': membership,
            'project':    project,
            'code':       project.project_id,
            'tender':     project.program.name if project.program else '',
            'designer':   _person(assignment.assigned_to) if assignment else '',
            'status':     (assignment.get_status_display() if assignment
                           else 'No design assignment'),
            'counted':    design_boq_counts(assignment),
        })
    return rows


def consolidate(member_rows):
    """The live consolidated BOQ of the counted members. 4 queries, whatever the size.

    aggregate_group_boq() supplies the catalogue lines, the ACCB configurations and the
    lines with no catalogue item, exactly as SCM's screen gets them (3 queries). One more
    query fetches every counted line any warning or marker needs:

      * a blank quantity — aggregate_group_boq() drops it (quantity > 0 only), so it is
        listed here as counted as 0 (Q5); a 0 is a statement and is not listed (Q-F);
      * a retired catalogue item — still summed (it is a real BOQ line), marked "Retired";
      * OPX-037 / OPX-038 with a fraction, or a line whose stored unit differs from the
        catalogue unit — listed; the line still counts in the total.
    """
    counted_ids = [r['project'].pk for r in member_rows if r['counted']]
    agg = aggregate_group_boq(counted_ids)

    # Every counted line that is blank, on a retired item, on a whole-number item, or whose
    # unit could differ from its catalogue unit. The unit comparison is done in Python
    # below: a database "<>" against a NULL catalogue unit would make every unlinked line
    # match. The filter only keeps the fetch small.
    flagged = list(
        BOQItem.objects
        .filter(boq__project_id__in=counted_ids)
        .filter(Q(boq_quantity__isnull=True)
                | Q(item_master__is_active=False)
                | Q(item_master__code__in=WHOLE_NUMBER_CODES)
                | ~Q(uom=F('item_master__unit')))
        .values('boq__project__project_id', 'item_master_id', 'item_master__code',
                'item_master__unit', 'item_master__is_active', 'accb_config__config_key',
                'description', 'uom', 'boq_quantity', 'serial_no')
        .order_by('boq__project__project_id', 'serial_no'))

    retired_items = {row['item_master_id'] for row in flagged
                     if row['item_master_id'] is not None
                     and row['item_master__is_active'] is False}
    retired_accb_keys = {row['accb_config__config_key'] for row in flagged
                         if row['accb_config__config_key'] is not None
                         and row['item_master__is_active'] is False}

    blanks, unit_warnings = [], []
    for row in flagged:
        site, code, qty = (row['boq__project__project_id'], row['item_master__code'],
                           row['boq_quantity'])
        if qty is None:
            blanks.append({'site': site, 'code': code or '',
                           'description': row['description']})
        if row['item_master_id'] is None:
            continue
        reasons = []
        if code in WHOLE_NUMBER_CODES and qty is not None and qty != qty.to_integral_value():
            reasons.append('not a whole number')
        if row['uom'] != row['item_master__unit']:
            reasons.append('unit differs from the catalogue')
        if reasons:
            unit_warnings.append({'site': site, 'code': code, 'quantity': qty,
                                  'line_unit': row['uom'],
                                  'catalogue_unit': row['item_master__unit'],
                                  'reasons': reasons})

    # The per-site split, summed per SITE: aggregate_group_boq()'s catalogue contributions
    # are one entry per LINE, so two lines on one item at one site would show twice.
    for line in agg['lines']:
        per_site = {}
        for site, qty in line['contributions']:
            per_site[site] = per_site.get(site, Decimal('0')) + qty
        line['per_site'] = per_site
        line['per_site_list'] = sorted(per_site.items())
        line['retired'] = line['item_master'] in retired_items
    for accb in agg['accb_lines']:
        accb['per_site'] = dict(accb['contributions'])
        accb['per_site_list'] = accb['contributions']
        accb['retired'] = accb['config_key'] in retired_accb_keys
        accb['code'] = ACCB_CONFIGURED_CODE

    return {
        'lines':          agg['lines'],
        'accb_lines':     agg['accb_lines'],
        'unlinked':       agg['unlinked'],
        'counted_codes':  [r['code'] for r in member_rows if r['counted']],
        'not_counted':    [r for r in member_rows if not r['counted']],
        'blanks':         blanks,
        'unit_warnings':  unit_warnings,
    }


# ---------------------------------------------------------------------------
# B-5b — plain data: the one shape the live view, the live Excel and every snapshot use
# ---------------------------------------------------------------------------

#: Bump when group_boq_data()'s shape changes; hydrate() refuses any other (Zuber A1), so an
#: old snapshot can never be silently misread by newer code.
GROUP_BOQ_DATA_VERSION = 1

#: Every key group_boq_data() writes, per level. NO PRICES (S1): a snapshot stores these and
#: nothing else. tests_design_boq_group_snapshots pins this list by hand.
GROUP_BOQ_DATA_KEYS = {
    'top':          ('version', 'group_name', 'sites', 'lines', 'accb_lines', 'unlinked',
                     'blanks', 'unit_warnings'),
    'site':         ('id', 'code', 'name', 'tender', 'designer', 'status', 'counted'),
    'line':         ('code', 'description', 'unit', 'site_count', 'total', 'retired', 'per_site'),
    'accb':         ('config_key', 'description', 'unit', 'site_count', 'total', 'retired',
                     'per_site'),
    'unlinked':     ('site', 'description', 'unit', 'quantity'),
    'blank':        ('site', 'code', 'description'),
    'unit_warning': ('site', 'code', 'quantity', 'line_unit', 'catalogue_unit', 'reasons'),
}

#: What group_boq_data() reads from consolidate() and _member_rows(), per level. With
#: GROUP_BOQ_KEYS_IGNORED it must cover every key they return (the key-coverage test, Zuber
#: A1): a key added to consolidate() later fails that test until someone decides on it.
#: consolidate()'s `unlinked` holds BOQItem objects, not dicts; their four fields are read
#: in group_boq_data() and pinned by the round-trip test instead.
GROUP_BOQ_KEYS_READ = {
    'boq':          {'lines', 'accb_lines', 'unlinked', 'blanks', 'unit_warnings'},
    'line':         {'item_master__code', 'item_master__description', 'item_master__unit',
                     'site_count', 'total_quantity', 'retired', 'per_site_list'},
    'accb':         {'config_key', 'description', 'unit', 'site_count', 'total_quantity',
                     'retired', 'per_site_list'},
    'blank':        {'site', 'code', 'description'},
    'unit_warning': {'site', 'code', 'quantity', 'line_unit', 'catalogue_unit', 'reasons'},
    'member_row':   {'project', 'code', 'tender', 'designer', 'status', 'counted'},
}

#: What consolidate() and _member_rows() return that is deliberately NOT stored, and why.
GROUP_BOQ_KEYS_IGNORED = {
    'boq':        {'counted_codes': 'rebuilt from sites[].counted by hydrate()',
                   'not_counted':   'rebuilt from sites[].counted by hydrate()'},
    'line':       {'item_master':             'a database id; the code names the item',
                   'item_master__sort_order': 'the lines are stored already in this order',
                   'contributions':           'per BOQ line; per_site_list is the per-site sum',
                   'per_site':                'the dict form of per_site_list, rebuilt'},
    'accb':       {'contributions': 'the same pairs as per_site_list',
                   'per_site':      'the dict form of per_site_list, rebuilt',
                   'code':          'always ACCB_CONFIGURED_CODE, put back by hydrate()'},
    'blank':        {},
    'unit_warning': {},
    'member_row': {'membership': 'live only: the Sites tab Remove button'},
}


def _canon(value):
    """A quantity as a canonical string, or None for a blank.

    2340, 2340.00 and 2.34E+3 all give '2340'; 38.40 gives '38.4'; 0 and -0 give '0'.
    Equal quantities therefore always give equal strings (Postgres sums numeric(10,2) to
    2340.00 where SQLite may give 2340), so the fingerprint can't see a difference that is
    only formatting. Strings, not JSON numbers: a JSON number comes back as a float.
    """
    if value is None:
        return None
    value = value if isinstance(value, Decimal) else Decimal(str(value))
    if value == 0:
        return '0'
    return format(value.normalize(), 'f')


def _dec(text):
    """The Decimal of a _canon() string, or None for a blank."""
    return None if text is None else Decimal(text)


def _per_site_data(pairs):
    return [[site, _canon(qty)] for site, qty in pairs]


def group_boq_data(group_name, member_rows, boq):
    """The consolidated BOQ as plain, JSON-ready data: exactly what a snapshot stores (S1),
    and what the live screen and live Excel render from (through hydrate()).

    Reads only `consolidate()`'s output and the member rows; no query. The keys written are
    GROUP_BOQ_DATA_KEYS and nothing else, so no price can ride along.
    """
    return {
        'version':    GROUP_BOQ_DATA_VERSION,
        'group_name': group_name,
        'sites': [{
            'id':       r['project'].pk,
            'code':     r['code'],
            'name':     r['project'].site_name or '',
            'tender':   r['tender'],
            'designer': r['designer'],
            'status':   r['status'],          # the status as shown, at this moment (S5)
            'counted':  r['counted'],
        } for r in member_rows],
        'lines': [{
            'code':        line['item_master__code'],
            'description': line['item_master__description'],
            'unit':        line['item_master__unit'],
            'site_count':  line['site_count'],
            'total':       _canon(line['total_quantity']),
            'retired':     line['retired'],
            'per_site':    _per_site_data(line['per_site_list']),
        } for line in boq['lines']],
        'accb_lines': [{
            'config_key':  accb['config_key'],
            'description': accb['description'],
            'unit':        accb['unit'],
            'site_count':  accb['site_count'],
            'total':       _canon(accb['total_quantity']),
            'retired':     accb['retired'],
            'per_site':    _per_site_data(accb['per_site_list']),
        } for accb in boq['accb_lines']],
        'unlinked': [{
            'site':        item.boq.project.project_id,
            'description': item.description,
            'unit':        item.uom,
            'quantity':    _canon(item.boq_quantity),
        } for item in boq['unlinked']],
        'blanks': [{
            'site': b['site'], 'code': b['code'], 'description': b['description'],
        } for b in boq['blanks']],
        'unit_warnings': [{
            'site':           w['site'],
            'code':           w['code'],
            'quantity':       _canon(w['quantity']),
            'line_unit':      w['line_unit'],
            'catalogue_unit': w['catalogue_unit'],
            'reasons':        list(w['reasons']),
        } for w in boq['unit_warnings']],
    }


def _require_version(data):
    """Refuse data in a shape this code doesn't know (Zuber A1). Never guess at old data."""
    version = data.get('version') if isinstance(data, dict) else None
    if version != GROUP_BOQ_DATA_VERSION:
        raise ValueError(f'Design BOQ group data version {version!r} is not one this code '
                         f'can read (it reads version {GROUP_BOQ_DATA_VERSION}).')


def _per_site_view(pairs):
    pairs = [(site, Decimal(qty)) for site, qty in pairs]
    return pairs, dict(pairs)


def hydrate(data):
    """Plain data (live, or a snapshot's stored copy) back into the shape the template and
    build_group_workbook() read: {'group_name', 'sites', 'boq'}.

    `boq` carries consolidate()'s key names for every line and ACCB row, with Decimals and
    (site, qty) tuples, so a page or sheet reads the same either way. The one change of
    shape: `unlinked` rows are plain dicts (site, description, unit, quantity) instead of
    BOQItem objects. No query.
    """
    _require_version(data)
    sites = [dict(s) for s in data['sites']]
    lines = []
    for line in data['lines']:
        pairs, per_site = _per_site_view(line['per_site'])
        lines.append({
            'item_master__code':        line['code'],
            'item_master__description': line['description'],
            'item_master__unit':        line['unit'],
            'site_count':               line['site_count'],
            'total_quantity':           Decimal(line['total']),
            'retired':                  line['retired'],
            'per_site_list':            pairs,
            'per_site':                 per_site,
        })
    accb_lines = []
    for accb in data['accb_lines']:
        pairs, per_site = _per_site_view(accb['per_site'])
        accb_lines.append({
            'config_key':     accb['config_key'],
            'description':    accb['description'],
            'unit':           accb['unit'],
            'site_count':     accb['site_count'],
            'total_quantity': Decimal(accb['total']),
            'retired':        accb['retired'],
            'per_site_list':  pairs,
            'per_site':       per_site,
            'code':           ACCB_CONFIGURED_CODE,
        })
    return {
        'group_name': data['group_name'],
        'sites': sites,
        'boq': {
            'lines':         lines,
            'accb_lines':    accb_lines,
            'unlinked':      [dict(u, quantity=_dec(u['quantity'])) for u in data['unlinked']],
            'counted_codes': [s['code'] for s in sites if s['counted']],
            'not_counted':   [s for s in sites if not s['counted']],
            'blanks':        [dict(b) for b in data['blanks']],
            'unit_warnings': [dict(w, quantity=_dec(w['quantity']), reasons=list(w['reasons']))
                              for w in data['unit_warnings']],
        },
    }


def _comparable(data):
    """THE projection "does the live BOQ still match a snapshot" is judged on (Zuber A2).

    The counted site set (by pk), each catalogue line's [code, unit, total, per-site
    split], each ACCB row's [config key, total, per-site split], and the lines with no
    catalogue item summed per (site, description, unit). Description, the retired flag and
    every status stay OUT: rewording, retiring or a counted-to-counted status move is not a
    change to what was counted. Both snapshot_fingerprint() and snapshot_changes() read
    this one projection, so the change list is empty exactly when the fingerprints match.
    """
    _require_version(data)
    unlinked = {}
    for u in data['unlinked']:
        key = (u['site'], u['description'], u['unit'] or '')
        unlinked[key] = unlinked.get(key, Decimal('0')) + (_dec(u['quantity']) or Decimal('0'))
    return {
        'counted':  sorted(s['id'] for s in data['sites'] if s['counted']),
        # Each code and config key is one row, so sorting never compares past the first item.
        'lines':    sorted([l['code'], l['unit'] or '', l['total'], sorted(l['per_site'])]
                           for l in data['lines']),
        'accb':     sorted([a['config_key'], a['total'], sorted(a['per_site'])]
                           for a in data['accb_lines']),
        'unlinked': sorted([site, description, unit, _canon(qty)]
                           for (site, description, unit), qty in unlinked.items()),
    }


def snapshot_fingerprint(data):
    """sha256 of the canonical JSON of _comparable(data)."""
    canonical = json.dumps(_comparable(data), sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _fmt(text):
    """A _canon() quantity for the change list: '2,340', '38.4'; None stays None ('—')."""
    return None if text is None else format(Decimal(text), ',f')


def snapshot_changes(old, new):
    """The short list of what differs between a snapshot's stored data (`old`) and the live
    data (`new`), S3 + Zuber A2. Nothing more: no full diff.

      sites_added / sites_dropped — site codes that joined or left the totals;
      totals — {'label', 'old', 'new'} for a catalogue line, ACCB row (full text) or line
               with no catalogue item whose total changed, appeared (old None) or went
               (new None);
      units  — {'label', 'old', 'new'} for a catalogue line whose unit changed, each side
               '<total> <unit>' ("OPX-038: 40 KWp → 40 Nos");
      split  — labels whose per-site split changed while the total and unit did not.

    Every difference in _comparable() lands in exactly one of these, so `any` is False
    exactly when snapshot_fingerprint(old) == snapshot_fingerprint(new).
    """
    a, b = _comparable(old), _comparable(new)
    old_codes = {s['id']: s['code'] for s in old['sites']}
    new_codes = {s['id']: s['code'] for s in new['sites']}
    changes = {
        'sites_added':   sorted(new_codes[i] for i in set(b['counted']) - set(a['counted'])),
        'sites_dropped': sorted(old_codes[i] for i in set(a['counted']) - set(b['counted'])),
        'totals': [], 'units': [], 'split': [],
    }

    # Catalogue lines by code: [unit, total, split].
    before_lines = {row[0]: row[1:] for row in a['lines']}
    after_lines = {row[0]: row[1:] for row in b['lines']}
    for code in sorted(set(before_lines) | set(after_lines)):
        before, after = before_lines.get(code), after_lines.get(code)
        if before == after:
            continue
        if before is None or after is None:
            changes['totals'].append({'label': code, 'old': before and _fmt(before[1]),
                                      'new': after and _fmt(after[1])})
        elif before[0] != after[0]:
            changes['units'].append({'label': code,
                                     'old': f'{_fmt(before[1])} {before[0]}'.strip(),
                                     'new': f'{_fmt(after[1])} {after[0]}'.strip()})
        elif before[1] != after[1]:
            changes['totals'].append({'label': code, 'old': _fmt(before[1]),
                                      'new': _fmt(after[1])})
        else:
            changes['split'].append(code)

    # ACCB rows by configuration: [total, split], labelled by their full text (Zuber A2).
    text = {x['config_key']: x['description'] for x in old['accb_lines']}
    text.update({x['config_key']: x['description'] for x in new['accb_lines']})
    before_accb = {row[0]: row[1:] for row in a['accb']}
    after_accb = {row[0]: row[1:] for row in b['accb']}
    for key in sorted(set(before_accb) | set(after_accb)):
        before, after = before_accb.get(key), after_accb.get(key)
        if before == after:
            continue
        if before is None or after is None or before[0] != after[0]:
            changes['totals'].append({'label': text[key], 'old': before and _fmt(before[0]),
                                      'new': after and _fmt(after[0])})
        else:
            changes['split'].append(text[key])

    # Lines with no catalogue item, by (site, description, unit).
    before_unl = {tuple(row[:3]): row[3] for row in a['unlinked']}
    after_unl = {tuple(row[:3]): row[3] for row in b['unlinked']}
    for key in sorted(set(before_unl) | set(after_unl)):
        if before_unl.get(key) != after_unl.get(key):
            site, description, _unit = key
            changes['totals'].append({'label': f'{site} {description} (no catalogue item)',
                                      'old': _fmt(before_unl.get(key)),
                                      'new': _fmt(after_unl.get(key))})

    changes['any'] = any(changes[k] for k in
                         ('sites_added', 'sites_dropped', 'totals', 'units', 'split'))
    return changes


def _reads_every_site(request):
    """True if this user's BOQ read gate is every project — the Design Head, his deputy and
    Design QC among design users. boq_readable_projects_q() returns the empty Q() for
    exactly those portfolio readers, and an empty Q is falsy."""
    return not _readable_q(request)


def _snapshot_sites_open(request, site_ids, readable):
    """S4 + Zuber A7: may this user open a snapshot storing `site_ids`?

    Every stored site must be readable now — its OWN stored sites, not the group's current
    members, so a site that has since left the group still guards it, and a soft-deleted
    one too (Project has no soft-delete manager and the read gate ignores is_deleted).
    A stored site that is not in `readable` is either one the user may not open, or one
    whose row no longer exists. Only a user who reads every site passes the second (they
    could open it if it existed); everyone else is refused. Nothing widens.
    """
    if set(site_ids) <= readable:
        return True
    return _reads_every_site(request)


def _snapshot_or_refuse(request, pk, snapshot_pk):
    """(snapshot, None) for a design user who can open every site the snapshot stores,
    else (None, a 403). Scoped to the group in the URL: another group's snapshot is a 404.
    2 queries (3 for a plain designer: the deputy lookup)."""
    # Design users only (ruling 33, Zuber Q-A), as for every design BOQ group page.
    if not user_can_use_design_boq_groups(request.user):
        return None, HttpResponseForbidden(NOT_A_DESIGN_USER)
    snapshot = get_object_or_404(DesignBOQGroupSnapshot, pk=snapshot_pk, group_id=pk)
    readable = _readable_ids(request, Project.objects.filter(pk__in=snapshot.site_ids))
    # S4: the snapshot's own stored sites decide, whatever the group holds today.
    if not _snapshot_sites_open(request, snapshot.site_ids, readable):
        return None, HttpResponseForbidden(SNAPSHOT_NOT_OPEN)
    return snapshot, None


def _snapshot_panel(request, group, live_data):
    """The Snapshots tab and the "differs" banner: (snapshots, newest, changes).

    Fixed cost whatever the number of snapshots: the list without `data` (1 query); when
    there is any, the readable subset of every site they store (1); and the newest one's
    `data` (1) when this user may open it. `changes` is None when there is no snapshot,
    the newest matches the live data, or the user can't open the newest — comparing it
    would show figures of a site they can't read (Zuber Q-B: nothing widens).
    """
    # Newest first (Meta.ordering). `data` is left behind: up to ~60 KB a row, and only the
    # newest one's is ever needed, fetched below.
    snapshots = list(DesignBOQGroupSnapshot.objects.filter(group=group).defer('data'))
    if not snapshots:
        return [], None, None
    every_site = set().union(*(s.site_ids for s in snapshots))
    readable = _readable_ids(request, Project.objects.filter(pk__in=every_site))
    for snapshot in snapshots:
        snapshot.can_open = _snapshot_sites_open(request, snapshot.site_ids, readable)
    newest = snapshots[0]
    changes = None
    if newest.can_open:
        stored = (DesignBOQGroupSnapshot.objects.filter(pk=newest.pk)
                  .values_list('data', flat=True).get())
        if snapshot_fingerprint(stored) != snapshot_fingerprint(live_data):
            changes = snapshot_changes(stored, live_data)
    return snapshots, newest, changes


def _live_view(group, members):
    """(member_rows, live_data, view) for a group's live consolidated BOQ — the screen and the
    live Excel both start here, so both render from the plain data a snapshot would store."""
    member_rows = _member_rows(members)
    live_data = group_boq_data(group.name, member_rows, consolidate(member_rows))
    return member_rows, live_data, hydrate(live_data)


def _picker_rows(request, group):
    """Every OPEX site with a design assignment that the user can open, as JSON-ready dicts
    for the add-sites picker (Zuber Q-B: the picker lists only sites the user can open).

    3 queries whatever the number of sites: the readable ids, their assignments with
    site, tender and designer, and every design-group membership among them (to grey out
    sites in another group and name it).
    """
    candidates = Project.objects.filter(project_type='OPEX', is_deleted=False,
                                        design_assignment__isnull=False)
    readable = _readable_ids(request, candidates)
    in_groups = {pid: (gid, name) for pid, gid, name in
                 DesignBOQGroupMembership.objects
                 .filter(project_id__in=readable)
                 .values_list('project_id', 'group_id', 'group__name')}
    rows = []
    for assignment in (DesignAssignment.objects
                       .filter(project_id__in=readable)
                       .select_related('project__program', 'assigned_to__user')
                       .order_by('project__project_id')):
        project = assignment.project
        group_id, group_name = in_groups.get(project.pk, (None, ''))
        rows.append({
            'id':       project.pk,
            'code':     project.project_id,
            'name':     project.site_name or '',
            'tender':   project.program.name if project.program else '',
            'designer': _person(assignment.assigned_to),
            'status':   assignment.get_status_display(),
            'counted':  design_boq_counts(assignment),
            'here':     group_id == group.pk,
            'group':    '' if group_id == group.pk else group_name,
        })
    return rows


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

@login_required
def design_boq_group_list(request):
    """Every design BOQ group: name, sites, counted sites, created by and when, and the
    create form. Access: design users (role Design or Design Head authority)."""
    # Design users only (ruling 33, Zuber Q-A).
    if not user_can_use_design_boq_groups(request.user):
        return HttpResponseForbidden(NOT_A_DESIGN_USER)

    # Each group with its live-site count and its counted-site count in one query. Both
    # Counts run over the same membership join; the joins to the site and its (one-to-one)
    # design assignment cannot multiply rows.
    live = Q(memberships__project__is_deleted=False)
    # B-5b: each group's snapshot count as a correlated subquery, NOT a third Count — a join
    # to snapshots would multiply the membership rows the two Counts above run over.
    snapshot_count = (DesignBOQGroupSnapshot.objects.filter(group=OuterRef('pk'))
                      .order_by().values('group').annotate(n=Count('pk')).values('n'))
    groups = list(
        DesignBOQGroup.objects
        .select_related('created_by__user')
        .annotate(site_count=Count('memberships', filter=live),
                  counted_count=Count('memberships', filter=live & Q(
                      memberships__project__design_assignment__status__in=(
                          DESIGN_BOQ_COUNTED_STATUSES))),
                  snapshot_count=Coalesce(Subquery(snapshot_count, output_field=IntegerField()), 0))
        .order_by(Lower('name')))

    # Which groups this user may open: every live member must be readable (Zuber Q-B).
    # One query for all memberships and one for the readable subset, whatever the number
    # of groups — never a check per group.
    members_by_group = {}
    for group_id, project_id in (DesignBOQGroupMembership.objects
                                 .filter(project__is_deleted=False)
                                 .values_list('group_id', 'project_id')):
        members_by_group.setdefault(group_id, set()).add(project_id)
    every_member = set().union(*members_by_group.values()) if members_by_group else set()
    readable = (_readable_ids(request, Project.objects.filter(pk__in=every_member))
                if every_member else set())
    for group in groups:
        group.can_open = members_by_group.get(group.pk, set()) <= readable
        group.created_by_name = _person(group.created_by)

    return render(request, 'projects/design/boq_groups.html', {
        'groups':          groups,
        'name_max':        GROUP_NAME_MAX,
        'group_not_open':  GROUP_NOT_OPEN,
    })


@login_required
def design_boq_group_create(request):
    """Create a design BOQ group and open it (straight into the picker, since it is
    empty). Access: design users. POST only; a GET goes back to the list."""
    # Design users only (ruling 33: any design user may create a group).
    if not user_can_use_design_boq_groups(request.user):
        return HttpResponseForbidden(NOT_A_DESIGN_USER)
    if request.method != 'POST':
        return redirect('design_boq_group_list')

    name, error = _clean_group_name(request.POST.get('name'))
    if error:
        messages.error(request, error)
        return redirect('design_boq_group_list')
    profile = request.user.profile
    try:
        with transaction.atomic():
            group = DesignBOQGroup.objects.create(name=name, created_by=profile)
            log_activity(None, profile,
                         f'{_person(profile)} created design BOQ group "{group.name}"',
                         entity_type='DesignBOQGroup', entity_id=group.pk,
                         action_code='design_boq_group_created')
    except IntegrityError:
        # Another group took the name between the pre-check and the insert.
        messages.error(request, f'A group named "{name}" already exists. Choose another name.')
        return redirect('design_boq_group_list')
    return redirect('design_boq_group_detail', pk=group.pk)


@login_required
def design_boq_group_rename(request, pk):
    """Rename a design BOQ group. Access: design users who can open every site in it.
    POST only; a GET goes back to the group."""
    group, _members, refusal = _open_group_or_refuse(request, pk)
    if refusal:
        return refusal
    if request.method != 'POST':
        return redirect('design_boq_group_detail', pk=group.pk)

    name, error = _clean_group_name(request.POST.get('name'), exclude_pk=group.pk)
    if error:
        messages.error(request, error)
        return redirect('design_boq_group_detail', pk=group.pk)
    if name == group.name:
        return redirect('design_boq_group_detail', pk=group.pk)
    old = group.name
    profile = request.user.profile
    try:
        with transaction.atomic():
            group.name = name
            group.save(update_fields=['name'])
            log_activity(None, profile,
                         f'{_person(profile)} renamed design BOQ group "{old}" to "{name}"',
                         entity_type='DesignBOQGroup', entity_id=group.pk,
                         action_code='design_boq_group_renamed')
    except IntegrityError:
        messages.error(request, f'A group named "{name}" already exists. Choose another name.')
        return redirect('design_boq_group_detail', pk=group.pk)
    messages.success(request, f'Renamed to "{name}".')
    return redirect('design_boq_group_detail', pk=group.pk)


@login_required
def design_boq_group_detail(request, pk):
    """One design BOQ group: its sites (with the add-sites picker), its live consolidated
    BOQ (with the "differs from the last snapshot" banner, B-5b) and its snapshots.
    Access: design users who can open every site in the group; each snapshot's View and
    Excel also need every site that snapshot stores (S4)."""
    group, members, refusal = _open_group_or_refuse(request, pk)
    if refusal:
        return refusal

    # B-5b: the live BOQ is rendered from the same plain data a snapshot stores.
    member_rows, live_data, live = _live_view(group, members)
    snapshots, newest, changes = _snapshot_panel(request, group, live_data)
    picker = _picker_rows(request, group)
    tab = request.GET.get('tab')
    return render(request, 'projects/design/boq_group_detail.html', {
        'group':        group,
        'member_rows':  member_rows,
        'boq':          live['boq'],
        # The same sentences the Summary sheet carries (warning_lines is shared).
        'warnings':     warning_lines(live['boq']),
        'picker_rows':  picker,
        'tenders':      sorted({row['tender'] for row in picker if row['tender']}),
        # Q2: a new, empty group opens straight into the picker.
        'open_picker':  not member_rows,
        'name_max':     GROUP_NAME_MAX,
        'active_tab':   tab if tab in TABS else 'sites',
        'snapshots':    snapshots,
        'newest':       newest,
        'changes':      changes,
        'note_max':     NOTE_MAX,
        'snapshot_not_open': SNAPSHOT_NOT_OPEN,
    })


@login_required
def design_boq_group_add_sites(request, pk):
    """Add the picked sites to a design BOQ group — all of them, or none with a reason for
    each refused site. Access: design users who can open the group and every site added.
    POST only."""
    group, members, refusal = _open_group_or_refuse(request, pk)
    if refusal:
        return refusal
    if request.method != 'POST':
        return redirect('design_boq_group_detail', pk=group.pk)

    # Posted pks, de-duplicated in order. A non-number is a site that doesn't exist.
    raw = request.POST.getlist('project_ids')
    ids, refused = [], []
    for value in raw:
        value = str(value).strip()
        if not value.isdigit():
            refused.append(SITE_NOT_FOUND)
        elif int(value) not in ids:
            ids.append(int(value))
    if not ids and not refused:
        messages.error(request, 'No sites were selected.')
        return redirect('design_boq_group_detail', pk=group.pk)

    # Validation is batched — three queries whatever the number of sites: the sites with
    # their design assignments, the subset the user may open, and any design-group
    # membership they already hold.
    projects = {p.pk: p for p in (Project.objects.filter(pk__in=ids)
                                  .select_related('design_assignment'))}
    readable = _readable_ids(request, Project.objects.filter(pk__in=ids))
    existing = {m.project_id: m for m in (DesignBOQGroupMembership.objects
                                          .filter(project_id__in=ids)
                                          .select_related('group'))}
    to_add = []
    for pid in ids:
        project = projects.get(pid)
        # A site the user can't open is reported exactly like one that doesn't exist (Q-B).
        if project is None or project.is_deleted or pid not in readable:
            refused.append(SITE_NOT_FOUND)
            continue
        code = project.project_id
        if project.project_type != 'OPEX':
            refused.append(f'{code}: not a RESCO site.')
        elif _assignment(project) is None:
            refused.append(f'{code}: has no design assignment.')
        elif pid in existing and existing[pid].group_id == group.pk:
            refused.append(f'{code}: already in this group.')
        elif pid in existing:
            refused.append(f'{code}: already in "{existing[pid].group.name}". A site can be '
                           f'in one design group only.')
        else:
            to_add.append(project)

    # ALL OR NOTHING (pre-flight P9, Zuber): the picker only offers valid sites, so a
    # refusal here means the list changed under the user; adding part of it would leave
    # them guessing which part.
    if refused:
        messages.error(request, 'No sites were added. Fix these and try again:')
        for line in dict.fromkeys(refused):        # each distinct reason once, in order
            messages.error(request, line)
        return redirect('design_boq_group_detail', pk=group.pk)

    profile = request.user.profile
    codes = [p.project_id for p in to_add]
    try:
        with transaction.atomic():
            # bulk_create: one INSERT. The model has no save() logic to bypass.
            DesignBOQGroupMembership.objects.bulk_create([
                DesignBOQGroupMembership(group=group, project=p, added_by=profile)
                for p in to_add])
            log_activity(None, profile,
                         f'{_person(profile)} added {len(codes)} site'
                         f'{"" if len(codes) == 1 else "s"} to design BOQ group '
                         f'"{group.name}": {_short_list(codes)}',
                         entity_type='DesignBOQGroup', entity_id=group.pk,
                         action_code='design_boq_group_sites_added')
    except IntegrityError:
        # uniq_design_boq_group_site refused a site another user added to a group between
        # the check above and this insert. The whole add rolls back.
        messages.error(request, 'No sites were added: one of them was put in a group by '
                                'someone else a moment ago. Open the picker again to see '
                                'where each site is.')
        return redirect('design_boq_group_detail', pk=group.pk)

    not_counted = sum(1 for p in to_add if not design_boq_counts(_assignment(p)))
    note = (f' {not_counted} show{"s" if not_counted == 1 else ""} as not counted yet until '
            f'the BOQ is submitted.' if not_counted else '')
    messages.success(request, f'{len(codes)} site{"" if len(codes) == 1 else "s"} added: '
                              f'{", ".join(codes)}.{note}')
    return redirect('design_boq_group_detail', pk=group.pk)


@login_required
def design_boq_group_remove_site(request, pk):
    """Remove one site from a design BOQ group (its BOQ is not changed). Access: design
    users who can open the group. POST only; the confirmation is on the page."""
    group, _members, refusal = _open_group_or_refuse(request, pk)
    if refusal:
        return refusal
    if request.method != 'POST':
        return redirect('design_boq_group_detail', pk=group.pk)

    raw = request.POST.get('membership_id', '')
    if not raw.isdigit():
        messages.error(request, 'No site was selected.')
        return redirect('design_boq_group_detail', pk=group.pk)
    # Scoped to THIS group: a membership id from another group is a 404, not a removal
    # from a group the user did not open.
    membership = get_object_or_404(DesignBOQGroupMembership.objects.select_related('project'),
                                   pk=int(raw), group=group)
    code = membership.project.project_id
    profile = request.user.profile
    with transaction.atomic():
        membership.delete()
        log_activity(None, profile,
                     f'{_person(profile)} removed {code} from design BOQ group "{group.name}"',
                     entity_type='DesignBOQGroup', entity_id=group.pk,
                     action_code='design_boq_group_site_removed')
    messages.success(request, f'{code} removed from "{group.name}". Its BOQ is unchanged.')
    return redirect('design_boq_group_detail', pk=group.pk)


@login_required
def design_boq_group_excel(request, pk):
    """Download the group's live consolidated BOQ as .xlsx — Summary, By site, Sites
    (Zuber Q6). Access: design users who can open every site in the group. No prices."""
    group, members, refusal = _open_group_or_refuse(request, pk)
    if refusal:
        return refusal

    # B-5b: from the same plain data a snapshot stores, through the same builder.
    _rows, _data, live = _live_view(group, members)
    now = timezone.localtime()
    workbook = build_group_workbook(
        f'{group.name} — live consolidated BOQ',
        [f'Generated {now:%d %b %Y %H:%M} IST from {_counted_phrase(live)}. '
         f'Quantities only, no prices.'],
        live['sites'], live['boq'])
    # The date is the IST date (TIME_ZONE is Asia/Kolkata).
    return _xlsx_response(workbook, f'{_safe_name(group.name)}_live_{timezone.localdate():%Y-%m-%d}.xlsx')


def _safe_name(name):
    """A user-typed group name made safe for a Content-Disposition header."""
    return re.sub(r'[^A-Za-z0-9._ -]', '_', name).strip() or 'group'


def _xlsx_response(workbook, filename):
    response = HttpResponse(content_type=XLSX_TYPE)
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    workbook.save(response)
    return response


def _counted_phrase(view):
    """'3 counted sites of 5' for a hydrate()d view — the Excel's intro line, both kinds."""
    counted = len(view['boq']['counted_codes'])
    return (f'{counted} counted site{"" if counted == 1 else "s"} of {len(view["sites"])}')


# ---------------------------------------------------------------------------
# B-5b — snapshots and group delete
# ---------------------------------------------------------------------------

@login_required
def design_boq_group_snapshot_take(request, pk):
    """Freeze the group's consolidated BOQ as a dated, immutable snapshot, with an optional
    note (S1, S6, S8). Access: design users who can open every site in the group — the
    group screen's rule. POST only; a GET goes back to the Snapshots tab."""
    group, members, refusal = _open_group_or_refuse(request, pk)
    if refusal:
        return refusal
    back = redirect(f"{_detail_url(group.pk)}?tab=snapshots")
    if request.method != 'POST':
        return back

    note = (request.POST.get('note') or '').strip()
    if len(note) > NOTE_MAX:
        messages.error(request, f'Keep the note to {NOTE_MAX} characters.')
        return back

    # Built by the SAME function as the live screen, from the members the access check just
    # passed — so a snapshot never stores a site this user wasn't checked against.
    _rows, data, _view = _live_view(group, members)
    counted = sum(1 for s in data['sites'] if s['counted'])
    # S6: a snapshot of nothing counted would be an empty record.
    if not counted:
        messages.error(request, NOTHING_TO_SNAPSHOT)
        return back

    profile = request.user.profile
    name = _person(profile)
    try:
        with transaction.atomic():
            # RACE: lock the group row, the same lock design_boq_group_delete takes, so a
            # snapshot and a delete of one group serialise (Zuber). If the delete won, the
            # row is gone and there is nothing to snapshot; if this won, the delete sees the
            # snapshot once it gets the lock and refuses.
            if not list(DesignBOQGroup.objects.select_for_update()
                        .filter(pk=group.pk).values_list('pk', flat=True)):
                messages.error(request, 'This group was deleted a moment ago.')
                return redirect('design_boq_group_list')
            DesignBOQGroupSnapshot.objects.create(
                group=group, taken_by=profile, taken_by_name=name, note=note, data=data,
                fingerprint=snapshot_fingerprint(data),
                site_ids=[s['id'] for s in data['sites']],
                sites_counted=counted, sites_not_counted=len(data['sites']) - counted)
            log_activity(None, profile,
                         f'{name} took a snapshot of design BOQ group "{group.name}": '
                         f'{counted} site{"" if counted == 1 else "s"} counted',
                         entity_type='DesignBOQGroup', entity_id=group.pk,
                         action_code='design_boq_group_snapshot_taken')
    except IntegrityError:
        # The group's row went between the lock and the insert's FK check — only possible
        # if the lock was not honoured (e.g. SQLite, which ignores FOR UPDATE).
        messages.error(request, 'This group was deleted a moment ago.')
        return redirect('design_boq_group_list')
    messages.success(request, f'Snapshot saved: {counted} site{"" if counted == 1 else "s"} '
                              f'counted.')
    return back


def _detail_url(pk):
    return reverse('design_boq_group_detail', kwargs={'pk': pk})


@login_required
def design_boq_group_snapshot_detail(request, pk, snapshot_pk):
    """One snapshot, read-only, from its stored copy only — never from live data.
    Access: design users who can open every site the snapshot STORES (S4), whatever the
    group holds today; Head and QC also when a stored site no longer exists (Zuber A7)."""
    snapshot, refusal = _snapshot_or_refuse(request, pk, snapshot_pk)
    if refusal:
        return refusal
    view = hydrate(snapshot.data)
    return render(request, 'projects/design/boq_group_snapshot.html', {
        'snapshot':  snapshot,
        'group_pk':  pk,
        'view':      view,
        'boq':       view['boq'],
        'warnings':  warning_lines(view['boq']),
    })


@login_required
def design_boq_group_snapshot_excel(request, pk, snapshot_pk):
    """One snapshot as the three-sheet .xlsx, from its stored copy only (S5). Access: as
    the snapshot page. File name <group>_snapshot_<YYYY-MM-DD_HHMM>.xlsx, IST."""
    snapshot, refusal = _snapshot_or_refuse(request, pk, snapshot_pk)
    if refusal:
        return refusal
    view = hydrate(snapshot.data)
    taken = timezone.localtime(snapshot.taken_at)
    intro = [f'Snapshot taken {taken:%d %b %Y %H:%M} IST by {snapshot.taken_by_name} from '
             f'{_counted_phrase(view)}. Quantities only, no prices.']
    if snapshot.note:
        intro.append(f'Note: {snapshot.note}')
    workbook = build_group_workbook(
        f'{view["group_name"]} — snapshot of {taken:%d %b %Y %H:%M} IST',
        intro, view['sites'], view['boq'])
    # Zuber A3: the group's name as stored at the time, in the title and the file name.
    return _xlsx_response(workbook,
                          f'{_safe_name(view["group_name"])}_snapshot_{taken:%Y-%m-%d_%H%M}.xlsx')


@login_required
def design_boq_group_delete(request, pk):
    """Delete a design BOQ group and its memberships; the sites' BOQs are untouched (S7).
    Only a group with no snapshots, and only after its name is typed exactly. Access:
    design users who can open every site in the group. POST only."""
    group, members, refusal = _open_group_or_refuse(request, pk)
    if refusal:
        return refusal
    if request.method != 'POST':
        return redirect('design_boq_group_detail', pk=group.pk)

    # Zuber A6: exact, case-sensitive, after trimming both ends — a deliberate act.
    if (request.POST.get('confirm_name') or '').strip() != group.name:
        messages.error(request, DELETE_NAME_MISMATCH)
        return redirect('design_boq_group_detail', pk=group.pk)

    profile = request.user.profile
    name = group.name
    with transaction.atomic():
        # RACE: the same row lock design_boq_group_snapshot_take takes, so a snapshot taken
        # at this moment either lands first (and is seen just below) or waits and then finds
        # the group gone.
        locked = DesignBOQGroup.objects.select_for_update().filter(pk=group.pk).first()
        if locked is None:
            messages.info(request, f'"{name}" was already deleted.')
            return redirect('design_boq_group_list')
        # S7: snapshots point at their group (PROTECT), so the group stays. The database
        # would refuse anyway; this says why first.
        if DesignBOQGroupSnapshot.objects.filter(group=locked).exists():
            messages.error(request, GROUP_HAS_SNAPSHOTS)
            return redirect('design_boq_group_detail', pk=group.pk)
        # Every membership, soft-deleted sites included: the log names what was in it.
        codes = list(locked.memberships.order_by('project__project_id')
                     .values_list('project__project_id', flat=True))
        try:
            locked.delete()             # memberships go with it (CASCADE)
        except ProtectedError:
            # A snapshot slipped in despite the lock (SQLite ignores FOR UPDATE).
            messages.error(request, GROUP_HAS_SNAPSHOTS)
            return redirect('design_boq_group_detail', pk=group.pk)
        sites = (f'{len(codes)} site{"" if len(codes) == 1 else "s"}: {_short_list(codes)}'
                 if codes else 'no sites')
        log_activity(None, profile,
                     f'{_person(profile)} deleted design BOQ group "{name}" with {sites}',
                     entity_type='DesignBOQGroup', entity_id=pk,
                     action_code='design_boq_group_deleted')
    messages.success(request, f'Deleted "{name}". The sites\' BOQs are unchanged.')
    return redirect('design_boq_group_list')


# ---------------------------------------------------------------------------
# The Excel
# ---------------------------------------------------------------------------

SUMMARY_SHEET = 'Summary'
BY_SITE_SHEET = 'By site'
SITES_SHEET = 'Sites'
SUMMARY_HEADERS = ['Code', 'Description', 'Unit', 'Sites', 'Total', 'Note']
UNLINKED_TITLE = 'Lines with no catalogue item (not summed across sites)'
UNLINKED_HEADERS = ['Site', 'Description', 'Unit', 'Quantity']
SITES_HEADERS = ['Site', 'Site name', 'Tender', 'Designer', 'Design status', 'Counted']


def warning_lines(boq):
    """The warnings shown above the table and on the Summary sheet, as plain sentences.
    One list for both, so the screen and the file cannot say different things.

    B-5b: `boq` is a hydrate()d BOQ, live or a snapshot's. A snapshot stores the warning
    FACTS, and they are worded here when shown (Zuber A9)."""
    lines = []
    if boq['not_counted']:
        lines.append('Not counted yet: ' + ', '.join(
            f"{r['code']} ({r['status']})" for r in boq['not_counted']) + '.')
    if boq['blanks']:
        lines.append('Blank quantities counted as 0: ' + ', '.join(
            f"{b['site']} {b['code'] or b['description'][:40]}" for b in boq['blanks']) + '.')
    for w in boq['unit_warnings']:
        qty = 'blank' if w['quantity'] is None else _num(w['quantity'])
        lines.append(f"{w['site']} {w['code']}: quantity {qty}, line unit {w['line_unit']}, "
                     f"catalogue unit {w['catalogue_unit']} — {' and '.join(w['reasons'])}. "
                     f"Still counted in the total.")
    return lines


def _num(value):
    """A Decimal as a plain number for a sentence: 12, 12.5 — never 12.00 or 1.2E+1."""
    if value is None:
        return ''
    text = format(value.normalize(), 'f') if isinstance(value, Decimal) else str(value)
    return text


def _bold_row(ws, row_number):
    for cell in ws[row_number]:
        cell.font = Font(bold=True)


def build_group_workbook(title, intro_lines, sites, boq):
    """The three-sheet workbook. Pure: reads only a hydrate()d view (B-5b), so the live
    Excel and a snapshot's Excel are the same code fed different plain data.

    `title` is the bold first line; `intro_lines` follow it (when it was generated or
    taken, by whom, the note). Quantities are written as numbers (openpyxl writes Decimal
    natively). ACCB descriptions are written whole: a cell holds up to 32,767 characters,
    and B-3c's text runs past 255.
    """
    wb = Workbook()
    wrap = Alignment(wrap_text=True, vertical='top')
    counted = boq['counted_codes']

    # ---- Summary -----------------------------------------------------------------
    ws = wb.active
    ws.title = SUMMARY_SHEET
    ws.append([title])
    ws['A1'].font = Font(bold=True, size=13)
    for line in intro_lines:
        ws.append([line])
    warnings = warning_lines(boq)
    if warnings:
        ws.append([])
        ws.append(['Warnings'])
        _bold_row(ws, ws.max_row)
        for line in warnings:
            ws.append([line])
    ws.append([])
    ws.append(SUMMARY_HEADERS)
    _bold_row(ws, ws.max_row)
    header_row = ws.max_row
    for line in boq['lines']:
        ws.append([line['item_master__code'], line['item_master__description'],
                   line['item_master__unit'], line['site_count'], line['total_quantity'],
                   'Retired' if line['retired'] else ''])
    for accb in boq['accb_lines']:
        ws.append([accb['code'], accb['description'], accb['unit'], accb['site_count'],
                   accb['total_quantity'], 'Retired' if accb['retired'] else ''])
    for row in ws.iter_rows(min_row=header_row + 1, min_col=2, max_col=2):
        row[0].alignment = wrap
    if boq['unlinked']:
        ws.append([])
        ws.append([UNLINKED_TITLE])
        _bold_row(ws, ws.max_row)
        ws.append(UNLINKED_HEADERS)
        _bold_row(ws, ws.max_row)
        for item in boq['unlinked']:
            ws.append([item['site'], item['description'], item['unit'], item['quantity']])
    for letter, width in (('A', 14), ('B', 80), ('C', 9), ('D', 8), ('E', 12), ('F', 10)):
        ws.column_dimensions[letter].width = width

    # ---- By site: items × counted sites -----------------------------------------
    by = wb.create_sheet(BY_SITE_SHEET)
    by.append(['Code', 'Description', 'Unit', 'Note'] + counted + ['Total'])
    _bold_row(by, 1)
    for line in boq['lines']:
        by.append([line['item_master__code'], line['item_master__description'],
                   line['item_master__unit'], 'Retired' if line['retired'] else '']
                  + [line['per_site'].get(code) for code in counted]
                  + [line['total_quantity']])
    for accb in boq['accb_lines']:
        by.append([accb['code'], accb['description'], accb['unit'],
                   'Retired' if accb['retired'] else '']
                  + [accb['per_site'].get(code) for code in counted]
                  + [accb['total_quantity']])
    by.freeze_panes = 'E2'
    by.column_dimensions['A'].width = 14
    by.column_dimensions['B'].width = 60

    # ---- Sites ----------------------------------------------------------------------
    st = wb.create_sheet(SITES_SHEET)
    st.append(SITES_HEADERS)
    _bold_row(st, 1)
    # Each site's status as stored: now for the live file, at the time for a snapshot (S5).
    for s in sites:
        st.append([s['code'], s['name'], s['tender'], s['designer'],
                   s['status'], 'Yes' if s['counted'] else 'No'])
    for letter, width in (('A', 14), ('B', 30), ('C', 28), ('D', 22), ('E', 30), ('F', 9)):
        st.column_dimensions[letter].width = width
    return wb

