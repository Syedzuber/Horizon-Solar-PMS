"""
SNAP-1 / SNAP-1b / SNAP-1c — the daily Site Dashboard snapshot: the figures, and the PNG
images that carry them.

    payload = collect_snapshot(now)                         # reads the DASH-B1 layer only
    pngs = render_snapshot_images(payload, previous)        # pure: no database access

WHERE EVERY FIGURE COMES FROM. Nothing here works a figure out for itself:
    site_dashboard.build_site_cards()      every card, as the CEO's dashboard builds them
    views_site_dashboard.kpi_tiles()       the 8 header tiles and their sub-lines, the
                                           very function the page calls (_kpis)
    site_dashboard.capacity_totals()       the DC kWp number behind the tile's text, for
                                           the day-on-day difference only
    site_breakdown.site_breakdown()        TB-1's in execution / Mobilized / Active /
                                           Active today, the tender page's own strip,
                                           and TB-3's Attendance today, Design and
                                           Procurement figures (SNAP-1c), from the same
                                           call
    views_site_dashboard.dates_as_of()     the page's "Dates as of" line
Each card's figures do not depend on who views it (the payment figure aside, which is
never read), so cutting the CEO's cards gives what anyone sees for the same sites; the
tests pin it.

WHAT IS DRAWN (SNAP-1b, Zuber's rulings R1-R6, 14 Nov 2026). One image, "All tenders",
with one section per tender that has at least one site in execution: a heading (code,
name, the tender's active PMs), TB-1's breakdown strip over that tender's sites, and the
dashboard tiles computed from that tender's cards only (the "Sites in execution" tile is
left out; the strip carries it). No company-wide totals, no PM or tender table, no
Residential, standalone CAPEX or no-tender RESCO section. More than
MAX_SECTIONS_PER_IMAGE tenders continue on a second image; a section is never split.

SNAP-1c (Zuber's rulings S1-S7 and answers Q1-Q4, 17 Nov 2026) adds, per section, TB-3's
figures exactly as the tender page shows them (S2, Q2): Attendance today as a fifth strip
figure set apart like Active today (no chevron), and a Design and a Procurement row
between the strip and the tiles (Design on one line, Procurement on two fixed lines, Q3).
Every new figure gets an arrow by Program id, as the old ones do (S4), except "of N
mobilized", which is the strip's Mobilized figure and carries its arrow there (Q4). The
figures are site_breakdown over the section's own sites, i.e. the tender page's figures
over all its sites, never the Site Dashboard's RESCO-only cut (S6): every tender is
all-RESCO today, and a non-RESCO site on a tender would still be counted here as there.
A Residential site is never counted in a tender section, even on a Program (Q1;
Project.save() refuses one, so the rule is defensive, as _tender_key()'s).

TENDERS SWITCHED OFF (SNAP-3a, rulings K3 and Q2, 20 Nov 2026). A tender whose
Program.in_daily_snapshot an Admin has switched off gets no section, so it is not drawn
and not in the headline, and it has no stored By tender row. The organisation tiles and
the By PM rows still count its sites: they are company-wide and not drawn. Switched back
on, it gets a section again, with no arrows that day, because yesterday's row has no
section for it (section_deltas). No other page reads the switch.

FINANCE COUNTS (SNAP-3b, rulings F1-F5 and answers Q2-Q6, 20 Nov 2026). Each section
gets a Finance line under Procurement, in the same box:
    N invoices · N payment requests · N approved · N pending · N paid
COUNTS ONLY, NEVER MONEY (F1): tender_finance_counts() selects ids and statuses, never an
amount column, and the payload carries five whole numbers per tender. A zero is drawn as
0 in plain ink on purpose: it tells the team that finance data is not being entered (SNAP-3c
will flag it). Vendor side only (F2): tenders have no client billing in PMS.
    invoices          invoice files on the tender's orders (VendorOrderDocument,
                      doc_type 'invoice') + contractor bills on its sites that were not
                      withdrawn (Q3: a bill is the contractor's invoice)
    payment requests  every request, any status (PaymentRequest has no withdrawn or
                      cancelled status to leave out)
    approved          approved or paid (Q2): the row reads as a funnel, requests >=
                      approved >= paid
    pending           awaiting approval, on hold, or approved and not yet paid; never
                      rejected
    paid              marked paid (CONFIRMED)
A request belongs to a tender through its order's picked tenders (VendorOrderProgram)
UNION the tenders of the order's sites (Q5), or through its contractor bill's site. An
order naming two tenders counts in both; a request linked to no tender counts nowhere.
Test data is dropped by the CEO strip's rule (Q4, payments.exclude_test_site_payments).
One line, no arrows (Q6); the counts are stored per tender so SNAP-3c can trend them (F5).
The image only: no page shows this line (F4).

WHAT IS STORED BUT NOT DRAWN. The SNAP-1 organisation row and the By PM / By tender rows
stay in the payload (and their tests), so the stored history keeps its shape; only
`tender_sections` is drawn.

WHOSE VIEW (SNAP-1 Q2). build_site_cards() needs a real user; the command has none. The
lowest-pk active CEO is used: every CEO sees the same sites (DASH-B1 D1). Not the Design
Head, who sees every site too but no money row in the activity feeds, which can make a
site look quiet that the CEO sees updated.

NO MONEY (locked rule). kpi_tiles() is called with sees_payments=False, so the payment
tile is never built, and no card's payment_open is read. The payload's keys are pinned
by tests_dashboard_snapshot. SNAP-3b's finance figures are counts of records, not money,
and read no amount (tests_snapshot_finance pins the SQL).

ROWS (stored only).
    By PM      one row per ACTIVE PM (role PM, both active flags) who is the assigned PM
               of at least one site in execution. Coordinators are not rows (they would
               count their sites twice). Sites whose assigned PM is not an active PM go
               to one "No active PM" row (Q3).
    By tender  Residential sites -> "Residential", even with a tender; else a site with a
               tender -> that tender's row; CAPEX without one -> "CAPEX (standalone)";
               RESCO (OPEX) without one -> "RESCO (no tender)" (Q4). Every site is in
               exactly one row.
"""
import io
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from .models import (
    APPROVAL_WITHDRAWN, VENDOR_ORDER_DOC_INVOICE, ContractorBillDetail, PaymentRequest,
    Program, Project, UserProfile, VendorOrder, VendorOrderDocument, VendorOrderProgram,
    VendorOrderSite,
)
from .payments import exclude_test_site_payments
from .site_breakdown import site_breakdown
from .site_dashboard import build_site_cards, capacity_totals
from .views_site_dashboard import dates_as_of, kpi_tiles

#: The image's fixed width (WhatsApp-friendly; SNAP-1 rendering rules).
IMAGE_WIDTH = 1080

#: R6: more tender sections than this continue on another image ("All tenders (2/2)").
#: Three since SNAP-1c (ruling S5): a section is 628 px tall with its Design and
#: Procurement rows, and a taller image is shrunk by WhatsApp until the small text blurs.
MAX_SECTIONS_PER_IMAGE = 3

#: The 8 figures, in the dashboard's order: (payload key, the page's tile label, a short
#: heading). collect_snapshot() checks the labels against what kpi_tiles() returns, so a
#: renamed or reordered tile stops the run instead of printing a figure under the wrong
#: label.
FIGURES = (
    ('sites', 'Sites in execution', 'Sites'),
    ('dc_kwp', 'DC kWp in execution', 'DC kWp'),
    ('behind', 'Behind plan', 'Behind'),
    ('civil', 'Needs Civil date', 'Civil'),
    ('overdue', 'Overdue tasks', 'Overdue'),
    ('blocked', 'Blocked tasks', 'Blocked'),
    ('issues', 'Open issues', 'Issues'),
    ('quiet', 'Gone quiet', 'Quiet'),
)
FIGURE_KEYS = tuple(key for key, _, _ in FIGURES)

#: A tender section's tiles: every figure but "Sites in execution", which the strip shows.
SECTION_KEYS = tuple(key for key in FIGURE_KEYS if key != 'sites')

#: The two rows of tiles in a section: four, then three wider ones (approved prototype).
SECTION_TILE_ROWS = (('dc_kwp', 'overdue', 'behind', 'civil'),
                     ('blocked', 'issues', 'quiet'))

#: A section tile's colour, exactly as the Site Dashboard tones it (views_site_dashboard
#: ._kpis): red for Behind plan, Overdue and Blocked tasks, amber for Needs Civil date,
#: Open issues and Gone quiet, and plain when the figure is 0 (Zuber, 14 Nov). Kept as a
#: constant because the stored tile carries no tone; tests_dashboard_snapshot pins it
#: against kpi_tiles()' own tones so the two cannot drift apart.
SECTION_TONES = {'behind': 'crit', 'civil': 'warn', 'overdue': 'crit', 'blocked': 'crit',
                 'issues': 'warn', 'quiet': 'warn'}

#: TB-1's strip: (SiteBreakdown attribute and payload key, label, grey definition).
#: The labels were confirmed by Zuber, 14 Nov. Attendance today is SNAP-1c's fifth figure,
#: worded as the tender page's TB-3 box words it (ruling Q2).
STRIP = (
    ('in_execution', 'Sites in execution', None),
    ('mobilized', 'Mobilized', 'DC + design started'),
    ('active', 'Active', 'Civil In Progress/Done'),
    ('active_today', 'Active today', 'any PMS update, today'),
    ('attendance_today', 'Attendance today', 'contractor entry, IST day'),
)
STRIP_KEYS = tuple(key for key, _, _ in STRIP)

#: The strip figures chained with '›' (TB-1 B5); the rest stand apart behind a divider.
STRIP_CHAIN = ('in_execution', 'mobilized', 'active')

#: SNAP-1c: the Design and Procurement rows' figures, SiteBreakdown attributes all.
#: "of N mobilized" reads the strip's Mobilized, so it is not listed again.
LINE_KEYS = ('design_not_started', 'design_in_progress', 'design_released',
             'received', 'pending', 'dcs_to_confirm', 'dcs_overdue')

#: Every figure a section's `breakdown` carries, in payload order.
BREAKDOWN_KEYS = STRIP_KEYS + LINE_KEYS

#: The rows' colours when the figure is not 0 (ruling S3): what is done green, what waits
#: on someone amber, the same idea as the tiles; every other figure, and any 0, is plain.
LINE_TONES = {'design_released': 'good', 'received': 'good',
              'pending': 'warn', 'dcs_to_confirm': 'warn', 'dcs_overdue': 'warn'}

#: SNAP-3b: the Finance line's five counts, in drawing order. 'payment_pending', not
#: 'pending': Procurement already has a 'pending' key, and _lines() looks a figure's value
#: up by key across both rows. None is in LINE_TONES, so all are drawn in plain ink (Q6).
FINANCE_KEYS = ('invoices', 'requests', 'approved', 'payment_pending', 'paid')

#: Which request statuses each request count takes (F2, Q2). `requests` takes all five.
FINANCE_APPROVED = frozenset({PaymentRequest.APPROVED, PaymentRequest.CONFIRMED})
FINANCE_PENDING = frozenset({PaymentRequest.PENDING_APPROVAL, PaymentRequest.ON_HOLD,
                             PaymentRequest.APPROVED})

NO_ACTIVE_PM = 'No active PM'
RESIDENTIAL_ROW = 'Residential'
CAPEX_ROW = 'CAPEX (standalone)'
RESCO_ROW = 'RESCO (no tender)'

#: The command's one line when there is nothing to draw (exit 0).
NO_TENDER_SKIP = 'skipped: no tender has a site in execution'

#: The page's filters cleared: the header exactly as an unfiltered page draws it.
#: kpi_tiles() builds each tile's link from these, which the snapshot never uses.
_UNFILTERED = {'sort': 'latest', 'q': '', 'type': '', 'tender': None, 'filter': ''}


class SnapshotError(Exception):
    """The snapshot cannot be taken; the command prints the message and exits non-zero."""


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def snapshot_viewer():
    """The user whose dashboard the snapshot reads: the lowest-pk active CEO (Q2)."""
    profile = (UserProfile.objects
               .filter(role='CEO', is_active=True, user__is_active=True)
               .select_related('user').order_by('pk').first())
    if profile is None:
        raise SnapshotError('no active CEO account to read the dashboard as')
    return profile.user


def _figures(cards):
    """The 8 figures of `cards`, from kpi_tiles(): [{'key', 'label', 'value', 'detail',
    'tone'}] in FIGURES order. `value` is the page's own value (the DC tile's is text,
    e.g. '8,239')."""
    tiles = kpi_tiles(cards, _UNFILTERED, False)
    if [t['label'] for t in tiles] != [label for _, label, _ in FIGURES]:
        raise SnapshotError('the Site Dashboard tiles have changed; snapshot.FIGURES '
                            'must be updated: ' + ', '.join(t['label'] for t in tiles))
    return [{'key': key, 'label': tile['label'], 'value': tile['value'],
             'detail': tile['detail'], 'tone': tile.get('tone', '')}
            for (key, _, _), tile in zip(FIGURES, tiles)]


def _row(row_id, name, cards):
    """One stored row: who or what it is, and the 8 figures as the page prints them."""
    return {'id': row_id, 'name': name,
            'values': {f['key']: f['value'] for f in _figures(cards)}}


def _dc_number(cards):
    """The DC kWp tile's number, for the day-on-day difference: capacity_totals(), the
    sum the tile prints, rounded as the tile rounds it (views_site_dashboard._whole)."""
    total = capacity_totals(cards).dc_kwp
    return round(total) if total is not None else 0


def _active_pm_owners(live):
    """{site pk: (user id, name) of its assigned PM, or None when that PM is not an
    active PM}. Shared by the By PM rows and the section headings (Q3), so both name the
    same people."""
    # Each live site's assigned PM, with what decides whether they count: role PM and
    # both active flags (UserProfile.is_active is this app's deactivation, User's
    # Django's). One query for every site; no figure is read here, only who owns a site.
    owners = {}
    for pk, profile_pk, role, p_active, u_active, user_id, first, last, username in (
            Project.objects.filter(pk__in=[c.id for c in live])
            .values_list('pk', 'assigned_pm_id', 'assigned_pm__role',
                         'assigned_pm__is_active', 'assigned_pm__user__is_active',
                         'assigned_pm__user_id', 'assigned_pm__user__first_name',
                         'assigned_pm__user__last_name', 'assigned_pm__user__username')):
        if profile_pk is not None and role == 'PM' and p_active and u_active:
            owners[pk] = (user_id, f'{first} {last}'.strip() or username)
        else:
            owners[pk] = None
    return owners


def _pm_rows(live, owners):
    """By PM (stored only; see the module docstring). Sorted by Overdue tasks, then Gone
    quiet, both descending, then name; the No active PM row last."""
    by_pm, orphans = {}, []
    for card in live:
        owner = owners.get(card.id)
        if owner is None:
            orphans.append(card)
        else:
            by_pm.setdefault(owner, []).append(card)

    rows = [_row(user_id, name, cards) for (user_id, name), cards in by_pm.items()]
    rows.sort(key=lambda r: (-r['values']['overdue'], -r['values']['quiet'],
                             r['name'].lower()))
    if orphans:
        rows.append(_row(None, NO_ACTIVE_PM, orphans))
    return rows


def _tender_key(card):
    """(row key, row name) for one site (Q4)."""
    if card.project_type == 'Residential':
        return ('residential', RESIDENTIAL_ROW)
    if card.program_id is not None:
        code, name = card.program_code or '', card.program_name or ''
        # The short code first, as the dashboard's tender filter shows it, then the
        # name when it says something more.
        label = f'{code} · {name}' if code and name and code != name else (code or name)
        return (f'program:{card.program_id}', label)
    if card.project_type == 'CAPEX':
        return ('capex', CAPEX_ROW)
    return ('resco', RESCO_ROW)


def _tender_rows(live):
    """By tender (stored only). Tenders sorted by Overdue tasks, then Gone quiet, both
    descending, then name; Residential, CAPEX (standalone) and RESCO (no tender) after
    them in that order, each only when it has a site."""
    groups = {}
    for card in live:
        groups.setdefault(_tender_key(card), []).append(card)
    tenders, others = [], {}
    for (key, name), cards in groups.items():
        if key.startswith('program:'):
            row = _row(int(key.split(':', 1)[1]), name, cards)
            tenders.append(row)
        else:
            others[key] = _row(None, name, cards)
    tenders.sort(key=lambda r: (-r['values']['overdue'], -r['values']['quiet'],
                                r['name'].lower()))
    return tenders + [others[k] for k in ('residential', 'capex', 'resco') if k in others]


def switched_off_tender_ids():
    """The ids of the tenders an Admin has left out of the snapshot (SNAP-3a K3). One
    query. Soft-deleted Programs are not filtered: their sites are on no card anyway."""
    return frozenset(Program.objects.filter(in_daily_snapshot=False)
                     .values_list('pk', flat=True))


def _in_switched_off_tender(card, switched_off):
    """True when `card` would count in a switched-off tender's section or By tender row.
    Asked through _tender_key, so a Residential site on such a Program stays in the
    Residential row, as it always has (Q1)."""
    return (_tender_key(card)[0].startswith('program:')
            and card.program_id in switched_off)


def section_cards(live):
    """{program id: that tender's cards} for every tender with a site in execution (R4).
    A Residential site is never in a section, even on a Program (Q1); a CAPEX site on a
    Program is in its tender (only CAPEX *without* a tender is "standalone")."""
    groups = {}
    for card in live:
        if card.program_id is None or card.project_type == 'Residential':
            continue
        groups.setdefault(card.program_id, []).append(card)
    return groups


def tender_finance_counts(program_ids):
    """SNAP-3b: {program id: {FINANCE_KEYS: count}} for each of `program_ids`, zero-filled.
    FIVE queries however many orders, requests or bills there are. See the module
    docstring's FINANCE COUNTS for what each count means.

    NO AMOUNT IS READ (F1): every query ends in values_list() over ids and statuses, so no
    amount, approved_amount or invoice_amount column is selected, and none is filtered on.
    tests_snapshot_finance pins the SQL.
    """
    ids = set(program_ids)
    counts = {pid: dict.fromkeys(FINANCE_KEYS, 0) for pid in ids}
    if not ids:
        return counts

    # The two record-keeping links from an order to a tender (Q5), each kept to the
    # tenders being drawn. The picked tenders: the raiser may edit these after raising
    # (VendorOrderProgram), so they can differ from the sites' tenders, and both count.
    program_links = VendorOrderProgram.objects.filter(program_id__in=ids)
    # The order's sites' tenders. A test or soft-deleted site links no tender: a test
    # site is in no section, and a deleted site is in no reader (the soft-delete rule).
    # Residential is left out as section_cards() leaves it out (Q1 of SNAP-1b), though
    # Project.save() already refuses a Residential site on a Program.
    site_links = (VendorOrderSite.objects
                  .filter(project__program_id__in=ids, project__is_deleted=False,
                          project__is_test=False)
                  .exclude(project__project_type='Residential'))
    # Queries 1 and 2: {order id: its tenders among `ids`}. A set per order, so an order
    # whose picked tender is also its sites' tender counts there once.
    order_tenders = defaultdict(set)
    for order_id, pid in program_links.values_list('order_id', 'program_id'):
        order_tenders[order_id].add(pid)
    for order_id, pid in site_links.values_list('order_id', 'project__program_id'):
        order_tenders[order_id].add(pid)
    # The same orders as ONE subquery for queries 3 and 4, so no list of order ids is sent
    # as parameters, however many orders there are. One subquery over VendorOrder, not
    # "order IN (picked) OR order IN (sites)": SQLite 3.50 (the test suite's database)
    # fails that OR on VendorOrderDocument with "internal query planner error". The join
    # can repeat an order, which IN ignores.
    linked_orders = (VendorOrder.objects
                     .filter(Q(programs__in=program_links) | Q(sites__in=site_links))
                     .values('pk'))

    # Query 3: every request on a linked order, or on a contractor bill whose site is on
    # one of the tenders (bill -> its one site's tender, Q5), minus test data by the CEO
    # strip's rule (Q4). A bill on a deleted site is left out by the soft-delete rule. No
    # status is filtered out: `requests` counts all five, and the loop sorts the rest.
    # order_by() drops Meta.ordering's -requested_date, which nothing here needs.
    requests = exclude_test_site_payments(PaymentRequest.objects.filter(
        Q(vendor_order__in=linked_orders) | Q(contractor_bill__project__program_id__in=ids,
                   contractor_bill__project__is_deleted=False)))
    for order_id, bill_pid, bill_type, status in (
            requests.order_by().values_list('vendor_order_id',
                                            'contractor_bill__project__program_id',
                                            'contractor_bill__project__project_type',
                                            'status')):
        if order_id is not None:
            tenders = order_tenders[order_id]
        else:
            tenders = () if bill_type == 'Residential' else (bill_pid,)
        # A request on an order naming two tenders counts in both (F3).
        for pid in tenders:
            tally = counts[pid]
            tally['requests'] += 1
            tally['approved'] += status in FINANCE_APPROVED
            tally['payment_pending'] += status in FINANCE_PENDING
            tally['paid'] += status == PaymentRequest.CONFIRMED

    # Query 4: invoice files on linked orders. Test data by the same rule as the requests
    # (exclude_test_site_payments' order term, restated for a document's order): an
    # order is dropped when EVERY site it names is a test site; one with no site at all
    # is kept, because "no site" is not evidence of test data.
    order_sites = VendorOrderSite.objects.filter(order=OuterRef('order'))
    invoices = (VendorOrderDocument.objects
                .filter(order__in=linked_orders, doc_type=VENDOR_ORDER_DOC_INVOICE)
                .exclude(Exists(order_sites)
                         & ~Exists(order_sites.filter(project__is_test=False))))
    for order_id in invoices.order_by().values_list('order_id', flat=True):
        for pid in order_tenders[order_id]:
            counts[pid]['invoices'] += 1

    # Query 5: contractor bills on the tenders' sites that were not withdrawn (Q3). A
    # rejected bill still counts: the contractor did send that invoice.
    bills = (ContractorBillDetail.objects
             .filter(project__program_id__in=ids, project__is_deleted=False,
                     project__is_test=False)
             .exclude(project__project_type='Residential')
             .exclude(request__status=APPROVAL_WITHDRAWN))
    for pid in bills.values_list('project__program_id', flat=True):
        counts[pid]['invoices'] += 1
    return counts


def _tender_sections(live, owners, now):
    """SNAP-1b: one section per qualifying tender, keyed by the Program id as TEXT (a
    JSONField turns int keys into strings anyway, so the stored and the fresh payload
    match on the same key; Zuber, 14 Nov).

    {'<program id>': {'id', 'code', 'name', 'pms', 'tiles', 'dc_kwp_number', 'breakdown',
                      'finance'}}

    `breakdown` holds BREAKDOWN_KEYS: the strip's five figures and the Design /
    Procurement rows' seven (SNAP-1c), all read off the one SiteBreakdown. `finance`
    holds FINANCE_KEYS (SNAP-3b), the tender's five finance counts.
    """
    sections = {}
    groups = section_cards(live)
    # One call for every tender, so the finance counts cost five queries in all, not
    # five per tender. Only the tenders drawn are asked for: a switched-off tender
    # (SNAP-3a) has no section and so no finance row.
    finance = tender_finance_counts(groups)
    for program_id, cards in groups.items():
        figures = {f['key']: f for f in _figures(cards)}
        # TB-1's helper over exactly these sites: the same call the tender page makes
        # over program.sites.all() (Q2), one query per tender. Its TB-3 fields come from
        # that same query, so SNAP-1c adds no query.
        breakdown = site_breakdown([c.id for c in cards], now)
        # The tender's active PMs by how many of its sites they hold, then name (Q3).
        counts = Counter(owners[c.id] for c in cards if owners.get(c.id))
        pms = [name for (_, name), _ in sorted(counts.items(),
                                               key=lambda kv: (-kv[1], kv[0][1].lower()))]
        first = cards[0]
        sections[str(program_id)] = {
            'id': program_id,
            'code': first.program_code or '',
            'name': first.program_name or '',
            'pms': pms,
            'tiles': [{'key': key, 'label': figures[key]['label'],
                       'value': figures[key]['value'], 'detail': figures[key]['detail']}
                      for key in SECTION_KEYS],
            'dc_kwp_number': _dc_number(cards),
            'breakdown': {key: getattr(breakdown, key) for key in BREAKDOWN_KEYS},
            'finance': finance[program_id],
        }
    return sections


def taken_at_label(moment):
    """'14 Nov 2026, 8:02 PM IST': the collection moment in IST, to the minute (P8)."""
    local = timezone.localtime(moment)
    hour = local.hour % 12 or 12
    return (f'{local.day} {local:%b %Y}, {hour}:{local:%M} '
            f'{"AM" if local.hour < 12 else "PM"} IST')


def date_label(day):
    return f'{day.day} {day:%b %Y}'


def collect_snapshot(now=None):
    """The day's figures as a JSON-ready payload (see the module docstring).

    {'snapshot_date', 'snapshot_date_label', 'taken_at', 'taken_at_label',
     'dates_as_of': {'text', 'stale'}, 'viewer',
     'org': {'tiles': [...], 'dc_kwp_number'},
     'pms': [{'id', 'name', 'values'}], 'tenders': [{'id', 'name', 'values'}],
     'tender_sections': {'<program id>': {...}}}
    """
    now = now or timezone.now()
    today = timezone.localtime(now).date()
    viewer = snapshot_viewer()
    # The dashboard's own build for that CEO, on the same IST day; drafts are not in
    # execution, so they count in no tile (views_site_dashboard.site_dashboard's `live`).
    live = [c for c in build_site_cards(viewer, today=today) if not c.is_draft]
    owners = _active_pm_owners(live)
    # SNAP-3a (Q2): switched-off tenders are cut from the sections and the By tender rows
    # only; `org` and `pms` below still read every live site.
    switched_off = switched_off_tender_ids()
    tender_live = [c for c in live if not _in_switched_off_tender(c, switched_off)]
    return {
        'snapshot_date': today.isoformat(),
        'snapshot_date_label': date_label(today),
        'taken_at': now.isoformat(),
        'taken_at_label': taken_at_label(now),
        'dates_as_of': dates_as_of(now),
        'viewer': viewer.username,
        'org': {'tiles': _figures(live), 'dc_kwp_number': _dc_number(live)},
        'pms': _pm_rows(live, owners),
        'tenders': _tender_rows(tender_live),
        'tender_sections': _tender_sections(tender_live, owners, now),
    }


def section_value(section, key):
    """A section's figure by key: a tile's value, or a strip or row value. A row figure
    missing from an older stored payload (before SNAP-1c) is None: no arrow."""
    if key in BREAKDOWN_KEYS:
        return section.get('breakdown', {}).get(key)
    for tile in section.get('tiles', ()):
        if tile['key'] == key:
            return tile['value']
    return None


def ordered_sections(payload):
    """The tender sections in drawing order: Overdue tasks, then Gone quiet, both
    descending, then tender code. A SNAP-1 payload (no sections) gives []."""
    return sorted((payload.get('tender_sections') or {}).values(),
                  key=lambda s: (-section_value(s, 'overdue'), -section_value(s, 'quiet'),
                                 (s['code'] or s['name']).lower(), s['id']))


def section_title(section):
    """(code, name): the name only when it says more than the code."""
    code = section['code'] or section['name']
    name = section['name'] if section['name'] and section['name'] != code else ''
    return code, name


def pm_line(section):
    """'PM: A, B', or 'No active PM' (Q3)."""
    return 'PM: ' + ', '.join(section['pms']) if section['pms'] else NO_ACTIVE_PM


def headline(payload):
    """'MPUVL 94 sites · IPGCLG 20 sites · RUDEH 1 site', in drawing order, for the
    email and the in-app line."""
    parts = []
    for section in ordered_sections(payload):
        sites = section['breakdown']['in_execution']
        parts.append(f'{section_title(section)[0]} {sites:,} site{"s" if sites != 1 else ""}')
    return ' · '.join(parts) or 'no tender has a site in execution'


# ---------------------------------------------------------------------------
# Day-on-day difference
# ---------------------------------------------------------------------------

def _org_number(payload, key):
    """An organisation figure as a number: the DC tile's text is replaced by the sum it
    was printed from."""
    if key == 'dc_kwp':
        return payload['org'].get('dc_kwp_number')
    for tile in payload['org']['tiles']:
        if tile['key'] == key:
            return tile['value']
    return None


def _difference(now_value, before):
    """now - before, or None when either is not a number or they are equal (no arrow)."""
    if not isinstance(now_value, int) or not isinstance(before, int):
        return None
    return (now_value - before) or None


def org_deltas(payload, previous):
    """{figure key: difference vs `previous`, or None}. None when there is no previous
    payload, the figure is missing from it, or the difference is 0 (no arrow drawn).
    Stored-row arithmetic only since SNAP-1b: the organisation row is not drawn."""
    return {key: _difference(_org_number(payload, key),
                             _org_number(previous, key) if previous else None)
            for key in FIGURE_KEYS}


def _section_number(section, key):
    """A section figure as a number: the DC tile's text is replaced by its sum."""
    if key == 'dc_kwp':
        return section.get('dc_kwp_number')
    return section_value(section, key)


def section_deltas(section, previous_payload):
    """{tile, strip or row key: difference vs the same tender (by Program id) in
    `previous_payload`, or None}. No previous payload, a SNAP-1 payload without
    sections, or a tender that had no section then: no arrows at all. A payload from
    before SNAP-1c has no row figures, so those get no arrow the first night (Q4)."""
    sections = (previous_payload or {}).get('tender_sections') or {}
    before = sections.get(str(section['id']))
    return {key: _difference(_section_number(section, key),
                             _section_number(before, key) if before else None)
            for key in SECTION_KEYS + BREAKDOWN_KEYS}


def delta_text(delta):
    """'▲ 3' / '▼ 12', or '' for no arrow."""
    if not delta:
        return ''
    return f'{"▲" if delta > 0 else "▼"} {abs(delta):,}'


def breakdown_lines(section, deltas=None):
    """SNAP-1c: the Design and Procurement rows as [(label, runs)], one entry per drawn
    line; the renderer draws the runs and the dry run prints them, so both say the same.

    Each run is (text, kind, key): kind 'num' is a figure (bold, coloured by
    line_colour(key, value)), 'arrow' its ▲/▼, 'text' the words between.

    The words are the tender page's TB-3 rows (_site_breakdown_strip.html) exactly (S2,
    Q2). Procurement takes two lines (Q3): its second starts where the page has
    PROCUREMENT_BREAK, so line one + PROCUREMENT_BREAK + line two is the page's row;
    tests_dashboard_snapshot pins that against the rendered include. "DC" / "DCs" follows
    Django's pluralize, as the page does.
    """
    b, deltas = section['breakdown'], deltas or {}

    def figure(key):
        runs = [(_number(b[key]), 'num', key)]
        arrow = delta_text(deltas.get(key))
        if arrow:
            runs.append((f' {arrow}', 'arrow', key))
        return runs

    def words(text):
        return [(text, 'text', None)]

    dcs = b['dcs_to_confirm']
    return [
        ('Design', figure('design_not_started') + words(' not started · ')
         + figure('design_in_progress') + words(' in progress · ')
         + figure('design_released') + words(' released + BOQ')),
        # "of N mobilized" carries no arrow: Mobilized has its own in the strip (Q4).
        ('Procurement', figure('received')
         + words(f' received of {_number(b["mobilized"])} mobilized')
         + words(' · ') + figure('pending') + words(' pending')),
        ('', figure('dcs_to_confirm') + words(f' DC{"" if dcs == 1 else "s"} to confirm, ')
         + figure('dcs_overdue') + words(' overdue')),
    ]


#: What the page has where the image breaks Procurement onto its second line.
PROCUREMENT_BREAK = ' · '


def finance_line(section):
    """SNAP-3b: the Finance line as (label, runs), in breakdown_lines()' run shape, so
    the renderer and the dry run draw it as they draw the rows above it.

    'N invoices · N payment requests · N approved · N pending · N paid'. Counts only, no
    arrows (Q6), plain ink at every value, zeros included. "invoice" and "payment
    request" take Django's pluralize rule, as "DC" does in the Procurement row.
    """
    f = section['finance']

    def count(key, singular=None):
        n = f[key]
        runs = [(_number(n), 'num', key)]
        if singular:
            return runs + [(f' {singular}{"" if n == 1 else "s"}', 'text', None)]
        return runs

    def words(text):
        return [(text, 'text', None)]

    return ('Finance', count('invoices', 'invoice') + words(' · ')
            + count('requests', 'payment request') + words(' · ')
            + count('approved') + words(' approved · ')
            + count('payment_pending') + words(' pending · ')
            + count('paid') + words(' paid'))


def section_lines(section, deltas=None):
    """Every line in a section's white box, in drawing order: breakdown_lines()' Design
    and Procurement rows, then SNAP-3b's Finance line."""
    return breakdown_lines(section, deltas) + [finance_line(section)]


def line_text(runs):
    """One drawn line as plain text, arrows included (the dry run's form)."""
    return ''.join(text for text, _, _ in runs)


# ---------------------------------------------------------------------------
# Rendering (pure: the payload in, PNG bytes out)
# ---------------------------------------------------------------------------

#: DejaVu Sans, already committed with its licence for the checklist PDFs (checklist_pdf).
FONT_DIR = Path(__file__).resolve().parent / 'fonts' / 'dejavu'

# The Site Dashboard's palette (site_dashboard.html's --sd-* variables), plus the pale
# greens of the approved prototype's strips.
BRAND = '#1a7a4a'
GOLD = '#f0a829'
NAVY = '#243a6b'
INK = '#16231b'
MUTED = '#56655c'
FAINT = '#7a887f'
LINE = '#d9e0d7'
PALE_GREEN = '#e8f4ec'
PALE_GREEN_LINE = '#c6e0cf'
ON_BRAND = '#d6ecdf'
TONE_COLOURS = {'crit': '#c23b2a', 'warn': '#a86400'}

#: SNAP-1c row colours (LINE_TONES): the brand green for done, the tiles' amber for waiting.
LINE_COLOURS = {'good': BRAND, 'warn': TONE_COLOURS['warn']}

MARGIN = 32

# Vertical layout, in pixels. The image's height is PAGE_HEAD_H + sections x SECTION_H +
# FOOTER_H, so a section never needs measuring before it is drawn.
BAND_H = 124            # green band: kicker, title, collected line
GOLD_H = 4
STAMP_H = 40            # pale-green "Dates as of" strip
PAGE_HEAD_H = BAND_H + GOLD_H + STAMP_H + 16
HEADING_H = 64
STRIP_TOP = HEADING_H + 12
STRIP_H = 96
# SNAP-1c: the Design and Procurement box, three fixed lines (Q3: Procurement wraps onto a
# second line always, not only when long, so a section's height never depends on its
# figures and image_height() stays arithmetic). SNAP-3b adds the Finance line as a fourth,
# behind a second rule (layout A: one line, +40 px a section).
LINES_TOP = STRIP_TOP + STRIP_H + 12
LINES_PAD = 14          # inside the box, above the first line and below the last
LINE_H = 36             # one text line
LINE_RULE = 4           # a rule between two labelled rows (Design | Procurement | Finance)
LINES_H = 2 * LINES_PAD + 4 * LINE_H + 2 * LINE_RULE
LINE_LABEL_W = 160      # the bold "Design" / "Procurement" / "Finance" column
TILES_TOP = LINES_TOP + LINES_H + 16
TILE_H = 120
TILE_GAP = 12
SECTION_H = TILES_TOP + 2 * TILE_H + TILE_GAP + 36      # 668 (SNAP-3b Q6; 628 before)
FOOTER_H = 56

# The strip's columns: three chained figures with '›' between them, then Active today and
# Attendance today, each set apart behind a divider (TB-1 B5; SNAP-1c S1). Widths are per
# column so five fit in 1080 px: each fits its label and grey definition (sized when the
# widest was "any PMS update, last 24 h" at 168 px; TB-5's shorter "any PMS update, today"
# left the widths as they were), and a 3-digit figure with a 3-digit arrow.
STRIP_COL_W = {'in_execution': 176, 'mobilized': 150, 'active': 160, 'active_today': 182,
               'attendance_today': 176}
STRIP_ARROW_W = 30
STRIP_DIVIDER_W = 30


@dataclass(frozen=True)
class _Fonts:
    kicker: object
    title: object
    sub: object
    stamp: object
    code: object
    name: object
    pm: object
    strip_label: object
    strip_value: object
    strip_def: object
    chevron: object
    tile_label: object
    tile_value: object
    tile_detail: object
    delta: object
    line_label: object
    line_num: object
    line_text: object


def _fonts():
    from PIL import ImageFont   # Lazy import: Pillow is needed only when rendering.
    regular = str(FONT_DIR / 'DejaVuSans.ttf')
    bold = str(FONT_DIR / 'DejaVuSans-Bold.ttf')

    def r(size):
        return ImageFont.truetype(regular, size)

    def b(size):
        return ImageFont.truetype(bold, size)
    return _Fonts(kicker=b(15), title=b(34), sub=r(18), stamp=r(16),
                  code=b(28), name=r(20), pm=r(16),
                  strip_label=b(15), strip_value=b(30), strip_def=r(13), chevron=r(30),
                  tile_label=b(16), tile_value=b(34), tile_detail=r(14), delta=r(15),
                  line_label=b(20), line_num=b(20), line_text=r(20))


def fit_text(draw, text, font, max_width):
    """`text` cut with '…' to fit `max_width` pixels; never wrapped."""
    text = str(text)
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + '…', font=font) > max_width:
        text = text[:-1]
    return text.rstrip() + '…'


def _number(value):
    return f'{value:,}' if isinstance(value, int) else str(value)


def _page_header(draw, fonts, payload, title):
    """The green band and the "Dates as of" strip. Returns the y after (PAGE_HEAD_H)."""
    width = IMAGE_WIDTH - 2 * MARGIN
    draw.rectangle([0, 0, IMAGE_WIDTH, BAND_H], fill=BRAND)
    draw.text((MARGIN, 18), 'SITE DASHBOARD · DAILY SNAPSHOT', font=fonts.kicker,
              fill=ON_BRAND)
    draw.text((MARGIN, 42), fit_text(draw, title, fonts.title, width), font=fonts.title,
              fill='white')
    draw.text((MARGIN, 90), f'Collected {payload["taken_at_label"]}', font=fonts.sub,
              fill=ON_BRAND)
    draw.rectangle([0, BAND_H, IMAGE_WIDTH, BAND_H + GOLD_H], fill=GOLD)
    top = BAND_H + GOLD_H
    draw.rectangle([0, top, IMAGE_WIDTH, top + STAMP_H], fill=PALE_GREEN)
    stamp = payload['dates_as_of']
    # Amber when the morning refresh has not run, as the page draws it.
    draw.text((MARGIN, top + 10), fit_text(draw, stamp['text'], fonts.stamp, width),
              font=fonts.stamp, fill=TONE_COLOURS['warn'] if stamp['stale'] else MUTED)
    return PAGE_HEAD_H


def _heading(draw, fonts, section, top):
    """Navy bar, code (bold) and name, and the PM line under them."""
    draw.rectangle([MARGIN, top, MARGIN + 6, top + HEADING_H - 8], fill=NAVY)
    x = MARGIN + 20
    right = IMAGE_WIDTH - MARGIN
    code, name = section_title(section)
    code = fit_text(draw, code, fonts.code, right - x)
    draw.text((x, top - 2), code, font=fonts.code, fill=NAVY)
    if name:
        nx = x + draw.textlength(code, font=fonts.code) + 14
        if nx < right - 40:
            draw.text((nx, top + 6), fit_text(draw, name, fonts.name, right - nx),
                      font=fonts.name, fill=MUTED)
    draw.text((x, top + 38), fit_text(draw, pm_line(section), fonts.pm, right - x),
              font=fonts.pm, fill=MUTED)


def _strip(draw, fonts, section, deltas, top):
    """TB-1's breakdown strip: in execution › Mobilized › Active, then Active today and
    Attendance today (SNAP-1c), each set apart behind a divider."""
    draw.rounded_rectangle([MARGIN, top, IMAGE_WIDTH - MARGIN, top + STRIP_H], radius=12,
                           fill=PALE_GREEN, outline=PALE_GREEN_LINE, width=2)
    x = MARGIN + 20
    for key, label, definition in STRIP:
        width = STRIP_COL_W[key]
        if key not in STRIP_CHAIN:
            divider = x + 6
            draw.line([divider, top + 16, divider, top + STRIP_H - 16],
                      fill=PALE_GREEN_LINE, width=2)
            x += STRIP_DIVIDER_W
        draw.text((x, top + 12), label, font=fonts.strip_label, fill=MUTED)
        value = _number(section['breakdown'][key])
        draw.text((x, top + 32), value, font=fonts.strip_value, fill=INK)
        arrow = delta_text(deltas.get(key))
        if arrow:
            ax = x + draw.textlength(value, font=fonts.strip_value) + 8
            draw.text((ax, top + 44), arrow, font=fonts.delta, fill=FAINT)
        if definition:
            draw.text((x, top + 72), fit_text(draw, definition, fonts.strip_def, width - 8),
                      font=fonts.strip_def, fill=FAINT)
        x += width
        if key in STRIP_CHAIN[:-1]:
            draw.text((x, top + 30), '›', font=fonts.chevron, fill=FAINT)
            x += STRIP_ARROW_W


def line_colour(key, value):
    """A row figure's colour (ruling S3): green or amber per LINE_TONES when not 0, plain
    ink at 0 and for every untoned figure."""
    tone = LINE_TONES.get(key)
    return LINE_COLOURS[tone] if tone and value else INK


def _lines(draw, fonts, section, deltas, top):
    """SNAP-1c: the Design row, a rule, then Procurement on two lines (breakdown_lines),
    in a white box like the tender page's. SNAP-3b: a second rule, then Finance."""
    draw.rounded_rectangle([MARGIN, top, IMAGE_WIDTH - MARGIN, top + LINES_H], radius=12,
                           fill='white', outline=LINE, width=2)
    # Every figure's value by key, for line_colour(). FINANCE_KEYS and BREAKDOWN_KEYS
    # share no key (tests_snapshot_finance pins it), so neither row hides the other's.
    values = {**section['breakdown'], **section['finance']}
    rules = 0
    for n, (label, runs) in enumerate(section_lines(section, deltas)):
        # A labelled row after the first starts behind a rule; Procurement's unlabelled
        # second line does not.
        if label and n:
            rule = top + LINES_PAD + n * LINE_H + rules * LINE_RULE + LINE_RULE // 2
            draw.line([MARGIN + 16, rule, IMAGE_WIDTH - MARGIN - 16, rule], fill=LINE,
                      width=1)
            rules += 1
        y = top + LINES_PAD + n * LINE_H + rules * LINE_RULE + 6
        if label:
            draw.text((MARGIN + 20, y), label, font=fonts.line_label, fill=INK)
        x = MARGIN + 20 + LINE_LABEL_W
        for text, kind, key in runs:
            if kind == 'num':
                font, fill, dy = fonts.line_num, line_colour(key, values[key]), 0
            elif kind == 'arrow':
                font, fill, dy = fonts.delta, FAINT, 4
            else:
                font, fill, dy = fonts.line_text, MUTED, 0
            draw.text((x, y + dy), text, font=font, fill=fill)
            x += draw.textlength(text, font=font)


def _tile(draw, fonts, tile, delta, x, y, width):
    """One dashboard tile: label, value (toned when not zero), arrow, sub-line."""
    draw.rounded_rectangle([x, y, x + width, y + TILE_H], radius=10, fill='white',
                           outline=LINE, width=2)
    inner = width - 32
    draw.text((x + 16, y + 12), fit_text(draw, tile['label'], fonts.tile_label, inner),
              font=fonts.tile_label, fill=MUTED)
    tone = SECTION_TONES.get(tile['key'])
    colour = TONE_COLOURS[tone] if tone and tile['value'] else INK
    value = _number(tile['value'])
    draw.text((x + 16, y + 36), value, font=fonts.tile_value, fill=colour)
    arrow = delta_text(delta)
    if arrow:
        ax = x + 16 + draw.textlength(value, font=fonts.tile_value) + 10
        draw.text((ax, y + 52), arrow, font=fonts.delta, fill=FAINT)
    if tile['detail']:
        draw.text((x + 16, y + 90), fit_text(draw, tile['detail'], fonts.tile_detail, inner),
                  font=fonts.tile_detail, fill=FAINT)


def _section(draw, fonts, section, deltas, top):
    """One tender: heading, strip, Design / Procurement / Finance rows, 4 + 3 tiles.
    Returns the y after (top + SECTION_H)."""
    _heading(draw, fonts, section, top)
    _strip(draw, fonts, section, deltas, top + STRIP_TOP)
    _lines(draw, fonts, section, deltas, top + LINES_TOP)
    tiles = {tile['key']: tile for tile in section['tiles']}
    for row_no, keys in enumerate(SECTION_TILE_ROWS):
        y = top + TILES_TOP + row_no * (TILE_H + TILE_GAP)
        width = (IMAGE_WIDTH - 2 * MARGIN - TILE_GAP * (len(keys) - 1)) / len(keys)
        for n, key in enumerate(keys):
            x = MARGIN + n * (width + TILE_GAP)
            _tile(draw, fonts, tiles[key], deltas.get(key), round(x), y, round(width))
    return top + SECTION_H


def _png(height, draw_fn):
    from PIL import Image, ImageDraw   # Lazy import: see _fonts().
    image = Image.new('RGB', (IMAGE_WIDTH, height), 'white')
    draw_fn(ImageDraw.Draw(image))
    out = io.BytesIO()
    image.save(out, format='PNG', optimize=True)
    return out.getvalue()


FOOTER_TEXT = "Horizon Solar PMS · ▲▼ = change since yesterday's snapshot"


def image_title(n, total):
    return 'All tenders' if total == 1 else f'All tenders ({n}/{total})'


def image_height(section_count):
    return PAGE_HEAD_H + section_count * SECTION_H + FOOTER_H


def section_pages(payload):
    """The sections in drawing order, in pieces of MAX_SECTIONS_PER_IMAGE (R6)."""
    sections = ordered_sections(payload)
    return [sections[i:i + MAX_SECTIONS_PER_IMAGE]
            for i in range(0, len(sections), MAX_SECTIONS_PER_IMAGE)]


def render_snapshot_images(payload, previous_payload=None):
    """The PNG images for `payload`, as a list of bytes, in sending order: up to
    MAX_SECTIONS_PER_IMAGE tender sections each, never splitting one, with ▲/▼ against
    the same tender in `previous_payload`. No qualifying tender: [] (nothing to send).

    Pure: reads nothing but its arguments and the bundled font.
    """
    pages = section_pages(payload)
    if not pages:
        return []
    fonts = _fonts()
    images = []
    for n, chunk in enumerate(pages, start=1):
        height = image_height(len(chunk))

        def draw_fn(draw, chunk=chunk, n=n, height=height):
            y = _page_header(draw, fonts, payload, image_title(n, len(pages)))
            for section in chunk:
                y = _section(draw, fonts, section, section_deltas(section, previous_payload),
                             y)
            draw.line([MARGIN, height - FOOTER_H + 8, IMAGE_WIDTH - MARGIN,
                       height - FOOTER_H + 8], fill=LINE)
            draw.text((MARGIN, height - 34), FOOTER_TEXT, font=fonts.tile_detail, fill=FAINT)
        images.append(_png(height, draw_fn))
    return images
