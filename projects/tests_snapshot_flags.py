"""
SNAP-3c — trend flags and the "Needs action" box (snapshot_flags.py, snapshot.py's box
and dots, send_dashboard_snapshot's history and dry run).

WHAT THIS PINS
    Rules      R1-R13 each: an amber case, a red case and a no-flag case, with the exact
               sentence. R6, R8, R9, R11 and R13 have no amber as ruled (each says why).
    History    under 3 days of stored history: no trend flag, hard limits still fire; a
               missing d-3 row falls back to the oldest row in the last 3 days; a missing
               day among d-3..today means amber at most (Q8); a figure missing from old
               rows (before SNAP-1c / 3b / 3c) has no trend; today's own row and later rows
               are never read.
    Sunday     no R7 or R8; every other rule still reads. R7's "3 days ago" is the newest
               non-Sunday row at or before d-3, and the history query fetches it (Q2).
    Footer     the dot key, each ● in its own colour, on one line (Q3).
    Box (Q1)   one line per role, its worst flag; reds first across roles, then the
               table's order; at most 4 lines, the 4th ending "+N more"; one green line
               when clean; a dot on every flagged figure, shown in the box or not.
    Same in,   the same section and history give the same flags and sentences, whatever
    same out   order the history arrives in.
    Payload    `checks` = scheduled (equal to the Behind tile's text, Q8), attendance not
               Active (Z1), Active without material (Z2 (b)); no query added; keys shared
               with no other block.
    Layout     the box is 4 lines tall always; the longest sentences fit at 3-digit
               figures; a cut is at a word, never inside a number.
    Command    the history is the last 3 days' rows plus the newest older one, in 2
               queries; the dry run prints the box and every flag's reason; a stored row 3
               days back gives a trend flag end to end.

Run with:
    python manage.py test projects.tests_snapshot_flags --settings=solarpms.test_settings
"""
import io
import re
from datetime import date, timedelta
from types import SimpleNamespace
from unittest import mock

from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from . import snapshot as S
from . import snapshot_flags as F
from .management.commands.send_dashboard_snapshot import trend_history
from .models import DashboardSnapshot
from .site_breakdown import SiteBreakdown, site_breakdown
from .site_dashboard import REASON_SCHEDULED, build_site_cards
from .tests_dashboard_snapshot import NOW, SnapshotFixture
from .tests_sched2_scheduler import FROZEN_TODAY

WED = date(2026, 11, 18)      # a Wednesday: every rule reads; its d-3 is a Sunday
THU = date(2026, 11, 19)      # a Thursday: d-3 is a Monday (R7's trend cases)
SUN = date(2026, 11, 22)      # a Sunday: no R7 / R8

CLEAN_TILES = dict(dc_kwp='1,000', behind=0, civil=0, overdue=0, blocked=0, issues=0, quiet=0)
CLEAN_BREAKDOWN = dict(in_execution=10, mobilized=10, active=10, active_today=10,
                       attendance_today=0, design_not_started=0, design_in_progress=0,
                       design_released=10, received=0, pending=0, dcs_to_confirm=0,
                       dcs_overdue=0)
CLEAN_FINANCE = dict(invoices=1, requests=1, approved=1, payment_pending=0, paid=1)
CLEAN_CHECKS = dict(scheduled=10, attendance_not_active=0, active_no_material=0)


def _section(pid=1, **values):
    """A section in collect_snapshot()'s shape on which no rule fires, with `values`
    put into whichever block holds that key."""
    tiles, breakdown = dict(CLEAN_TILES), dict(CLEAN_BREAKDOWN)
    finance, checks = dict(CLEAN_FINANCE), dict(CLEAN_CHECKS)
    for key, value in values.items():
        for block in (tiles, breakdown, finance, checks):
            if key in block:
                block[key] = value
                break
        else:
            raise KeyError(key)
    return {'id': pid, 'code': f'T{pid}', 'name': f'Tender {pid}', 'pms': ['A PM'],
            'tiles': [{'key': key, 'label': label, 'value': tiles[key], 'detail': ''}
                      for key, label, _ in S.FIGURES if key != 'sites'],
            'dc_kwp_number': 1000, 'breakdown': breakdown, 'finance': finance,
            'checks': checks}


def _history(days, today=WED):
    """{ISO date: payload} from {days back: section}; None leaves that day unstored."""
    return {(today - timedelta(days=k)).isoformat(): {'tender_sections': {str(s['id']): s}}
            for k, s in days.items() if s is not None}


def _series(values, today=WED, **fixed):
    """(today's section, history) for one or more figures over [d-3, d-2, d-1, today];
    `values` maps key -> its four values, None = that day's row is missing."""
    days = {}
    for i, k in enumerate((3, 2, 1, 0)):
        if all(v[i] is None for v in values.values()):
            days[k] = None
            continue
        days[k] = _section(**fixed, **{key: v[i] for key, v in values.items()
                                       if v[i] is not None})
    today_section = days.pop(0)
    return today_section, _history(days, today)


def _flag(rule, section, history=None, today=WED):
    found = [f for f in F.evaluate(section, today.isoformat(), history) if f.rule == rule]
    assert len(found) <= 1, found
    return found[0] if found else None


class CleanTests(SimpleTestCase):

    def test_the_clean_section_has_no_flag_with_or_without_history(self):
        section, history = _series({'overdue': [0, 0, 0, 0]})
        for hist in (None, history):
            self.assertEqual(F.evaluate(section, WED.isoformat(), hist), [])
        box = F.needs_action(section, WED.isoformat(), history)
        self.assertEqual(box.line_texts(), [F.CLEAN_LINE])
        self.assertEqual(S.action_rows(box), [('green', F.CLEAN_LINE, '', '')])
        self.assertEqual(box.dots, {})

    def test_the_blocks_share_no_key(self):
        tiles = {key for key, _, _ in S.FIGURES}
        blocks = [set(S.BREAKDOWN_KEYS), set(S.FINANCE_KEYS), set(F.CHECK_KEYS), tiles]
        for i, a in enumerate(blocks):
            for b in blocks[i + 1:]:
                self.assertFalse(a & b, (a & b))


# ---------------------------------------------------------------------------
# The rules, one class each: amber, red, no flag, and the sentence
# ---------------------------------------------------------------------------

class R1OverdueTests(SimpleTestCase):

    def test_amber_up_from_three_days_ago(self):
        flag = _flag('R1', *_series({'overdue': [5, 6, 6, 7]}))
        self.assertEqual((flag.colour, flag.role), (F.AMBER, F.ROLE_PM))
        self.assertEqual(flag.sentence,
                         '7 overdue tasks, up from 5 in 3 days — PM to review.')

    def test_red_up_each_of_the_last_three_days(self):
        flag = _flag('R1', *_series({'overdue': [1, 2, 3, 4]}))
        self.assertEqual(flag.colour, F.RED)
        self.assertEqual(flag.text, '4 overdue tasks, up 3 days running')

    def test_red_past_the_hard_limit_with_no_history(self):
        flag = _flag('R1', _section(overdue=11))
        self.assertEqual(flag.colour, F.RED)
        self.assertEqual(flag.text, '11 overdue tasks, over 1 per site')
        self.assertIn('11 > 1 × 10 sites in execution', flag.reason)

    def test_no_flag_flat_falling_or_at_the_limit(self):
        for values in ([5, 5, 5, 5], [8, 7, 6, 5], [None, None, None, 10]):
            with self.subTest(values=values):
                self.assertIsNone(_flag('R1', *_series({'overdue': values})))


class R2BehindTests(SimpleTestCase):

    def test_amber_up_but_within_a_quarter_of_the_scheduled_sites(self):
        flag = _flag('R2', *_series({'behind': [1, 1, 1, 2]}, scheduled=8))
        self.assertEqual(flag.colour, F.AMBER)
        self.assertEqual(flag.text, '2 of 8 scheduled sites behind plan, up from 1 in 3 days')

    def test_red_over_a_quarter_of_the_scheduled_sites(self):
        flag = _flag('R2', _section(behind=3, scheduled=8))
        self.assertEqual(flag.colour, F.RED)
        self.assertEqual(flag.text, '3 of 8 scheduled sites behind plan, over 25%')

    def test_exactly_a_quarter_is_not_over_it(self):
        self.assertIsNone(_flag('R2', _section(behind=2, scheduled=8)))

    def test_no_scheduled_count_stored_reads_the_trend_only(self):
        section, history = _series({'behind': [1, 1, 1, 9]})
        del section['checks']                   # a row stored before SNAP-3c
        flag = _flag('R2', section, history)
        self.assertEqual((flag.colour, flag.text),
                         (F.AMBER, '9 sites behind plan, up from 1 in 3 days'))

    def test_no_flag(self):
        self.assertIsNone(_flag('R2', *_series({'behind': [2, 2, 1, 1]}, scheduled=8)))


class R3QuietTests(SimpleTestCase):

    def test_amber(self):
        flag = _flag('R3', *_series({'quiet': [0, 0, 1, 2]}))
        self.assertEqual((flag.colour, flag.text),
                         (F.AMBER, '2 of 10 sites gone quiet, up from 0 in 3 days'))

    def test_red_over_a_fifth(self):
        flag = _flag('R3', _section(quiet=3))
        self.assertEqual((flag.colour, flag.text),
                         (F.RED, '3 of 10 sites gone quiet, over 20%'))
        self.assertEqual(flag.action, 'PM to follow up')

    def test_no_flag(self):
        self.assertIsNone(_flag('R3', *_series({'quiet': [2, 2, 2, 2]})))


class R4CivilTests(SimpleTestCase):

    def test_red_unchanged_for_three_days(self):
        flag = _flag('R4', *_series({'civil': [4, 4, 4, 4]}))
        self.assertEqual(flag.sentence,
                         '4 sites need a Civil date, unchanged for 3 days — PM to set dates.')
        self.assertEqual(flag.colour, F.RED)

    def test_red_not_falling_any_day(self):
        flag = _flag('R4', *_series({'civil': [2, 3, 3, 4]}))
        self.assertEqual((flag.colour, flag.text),
                         (F.RED, '4 sites need a Civil date, not falling for 3 days'))

    def test_amber_not_below_three_days_ago(self):
        flag = _flag('R4', *_series({'civil': [4, 3, 5, 4]}))
        self.assertEqual((flag.colour, flag.text),
                         (F.AMBER, '4 sites need a Civil date, same as 3 days ago'))

    def test_no_flag_falling_or_zero(self):
        for values in ([5, 4, 3, 2], [0, 0, 0, 0]):
            with self.subTest(values=values):
                self.assertIsNone(_flag('R4', *_series({'civil': values})))


class R5BlockedTests(SimpleTestCase):

    def test_red_blocked_on_all_four_rows(self):
        flag = _flag('R5', *_series({'blocked': [1, 1, 1, 1]}))
        self.assertEqual((flag.colour, flag.text),
                         (F.RED, '1 blocked task, blocked 3 days running'))

    def test_amber_up(self):
        flag = _flag('R5', *_series({'blocked': [0, 0, 0, 2]}))
        self.assertEqual((flag.colour, flag.text),
                         (F.AMBER, '2 blocked tasks, up from 0 in 3 days'))

    def test_blocked_three_days_but_not_on_d3_is_amber_not_red(self):
        # "3 days running" is read over the four rows d-3..today (Q8): d-3 had none.
        flag = _flag('R5', *_series({'blocked': [0, 1, 1, 1]}))
        self.assertEqual((flag.colour, flag.text),
                         (F.AMBER, '1 blocked task, up from 0 in 3 days'))

    def test_no_flag(self):
        for values in ([2, 1, 0, 0], [None, 3, 3, 3]):
            with self.subTest(values=values):
                self.assertIsNone(_flag('R5', *_series({'blocked': values})))


class R6IssuesTests(SimpleTestCase):
    """No amber: "not reduced in 3 days" is the hard limit, and an increase is not a
    reduction either, so every trend that fires is red (ruled as written)."""

    def test_red_not_reduced(self):
        flag = _flag('R6', *_series({'issues': [3, 3, 3, 3]}))
        self.assertEqual(flag.sentence,
                         '3 open issues, not reduced in 3 days — PM to resolve.')

    def test_red_up(self):
        flag = _flag('R6', *_series({'issues': [2, 2, 2, 3]}))
        self.assertEqual((flag.colour, flag.text),
                         (F.RED, '3 open issues, up from 2 in 3 days'))

    def test_no_flag_reduced_or_no_history(self):
        for values in ([4, 3, 3, 3], [None, None, None, 3], [0, 0, 0, 0]):
            with self.subTest(values=values):
                self.assertIsNone(_flag('R6', *_series({'issues': values})))


class R7ActiveTodayTests(SimpleTestCase):

    def test_red_under_a_quarter(self):
        flag = _flag('R7', _section(active_today=2))
        self.assertEqual((flag.colour, flag.text),
                         (F.RED, 'Active today 2 of 10 sites, under a quarter'))

    # The trend cases run on THU: WED's d-3 is a Sunday, which R7 never compares with.
    def test_red_down_each_day(self):
        flag = _flag('R7', *_series({'active_today': [9, 8, 7, 6]}, today=THU), today=THU)
        self.assertEqual((flag.colour, flag.text),
                         (F.RED, 'Active today 6 of 10 sites, down 3 days running'))

    def test_amber_down(self):
        flag = _flag('R7', *_series({'active_today': [8, 8, 8, 5]}, today=THU), today=THU)
        self.assertEqual((flag.colour, flag.text),
                         (F.AMBER, 'Active today 5 of 10 sites, down from 8 in 3 days'))

    def test_no_flag(self):
        self.assertIsNone(_flag('R7', *_series({'active_today': [5, 6, 7, 8]})))

    # Q2: today is Wednesday 18 Nov, so d-3 is Sunday 15 Nov. R7 compares against the
    # newest non-Sunday row at or before d-3: Saturday 14 Nov (4 days back).
    SUNDAY_WEEK = {4: 8, 3: 1, 2: 7, 1: 6}       # Sat 8, Sun 1, Mon 7, Tue 6; today 5

    def _sunday_week(self, days):
        history = _history({k: _section(active_today=v) for k, v in days.items()})
        return _section(active_today=5), history

    def test_r7_skips_a_sunday_row_as_the_comparison_point(self):
        self.assertEqual(WED - timedelta(days=3), SUN - timedelta(days=7))   # a Sunday
        flag = _flag('R7', *self._sunday_week(self.SUNDAY_WEEK))
        # Against Sunday's 1 the 5 would look like a rise; against Saturday's 8 it fell.
        self.assertEqual((flag.colour, flag.text),
                         (F.AMBER, 'Active today 5 of 10 sites, down from 8 in 3 days'))
        self.assertIn('(8 → 5)', flag.reason)

    def test_r7_with_no_weekday_row_at_or_before_d3_has_no_trend(self):
        days = {k: v for k, v in self.SUNDAY_WEEK.items() if k != 4}   # no Saturday
        self.assertIsNone(_flag('R7', *self._sunday_week(days)))

    def test_other_rules_still_compare_against_the_sunday_row(self):
        history = _history({4: _section(overdue=0), 3: _section(overdue=2),
                            2: _section(overdue=2), 1: _section(overdue=2)})
        flag = _flag('R1', _section(overdue=3), history)
        self.assertEqual(flag.text, '3 overdue tasks, up from 2 in 3 days')


class R8AttendanceTests(SimpleTestCase):
    """No amber and no trend: the ruled check is today's site sets only (Z1)."""

    def test_red_attendance_at_a_site_that_is_not_active(self):
        flag = _flag('R8', _section(attendance_today=3, attendance_not_active=1))
        self.assertEqual(flag.sentence, 'Attendance at 3 sites, 1 of them not Active — '
                                        'Site Engineers to check.')
        self.assertEqual((flag.colour, flag.role, flag.figures),
                         (F.RED, F.ROLE_SE, ('attendance_today',)))

    def test_no_flag(self):
        self.assertIsNone(_flag('R8', _section(attendance_today=3, attendance_not_active=0)))
        section = _section(attendance_today=3)
        del section['checks']
        self.assertIsNone(_flag('R8', section))


class R9DcsOverdueTests(SimpleTestCase):
    """No amber: any overdue challan is past the hard limit."""

    def test_red_any(self):
        flag = _flag('R9', _section(dcs_overdue=1))
        self.assertEqual(flag.sentence, '1 DC overdue — Site Engineers to confirm receipts.')

    def test_red_up_says_from_what(self):
        flag = _flag('R9', *_series({'dcs_overdue': [4, 4, 5, 6]}))
        self.assertEqual(flag.sentence, '6 DCs overdue, up from 4 in 3 days — Site '
                                        'Engineers to confirm receipts.')

    def test_no_flag(self):
        self.assertIsNone(_flag('R9', *_series({'dcs_overdue': [3, 1, 0, 0]})))


class R10MaterialTests(SimpleTestCase):

    def test_amber_any_active_site_without_material(self):
        flag = _flag('R10', _section(active=5, active_no_material=2))
        self.assertEqual(flag.sentence,
                         'Material pending at 2 of 5 Active sites — SCM to follow up.')
        self.assertEqual((flag.colour, flag.role, flag.figures),
                         (F.AMBER, F.ROLE_SCM, ('received',)))

    def test_red_up_three_days_running(self):
        flag = _flag('R10', *_series({'active_no_material': [1, 2, 3, 4]}, active=5))
        self.assertEqual((flag.colour, flag.text),
                         (F.RED, 'Material pending at 4 of 5 Active sites, up 3 days running'))

    def test_no_red_against_rows_from_before_snap_3c(self):
        section, history = _series({'active_no_material': [1, 2, 3, 4]}, active=5)
        for payload in history.values():
            for old in payload['tender_sections'].values():
                del old['checks']
        self.assertEqual(_flag('R10', section, history).colour, F.AMBER)

    def test_no_flag(self):
        self.assertIsNone(_flag('R10', _section(active=5, active_no_material=0)))


class R11DesignTests(SimpleTestCase):
    """No amber: "not rising against 3 days ago" is "no new release in 3 days", the hard
    limit, so the rule is red or nothing."""

    def test_red_no_release_in_three_days(self):
        flag = _flag('R11', *_series({'design_released': [3, 3, 3, 3]},
                                     design_in_progress=6))
        self.assertEqual(flag.sentence, 'No new design release in 3 days, 6 in progress — '
                                        'Design Head to review.')
        self.assertEqual(flag.role, F.ROLE_DESIGN)

    def test_no_flag_released_rose_nothing_in_progress_or_no_history(self):
        cases = (({'design_released': [2, 3, 3, 3]}, 6),
                 ({'design_released': [3, 3, 3, 3]}, 0),
                 ({'design_released': [None, None, 3, 3]}, 6))
        for values, in_progress in cases:
            with self.subTest(values=values, in_progress=in_progress):
                self.assertIsNone(_flag('R11', *_series(values,
                                                        design_in_progress=in_progress)))

    def test_no_trend_against_rows_from_before_snap_1c(self):
        section, history = _series({'design_released': [3, 3, 3, 3]}, design_in_progress=6)
        for payload in history.values():
            for old in payload['tender_sections'].values():
                del old['breakdown']['design_released']
        self.assertIsNone(_flag('R11', section, history))


class R12StalledTests(SimpleTestCase):

    def test_red_both_unchanged_as_one_line(self):
        flag = _flag('R12', *_series({'mobilized': [4, 4, 4, 4], 'active': [2, 2, 2, 2]}))
        self.assertEqual(flag.sentence,
                         'Mobilized 4 and Active 2 of 10, unchanged for 3 days — PM to push.')
        self.assertEqual(flag.figures, ('mobilized', 'active'))

    def test_a_figure_equal_to_sites_in_execution_is_skipped(self):
        flag = _flag('R12', *_series({'mobilized': [4, 4, 4, 4], 'active': [10] * 4}))
        self.assertEqual(flag.text, 'Mobilized 4 of 10, unchanged for 3 days')
        self.assertEqual(flag.figures, ('mobilized',))

    def test_amber_not_rising(self):
        flag = _flag('R12', *_series({'mobilized': [5, 4, 5, 5], 'active': [10] * 4}))
        self.assertEqual((flag.colour, flag.text),
                         (F.AMBER, 'Mobilized 5 of 10, not rising in 3 days'))

    def test_no_flag(self):
        self.assertIsNone(_flag('R12', *_series({'mobilized': [1, 2, 3, 4],
                                                 'active': [1, 1, 2, 3]})))


class R13FinanceTests(SimpleTestCase):
    """No amber: a missing finance entry while material is on site is past the limit."""

    def test_the_first_zero_of_the_funnel_and_its_role(self):
        cases = (
            (dict(invoices=0, requests=0, approved=0, paid=0), F.ROLE_SCM,
             '0 invoices although 8 sites have material — SCM to enter.'),
            (dict(invoices=2, requests=0, approved=0, paid=0), F.ROLE_SCM,
             '0 payment requests although 8 sites have material — SCM to enter.'),
            (dict(invoices=2, requests=3, approved=0, paid=0), F.ROLE_PAYMENT,
             '0 approvals although 8 sites have material — Payment approver to approve.'),
            (dict(invoices=2, requests=3, approved=1, paid=0), F.ROLE_PAYMENT,
             '0 payments although 8 sites have material — Finance to record.'),
        )
        for counts, role, sentence in cases:
            with self.subTest(counts=counts):
                flag = _flag('R13', _section(received=8, **counts))
                self.assertEqual((flag.colour, flag.role, flag.sentence),
                                 (F.RED, role, sentence))

    def test_one_site_reads_singular(self):
        flag = _flag('R13', _section(received=1, invoices=0))
        self.assertEqual(flag.text, '0 invoices although 1 site has material')

    def test_only_when_material_is_received(self):
        self.assertIsNone(_flag('R13', _section(received=0, invoices=0, requests=0,
                                                approved=0, paid=0)))

    def test_pending_is_never_flagged(self):
        self.assertIsNone(_flag('R13', _section(received=8, payment_pending=0)))


# ---------------------------------------------------------------------------
# History (Q8) and Sunday
# ---------------------------------------------------------------------------

class HistoryTests(SimpleTestCase):

    def test_under_three_days_of_history_no_trend_but_hard_limits_fire(self):
        section, history = _series({'overdue': [None, 1, 1, 5], 'quiet': [None, 0, 0, 3]})
        self.assertIsNone(_flag('R1', section, history))     # 5 up from 1: no trend yet
        quiet = _flag('R3', section, history)                 # 3 of 10: past the limit
        self.assertEqual((quiet.colour, quiet.text),
                         (F.RED, '3 of 10 sites gone quiet, over 20%'))
        self.assertIn('under 3 days of history', quiet.reason)

    def test_a_missing_d3_row_falls_back_to_the_oldest_in_the_window(self):
        section, history = _series({'overdue': [None, 1, 2, 3]})
        history.update(_history({5: _section(overdue=9)}))   # history exists, older
        flag = _flag('R1', section, history)
        self.assertEqual((flag.colour, flag.text),
                         (F.AMBER, '3 overdue tasks, up from 1 in 3 days'))

    def test_a_missing_day_means_amber_at_most(self):
        flag = _flag('R1', *_series({'overdue': [1, None, 3, 4]}))
        self.assertEqual(flag.colour, F.AMBER)

    def test_a_tender_with_no_section_then_has_no_trend(self):
        section, _ = _series({'overdue': [0, 0, 0, 5]})
        other = _history({3: _section(pid=2), 2: _section(pid=2), 1: _section(pid=2)})
        self.assertIsNone(_flag('R1', section, other))

    def test_today_and_later_rows_are_never_read(self):
        section, history = _series({'overdue': [5, 5, 5, 5]})
        history[WED.isoformat()] = {'tender_sections': {'1': _section(overdue=0)}}
        history[(WED + timedelta(days=1)).isoformat()] = {'tender_sections': {
            '1': _section(overdue=0)}}
        self.assertIsNone(_flag('R1', section, history))

    def test_a_sunday_has_no_r7_or_r8_and_every_other_rule_reads(self):
        section = _section(active_today=0, attendance_today=3, attendance_not_active=3,
                           overdue=11)
        rules = {f.rule for f in F.evaluate(section, SUN.isoformat())}
        self.assertEqual(rules, {'R1'})
        rules = {f.rule for f in F.evaluate(section, (SUN + timedelta(days=1)).isoformat())}
        self.assertEqual(rules, {'R1', 'R7', 'R8'})


# ---------------------------------------------------------------------------
# The box (Q1)
# ---------------------------------------------------------------------------

#: One flag per role: R1 red (PM), R9 red (Site Engineers), R10 amber (SCM), R11 red
#: (Design Head; needs history), R13 red (Payment approver / Finance: approved = 0).
#: Active stays at 10 of 10, so R12 has nothing to flag.
EVERY_ROLE = dict(overdue=11, dcs_overdue=2, active_no_material=1,
                  design_in_progress=4, received=3, approved=0, paid=0)


def _every_role():
    section, history = _series({'design_released': [3, 3, 3, 3]},
                               **{k: v for k, v in EVERY_ROLE.items()})
    return section, history


class BoxTests(SimpleTestCase):

    def test_one_line_per_role_its_worst_flag(self):
        # PM has R1 amber and R3 red: its line is the red R3.
        section, history = _series({'overdue': [5, 6, 6, 7]}, quiet=3)
        box = F.needs_action(section, WED.isoformat(), history)
        self.assertEqual([(f.rule, f.colour) for f in box.flags],
                         [('R3', F.RED), ('R1', F.AMBER)])
        self.assertEqual([f.rule for f in box.lines], ['R3'])
        self.assertEqual(box.more, 0)

    def test_reds_first_across_roles_then_the_tables_order(self):
        section, history = _series({'design_released': [3, 3, 3, 3]}, overdue=8,
                                   dcs_overdue=2, active_no_material=1,
                                   design_in_progress=4)
        box = F.needs_action(section, WED.isoformat(), history)
        # R1 at 8 of 10 is no flag; R9 red, R11 red, R10 amber.
        self.assertEqual([(f.role, f.rule) for f in box.lines],
                         [(F.ROLE_SE, 'R9'), (F.ROLE_DESIGN, 'R11'), (F.ROLE_SCM, 'R10')])

    def test_five_roles_four_lines_and_plus_n_more(self):
        section, history = _every_role()
        box = F.needs_action(section, WED.isoformat(), history)
        self.assertEqual([f.rule for f in box.lines], ['R1', 'R9', 'R11', 'R13'])
        self.assertEqual(box.more, 1)
        texts = box.line_texts()
        self.assertEqual(len(texts), F.MAX_ACTION_LINES)
        self.assertTrue(texts[-1].endswith('  +1 more'), texts[-1])
        self.assertEqual(S.action_rows(box)[-1][3], '  +1 more')
        self.assertEqual(texts[0], '11 overdue tasks, over 1 per site — PM to review.')

    def test_every_flagged_figure_gets_its_dot_shown_in_the_box_or_not(self):
        section, history = _every_role()
        box = F.needs_action(section, WED.isoformat(), history)
        self.assertEqual(box.dots, {'overdue': F.RED, 'dcs_overdue': F.RED,
                                    'design_released': F.RED, 'approved': F.RED,
                                    'received': F.AMBER})

    def test_same_input_same_sentences_in_any_history_order(self):
        section, history = _every_role()
        first = F.needs_action(section, WED.isoformat(), history)
        backwards = dict(reversed(list(history.items())))
        second = F.needs_action(section, WED.isoformat(), backwards)
        self.assertEqual(first, second)
        self.assertEqual(first.line_texts(), second.line_texts())
        self.assertEqual([f.reason for f in first.flags], [f.reason for f in second.flags])

    def test_the_box_has_the_engines_line_count_and_a_fixed_height(self):
        self.assertIs(S.MAX_ACTION_LINES, F.MAX_ACTION_LINES)
        self.assertEqual(S.ACTION_H, 2 * S.ACTION_PAD + F.MAX_ACTION_LINES * S.ACTION_LINE_H)
        self.assertEqual(S.STRIP_TOP, S.ACTION_TOP + S.ACTION_H + 12)
        self.assertEqual(S.SECTION_H, 668 + S.ACTION_H + 12)


# ---------------------------------------------------------------------------
# Drawing (pure)
# ---------------------------------------------------------------------------

class DrawTests(SimpleTestCase):

    def _draw(self):
        from PIL import Image, ImageDraw
        return ImageDraw.Draw(Image.new('RGB', (10, 10))), S._fonts()

    def test_the_longest_sentences_fit_uncut_at_three_digit_figures(self):
        draw, fonts = self._draw()
        big = _section(overdue=999, behind=888, quiet=888, civil=888, blocked=888,
                       issues=888, active_today=0, attendance_today=888,
                       attendance_not_active=888, dcs_overdue=888, active=888,
                       active_no_material=888, design_in_progress=888, mobilized=888,
                       received=888, approved=0, in_execution=999, scheduled=999)
        # Flat history gives the "unchanged / 3 days running" forms; a low one three days
        # back gives the longer "up from 100 in 3 days" forms.
        low = _section(**{k: 100 for k in ('overdue', 'behind', 'quiet', 'civil', 'blocked',
                                            'issues', 'dcs_overdue', 'active_no_material')},
                       active_today=999, in_execution=999, scheduled=999)
        room = S.ACTION_TEXT_END - S.ACTION_TEXT_X
        flags = []
        for history in (_history({k: big for k in (1, 2, 3)}), _history({3: low})):
            found = F.evaluate(big, WED.isoformat(), history)
            self.assertGreaterEqual(len(found), 10)
            flags += found
        for flag in flags:
            with self.subTest(rule=flag.rule, text=flag.text):
                width = (draw.textlength(flag.text, font=fonts.line_text)
                         + draw.textlength(f' — {flag.action}.', font=fonts.line_num)
                         + draw.textlength('  +1 more', font=fonts.line_text))
                self.assertLessEqual(width, room, flag.sentence)

    def test_a_cut_is_at_a_word_never_inside_a_number(self):
        draw, fonts = self._draw()
        text = 'Attendance at 1,234 sites, 567 of them not Active'
        for width in (120, 200, 300, 400):
            with self.subTest(width=width):
                cut = S.fit_words(draw, text, fonts.line_text, width)
                self.assertTrue(cut.endswith('…'))
                self.assertTrue(text.startswith(cut[:-1]))
                self.assertTrue(cut[:-1] == text or text[len(cut) - 1] == ' ', cut)
        self.assertEqual(S.fit_words(draw, text, fonts.line_text, 2000), text)

    def test_rendering_with_history_reads_no_database_and_draws_every_dot(self):
        section, history = _every_role()
        payload = {'snapshot_date': WED.isoformat(), 'taken_at_label': '18 Nov 2026, 8:00 PM IST',
                   'dates_as_of': {'text': 'Dates as of 18 Nov', 'stale': False},
                   'tender_sections': {'1': section}}
        with mock.patch.object(S, '_dot', wraps=S._dot) as dot:
            images = S.render_snapshot_images(payload, None, history=history)
        self.assertEqual(len(images), 1)
        colours = [c.args[3] for c in dot.call_args_list]
        # 4 box lines + 5 dotted figures.
        self.assertEqual(sorted(colours), sorted([F.RED] * 4 + [F.RED] * 4 + [F.AMBER]))

    def test_the_footer_keys_the_dots_in_their_colours_and_fits(self):
        draw, fonts = self._draw()
        self.assertTrue(S.FOOTER_TEXT.endswith(' · ● red = act now · ● amber = watch'))
        self.assertEqual([c for t, c in S.FOOTER_RUNS if t == '●'], ['red', 'amber'])
        self.assertTrue(all(c in S.DOT_COLOURS for t, c in S.FOOTER_RUNS if c))
        self.assertLessEqual(draw.textlength(S.FOOTER_TEXT, font=fonts.tile_detail),
                             S.IMAGE_WIDTH - 2 * S.MARGIN)

    def test_a_clean_section_draws_one_green_dot(self):
        payload = {'snapshot_date': WED.isoformat(), 'taken_at_label': 'x',
                   'dates_as_of': {'text': 'x', 'stale': False},
                   'tender_sections': {'1': _section()}}
        with mock.patch.object(S, '_dot', wraps=S._dot) as dot:
            S.render_snapshot_images(payload, None)
        self.assertEqual([c.args[3] for c in dot.call_args_list], ['green'])


# ---------------------------------------------------------------------------
# The stored checks, the history query and the command
# ---------------------------------------------------------------------------

class ChecksUnitTests(SimpleTestCase):

    def test_set_arithmetic_and_the_scheduled_count(self):
        cards = [SimpleNamespace(schedule=SimpleNamespace(reason=r))
                 for r in (REASON_SCHEDULED, REASON_SCHEDULED, 'no_civil_date')]
        breakdown = SiteBreakdown(in_execution_ids=(1, 2, 3, 4, 5), mobilized_ids=(1, 2),
                                  active_ids=(1, 2, 3), active_today_ids=(),
                                  received_ids=(1,), attendance_today_ids=(2, 4, 5))
        self.assertEqual(S._checks(cards, breakdown),
                         {'scheduled': 2,
                          'attendance_not_active': 2,       # 4 and 5
                          'active_no_material': 2})         # 2 (pending) and 3 (no DC)


class ChecksTests(SnapshotFixture):

    def _cards(self):
        viewer = S.snapshot_viewer()
        return S.section_cards([c for c in build_site_cards(viewer, today=FROZEN_TODAY)
                                if not c.is_draft])

    def test_checks_equal_the_tile_text_and_the_breakdown_sets(self):
        site = self._site(civil_due=FROZEN_TODAY + timedelta(days=3), program=self.tender_a)
        payload = self._collect()
        groups = self._cards()
        self.assertIn(site.pk, [c.id for c in groups[self.tender_a.pk]])
        for pid, section in payload['tender_sections'].items():
            behind = next(t for t in section['tiles'] if t['key'] == 'behind')['detail']
            scheduled = int(re.search(r'(\d[\d,]*) scheduled', behind).group(1))
            self.assertEqual(section['checks']['scheduled'], scheduled, behind)
            b = site_breakdown([c.id for c in groups[int(pid)]], NOW)
            self.assertEqual(section['checks']['attendance_not_active'],
                             len(set(b.attendance_today_ids) - set(b.active_ids)))
            self.assertEqual(section['checks']['active_no_material'],
                             len(set(b.active_ids) - set(b.received_ids)))
        alpha = payload['tender_sections'][str(self.tender_a.pk)]
        self.assertGreaterEqual(alpha['checks']['scheduled'], 1)

    def test_the_checks_add_no_query(self):
        with CaptureQueriesContext(connection) as with_checks:
            self._collect()
        with mock.patch.object(S, '_checks', return_value={}):
            with CaptureQueriesContext(connection) as without:
                self._collect()
        self.assertEqual(len(with_checks), len(without))


class CommandTests(SnapshotFixture):

    def _call(self, *args):
        out = io.StringIO()
        with mock.patch('django.utils.timezone.now', return_value=NOW):
            call_command('send_dashboard_snapshot', *args, stdout=out)
        return out.getvalue()

    def _row(self, days_back, payload=None):
        day = FROZEN_TODAY - timedelta(days=days_back)
        return DashboardSnapshot.objects.create(
            snapshot_date=day, taken_at=NOW - timedelta(days=days_back),
            payload=payload or {'tender_sections': {}}, created_by_command='test')

    def test_the_history_is_three_days_and_the_newest_older_row_in_two_queries(self):
        for back in (6, 4, 2, 0):
            self._row(back, {'tender_sections': {}, 'mark': back})
        with CaptureQueriesContext(connection) as ctx:
            history = trend_history(FROZEN_TODAY.isoformat())
        self.assertEqual(len(ctx), 2)
        self.assertEqual(sorted(history),
                         [(FROZEN_TODAY - timedelta(days=k)).isoformat() for k in (4, 2)])

    def test_a_sunday_older_row_brings_the_weekday_row_before_it(self):
        # 9 Nov (Monday): d-3 is 6 Nov. The newest older row is Sunday 1 Nov, so R7's
        # comparison row, Saturday 31 Oct, is fetched too (Q2); 25 Oct is not.
        self.assertEqual((FROZEN_TODAY - timedelta(days=8)).weekday(), F.SUNDAY)
        for back in (15, 9, 8, 2):
            self._row(back)
        with CaptureQueriesContext(connection) as ctx:
            history = trend_history(FROZEN_TODAY.isoformat())
        self.assertEqual(len(ctx), 3)
        self.assertEqual(sorted(history),
                         [(FROZEN_TODAY - timedelta(days=k)).isoformat() for k in (9, 8, 2)])

    def test_a_row_three_days_back_gives_a_trend_flag_in_the_dry_run(self):
        # opex_a1 has one blocked task today; in 3 days the tender had none.
        before = S.collect_snapshot(NOW)
        alpha = before['tender_sections'][str(self.tender_a.pk)]
        next(t for t in alpha['tiles'] if t['key'] == 'blocked')['value'] = 0
        self._row(3, before)
        out = self._call('--dry-run', '--out-dir', self.tempdir())
        self.assertIn('  Needs action:\n', out)
        # Every flag's reason is printed, whether or not it won its role's box line.
        self.assertRegex(out, r'- amber PM: R5: worse than 3 days ago \(0 → 1\)')
        payload = S.collect_snapshot(NOW)
        box = S.section_box(payload['tender_sections'][str(self.tender_a.pk)], payload,
                            trend_history(FROZEN_TODAY.isoformat()))
        r5 = next(f for f in box.flags if f.rule == 'R5')
        self.assertEqual((r5.colour, r5.sentence),
                         (F.AMBER, '1 blocked task, up from 0 in 3 days — PM to unblock.'))
        self.assertEqual(DashboardSnapshot.objects.count(), 1)     # dry run wrote nothing

    def test_the_dry_run_prints_exactly_the_box_the_image_draws(self):
        out = self._call('--dry-run', '--out-dir', self.tempdir())
        payload = S.collect_snapshot(NOW)
        for section in S.ordered_sections(payload):
            box = S.section_box(section, payload, {})
            for colour, what, who, more in S.action_rows(box):
                self.assertIn(f'    [{colour}] {what}{who}{more}\n', out)
            for flag in box.flags:
                self.assertIn(flag.reason, out)

    def tempdir(self):
        import tempfile
        return tempfile.mkdtemp()
