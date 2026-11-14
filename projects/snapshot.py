"""
SNAP-1 / SNAP-1b — the daily Site Dashboard snapshot: the figures, and the PNG images that
carry them.

    payload = collect_snapshot(now)                         # reads the DASH-B1 layer only
    pngs = render_snapshot_images(payload, previous)        # pure: no database access

WHERE EVERY FIGURE COMES FROM. Nothing here works a figure out for itself:
    site_dashboard.build_site_cards()      every card, as the CEO's dashboard builds them
    views_site_dashboard.kpi_tiles()       the 8 header tiles and their sub-lines, the
                                           very function the page calls (_kpis)
    site_dashboard.capacity_totals()       the DC kWp number behind the tile's text, for
                                           the day-on-day difference only
    site_breakdown.site_breakdown()        TB-1's in execution / Mobilized / Active /
                                           Active today, the tender page's own strip
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
A Residential site is never counted in a tender section, even on a Program (Q1;
Project.save() refuses one, so the rule is defensive, as _tender_key()'s).

WHAT IS STORED BUT NOT DRAWN. The SNAP-1 organisation row and the By PM / By tender rows
stay in the payload (and their tests), so the stored history keeps its shape; only
`tender_sections` is drawn.

WHOSE VIEW (SNAP-1 Q2). build_site_cards() needs a real user; the command has none. The
lowest-pk active CEO is used: every CEO sees the same sites (DASH-B1 D1). Not the Design
Head, who sees every site too but no money row in the activity feeds, which can make a
site look quiet that the CEO sees updated.

NO MONEY (locked rule). kpi_tiles() is called with sees_payments=False, so the payment
tile is never built, and no card's payment_open is read. The payload's keys are pinned
by tests_dashboard_snapshot.

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
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from django.utils import timezone

from .models import Project, UserProfile
from .site_breakdown import site_breakdown
from .site_dashboard import build_site_cards, capacity_totals
from .views_site_dashboard import dates_as_of, kpi_tiles

#: The image's fixed width (WhatsApp-friendly; SNAP-1 rendering rules).
IMAGE_WIDTH = 1080

#: R6: more tender sections than this continue on another image ("All tenders (2/2)").
MAX_SECTIONS_PER_IMAGE = 5

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
#: The labels were confirmed by Zuber, 14 Nov.
STRIP = (
    ('in_execution', 'Sites in execution', None),
    ('mobilized', 'Mobilized', 'DC + design started'),
    ('active', 'Active', 'Civil In Progress/Done'),
    ('active_today', 'Active today', 'any PMS update, last 24 h'),
)
STRIP_KEYS = tuple(key for key, _, _ in STRIP)

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


def _tender_sections(live, owners, now):
    """SNAP-1b: one section per qualifying tender, keyed by the Program id as TEXT (a
    JSONField turns int keys into strings anyway, so the stored and the fresh payload
    match on the same key; Zuber, 14 Nov).

    {'<program id>': {'id', 'code', 'name', 'pms', 'tiles', 'dc_kwp_number', 'breakdown'}}
    """
    sections = {}
    for program_id, cards in section_cards(live).items():
        figures = {f['key']: f for f in _figures(cards)}
        # TB-1's helper over exactly these sites: the same call the tender page makes
        # over program.sites.all() (Q2), one query per tender.
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
            'breakdown': {key: getattr(breakdown, key) for key in STRIP_KEYS},
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
    return {
        'snapshot_date': today.isoformat(),
        'snapshot_date_label': date_label(today),
        'taken_at': now.isoformat(),
        'taken_at_label': taken_at_label(now),
        'dates_as_of': dates_as_of(now),
        'viewer': viewer.username,
        'org': {'tiles': _figures(live), 'dc_kwp_number': _dc_number(live)},
        'pms': _pm_rows(live, owners),
        'tenders': _tender_rows(live),
        'tender_sections': _tender_sections(live, owners, now),
    }


def section_value(section, key):
    """A section's figure by key: a tile's value, or a strip value."""
    if key in STRIP_KEYS:
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
    """{tile or strip key: difference vs the same tender (by Program id) in
    `previous_payload`, or None}. No previous payload, a SNAP-1 payload without
    sections, or a tender that had no section then: no arrows at all."""
    sections = (previous_payload or {}).get('tender_sections') or {}
    before = sections.get(str(section['id']))
    return {key: _difference(_section_number(section, key),
                             _section_number(before, key) if before else None)
            for key in SECTION_KEYS + STRIP_KEYS}


def delta_text(delta):
    """'▲ 3' / '▼ 12', or '' for no arrow."""
    if not delta:
        return ''
    return f'{"▲" if delta > 0 else "▼"} {abs(delta):,}'


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
TILES_TOP = STRIP_TOP + STRIP_H + 16
TILE_H = 120
TILE_GAP = 12
SECTION_H = TILES_TOP + 2 * TILE_H + TILE_GAP + 36
FOOTER_H = 56

# The strip's columns: three chained figures with '›' between them, then a divider and
# Active today, set apart (TB-1 B5: it is not part of the chain).
STRIP_COL_W = 210
STRIP_ARROW_W = 34


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
                  tile_label=b(16), tile_value=b(34), tile_detail=r(14), delta=r(15))


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
    """TB-1's breakdown strip: in execution › Mobilized › Active, then Active today."""
    draw.rounded_rectangle([MARGIN, top, IMAGE_WIDTH - MARGIN, top + STRIP_H], radius=12,
                           fill=PALE_GREEN, outline=PALE_GREEN_LINE, width=2)
    x = MARGIN + 20
    for key, label, definition in STRIP:
        if key == 'active_today':
            divider = x + 8
            draw.line([divider, top + 16, divider, top + STRIP_H - 16],
                      fill=PALE_GREEN_LINE, width=2)
            x = divider + 28
        draw.text((x, top + 12), label, font=fonts.strip_label, fill=MUTED)
        value = _number(section['breakdown'][key])
        draw.text((x, top + 32), value, font=fonts.strip_value, fill=INK)
        arrow = delta_text(deltas.get(key))
        if arrow:
            ax = x + draw.textlength(value, font=fonts.strip_value) + 8
            draw.text((ax, top + 44), arrow, font=fonts.delta, fill=FAINT)
        if definition:
            draw.text((x, top + 72), fit_text(draw, definition, fonts.strip_def,
                                              STRIP_COL_W - 8),
                      font=fonts.strip_def, fill=FAINT)
        x += STRIP_COL_W
        if key in ('in_execution', 'mobilized'):
            draw.text((x, top + 30), '›', font=fonts.chevron, fill=FAINT)
            x += STRIP_ARROW_W


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
    """One tender: heading, strip, 4 + 3 tiles. Returns the y after (top + SECTION_H)."""
    _heading(draw, fonts, section, top)
    _strip(draw, fonts, section, deltas, top + STRIP_TOP)
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
