"""
SNAP-3c — trend flags and the "Needs action" box of each tender section in the daily
snapshot image. Zuber's rules R1-R13 (20 Nov 2026) and answers Q1-Q8 (20 Nov 2026).

    box = needs_action(section, payload['snapshot_date'], history)
    box.lines      the flags drawn in the box: one per role, at most MAX_ACTION_LINES
    box.more       how many more roles have a flag than the box shows ("+N more")
    box.flags      every flag, worst first, each with the reason the dry run prints
    box.dots       {figure key: colour}: the dot drawn beside each flagged figure

PURE. Everything here reads its arguments only: today's section (collect_snapshot's
shape) and the stored payloads before today, as {ISO date: payload}. No query, no clock,
no AI: the same numbers always give the same flags and the same sentences (fixed
templates), which the tests pin.

THE GENERAL RULE (for every figure with a trend)
    amber   worse than 3 days ago. "3 days ago" is the stored row of that date; if that
            row is missing, the oldest row within the last 3 days (d-2, then d-1).
    red     worse than 3 days ago AND worse on each of the last 3 days, read over the
            four rows d-3, d-2, d-1 and today (Q8). A missing day among them means amber
            at most. Or past the figure's hard limit (the table below).
    history a trend needs a stored row dated 3 or more days before today that carries
            the figure (rows from before SNAP-1c have no Design / Procurement figures,
            from before 3b no Finance, from before 3c no `checks`). Hard limits that
            read only today fire from the first night.

    #    figure                   worse        hard limit -> red                   role
    R1   Overdue tasks            up           > 1 per site in execution          PM
    R2   Behind plan              up           > 25% of scheduled sites           PM
    R3   Gone quiet               up           > 20% of sites in execution        PM
    R4   Needs Civil date         not falling  > 0, unchanged for 3 days          PM
    R5   Blocked tasks            up           > 0 on each of the 4 rows          PM
    R6   Open issues              up           > 0 and not below 3 days ago       PM
    R7   Active today             down         < 25% of sites in execution        PM
    R8   Attendance vs Active     -            a site with attendance today that  Site Engineers
                                               is not Active (Z1)
    R9   DCs overdue              up           any                                Site Engineers
    R10  Material pending among   -            amber when any (Z2 reading (b):    SCM
         Active sites                          Active minus received); red when
                                               it rose on each of the last 3 days
    R11  Design released          not rising   no new release in 3 days while     Design Head
                                               any design is in progress
    R12  Mobilized / Active       not rising   unchanged for 3 days; a figure     PM
                                               already equal to sites in
                                               execution is skipped (Q5); the two
                                               are one line
    R13  Finance counts (3b)      -            the FIRST zero of invoices ->      SCM, or
                                               requests -> approved -> paid,      Payment approver
                                               only when material is received at  / Finance
                                               a site; pending is never flagged
                                               (Q4)
    Sundays: no R7 or R8 (no Active-today or attendance flag). And R7's "3 days ago" is
    the newest non-Sunday row at or before d-3, never a Sunday row (SNAP-3c Q2).

THE BOX (Q1). Each role's worst flag (red before amber; then the table's order) is one
line; the lines are sorted the same way across roles, reds first. At most
MAX_ACTION_LINES lines; when more roles have a flag, the last line ends "+N more". No flag
at all: one green CLEAN_LINE. Every flag, shown or not, puts its dot on its figures.
"""
from dataclasses import dataclass
from datetime import date, timedelta

# --- Thresholds (Zuber, 20 Nov 2026): the one place they are written ---------------
#: The trend window: "3 days ago", "each of the last 3 days", "unchanged for 3 days".
TREND_DAYS = 3
#: R1: overdue tasks above this many per site in execution are red.
OVERDUE_PER_SITE = 1
#: R2: behind-plan sites above this share of the scheduled sites are red.
BEHIND_PERCENT = 25
#: R3: gone-quiet sites above this share of the sites in execution are red.
QUIET_PERCENT = 20
#: R7: sites active today below this share of the sites in execution are red.
ACTIVE_TODAY_PERCENT = 25
#: date.weekday() of Sunday: no Active-today or attendance flag that day (R7, R8).
SUNDAY = 6

RED, AMBER = 'red', 'amber'
_COLOUR_RANK = {RED: 0, AMBER: 1}

ROLE_PM = 'PM'
ROLE_SE = 'Site Engineers'
ROLE_SCM = 'SCM'
ROLE_DESIGN = 'Design Head'
ROLE_PAYMENT = 'Payment approver / Finance'
#: Q1: one box line per role. R13's approved and paid flags share the last role.
ROLES = (ROLE_PM, ROLE_SE, ROLE_SCM, ROLE_DESIGN, ROLE_PAYMENT)

#: The table's order: the tie-break within a colour, inside a role and across roles.
RULES = ('R1', 'R2', 'R3', 'R4', 'R5', 'R6', 'R7', 'R8', 'R9', 'R10', 'R11', 'R12', 'R13')

#: Q1: up to four lines; the fourth ends "+N more" when more roles have a flag.
MAX_ACTION_LINES = 4

CLEAN_LINE = 'No red flags or attention items today.'

#: SNAP-3c's stored per-section figures (section['checks']) that no tile or row draws:
#:   scheduled              R2's base, the Behind plan tile's "of N scheduled" as a number
#:   attendance_not_active  R8: sites with attendance today whose Civil task has not
#:                          started (attendance_today_ids minus active_ids, Z1)
#:   active_no_material     R10: Active sites with no material received (active_ids
#:                          minus received_ids, Z2 reading (b))
CHECK_KEYS = ('scheduled', 'attendance_not_active', 'active_no_material')

#: R13's funnel, in order: (finance key, the words after the 0, role, action).
FUNNEL = (
    ('invoices', 'invoices', ROLE_SCM, 'SCM to enter'),
    ('requests', 'payment requests', ROLE_SCM, 'SCM to enter'),
    ('approved', 'approvals', ROLE_PAYMENT, 'Payment approver to approve'),
    ('paid', 'payments', ROLE_PAYMENT, 'Finance to record'),
)


@dataclass(frozen=True)
class Flag:
    """One flagged figure: its rule, colour and role, the box sentence in two parts (what
    is wrong; who acts), the figure keys that get a dot, and the dry run's reason."""
    rule: str
    colour: str
    role: str
    text: str
    action: str
    figures: tuple
    reason: str

    @property
    def sentence(self):
        return f'{self.text} — {self.action}.'

    @property
    def rank(self):
        return (_COLOUR_RANK[self.colour], RULES.index(self.rule))


@dataclass(frozen=True)
class ActionBox:
    flags: tuple    # every flag, worst first
    lines: tuple    # the flags drawn, one per role, at most MAX_ACTION_LINES
    more: int       # roles with a flag that the box has no line for
    dots: dict      # {figure key: RED or AMBER}, the worst flag on that figure

    def line_texts(self):
        """The box as plain text, one string per drawn line (the dry run's form)."""
        if not self.lines:
            return [CLEAN_LINE]
        texts = [flag.sentence for flag in self.lines]
        if self.more:
            texts[-1] += f'  {more_text(self.more)}'
        return texts


def more_text(n):
    return f'+{n} more'


def figure(section, key):
    """A section's figure by key, from its breakdown, finance, checks or tiles; None when
    the section (or a payload too old to have that figure) lacks it. The four hold no
    key in common (tests_snapshot_flags pins it), so the order looked in is moot."""
    if not section:
        return None
    for block in ('breakdown', 'finance', 'checks'):
        values = section.get(block) or {}
        if key in values:
            return values[key]
    for tile in section.get('tiles', ()):
        if tile['key'] == key:
            return tile['value']
    return None


def _n(value):
    return f'{value:,}'


def _plural(n, singular, plural=None):
    return f'{_n(n)} {singular if n == 1 else (plural or singular + "s")}'


def _up(before, after):
    return after > before


def _down(before, after):
    return after < before


def _not_falling(before, after):
    return after >= before


def _not_rising(before, after):
    return after <= before


class _Series:
    """One figure of one tender over today and the stored days before it."""

    def __init__(self, key, section, today, past, skip_sundays=False):
        self.key = key
        self.now = figure(section, key)
        self.skip_sundays = skip_sundays
        # {k: the figure k days before today, or None when that row is missing or lacks
        # the figure (or the tender had no section that day)}.
        self._by_date = {day: figure(sec, key) for day, sec in past}
        self.back = {k: self._by_date.get(today - timedelta(days=k))
                     for k in range(1, TREND_DAYS + 1)}
        # The history condition: some stored row 3 or more days old carries the figure.
        self._cutoff = today - timedelta(days=TREND_DAYS)
        self.has_history = any(day <= self._cutoff and value is not None
                               for day, value in self._by_date.items())

    def ref(self):
        """The "3 days ago" value: that day's row, else the oldest within the last 3
        days; None when there is no trend to read (no history, or no row in the window).

        With skip_sundays (R7, SNAP-3c Q2): the newest NON-Sunday row at or before d-3,
        and no fallback into the last 3 days. Sites barely update on a Sunday, so a
        Sunday row as the comparison point would make every later day look better."""
        if self.now is None or not self.has_history:
            return None
        if self.skip_sundays:
            days = [day for day, value in self._by_date.items()
                    if day <= self._cutoff and value is not None and day.weekday() != SUNDAY]
            return self._by_date[max(days)] if days else None
        for k in range(TREND_DAYS, 0, -1):
            if self.back[k] is not None:
                return self.back[k]
        return None

    def run(self):
        """[d-3, d-2, d-1, today] when all four are stored (Q8), else None."""
        values = [self.back[k] for k in range(TREND_DAYS, 0, -1)] + [self.now]
        return values if None not in values else None

    def worse_than_ref(self, worse):
        ref = self.ref()
        return ref is not None and worse(ref, self.now)

    def worse_each_day(self, worse):
        """Red by trend: worse than 3 days ago and worse on each of the last 3 days.
        "3 days ago" is ref(), which is run[0] unless skip_sundays moved it off a Sunday."""
        run = self.run()
        return (run is not None and self.worse_than_ref(worse)
                and all(worse(a, b) for a, b in zip(run, run[1:])))

    def unchanged(self):
        """The same value on all four rows d-3..today ("unchanged for 3 days", Q8)."""
        run = self.run()
        return run is not None and len(set(run)) == 1

    def trail(self):
        """'31 → 49 → 53 → 59' over the rows there are, for the dry run's reason."""
        values = [self.back[k] for k in range(TREND_DAYS, 0, -1)] + [self.now]
        return ' → '.join('–' if v is None else _n(v) for v in values)


class _Context:
    def __init__(self, section, today, past):
        self.section, self.today, self.past = section, today, past
        self.n = figure(section, 'in_execution')
        self._series = {}

    def series(self, key, skip_sundays=False):
        if (key, skip_sundays) not in self._series:
            self._series[key, skip_sundays] = _Series(key, self.section, self.today,
                                                      self.past, skip_sundays)
        return self._series[key, skip_sundays]


def _reason(rule, series, *, hard=None, running=False, amber=False, label='hard limit'):
    """The dry run's line for one flag: which condition fired, with the numbers. `label`
    names the today-only condition; R10's is an amber, not a hard limit."""
    parts = []
    if hard:
        parts.append(f'{label}: {hard}')
    if running:
        parts.append('worse on each of the last 3 days')
    elif amber:
        parts.append(f'worse than 3 days ago ({_n(series.ref())} → {_n(series.now)})')
    if series is not None:
        parts.append(f'd-3..today {series.trail()}'
                     + ('' if series.has_history else ' (under 3 days of history: no trend)'))
    return f'{rule}: ' + '; '.join(parts)


def _trend_words(series, running, worse_word):
    """', up 3 days running' / ', up from 15 in 3 days', for a trend that fired. "in 3
    days", not "three days ago": the shorter form keeps a 3-digit sentence on one line
    (Zuber, 20 Nov: shorter templates before any cut)."""
    if running:
        return f', {worse_word} 3 days running'
    return f', {worse_word} from {_n(series.ref())} in 3 days'


def _rising_rule(ctx, rule, key, limit_text, hard, text, action, worse=_up,
                 worse_word='up', role=ROLE_PM, skip_sundays=False):
    """The shape of R1, R2, R3, R7 and R9: amber when worse than 3 days ago, red when
    worse each day or past the hard limit. `hard` is the limit's verdict on today.
    `skip_sundays` moves "3 days ago" off a Sunday row (R7 only, _Series.ref)."""
    s = ctx.series(key, skip_sundays)
    if s.now is None:
        return None
    running, amber = s.worse_each_day(worse), s.worse_than_ref(worse)
    if not (running or amber or hard):
        return None
    words = _trend_words(s, running, worse_word) if (running or amber) else limit_text
    return Flag(rule, RED if (running or hard) else AMBER, role, text(s.now) + words, action,
                (key,), _reason(rule, s, hard=hard and _limit_reason(rule, ctx, s),
                                running=running, amber=amber))


def _limit_reason(rule, ctx, s):
    n, sched = ctx.n, figure(ctx.section, 'scheduled')
    return {
        'R1': f'{_n(s.now)} > {OVERDUE_PER_SITE} × {_n(n or 0)} sites in execution',
        'R2': f'{_n(s.now)} > {BEHIND_PERCENT}% of {_n(sched or 0)} scheduled',
        'R3': f'{_n(s.now)} > {QUIET_PERCENT}% of {_n(n or 0)} sites in execution',
        'R7': f'{_n(s.now)} < {ACTIVE_TODAY_PERCENT}% of {_n(n or 0)} sites in execution',
        'R9': f'{_n(s.now)} > 0',
    }[rule]


def _over(value, percent, base):
    """value > percent% of base, in whole numbers (no float edge at exactly 25%)."""
    return bool(base) and value * 100 > percent * base


def _r1(ctx):
    v, n = ctx.series('overdue').now, ctx.n
    return _rising_rule(ctx, 'R1', 'overdue', ', over 1 per site',
                        v is not None and n is not None and v > OVERDUE_PER_SITE * n,
                        lambda v: _plural(v, 'overdue task'), 'PM to review')


def _r2(ctx):
    v, sched = ctx.series('behind').now, figure(ctx.section, 'scheduled')
    if sched is None:
        # A section stored before SNAP-3c (or a test stub) has no scheduled count: the
        # trend still reads, the hard limit cannot.
        text = lambda v: f'{_plural(v, "site")} behind plan'
    else:
        text = lambda v: f'{_n(v)} of {_plural(sched, "scheduled site")} behind plan'
    return _rising_rule(ctx, 'R2', 'behind', f', over {BEHIND_PERCENT}%',
                        v is not None and sched is not None
                        and _over(v, BEHIND_PERCENT, sched),
                        text, 'PM to review')


def _r3(ctx):
    v, n = ctx.series('quiet').now, ctx.n
    return _rising_rule(ctx, 'R3', 'quiet', f', over {QUIET_PERCENT}%',
                        v is not None and n is not None and _over(v, QUIET_PERCENT, n),
                        lambda v: f'{_n(v)} of {_plural(n or 0, "site")} gone quiet',
                        'PM to follow up')


def _r4(ctx):
    s = ctx.series('civil')
    # Not falling is only worse while some site still needs a date: a 0 that stays 0 is
    # the goal, not a flag.
    if not s.now:
        return None
    unchanged = s.unchanged()
    running, amber = s.worse_each_day(_not_falling), s.worse_than_ref(_not_falling)
    if not (running or amber):
        return None
    if unchanged:
        words = ', unchanged for 3 days'
    elif running:
        words = ', not falling for 3 days'
    elif s.now > s.ref():
        words = f', up from {_n(s.ref())} in 3 days'
    else:
        words = ', same as 3 days ago'
    return Flag('R4', RED if (running or unchanged) else AMBER, ROLE_PM,
                f'{_plural(s.now, "site")} need a Civil date{words}', 'PM to set dates',
                ('civil',), _reason('R4', s, hard=unchanged and f'{s.now} > 0, unchanged',
                                    running=running and not unchanged, amber=amber))


def _r5(ctx):
    s = ctx.series('blocked')
    if s.now is None:
        return None
    run = s.run()
    stuck = run is not None and all(v > 0 for v in run)
    running, amber = s.worse_each_day(_up), s.worse_than_ref(_up)
    if not (stuck or running or amber):
        return None
    if running:
        words = ', up 3 days running'
    elif stuck:
        words = ', blocked 3 days running'
    else:
        words = _trend_words(s, False, 'up')
    return Flag('R5', RED if (stuck or running) else AMBER, ROLE_PM,
                _plural(s.now, 'blocked task') + words, 'PM to unblock', ('blocked',),
                _reason('R5', s, hard=stuck and 'blocked on each of the 4 rows',
                        running=running, amber=amber))


def _r6(ctx):
    s = ctx.series('issues')
    if not s.now:
        return None
    ref = s.ref()
    # "> 0 and not reduced in 3 days": an increase is not a reduction either, so any
    # trend that fires here is red; amber is left for nothing (ruled as written).
    not_reduced = ref is not None and s.now >= ref
    running = s.worse_each_day(_up)
    if not (not_reduced or running):
        return None
    if running:
        words = ', up 3 days running'
    elif s.now > ref:
        words = f', up from {_n(ref)} in 3 days'
    else:
        words = ', not reduced in 3 days'
    return Flag('R6', RED, ROLE_PM, _plural(s.now, 'open issue') + words, 'PM to resolve',
                ('issues',), _reason('R6', s, hard=not_reduced
                                     and f'{s.now} > 0 and not below {ref} three days ago',
                                     running=running))


def _r7(ctx):
    if ctx.today.weekday() == SUNDAY:
        return None
    v, n = ctx.series('active_today').now, ctx.n
    return _rising_rule(ctx, 'R7', 'active_today', ', under a quarter',
                        v is not None and n and v * 100 < ACTIVE_TODAY_PERCENT * n,
                        lambda v: f'Active today {_n(v)} of {_plural(n or 0, "site")}',
                        'PM to check sites', worse=_down, worse_word='down',
                        skip_sundays=True)


def _r8(ctx):
    if ctx.today.weekday() == SUNDAY:
        return None
    y, x = figure(ctx.section, 'attendance_not_active'), figure(ctx.section, 'attendance_today')
    if not y or x is None:
        return None
    # Z1 wording (Q3): "Attendance at 3 sites, 1 of them not Active".
    return Flag('R8', RED, ROLE_SE, f'Attendance at {_plural(x, "site")}, {_n(y)} of them '
                f'not Active', 'Site Engineers to check', ('attendance_today',),
                f'R8: hard limit: {_n(y)} site(s) with attendance today have no Civil task '
                f'In Progress/Done')


def _r9(ctx):
    v = ctx.series('dcs_overdue').now
    return _rising_rule(ctx, 'R9', 'dcs_overdue', '', bool(v),
                        lambda v: f'{_n(v)} DC{"" if v == 1 else "s"} overdue',
                        'Site Engineers to confirm receipts', role=ROLE_SE)


def _r10(ctx):
    s = ctx.series('active_no_material')
    x = figure(ctx.section, 'active')
    if not s.now or x is None:
        return None
    running = s.worse_each_day(_up)
    return Flag('R10', RED if running else AMBER, ROLE_SCM,
                f'Material pending at {_n(s.now)} of {_n(x)} Active sites'
                + (', up 3 days running' if running else ''), 'SCM to follow up',
                ('received',),
                _reason('R10', s, hard=f'{s.now} Active site(s) with no material received',
                        running=running, label='amber when any'))


def _r11(ctx):
    s, in_progress = ctx.series('design_released'), figure(ctx.section, 'design_in_progress')
    # Nothing in progress means nothing to release: no flag however flat the count.
    if s.now is None or not in_progress:
        return None
    # "Not rising" against 3 days ago IS "no new release in 3 days", the hard limit, so
    # this rule is red or nothing.
    if not (s.worse_than_ref(_not_rising) or s.worse_each_day(_not_rising)):
        return None
    return Flag('R11', RED, ROLE_DESIGN,
                f'No new design release in 3 days, {_n(in_progress)} in progress',
                'Design Head to review', ('design_released',),
                _reason('R11', s, hard=f'released {s.now} not above {s.ref()} three days '
                                       f'ago while {in_progress} in progress'))


def _r12(ctx):
    parts, colours, unchanged, reasons = [], [], [], []
    for key, label in (('mobilized', 'Mobilized'), ('active', 'Active')):
        s = ctx.series(key)
        # Q5: a figure already equal to the sites in execution has nowhere to rise.
        if s.now is None or ctx.n is None or s.now >= ctx.n:
            continue
        flat = s.unchanged()
        running, amber = s.worse_each_day(_not_rising), s.worse_than_ref(_not_rising)
        if not (flat or running or amber):
            continue
        parts.append((key, f'{label} {_n(s.now)}'))
        colours.append(RED if (flat or running) else AMBER)
        unchanged.append(flat)
        reasons.append(_reason(f'R12 {key}', s, hard=flat and 'unchanged on all 4 rows',
                               running=running and not flat, amber=amber))
    if not parts:
        return None
    words = 'unchanged for 3 days' if all(unchanged) else 'not rising in 3 days'
    return Flag('R12', RED if RED in colours else AMBER, ROLE_PM,
                f'{" and ".join(t for _, t in parts)} of {_n(ctx.n)}, {words}',
                'PM to push', tuple(k for k, _ in parts), ' | '.join(reasons))


def _r13(ctx):
    received = figure(ctx.section, 'received')
    if not received:
        return None
    for key, words, role, action in FUNNEL:
        count = figure(ctx.section, key)
        if count is None:
            return None     # a section stored before SNAP-3b
        if count == 0:
            # Q4: only the first zero. Every count after it is 0 too, or was entered
            # out of order, and either way the first gap is the one to fill.
            return Flag('R13', RED, role,
                        f'0 {words} although {_plural(received, "site")} '
                        f'{"has" if received == 1 else "have"} material', action, (key,),
                        f'R13: hard limit: {key} = 0 while material is received at '
                        f'{received} site(s) (funnel invoices → requests → approved → paid)')
    return None


_RULE_FUNCTIONS = (_r1, _r2, _r3, _r4, _r5, _r6, _r7, _r8, _r9, _r10, _r11, _r12, _r13)


def _past_sections(section_id, today, history):
    """[(date, this tender's section or None)] for every stored payload before today."""
    out = []
    for iso, payload in (history or {}).items():
        day = date.fromisoformat(str(iso)[:10])
        if day < today:
            sections = (payload or {}).get('tender_sections') or {}
            out.append((day, sections.get(str(section_id))))
    return out


def evaluate(section, snapshot_date, history=None):
    """Every flag of one tender section, worst first (colour, then the table's order)."""
    today = date.fromisoformat(str(snapshot_date)[:10])
    ctx = _Context(section, today, _past_sections(section['id'], today, history))
    flags = [flag for rule in _RULE_FUNCTIONS if (flag := rule(ctx)) is not None]
    return sorted(flags, key=lambda f: f.rank)


def needs_action(section, snapshot_date, history=None):
    """The section's "Needs action" box (see the module docstring's THE BOX)."""
    flags = evaluate(section, snapshot_date, history)
    worst = {}
    for flag in flags:              # sorted, so the first per role is its worst
        worst.setdefault(flag.role, flag)
    per_role = sorted(worst.values(), key=lambda f: (f.rank, ROLES.index(f.role)))
    dots = {}
    for flag in reversed(flags):    # worst last, so it wins the dot
        for key in flag.figures:
            dots[key] = flag.colour
    return ActionBox(flags=tuple(flags), lines=tuple(per_role[:MAX_ACTION_LINES]),
                     more=max(0, len(per_role) - MAX_ACTION_LINES), dots=dots)
