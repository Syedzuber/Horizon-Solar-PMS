"""
SNAP-1 — the daily Site Dashboard snapshot: the figures, and the PNG images that carry them.

    payload = collect_snapshot(now)                         # reads the DASH-B1 layer only
    pngs = render_snapshot_images(payload, previous)        # pure: no database access

WHERE EVERY FIGURE COMES FROM. Nothing here works a figure out for itself:
    site_dashboard.build_site_cards()      every card, as the CEO's dashboard builds them
    views_site_dashboard.kpi_tiles()       the 8 header tiles and their sub-lines, the
                                           very function the page calls (_kpis)
    site_dashboard.capacity_totals()       the DC kWp number behind the tile's text, for
                                           the day-on-day difference only
    views_site_dashboard.dates_as_of()     the page's "Dates as of" line
The organisation row is kpi_tiles() over the CEO's non-draft cards: exactly the page's
header with no filter. A PM row and a tender row are kpi_tiles() over a SUBSET of those
same cards, so a row's figures are what the page shows for those sites. Each card's
figures do not depend on who views it (the payment figure aside, which is never read),
so cutting the CEO's cards gives what a PM sees for the same sites; the tests pin both.

WHOSE VIEW (SNAP-1 Q2). build_site_cards() needs a real user; the command has none. The
lowest-pk active CEO is used: every CEO sees the same sites (DASH-B1 D1). Not the Design
Head, who sees every site too but no money row in the activity feeds, which can make a
site look quiet that the CEO sees updated.

NO MONEY (locked rule). kpi_tiles() is called with sees_payments=False, so the payment
tile is never built, and no card's payment_open is read. The payload's keys are pinned
by tests_dashboard_snapshot.

ROWS.
    By PM      one row per ACTIVE PM (role PM, both active flags) who is the assigned PM
               of at least one site in execution. Coordinators are not rows (they would
               count their sites twice). Sites whose assigned PM is not an active PM go
               to one "No active PM" row, drawn only when there is one (Q3).
    By tender  Residential sites -> "Residential", even with a tender; else a site with a
               tender -> that tender's row; CAPEX without one -> "CAPEX (standalone)";
               RESCO (OPEX) without one -> "RESCO (no tender)" (Q4). Every site is in
               exactly one row.
"""
import io
from dataclasses import dataclass
from pathlib import Path

from django.utils import timezone

from .models import Project, UserProfile
from .site_dashboard import build_site_cards, capacity_totals
from .views_site_dashboard import dates_as_of, kpi_tiles

#: The image's fixed width (WhatsApp-friendly; SNAP-1 rendering rules).
IMAGE_WIDTH = 1080

#: A table longer than this continues on another image ("By PM (2/2)").
MAX_TABLE_ROWS = 18

#: The 8 figures, in the dashboard's order: (payload key, the page's tile label, the
#: table's two-line column heading). collect_snapshot() checks the labels against what
#: kpi_tiles() returns, so a renamed or reordered tile stops the run instead of
#: printing a figure under the wrong heading.
FIGURES = (
    ('sites', 'Sites in execution', ('Sites', '')),
    ('dc_kwp', 'DC kWp in execution', ('DC', 'kWp')),
    ('behind', 'Behind plan', ('Behind', 'plan')),
    ('civil', 'Needs Civil date', ('Needs', 'Civil')),
    ('overdue', 'Overdue tasks', ('Overdue', 'tasks')),
    ('blocked', 'Blocked tasks', ('Blocked', 'tasks')),
    ('issues', 'Open issues', ('Open', 'issues')),
    ('quiet', 'Gone quiet', ('Gone', 'quiet')),
)
FIGURE_KEYS = tuple(key for key, _, _ in FIGURES)

#: The dashboard's tones for the six filter tiles (views_site_dashboard._kpis); a zero
#: draws plain there, and here.
TONES = {'behind': 'crit', 'civil': 'warn', 'overdue': 'crit', 'blocked': 'crit',
         'issues': 'warn', 'quiet': 'warn'}

NO_ACTIVE_PM = 'No active PM'
RESIDENTIAL_ROW = 'Residential'
CAPEX_ROW = 'CAPEX (standalone)'
RESCO_ROW = 'RESCO (no tender)'

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
    """One table row: who or what it is, and the 8 figures as the page prints them."""
    return {'id': row_id, 'name': name,
            'values': {f['key']: f['value'] for f in _figures(cards)}}


def _dc_number(cards):
    """The DC kWp tile's number, for the day-on-day difference: capacity_totals(), the
    sum the tile prints, rounded as the tile rounds it (views_site_dashboard._whole)."""
    total = capacity_totals(cards).dc_kwp
    return round(total) if total is not None else 0


def _person(user):
    return (user.get_full_name() or user.username).strip()


def _pm_rows(live):
    """By PM (see the module docstring). Sorted by Overdue tasks, then Gone quiet, both
    descending, then name; the No active PM row last."""
    # Each live site's assigned PM, with what decides whether they get a row: role PM
    # and both active flags (UserProfile.is_active is this app's deactivation, User's
    # Django's). One query for every site; no figure is read here, only who owns a site.
    owners = {}
    for pk, profile_pk, role, p_active, u_active, user_id, first, last, username in (
            Project.objects.filter(pk__in=[c.id for c in live])
            .values_list('pk', 'assigned_pm_id', 'assigned_pm__role',
                         'assigned_pm__is_active', 'assigned_pm__user__is_active',
                         'assigned_pm__user_id', 'assigned_pm__user__first_name',
                         'assigned_pm__user__last_name', 'assigned_pm__user__username')):
        if profile_pk is not None and role == 'PM' and p_active and u_active:
            name = f'{first} {last}'.strip() or username
            owners[pk] = (user_id, name)
        else:
            owners[pk] = None

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
    """By tender. Tenders sorted by Overdue tasks, then Gone quiet, both descending (as
    the PM table), then name; Residential, CAPEX (standalone) and RESCO (no tender)
    after them in that order, each only when it has a site."""
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
     'pms': [{'id', 'name', 'values'}], 'tenders': [{'id', 'name', 'values'}]}
    """
    now = now or timezone.now()
    today = timezone.localtime(now).date()
    viewer = snapshot_viewer()
    # The dashboard's own build for that CEO, on the same IST day; drafts are not in
    # execution, so they count in no tile (views_site_dashboard.site_dashboard's `live`).
    live = [c for c in build_site_cards(viewer, today=today) if not c.is_draft]
    return {
        'snapshot_date': today.isoformat(),
        'snapshot_date_label': date_label(today),
        'taken_at': now.isoformat(),
        'taken_at_label': taken_at_label(now),
        'dates_as_of': dates_as_of(now),
        'viewer': viewer.username,
        'org': {'tiles': _figures(live), 'dc_kwp_number': _dc_number(live)},
        'pms': _pm_rows(live),
        'tenders': _tender_rows(live),
    }


def headline(payload):
    """'142 sites in execution · 77 overdue tasks · 110 gone quiet', for the email and
    the in-app line."""
    values = {t['key']: t['value'] for t in payload['org']['tiles']}
    return (f'{values["sites"]} sites in execution · {values["overdue"]} overdue tasks · '
            f'{values["quiet"]} gone quiet')


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


def org_deltas(payload, previous):
    """{figure key: difference vs `previous`, or None}. None when there is no previous
    payload, the figure is missing from it, or the difference is 0 (no arrow drawn)."""
    deltas = {}
    for key in FIGURE_KEYS:
        now_value = _org_number(payload, key)
        before = _org_number(previous, key) if previous else None
        if not isinstance(now_value, int) or not isinstance(before, int):
            deltas[key] = None
            continue
        deltas[key] = (now_value - before) or None
    return deltas


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

# The Site Dashboard's palette (site_dashboard.html's --sd-* variables).
BRAND = '#1a7a4a'
GOLD = '#f0a829'
NAVY = '#243a6b'
INK = '#16231b'
MUTED = '#56655c'
FAINT = '#7a887f'
LINE = '#d9e0d7'
SURFACE_2 = '#edf1eb'
TONE_COLOURS = {'crit': '#c23b2a', 'warn': '#a86400'}

MARGIN = 32
NAME_COL = 248
NUM_COL = (IMAGE_WIDTH - 2 * MARGIN - NAME_COL) // len(FIGURES)
ROW_H = 40
HEAD_H = 52


@dataclass(frozen=True)
class _Fonts:
    title: object
    sub: object
    tile_value: object
    tile_label: object
    tile_detail: object
    head: object
    cell: object
    cell_bold: object


def _fonts():
    from PIL import ImageFont   # Lazy import: Pillow is needed only when rendering.
    regular = str(FONT_DIR / 'DejaVuSans.ttf')
    bold = str(FONT_DIR / 'DejaVuSans-Bold.ttf')
    return _Fonts(title=ImageFont.truetype(bold, 30), sub=ImageFont.truetype(regular, 18),
                  tile_value=ImageFont.truetype(bold, 34),
                  tile_label=ImageFont.truetype(bold, 16),
                  tile_detail=ImageFont.truetype(regular, 14),
                  head=ImageFont.truetype(bold, 15), cell=ImageFont.truetype(regular, 18),
                  cell_bold=ImageFont.truetype(bold, 18))


def fit_text(draw, text, font, max_width):
    """`text` cut with '…' to fit `max_width` pixels; never wrapped."""
    text = str(text)
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + '…', font=font) > max_width:
        text = text[:-1]
    return text.rstrip() + '…'


def _header(draw, fonts, title, lines, width):
    """The green band with the title, and the grey lines under it. Returns the y after."""
    draw.rectangle([0, 0, width, 64], fill=BRAND)
    draw.rectangle([0, 64, width, 68], fill=GOLD)
    draw.text((MARGIN, 16), fit_text(draw, title, fonts.title, width - 2 * MARGIN),
              font=fonts.title, fill='white')
    y = 84
    for text, colour in lines:
        draw.text((MARGIN, y), fit_text(draw, text, fonts.sub, width - 2 * MARGIN),
                  font=fonts.sub, fill=colour)
        y += 28
    return y + 8


TILE_COLS = 4
TILE_H = 116


def _org_tiles(draw, fonts, payload, deltas, top):
    """The 8 organisation tiles, 4 x 2, as the page draws them. Returns the y after."""
    tile_w = (IMAGE_WIDTH - 2 * MARGIN) // TILE_COLS
    for n, tile in enumerate(payload['org']['tiles']):
        col, row = n % TILE_COLS, n // TILE_COLS
        x, y = MARGIN + col * tile_w, top + row * TILE_H
        draw.rectangle([x, y, x + tile_w, y + TILE_H], outline=LINE, fill='white')
        inner = tile_w - 28
        draw.text((x + 14, y + 10), fit_text(draw, tile['label'], fonts.tile_label, inner),
                  font=fonts.tile_label, fill=MUTED)
        # As the page: the tone only when the figure is not zero.
        colour = TONE_COLOURS.get(tile['tone'], INK) if tile['value'] else INK
        value = f'{tile["value"]:,}' if isinstance(tile['value'], int) else str(tile['value'])
        draw.text((x + 14, y + 34), value, font=fonts.tile_value, fill=colour)
        arrow = delta_text(deltas.get(tile['key']))
        if arrow:
            vx = x + 14 + draw.textlength(value, font=fonts.tile_value) + 10
            draw.text((vx, y + 48), arrow, font=fonts.tile_detail, fill=FAINT)
        if tile['detail']:
            draw.text((x + 14, y + 84),
                      fit_text(draw, tile['detail'], fonts.tile_detail, inner),
                      font=fonts.tile_detail, fill=FAINT)
    rows = -(-len(payload['org']['tiles']) // TILE_COLS)
    return top + rows * TILE_H + 24


def _table(draw, fonts, caption, rows, top):
    """A caption, the two-line column headings and up to MAX_TABLE_ROWS rows."""
    draw.text((MARGIN, top), caption, font=fonts.title, fill=NAVY)
    y = top + 46
    draw.rectangle([MARGIN, y, IMAGE_WIDTH - MARGIN, y + HEAD_H], fill=SURFACE_2)
    x = MARGIN + NAME_COL
    for _, _, (first, second) in FIGURES:
        for line_no, word in enumerate((first, second)):
            if word:
                w = draw.textlength(word, font=fonts.head)
                draw.text((x + NUM_COL - 10 - w, y + 8 + line_no * 18), word,
                          font=fonts.head, fill=MUTED)
        x += NUM_COL
    y += HEAD_H
    for n, row in enumerate(rows):
        if n % 2:
            draw.rectangle([MARGIN, y, IMAGE_WIDTH - MARGIN, y + ROW_H], fill='#f7f9f6')
        draw.text((MARGIN + 10, y + 9),
                  fit_text(draw, row['name'], fonts.cell_bold, NAME_COL - 20),
                  font=fonts.cell_bold, fill=INK)
        x = MARGIN + NAME_COL
        for key in FIGURE_KEYS:
            value = row['values'][key]
            text = f'{value:,}' if isinstance(value, int) else str(value)
            colour = TONE_COLOURS.get(TONES.get(key), INK) if value else INK
            text = fit_text(draw, text, fonts.cell, NUM_COL - 14)
            w = draw.textlength(text, font=fonts.cell)
            draw.text((x + NUM_COL - 10 - w, y + 9), text, font=fonts.cell, fill=colour)
            x += NUM_COL
        draw.line([MARGIN, y + ROW_H, IMAGE_WIDTH - MARGIN, y + ROW_H], fill=LINE)
        y += ROW_H
    if not rows:
        draw.text((MARGIN + 10, y + 9), 'No sites in execution', font=fonts.cell, fill=FAINT)
        y += ROW_H
    return y + 24


def _chunks(rows):
    """`rows` in pieces of MAX_TABLE_ROWS; one empty piece for no rows."""
    return [rows[i:i + MAX_TABLE_ROWS] for i in range(0, len(rows), MAX_TABLE_ROWS)] or [[]]


def _caption(title, n, total):
    return title if total == 1 else f'{title} ({n}/{total})'


def _png(height, draw_fn):
    from PIL import Image, ImageDraw   # Lazy import: see _fonts().
    image = Image.new('RGB', (IMAGE_WIDTH, height), 'white')
    draw_fn(ImageDraw.Draw(image))
    out = io.BytesIO()
    image.save(out, format='PNG', optimize=True)
    return out.getvalue()


def _footer(draw, fonts, height, text):
    draw.text((MARGIN, height - 34), text, font=fonts.tile_detail, fill=FAINT)


def render_snapshot_images(payload, previous_payload=None):
    """The PNG images for `payload`, as a list of bytes, in sending order:

        1   header, organisation tiles (with ▲/▼ vs `previous_payload`), By PM
        2.. By PM continued, when it has more than MAX_TABLE_ROWS rows
        then By tender, split the same way

    Pure: reads nothing but its arguments and the bundled font.
    """
    fonts = _fonts()
    deltas = org_deltas(payload, previous_payload)
    stamp = payload['dates_as_of']
    lines = [(f'Figures as at {payload["taken_at_label"]} · all live sites, '
              f'test sites excluded', MUTED),
             (stamp['text'], TONE_COLOURS['warn'] if stamp['stale'] else FAINT)]
    footer = ('Horizon Solar PMS · the same figures as the Site Dashboard · ▲▼ change '
              'since the previous snapshot')
    pm_pages, tender_pages = _chunks(payload['pms']), _chunks(payload['tenders'])

    def table_height(rows):
        return 46 + HEAD_H + max(len(rows), 1) * ROW_H + 24

    images = []
    first_top = 84 + 28 * len(lines) + 8
    tiles_h = -(-len(payload['org']['tiles']) // TILE_COLS) * TILE_H + 24
    height = first_top + tiles_h + table_height(pm_pages[0]) + 48

    def first(draw):
        y = _header(draw, fonts, 'Site Dashboard · Daily snapshot · '
                    f'{payload["snapshot_date_label"]}', lines, IMAGE_WIDTH)
        y = _org_tiles(draw, fonts, payload, deltas, y)
        _table(draw, fonts, _caption('By PM', 1, len(pm_pages)), pm_pages[0], y)
        _footer(draw, fonts, height, footer)
    images.append(_png(height, first))

    def page(title, n, total, rows):
        short = [(f'Figures as at {payload["taken_at_label"]}', MUTED)]
        h = 84 + 28 + 8 + table_height(rows) + 48

        def draw_fn(draw):
            y = _header(draw, fonts, f'Site Dashboard · {payload["snapshot_date_label"]}',
                        short, IMAGE_WIDTH)
            _table(draw, fonts, _caption(title, n, total), rows, y)
            _footer(draw, fonts, h, footer)
        return _png(h, draw_fn)

    for n, rows in enumerate(pm_pages[1:], start=2):
        images.append(page('By PM', n, len(pm_pages), rows))
    for n, rows in enumerate(tender_pages, start=1):
        images.append(page('By tender', n, len(tender_pages), rows))
    return images
