"""Phase rules: the order an OPEX site's work is expected to happen in, and the record
kept when it happens out of that order. OPEX template v1 only; Residential untouched.

IT WARNS. IT NEVER REFUSES. B-08 (30 Aug 2026) stands: a start goes ahead whatever
these rules say. What a bypassed rule produces is one PhaseRuleOverride row per rule,
written in the same transaction as the start, with the reason left for the site's PM
side to give. Amended for phase rules on 20 Oct 2026 by product-owner decision: the
reason comes from user_can_manage_project, not from the person who started the work
(docs/execution-model.md §12).

A SEPARATE EVALUATOR FROM task_dependencies.py, on purpose. Dependencies are edges
authored per template between tasks; these rules are facts about the SITE (is the
design released, has material arrived, has installation begun) that hold whatever
edges exist. Nothing here reads or writes TaskDependency.

    RULES                          the rules, keyed by (template code, version)
    SiteRuleState                  the site facts, a fixed number of queries per site
    phase_rule_warnings(task)      which rules starting `task` would bypass
    start_warnings(...)            the same, asked only for a first start (the chokepoint)
    dc_creation_warnings(project)  R1, asked by create_delivery_challan
    record_phase_rule_overrides()  the only writer of PhaseRuleOverride rows
    explain_phase_rule_override()  the only writer of the reason

WHAT "STARTED" MEANS: status In Progress or Done. Blocked never counts, and a task
marked Not Applicable never counts whatever its stored status (ruling Q3) — but a
predecessor that is ENTIRELY N/A satisfies its rule (R4, R6; follow-up ruling 2). On OPEX every
Done task has been In Progress (rung 1 of the status ladder), so Done is not a second
way in, merely a later state of the same start.
"""
from typing import NamedTuple

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .models import (
    DESIGN_RELEASED, SUBJECT_TASK, DesignAssignment, PhaseRuleOverride, Project,
    StatusTransition, Task, TaskTemplate, first_component_received, log_activity,
)
from .notifications import send_notification
from .permissions import project_managers

# NotificationLog.template_name label. Not an Interakt template: in-app only.
T_AWAITING_REASON = 'phase_rule_override_awaiting'

STARTED_STATUSES = (Task.IN_PROGRESS, Task.DONE)

# Template codes the rules and the site facts are written against. `code`, never
# `label`: rewording a template label must not silently detach a rule (the reasoning
# DESIGN_MIRROR_CODE and DC_CATEGORY_TO_MIRROR_CODE give for themselves).
PHASE_INSTALLATION = 'INSTALLATION'
PHASE_TESTING_COMMISSIONING = 'TESTING_COMMISSIONING'
TASK_POST_INSTALLATION_APPROVALS = 'POST_INSTALLATION_APPROVALS'

# What a rule is about: the creation of a delivery challan, a task by its template-task
# code, or every task in a phase by the phase's template code. A HAND-ADDED task has no
# template-task code, so it follows its phase's rule (R3, R4) and no task rule.
APPLIES_TO_CHALLAN = 'challan'
APPLIES_TO_TASK = 'task'
APPLIES_TO_PHASE = 'phase'


class PhaseRule(NamedTuple):
    code: str              # R1..R6; PhaseRuleOverride.rule_code
    rule_set: str          # stored on the row, so the record says which rules applied
    applies_to: str        # APPLIES_TO_*
    targets: frozenset     # task codes or phase codes; empty for a challan rule
    satisfied_when: str    # the SiteRuleState fact that must be True for no warning
    text: str              # the plain words the person starting the work is shown


_OPEX_V1 = 'OPEX v1'

# THE RULES, keyed by (TaskTemplate.code, version_no). A site whose tasks come from a
# template version that is not a key here has no rules at all, which is how Residential
# and any later OPEX version stay untouched until somebody writes their rules down.
RULES = {
    ('OPEX', 1): (
        PhaseRule('R1', _OPEX_V1, APPLIES_TO_CHALLAN, frozenset(),
                  'design_released', 'Design not released yet'),
        PhaseRule('R2', _OPEX_V1, APPLIES_TO_TASK,
                  frozenset({'NET_METERING_APPROVAL', 'CEIG_APPROVAL'}),
                  'design_released', 'Design not released yet'),
        PhaseRule('R3', _OPEX_V1, APPLIES_TO_PHASE, frozenset({PHASE_INSTALLATION}),
                  'material_received', 'No material received at site yet'),
        PhaseRule('R4', _OPEX_V1, APPLIES_TO_PHASE,
                  frozenset({PHASE_TESTING_COMMISSIONING}),
                  'installation_started', 'No installation work started yet'),
        PhaseRule('R5', _OPEX_V1, APPLIES_TO_TASK,
                  frozenset({TASK_POST_INSTALLATION_APPROVALS}),
                  'testing_finished', 'Testing & Commissioning not finished yet'),
        PhaseRule('R6', _OPEX_V1, APPLIES_TO_TASK,
                  frozenset({'COMPLETION_CERTIFICATES_PAPERWORK'}),
                  'post_installation_started', 'Post-Installation Approvals not started yet'),
    ),
}


class SiteRuleState:
    """The facts the rules read, for one site, computed lazily and at most once each.

    A FIXED NUMBER OF QUERIES WHATEVER THE TASK COUNT: one for the site's tasks (their
    status, flags and template codes), one for the design, one for material. Every
    per-task question is then answered in Python from those rows, so evaluating every
    row on the project overview costs what evaluating one row costs.

    PHASE IDENTITY. ProjectPhase has no link to its template phase, so a phase's code
    is borrowed from any template task in it — the way utils.roles_for_phase() finds a
    phase's roles. A phase holding only hand-added tasks has no code, and its tasks get
    no rule (ruling Q4).

    Reads the database as it stands: on the write path that is the state BEFORE the
    start being checked, which is the state the rules are about.
    """

    def __init__(self, project):
        self.project = project
        self._rows = None
        self._design_released = None
        self._material_received = None

    def _load(self):
        if self._rows is not None:
            return
        # Every task on the site with the template codes the rules match on, in one
        # query: the phase code through the task's own template task, the rule-set key
        # through that template task's template.
        self._rows = list(
            Task.objects.filter(phase__project=self.project).values(
                'pk', 'phase_id', 'status', 'is_mirror', 'is_not_applicable',
                'template_task__code', 'template_task__phase__code',
                'template_task__phase__template__code',
                'template_task__phase__template__version_no',
            )
        )
        self._by_pk = {row['pk']: row for row in self._rows}
        self._phase_code = {}
        versions = set()
        for row in self._rows:
            if row['template_task__phase__code']:
                self._phase_code.setdefault(row['phase_id'], row['template_task__phase__code'])
                versions.add((row['template_task__phase__template__code'],
                              row['template_task__phase__template__version_no']))
        # One template version on the site, or none: a mixed site is not something
        # attach produces, and guessing which version's rules apply would be worse
        # than applying none.
        self._rules = RULES.get(versions.pop(), ()) if len(versions) == 1 else ()
        # A SITE WITH NO TASKS AT ALL — an OPEX site not yet activated — reads the
        # rules of the ACTIVE template for its project type, the version activation
        # would attach. Only R1 can fire there (it reads the design and the challan,
        # never a task), and R1 applies whether or not the site is activated (follow-up
        # ruling 1, 20 Oct 2026). One query, paid only by a site with no tasks. A site
        # that HAS tasks keeps the rules of the version its tasks came from.
        if not self._rows:
            active = (TaskTemplate.objects
                      .filter(project_type=self.project.project_type,
                              status=TaskTemplate.ACTIVE)
                      .values_list('code', 'version_no').first())
            self._rules = RULES.get(active, ()) if active else ()

    @property
    def rules(self):
        self._load()
        return self._rules

    def phase_code(self, task_pk):
        self._load()
        row = self._by_pk.get(task_pk)
        return self._phase_code.get(row['phase_id']) if row else None

    def task_code(self, task_pk):
        self._load()
        row = self._by_pk.get(task_pk)
        return row['template_task__code'] if row else None

    def _applicable_rows(self, *, phase_code=None, task_code=None):
        self._load()
        for row in self._rows:
            if row['is_not_applicable']:
                continue
            if phase_code is not None and self._phase_code.get(row['phase_id']) != phase_code:
                continue
            if task_code is not None and row['template_task__code'] != task_code:
                continue
            yield row

    # ---- the facts --------------------------------------------------------------

    @property
    def design_released(self):
        if self._design_released is None:
            self._design_released = DesignAssignment.objects.filter(
                project=self.project, status=DESIGN_RELEASED).exists()
        return self._design_released

    @property
    def material_received(self):
        if self._material_received is None:
            self._material_received = first_component_received(self.project)
        return self._material_received

    # R4 AND R6: A PREDECESSOR THAT IS ENTIRELY N/A IS SATISFIED (follow-up ruling 2,
    # 20 Oct 2026). With no applicable predecessor there is nothing to wait for, and a
    # warning on every start would be one nobody could ever clear. With SOME applicable
    # predecessor, only those count, and an N/A task still never counts as started —
    # whatever status it stored before it was marked. R5's all-N/A rule is unchanged:
    # all() of nothing was already True there. A predecessor with no tasks at all
    # reads the same way as an all-N/A one.

    @property
    def installation_started(self):
        rows = list(self._applicable_rows(phase_code=PHASE_INSTALLATION))
        return not rows or any(row['status'] in STARTED_STATUSES for row in rows)

    @property
    def testing_finished(self):
        # all() of nothing is True: a phase whose every task is N/A, or that has no
        # tasks, is finished.
        return all(row['status'] == Task.DONE
                   for row in self._applicable_rows(phase_code=PHASE_TESTING_COMMISSIONING))

    @property
    def post_installation_started(self):
        rows = list(self._applicable_rows(task_code=TASK_POST_INSTALLATION_APPROVALS))
        return not rows or any(row['status'] in STARTED_STATUSES for row in rows)


def phase_rule_warnings(task, project=None, state=None):
    """The rules starting `task` now would bypass, as PhaseRule tuples, in rule order.

    Empty for a mirror (a mirror moved by its source is never an override, ruling Q8),
    for a Not Applicable task, for any site that is not OPEX, and for any site whose
    template version has no RULES entry. Does not look at the task's own status:
    whether this move IS a start is start_warnings()'s question.

    Pass `state` when evaluating many tasks on one site, so the facts are read once.
    """
    if task.is_mirror or task.is_not_applicable:
        return []
    if state is None:
        project = project or task.phase.project
        if project.project_type != 'OPEX':
            return []
        state = SiteRuleState(project)
    elif state.project.project_type != 'OPEX':
        return []
    phase_code = state.phase_code(task.pk)
    task_code = state.task_code(task.pk)
    hits = []
    for rule in state.rules:
        if rule.applies_to == APPLIES_TO_TASK:
            applies = task_code in rule.targets
        elif rule.applies_to == APPLIES_TO_PHASE:
            applies = phase_code in rule.targets
        else:
            applies = False
        if applies and not getattr(state, rule.satisfied_when):
            hits.append(rule)
    return hits


def is_first_start(task, new_status):
    """True when moving `task` (its in-memory, pre-change status) to `new_status` is the
    first time the task's work starts.

    Not Started -> In Progress is. Not Started -> Blocked is not a start and is not
    checked. Blocked -> In Progress is a first start when the task has never been In
    Progress or Done before (ruling Q1) — otherwise Not Started -> Blocked -> In Progress
    would walk past every rule. That history is read from StatusTransition, which
    records every Task status change since 31 Aug 2026; the first OPEX task was created
    on 6 Sep 2026, so for OPEX the ledger is complete.
    """
    if new_status not in STARTED_STATUSES:
        return False
    if task.status == Task.NOT_STARTED:
        return True
    if task.status == Task.BLOCKED:
        return not StatusTransition.objects.filter(
            subject_type=SUBJECT_TASK, subject_id=task.pk,
            to_status__in=STARTED_STATUSES,
        ).exists()
    return False


def start_warnings(task, new_status, project):
    """The chokepoint's question: the rules this status move bypasses, or [].

    Costs nothing on a Residential or CAPEX site, a mirror, an N/A task or any move
    that is not a start — those return before the first query.
    """
    if project.project_type != 'OPEX' or task.is_mirror or task.is_not_applicable:
        return []
    if not is_first_start(task, new_status):
        return []
    return phase_rule_warnings(task, project=project)


def dc_creation_warnings(project):
    """R1: the rules creating a delivery challan on `project` now would bypass, or []."""
    if project.project_type != 'OPEX':
        return []
    state = SiteRuleState(project)
    return [rule for rule in state.rules
            if rule.applies_to == APPLIES_TO_CHALLAN
            and not getattr(state, rule.satisfied_when)]


def record_phase_rule_overrides(project, actor, rules, *, task=None, challan=None,
                                reason='', may_explain=False):
    """Write one PhaseRuleOverride per bypassed rule. THE ONLY WRITER OF THESE ROWS.

    MUST be called inside the caller's transaction.atomic(), the one that holds the
    start itself, so the start and its record commit together or not at all.

    `reason` is honoured only when `may_explain` — the caller's answer to
    user_can_manage_project() for `actor`. Then the row is written already explained;
    a blank reason, or anybody else, writes it awaiting the PM's reason (rulings Q5).

    THE PROJECT ROW IS LOCKED first, for two reasons. The "awaiting count goes 0 -> 1"
    test below must not be answered by two starts at once, or both would notify; and
    the already-recorded check must not race a second start of the same task, or the
    loser would hit the unique constraint and turn a start into an error page.

    The ActivityLog lines and the one notification run on commit, after the primary
    write, and never for a transaction that rolled back.
    """
    if not rules:
        return []
    reason = (reason or '').strip() if may_explain else ''
    Project.objects.select_for_update().filter(pk=project.pk).first()
    had_awaiting = PhaseRuleOverride.objects.filter(
        project=project, explained_at__isnull=True).exists()
    # Rules already recorded against this subject: skipped rather than refused, so a
    # duplicate never becomes an IntegrityError on a path that must not refuse.
    subject = {'task': task} if task is not None else {'delivery_challan': challan}
    recorded = set(PhaseRuleOverride.objects.filter(**subject)
                   .values_list('rule_code', flat=True))
    now = timezone.now()
    rows = []
    for rule in rules:
        if rule.code in recorded:
            continue
        rows.append(PhaseRuleOverride.objects.create(
            project=project, task=task, delivery_challan=challan,
            rule_code=rule.code, rule_set=rule.rule_set, rule_text=rule.text,
            started_by=actor, started_at=now,
            reason=reason or None,
            explained_by=actor if reason else None,
            explained_at=now if reason else None,
        ))
    if not rows:
        return rows
    # Notify only when this write takes the site from no row awaiting a reason to some
    # (CHANGED at go-ahead: not one notice per row). A row written already explained
    # awaits nothing.
    notify = not had_awaiting and not reason
    # A lambda, not functools.partial, as in approval_notices.py and punch_points.py.
    transaction.on_commit(lambda: _after_overrides_written(project, actor, rows, notify))
    return rows


def _subject_label(row):
    if row.delivery_challan_id:
        return f"Delivery challan {row.delivery_challan.dc_number}"
    return row.task.task_name


def _after_overrides_written(project, actor, rows, notify):
    for row in rows:
        log_activity(
            project, actor,
            f"Started early ({row.rule_text}): {_subject_label(row)}",
            entity_type='PhaseRuleOverride', entity_id=row.pk,
            action_code='phase_rule_override',
        )
        if row.explained_at is not None:
            log_activity(
                project, actor,
                f"Gave the reason for starting early: {_subject_label(row)} — {row.reason}",
                entity_type='PhaseRuleOverride', entity_id=row.pk,
                action_code='phase_rule_override_explained',
            )
    if not notify:
        return
    first = rows[0]
    message = (
        f"{project.project_id}: work started out of sequence — {_subject_label(first)} "
        f"({first.rule_text}). The site's PM side needs to give the reason, on the "
        f"site page under Started early."
    )
    link = reverse('project_overview', kwargs={'project_id': project.project_id}) + '#started-early'
    for recipient in project_managers(project):
        # The person who started the work is not notified of their own start (ruling
        # Q6), even when they are one of the site's managers.
        if recipient.pk == actor.pk:
            continue
        send_notification(
            recipient=recipient, message=message, channels=['in_app'], link=link,
            template=T_AWAITING_REASON, related_project=project, actor=actor,
        )


# Outcomes of explain_phase_rule_override(), so the view words each one once.
EXPLAINED = 'explained'
ALREADY_EXPLAINED = 'already_explained'
BLANK_REASON = 'blank_reason'


def explain_phase_rule_override(override, reason, profile):
    """Write the PM side's reason onto one override. THE ONLY WRITER OF THE REASON.

    The caller has already decided `profile` may (user_can_manage_project).

    filter().update() on `explained_at IS NULL`, not save(): two managers explaining
    the same row at once both reach this line, and only the first update matches a
    row — the second updates nothing and is told the row was already explained. That
    conditional update is also what makes the row append-only after its one
    explanation; save() itself refuses any existing row.
    """
    reason = (reason or '').strip()
    if not reason:
        return BLANK_REASON
    updated = PhaseRuleOverride.objects.filter(
        pk=override.pk, explained_at__isnull=True,
    ).update(reason=reason, explained_by=profile, explained_at=timezone.now())
    if not updated:
        return ALREADY_EXPLAINED
    log_activity(
        override.project, profile,
        f"Gave the reason for starting early: {_subject_label(override)} — {reason}",
        entity_type='PhaseRuleOverride', entity_id=override.pk,
        action_code='phase_rule_override_explained',
    )
    return EXPLAINED


# ---- readers -------------------------------------------------------------------

def overrides_for_project(project):
    """Every override on the site, newest first, with what the overview card prints."""
    return list(
        PhaseRuleOverride.objects.filter(project=project)
        .select_related('task', 'delivery_challan', 'started_by__user', 'explained_by__user')
    )


def overrides_for_task(task):
    return list(
        PhaseRuleOverride.objects.filter(task=task)
        .select_related('started_by__user', 'explained_by__user')
        .order_by('started_at', 'pk')
    )


def override_counts_by_task(task_pks):
    """{task pk: (overrides, awaiting reason)} for the given tasks, in ONE query — the
    task-row chip. Tasks with no override are absent."""
    counts = {}
    rows = (PhaseRuleOverride.objects.filter(task_id__in=task_pks)
            .values_list('task_id', 'explained_at'))
    for task_id, explained_at in rows:
        total, awaiting = counts.get(task_id, (0, 0))
        counts[task_id] = (total + 1, awaiting + (explained_at is None))
    return counts
