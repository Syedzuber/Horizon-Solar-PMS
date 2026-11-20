"""A site's BD (`Project.bd`, FIN-M-B5-1, ruling FR7) — the one writer and the queries.

`set_site_bd()` is the ONLY writer of `Project.bd`. Two callers: the overview's BD
modal (views.project_set_bd, CEO / Admin / System Admin) and the backfill command
(backfill_site_bd). Every change writes one ActivityLog line, "BD: old → new" (the
FIN-M-L R15 style, an empty seat reads "blank"):
  * `bd_assigned`   — set, changed or cleared from the form. Counts as the site's latest
                      activity on the Site Dashboard, exactly as `pm_assigned` does (it is
                      in neither activity_codes set);
  * `bd_backfilled` — written by the command. In NON_ACTIVITY_CODES, so the one-off fill
                      never shows as "latest activity" on the 17 sites it touches.
ProjectAdmin's fieldsets do not list `bd`, so Django admin cannot write past the log.

Scope (FIN-M-B5-1 rulings 1-3): non-deleted Residential sites in every status except
Cancelled, test sites excluded — Draft included, because M1 is recorded on the Draft (R5)
and its BD should be set before activation. `bd_scope_projects()` is that scope; the
backfill and `sites_without_bd()` both start from it.

The role string and the "active" rule live in permissions.py (SITE_BD_ROLE,
active_bd_profiles, profile_is_active_bd); nothing here compares a role.
"""
from collections import Counter
from dataclasses import dataclass

from django.db import transaction

from projects.models import ActivityLog, Project, Task, UserProfile, log_activity
from projects.permissions import (
    SITE_BD_ROLE, SITE_PM_ROLE, active_bd_profiles, profile_is_active_bd,
)

#: ActivityLog codes. Literal strings at the log_activity() calls too, so a source scan
#: (tests_activity_money) can resolve them; these names are for readers and tests.
BD_ASSIGNED = 'bd_assigned'
BD_BACKFILLED = 'bd_backfilled'

#: TaskTemplateTask.code of the Residential template's first task, "OCR, Documentation &
#: Verification" — the task BD has always been assigned, and the backfill's only source.
#: Matched on the template code, not the name, because a task can be renamed.
OCR_TASK_CODE = 'OCR_DOCUMENTATION_VERIFICATION'

# Project vocabulary (not role strings) the scope is written in.
RESIDENTIAL = 'Residential'
CANCELLED = 'Cancelled'

# Backfill outcomes and skip reasons — printed by the command and asserted by its tests.
OUTCOME_SET = 'set'
OUTCOME_ALREADY_SET = 'already set'
OUTCOME_SKIPPED = 'skipped'

REASON_NO_OCR_TASK = 'no OCR task'
REASON_SEVERAL_OCR_TASKS = 'several OCR tasks'
REASON_UNASSIGNED = 'OCR task unassigned'
REASON_HELD_BY_PM = 'OCR task held by a PM'
REASON_HELD_BY_OTHER = 'OCR task held by a non-BD role'
REASON_INACTIVE_BD = 'OCR task held by an inactive BD'
REASON_CHANGED_DURING_RUN = 'BD set by someone else during the run'


class SiteBdChangeRefused(Exception):
    """This BD write is impossible for the site, the chosen person or the moment. The
    message is written for the person who asked and is safe to show them."""


def display_name(profile):
    """A person's display name for log lines and command output; 'blank' for no one."""
    if profile is None:
        return 'blank'
    return profile.user.get_full_name() or profile.user.username


# THE ONE DEFINITION OF BD SCOPE (FIN-M-B5-1 rulings 1-3, follow-up ruling 1): the sites
# that carry a BD — non-deleted (R3), non-test Residential sites in every status except
# Cancelled, Draft included. The writer, the backfill, the "No BD set" list and the
# overview's pencil all read these two constants, through bd_scope_projects() (queryset)
# or site_in_bd_scope() (one loaded row). Change scope here and nowhere else;
# tests_site_bd pins the two forms equal.
BD_SCOPE_FILTER = {'is_deleted': False, 'is_test': False, 'project_type': RESIDENTIAL}
BD_SCOPE_EXCLUDED_STATUSES = frozenset({CANCELLED})

#: Shown when a site outside the scope is asked to take a BD.
OUT_OF_SCOPE_MESSAGE = ('A BD is recorded only on live Residential sites, not on a '
                        'deleted, cancelled, test or tender site.')


def bd_scope_projects():
    """Sites that carry a BD (see BD_SCOPE_FILTER). A queryset, so callers add
    select_related / ordering / further filters / select_for_update."""
    return (Project.objects
            .filter(**BD_SCOPE_FILTER)
            .exclude(status__in=BD_SCOPE_EXCLUDED_STATUSES))


def site_in_bd_scope(project):
    """True when the loaded `project` is in bd_scope_projects() — the same two constants
    read off the row, so it costs no query. Exists for the overview, whose query count
    is pinned: it decides the pencil without asking the database."""
    if project is None:
        return False
    return (all(getattr(project, name) == value for name, value in BD_SCOPE_FILTER.items())
            and project.status not in BD_SCOPE_EXCLUDED_STATUSES)


def sites_without_bd():
    """The "No BD set" list (FR21; the Finance Head check is built in FIN-M-B5-8):
    in-scope sites whose BD is empty, or whose BD is no longer an active BD (profile or
    login deactivated, or role changed) — the cases user_is_site_bd() answers False for
    every user.

    One query, no Python-side filtering. `exclude(bd__in=…)` keeps an empty seat in:
    Django writes it as NOT (bd_id IN (…) AND bd_id IS NOT NULL), so a NULL bd_id is
    never dropped by SQL's three-valued IN. Ordered by project_id for a stable list.
    """
    return (bd_scope_projects()
            .exclude(bd__in=active_bd_profiles())
            .select_related('bd__user')
            .order_by('project_id'))


def stale_bd_reason(project):
    """Why a site that HAS a BD is still on the "No BD set" list, for printed lists.
    Reads the already-loaded bd and its user; no query."""
    bd = project.bd
    if bd is None:
        return 'no BD set'
    if not profile_is_active_bd(bd) and bd.is_active and bd.user.is_active:
        return f'BD {display_name(bd)} is no longer in the BD role'
    return f'BD {display_name(bd)} is inactive'


def set_site_bd(project, new_bd, by, *, expected_old_id, action_code=BD_ASSIGNED, note=''):
    """Set, change or clear `project`'s BD. Returns True when the column changed, False
    for a no-op (same person, nothing written, no log line).

    `expected_old_id` is the BD pk the caller saw (None for an empty seat): the write is
    refused if the seat holds anyone else by the time it lands, so a stale form, or the
    backfill racing an Admin, never silently replaces someone's choice (the PM-2 Q3
    rule change_site_pm() applies to the PM seat). The backfill passes None, which is
    also what makes it never overwrite a set BD.

    Refuses: a site outside bd_scope_projects() — deleted, Cancelled, test or
    non-Residential (follow-up ruling 1) — and a new BD who is not an active BD. Clearing (new_bd=None) is always allowed. Who may
    call this is the caller's question (views.project_set_bd asks
    user_can_set_site_bd); `by` is only the actor the log line names.
    """
    new_id = new_bd.pk if new_bd is not None else None
    with transaction.atomic():
        # Row lock: two saves of the same site's BD serialise here, so the second one
        # sees the first one's value and is refused by the expected_old_id check
        # instead of overwriting it unseen. No select_related, so FOR UPDATE locks the
        # project row only, not the BD's profile. Fetched THROUGH bd_scope_projects(), so
        # the scope is checked on the locked row in the same query: a site cancelled,
        # deleted or marked test after the form opened comes back None and is refused.
        locked = bd_scope_projects().select_for_update().filter(pk=project.pk).first()
        if locked is None:
            raise SiteBdChangeRefused(OUT_OF_SCOPE_MESSAGE)
        if locked.bd_id != expected_old_id:
            raise SiteBdChangeRefused(
                "This site's BD was changed by someone else while you were editing. "
                'Reload the page and try again.')
        if locked.bd_id == new_id:
            return False
        if new_bd is not None and not profile_is_active_bd(new_bd):
            raise SiteBdChangeRefused(f'{display_name(new_bd)} is not an active BD user.')

        # The outgoing BD's name for the log line; one read, only on a real change.
        old_bd = (UserProfile.objects.select_related('user').get(pk=locked.bd_id)
                  if locked.bd_id is not None else None)
        locked.bd = new_bd
        # save(update_fields=…), not filter().update(): the row is already locked above,
        # so there is no race to close, and save() keeps Project's own invariants
        # (_validate_program_link) on the write.
        locked.save(update_fields=['bd'])

        action = f'BD: {display_name(old_bd)} → {display_name(new_bd)}'
        if note:
            action = f'{action} ({note})'
        # Two literal calls, not log_activity(…, action_code=action_code): the code must
        # be readable at the call site by tests_activity_money's source scan.
        if action_code == BD_BACKFILLED:
            log_activity(locked, by, action, entity_type='Project', entity_id=locked.pk,
                         action_code='bd_backfilled')
        else:
            log_activity(locked, by, action, entity_type='Project', entity_id=locked.pk,
                         action_code='bd_assigned')

    # Keep the caller's instance in step with the row it just wrote.
    project.bd = new_bd
    return True


# ---------------------------------------------------------------------------
# The backfill (FR7): fill Project.bd from the OCR task's assignee where that person is
# an active BD. Planning is read-only and two queries whatever the site count; the
# command decides whether to write.
# ---------------------------------------------------------------------------

@dataclass
class BackfillRow:
    """One in-scope site's backfill outcome. `bd` is the person set (or to be set) for
    OUTCOME_SET, the current BD for OUTCOME_ALREADY_SET, else None; `reason` is set for
    OUTCOME_SKIPPED."""
    project: Project
    outcome: str
    bd: UserProfile = None
    reason: str = ''


def plan_bd_backfill():
    """What the backfill would do to every in-scope site, in project_id order.

    Two queries: the sites (with their current BD) and every OCR task on them (with its
    assignee and login). A set BD is never replaced (OUTCOME_ALREADY_SET), whoever it is.
    """
    sites = list(bd_scope_projects().select_related('bd__user').order_by('project_id'))
    # Every OCR-template task on the in-scope sites, grouped by site. A site with zero
    # or several is skipped with that reason rather than guessed at.
    ocr_by_site = {}
    for task in (Task.objects
                 .filter(phase__project__in=[s.pk for s in sites],
                         template_task__code=OCR_TASK_CODE)
                 .select_related('assigned_to__user', 'phase')):
        ocr_by_site.setdefault(task.phase.project_id, []).append(task)

    rows = []
    for site in sites:
        if site.bd_id is not None:
            rows.append(BackfillRow(site, OUTCOME_ALREADY_SET, bd=site.bd))
            continue
        tasks = ocr_by_site.get(site.pk, [])
        if not tasks:
            rows.append(BackfillRow(site, OUTCOME_SKIPPED, reason=REASON_NO_OCR_TASK))
            continue
        if len(tasks) > 1:
            rows.append(BackfillRow(site, OUTCOME_SKIPPED, reason=REASON_SEVERAL_OCR_TASKS))
            continue
        assignee = tasks[0].assigned_to
        if assignee is None:
            rows.append(BackfillRow(site, OUTCOME_SKIPPED, reason=REASON_UNASSIGNED))
        elif profile_is_active_bd(assignee):
            rows.append(BackfillRow(site, OUTCOME_SET, bd=assignee))
        else:
            rows.append(BackfillRow(site, OUTCOME_SKIPPED,
                                    reason=_skip_reason_for(assignee)))
    return rows


def _skip_reason_for(assignee):
    """The skip reason for an OCR assignee who is not an active BD. Compares the
    assignee's role with the one profile_is_active_bd() accepts — never a role literal
    here — so 'inactive BD' means "a BD, but switched off"."""
    if assignee.role == SITE_BD_ROLE:
        return f'{REASON_INACTIVE_BD} ({display_name(assignee)})'
    if assignee.role == SITE_PM_ROLE:
        return f'{REASON_HELD_BY_PM} ({display_name(assignee)})'
    return f'{REASON_HELD_BY_OTHER} ({assignee.role or "no role"}: {display_name(assignee)})'


def reason_category(reason):
    """A skip reason without its "(person)" detail, for the command's totals line."""
    return reason.split(' (', 1)[0]


def count_backfill_log_rows():
    """ActivityLog rows the backfill has written, ever — read before and after a run so
    the command can report how many this run added."""
    return ActivityLog.objects.filter(action_code=BD_BACKFILLED).count()


def summarise(rows):
    """Counter of outcomes and of skip-reason categories, for the totals line."""
    outcomes = Counter(r.outcome for r in rows)
    reasons = Counter(reason_category(r.reason) for r in rows if r.outcome == OUTCOME_SKIPPED)
    return outcomes, reasons
