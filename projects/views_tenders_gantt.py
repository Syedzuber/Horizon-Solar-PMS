"""
DASH-B3 — the Tenders Gantt page: every RESCO site of every tender on one timeline.

Linked from the CEO Tenders view and the Site Dashboard header. Data and geometry come
from tenders_gantt.py (which builds on the Site Dashboard's data layer); this module reads
the GET parameters, filters and groups the built sites in Python, and turns each into a
plain row for the template.

STATE LIVES IN THE URL (G10): q, tender, sort, zoom, plan and group are GET parameters;
every control is a link or a GET form. Alpine holds only which rows and groups are open;
one inline script scrolls the chart to today. No fetch, no XHR, no JSON payload: every bar
is drawn server-side as an inline left/width percentage (G2).

NO MONEY (G12, R4). The site cards behind this page carry the payment figure for money
roles; Row and DraftRow below are built field by field and hold none of it.
"""
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode

from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache

from .decorators import _forbidden, login_required
from .permissions import user_can_view_site_dashboard
from .scheduling import REASON_NEEDS_ANCHOR
from .site_dashboard import ORDER_LATEST, ORDER_URGENT, ORDERS, sort_cards
from .tenders_gantt import (DAY_PX, ZOOM_MONTH, ZOOM_WEEK, ZOOMS, build_gantt, draw,
                            month_labels, place, short_date, tender_cards, today_fraction,
                            track_px, week_labels)
from .views_site_dashboard import (QUIET_DAYS, dates_as_of, relative_time, schedule_line)

#: Longest search string kept; anything longer is cut, never refused.
SEARCH_MAX = 100

#: Latest activity younger than this reads green (G11); QUIET_DAYS and older, amber.
FRESH = timedelta(hours=6)

SORT_LABELS = {ORDER_LATEST: 'Latest activity', ORDER_URGENT: 'Most urgent'}
ZOOM_LABELS = {ZOOM_MONTH: 'Month', ZOOM_WEEK: 'Week'}

#: The parameters' defaults. Only a non-default value goes into a link.
DEFAULTS = {'q': '', 'tender': None, 'sort': ORDER_LATEST, 'zoom': ZOOM_MONTH,
            'plan': '', 'group': ''}

@dataclass(frozen=True)
class Row:
    """One active site as the template draws it. Built field by field from the card:
    nothing money-related is copied (R4)."""
    number: int
    id: int
    project_id: str
    code: str
    name: str
    city: str
    tender: str
    pm: str
    dc: object
    ac: object
    large: bool
    schedule: object        # (text, tone) or None
    highlight: object       # the highlighted segment's label, or None
    latest: str
    latest_tone: str        # 'ok' (< 6 h), 'warn' (gone quiet) or ''
    lanes: tuple            # ((lane, drawn pieces), ...) for the six collapsed lanes
    detail: tuple           # the expanded list, same shape
    target: object          # Drawn diamond or None
    target_text: str


@dataclass(frozen=True)
class DraftRow:
    code: str
    name: str
    city: str
    tender: str
    pm: str
    dc: object
    ac: object
    created: object


def _kw(value):
    """A capacity as typed, without trailing zeros: Decimal('62.50') -> '62.5'."""
    return None if value is None else format(value.normalize(), ',f')


def _params(request, program_ids):
    """The page's GET parameters, validated. Anything unknown or malformed falls back to
    its default (G10), so no parameter can raise."""
    get = request.GET
    sort = get.get('sort', ORDER_LATEST)
    zoom = get.get('zoom', ZOOM_MONTH)
    try:
        tender = int(get.get('tender', ''))
    except ValueError:
        tender = None
    return {
        'q': get.get('q', '').strip()[:SEARCH_MAX],
        'tender': tender if tender in program_ids else None,
        'sort': sort if sort in ORDERS else ORDER_LATEST,
        'zoom': zoom if zoom in ZOOMS else ZOOM_MONTH,
        # "Show plan" and "Group sites needing a Civil date" are on unless turned off.
        'plan': 'off' if get.get('plan') == 'off' else '',
        'group': 'off' if get.get('group') == 'off' else '',
    }


def _query(params, **changes):
    """The query string with `changes` applied; defaults left out so links stay short."""
    merged = {**params, **changes}
    kept = {k: v for k, v in merged.items() if v not in ('', None) and v != DEFAULTS[k]}
    return '?' + urlencode(kept)


def _matches(card, q):
    """G10's search: site code, project ID, name, city and PM, case-insensitive."""
    if not q:
        return True
    haystack = ' '.join(str(v) for v in (card.code, card.project_id, card.site_name,
                                         card.city, card.pm_name or '')).lower()
    return q.lower() in haystack


def _latest(card, now):
    if card.latest is None:
        return 'No updates yet', 'warn' if card.quiet_days >= QUIET_DAYS else ''
    if card.quiet_days >= QUIET_DAYS:
        tone = 'warn'
    elif now - card.latest.at < FRESH:
        tone = 'ok'
    else:
        tone = ''
    return relative_time(card.latest.at, now), tone


def _row(number, site, timeline, show_plan, now):
    card = site.card
    latest, tone = _latest(card, now)

    def lanes(items):
        return tuple((lane, draw(lane.pieces, timeline, show_plan)) for lane in items)

    return Row(
        number=number, id=card.id, project_id=card.project_id, code=card.code,
        name=card.site_name, city=card.city, tender=card.program_code or '',
        pm=card.pm_name or 'No PM', dc=_kw(card.dc_kwp), ac=_kw(card.ac_kw),
        large=card.large_site,     # DC above site_dashboard.LARGE_SITE_KWP (300), G11
        schedule=schedule_line(card), highlight=site.highlight,
        latest=latest, latest_tone=tone,
        lanes=lanes(site.lanes), detail=lanes(site.detail),
        target=place(site.target, timeline) if site.target else None,
        target_text=f'Target {short_date(card.target_date)}' if card.target_date else '',
    )


def _draft_row(site):
    card = site.card
    return DraftRow(code=card.code, name=card.site_name, city=card.city,
                    tender=card.program_code or '', pm=card.pm_name or 'No PM',
                    dc=_kw(card.dc_kwp), ac=_kw(card.ac_kw), created=card.created_at)


def _sorted(sites, order):
    by_id = {s.card.id: s for s in sites}
    return [by_id[c.id] for c in sort_cards([s.card for s in sites], order)]


@login_required
@never_cache
def tenders_gantt(request):
    """
    The Tenders Gantt: every RESCO site of every tender on one timeline (DASH-B3), with a
    card per tender above it. Access: the Site Dashboard's audience, PM, Project
    Coordinator, CEO, Finance, SCM, Admin, System Admin and the Design Head
    (permissions.user_can_view_site_dashboard), each seeing the sites that dashboard shows
    them; everyone else gets the 403 page. GET only: it reads, never writes. never_cache,
    because task dates move daily and Back must not show yesterday's chart.
    """
    # Same gate as the Site Dashboard, because this page is a second view of the same
    # sites: a Site Engineer, a designer, BD or a profile-less account has no scope there,
    # so is refused here with the same 403 (role_required's _forbidden).
    if not user_can_view_site_dashboard(request.user):
        return _forbidden(request)

    # One IST day and one clock for every bar, "Late" and "3h ago" on the page.
    today = timezone.localdate()
    now = timezone.now()
    data = build_gantt(request.user, today=today)
    everything = data.sites + data.drafts
    params = _params(request, {s.card.program_id for s in everything})
    show_plan = params['plan'] != 'off'
    timeline = data.timeline

    # Search cuts every site; the tender cards are counted after it and before the tender
    # filter, so every card stays clickable and its counts equal the rows shown for it.
    searched = [s for s in everything if _matches(s.card, params['q'])]
    cards = tender_cards(searched)
    shown = [s for s in searched
             if params['tender'] is None or s.card.program_id == params['tender']]
    live = [s for s in shown if not s.card.is_draft]
    drafts = _sorted([s for s in shown if s.card.is_draft], params['sort'])

    if params['group'] == 'off':
        main, civil = live, []
    else:
        main = [s for s in live if s.card.schedule.reason != REASON_NEEDS_ANCHOR]
        civil = [s for s in live if s.card.schedule.reason == REASON_NEEDS_ANCHOR]
    main, civil = _sorted(main, params['sort']), _sorted(civil, params['sort'])
    rows = [_row(i, s, timeline, show_plan, now) for i, s in enumerate(main, start=1)]
    civil_rows = [_row(i, s, timeline, show_plan, now)
                  for i, s in enumerate(civil, start=len(rows) + 1)]
    draft_rows = [_draft_row(s) for s in drafts]
    # A collapsed group under an empty list reads as "you have no sites": chandan's 33
    # MPUVL sites all need a Civil date, so his page looked blank. Open the first group
    # that holds sites when everything above it is empty. Only the default open state
    # changes; group=off still ungroups (the Civil group does not exist then), and the
    # viewer can still collapse either group.
    civil_open = bool(civil_rows) and not rows
    drafts_open = bool(draft_rows) and not rows and not civil_rows

    def toggle(key):
        on = params[key] != 'off'
        return {'on': on, 'href': _query(params, **{key: '' if not on else 'off'})}

    return render(request, 'projects/tenders_gantt.html', {
        'rows': rows,
        'civil_rows': civil_rows,
        'drafts': draft_rows,
        'civil_open': civil_open,
        'drafts_open': drafts_open,
        'tender_cards': [{'card': c, 'active': params['tender'] == c.program_id,
                          # A second click on the selected card clears the filter (G9).
                          'href': _query(params, tender=None if params['tender'] == c.program_id
                                         else c.program_id)}
                         for c in cards],
        'params': params,
        'sort_links': [{'label': SORT_LABELS[o], 'active': params['sort'] == o,
                        'href': _query(params, sort=o)} for o in ORDERS],
        'zoom_links': [{'label': ZOOM_LABELS[z], 'active': params['zoom'] == z,
                        'href': _query(params, zoom=z)} for z in ZOOMS],
        'plan_toggle': toggle('plan'),
        'group_toggle': toggle('group'),
        'clear_href': _query(params, q='', tender=None),
        'filtered': bool(params['q'] or params['tender'] is not None),
        'months': month_labels(timeline),
        'weeks': week_labels(timeline) if params['zoom'] == ZOOM_WEEK else [],
        'track_px': track_px(timeline, params['zoom']),
        'day_px': DAY_PX[params['zoom']],
        'today': today,
        'today_frac': today_fraction(timeline, today),
        # The same position as a percentage, for G7's labels drawn at today.
        'today_pct': f'{float(today_fraction(timeline, today)) * 100:.3f}',
        'timeline': timeline,
        'dates_as_of': dates_as_of(now),
    })
