"""
OPEX design workflow views — Part 2: survey upload, allocation, due-date handshake,
and the blocked flag. Part 3: Arka submission and verdict, CAD upload, BOQ entry
and the artifact-pairing rules that bind them together. Part 4: QC review, the
attempt lifecycle, the Design Head's deputy, PM change requests and release.

A separate module from views.py (which is ~9,000 lines) because this is a self-contained
new subsystem; urls.py imports it alongside `views`. No existing view is modified.

STATUS TRANSITIONS LIVE HERE, NOT ON THE MODEL. Part 1 deliberately left the models
inert — no save() override, no signal, no state machine. Every status change in this
module is an explicit assignment in a view, inside a transaction, next to the
permission check that authorises it.

OPEX ONLY. Every entry point re-checks `project_type == 'OPEX'`; Residential design work
continues to run on its six design Task rows with PM-owned approval and is not reachable
from here.
"""
import json
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, OuterRef, Prefetch, Q, Subquery, Sum
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from .decorators import login_required
from .design_analytics import (
    METRIC_GROUPS, OPTIONAL_METRICS, catalogue_for_display,
    compute as compute_analytics, selected_metric_keys,
)
from .design_metrics import (
    STAGE_LABELS, effective_commitment, pending_extension, tender_metrics,
)
from .utils import design_due_date, record_transition
# Prompt 3.1b-3 — the PM gate's four in-app notifications. See the block above
# design_gate_next_actors() before adding a fifth.
from .notifications import send_notification
from .design_storage import (
    DesignStorageError, build_design_path, get_design_file_url, upload_design_file,
    validate_cad_zip,
)
# Part 12 — the catalogue screen reuses the Admin screen's form class unchanged rather
# than declaring a second one. forms.py imports only from .models, so this adds no cycle.
from .forms import BOQItemMasterForm, check_typed_date
from .models import (
    # Session E — the Design mirror derivation. `Task` is read and written ONLY by
    # apply_mirror_status() below, never by a view in this module: the design workspace
    # has no business touching execution rows and this import is not a licence to start.
    Task, REASON_MIRROR_DERIVED,
    # The delivery mirror derivation. `DCLineItem` is READ here and never written —
    # sync_delivery_mirrors() derives task state FROM delivery, never the reverse, and
    # no DC or GRN behaviour is reachable from this module. `_dc_item_severity` is
    # imported rather than re-spelled so that "a line arrived in full and undamaged"
    # has exactly one definition in the codebase; see that function and the docstring
    # of sync_delivery_mirrors().
    DCLineItem, DC_CATEGORY_TO_MIRROR_CODE, _dc_item_severity,
    Program, Project, UserProfile, BOQ, BOQItem, DesignAssignment, DueDateCommitment,
    DesignAttempt, ArkaSubmission, DesignFile, DesignChangeRequest, log_activity,
    SiteGroup, SiteGroupMembership, SITE_GROUP_DRAFT, SITE_GROUP_LOCKED,
    # D-1 (prompt 1.1b). Every membership read in this module is a PROCUREMENT read,
    # and now says so at the call site rather than by being the only kind that exists.
    GROUP_TYPE_PROCUREMENT,
    # Part 10 — the ONLY row this session's screen writes.
    DesignAnalyticsPreference,
    # The display-only "who owes the next move" derivation, beside the status
    # constants it is keyed on. Pure and query-free; design_head_sites() hands it the
    # three prefetched facts it reads. See its docstring in models.py.
    design_pending_at,
    DESIGN_AWAITING_SURVEY, DESIGN_AWAITING_ALLOCATION, DESIGN_ALLOCATED,
    DESIGN_DUE_DATE_PROPOSED, DESIGN_IN_DESIGN, DESIGN_SURVEY_RETURNED,
    DESIGN_ARKA_SUBMITTED, DESIGN_ARKA_REJECTED, DESIGN_ARTIFACTS_UPLOADED,
    DESIGN_IN_QC, DESIGN_QC_FAILED, DESIGN_RELEASED,
    DESIGN_AWAITING_HEAD_ARKA, DESIGN_AWAITING_HEAD_QC,
    # Prompt 3.1a — the PM-approval status and the "work finished" set.
    DESIGN_AWAITING_PM_APPROVAL, DESIGN_WORK_FINISHED_STATUSES,
    # The PM-rejected status, and the guards' "designer does not hold it" set.
    DESIGN_PM_REJECTED, DESIGN_NOT_WITH_DESIGNER_STATUSES,
    # The D13 prompt — the date guards' "the designer's clock has stopped" set.
    DESIGN_CLOCK_STOPPED_STATUSES,
    # Prompt 3.1b-1 — the ledger reasons for the PM's two verdicts.
    REASON_DESIGN_PM_APPROVED, REASON_DESIGN_PM_REJECTED,
    # Prompt 3.1b-2b — the Head's answers to a PM rejection: the ledger reason for
    # returning it to the PM, the attempt reason for sending it back to the designer, and
    # the ledger itself, read back by latest_design_transition().
    REASON_DESIGN_HEAD_RETURNED_TO_PM, ATTEMPT_REASON_PM_REJECTED,
    # Prompt 3.1b-2c — the Head's QC pass, now a handover to the PM rather than a release.
    REASON_DESIGN_HEAD_PASSED,
    StatusTransition, SUBJECT_DESIGN_ASSIGNMENT,
    ARKA_PENDING, ARKA_APPROVED, ARKA_REJECTED,
    QC_PENDING, QC_PASSED, QC_FAILED,
    ATTEMPT_REASON_INITIAL, ATTEMPT_REASON_QC_FAILED, ATTEMPT_REASON_PM_CHANGE_REQUEST,
    # Part 4.6 — the Design Head's triage verdict on a PM change request.
    CHANGE_REQUEST_PENDING, CHANGE_REQUEST_ACCEPTED, CHANGE_REQUEST_REJECTED,
    DESIGN_FILE_CAD_ZIP, DESIGN_FILE_CAD_PDF, DESIGN_FILE_CAD_DWG,
    DESIGN_FILE_BOQ_EXCEL, DESIGN_FILE_BOQ_PDF, DESIGN_FILE_KIND_CHOICES,
    DESIGN_FILE_CAD_KINDS, DESIGN_FILE_LEGACY_KINDS,
    DESIGN_ERROR_CATEGORIES, DESIGN_ERROR_CATEGORY_CHOICES,
    DESIGN_ERROR_CATEGORY_LABELS,
    REDO_ARKA, REDO_CAD, REDO_BOQ, DESIGN_REDO_CHOICES,
    DEFAULT_REDO_BY_CATEGORY, default_redo_for_category,
    # Part 11 — the OPEX catalogue helpers, so the read-only BOQ panel on the review
    # screen groups the sheet exactly the way the entry screen does.
    get_opex_boq_catalogue, opex_catalogue_category_order, get_opex_mandatory_items,
    split_opex_boq_rows, group_boq_rows_by_category,
    # Part 12 — the catalogue table itself, managed by the Design Head at the foot of
    # this module. Read here directly; the helpers above stay the only readers elsewhere.
    BOQItemMaster,
)
from .permissions import (
    project_boq_is_group_locked, user_can_edit_project_boq,
    user_can_manage_site_groups, user_can_qc_design, user_can_request_design_change,
    user_can_view_design, user_can_view_site_groups, user_has_design_head_authority,
    user_is_assigned_designer, user_is_design_head, user_is_design_head_deputy,
    user_can_qc_gate_design, user_can_head_gate_design, user_is_design_qc,
    user_can_view_design_qc_dashboard, user_can_view_qc_queue,
    user_is_assigned_qc_reviewer,
    # Session B - reviewer BOQ correction. Read here only to decide whether the QC review
    # screen renders the link; the authority itself is enforced in views.boq_correct().
    user_can_correct_boq,
    # Prompt 3.1b-1 — the PM's release approval: the per-site authority, and its
    # queryset form for the queue.
    can_approve_design_release, manageable_projects_q,
    # Prompt 3.1b-3 — the same PM audience, as a list of people to notify.
    project_managers,
    # Session 3.1c-i — the design-change window (D-a) and its two audiences (D-b).
    design_change_window_open, user_can_manage_project, user_may_raise_design_change_as_scm,
)

logger = logging.getLogger(__name__)

# Statuses at which the site has not yet started design work. Reallocation is allowed
# only while the assignment is still in one of these — see design_allocate().
#
# PART 8 ADDED `in_design`, and it is not optional. Allocation now lands a site straight
# in `in_design`, so without it the Head would lose the ability to correct a
# mis-allocation the instant he made one: every allocated site would be past the
# reallocation gate before he could see the result. `in_design` means work is under way
# with no Arka submitted yet, so nothing is orphaned by moving the site to a different
# designer. `arka_submitted` and everything after it remain closed — reallocating once an
# Arka exists would leave artifacts pointing at another designer's layout.
#
# `allocated` and `due_date_proposed` are kept here for the rows that already carry them;
# no new row can reach either. See DESIGN_MODULE_DEFERRED.md.
REALLOCATABLE_STATUSES = (DESIGN_AWAITING_ALLOCATION, DESIGN_ALLOCATED,
                          DESIGN_DUE_DATE_PROPOSED, DESIGN_IN_DESIGN)

# Hosts a survey folder link may point at. A survey folder is shared corporate storage,
# and an arbitrary URL here would be a link nobody can open, a tracking redirect, or a
# phishing target sitting on the Head's own screen — so the set is closed rather than
# merely validated as well-formed.
#
# Matched on the URL's HOST, suffix-wise, so tenant sub-domains work without listing
# every one: `horizon.sharepoint.com` ends with `sharepoint.com`. The suffix test is
# anchored on a leading dot (or a whole-host equality) so `evilsharepoint.com` does NOT
# match — see _survey_link_host_allowed().
SURVEY_LINK_ALLOWED_HOSTS = (
    'drive.google.com', 'docs.google.com',
    'onedrive.live.com', '1drv.ms', 'sharepoint.com',
    'dropbox.com',
)

# ── Part 3 ─────────────────────────────────────────────────────────────────────
# The two statuses from which a designer may submit an Arka version: the first one
# from `in_design`, and every replacement from `arka_rejected`.
#
# `arka_submitted` is deliberately EXCLUDED. Once a version is submitted it is either
# awaiting a verdict (resubmitting would leave the Head reviewing a version that no
# longer exists) or approved (resubmitting would silently orphan the CAD and BOQ
# artifacts already paired to it via derived_from_arka). Either way the Head must act
# first. See design_arka_submit().
ARKA_SUBMITTABLE_STATUSES = (DESIGN_IN_DESIGN, DESIGN_ARKA_REJECTED)

# Every CAD kind, current and legacy. READ PATHS ONLY — listing, download, history.
# The progression rule deliberately does NOT use this; see PROGRESSION_CAD_KINDS.
CAD_KINDS = DESIGN_FILE_CAD_KINDS

# What satisfies "CAD is present" for the move to `artifacts_uploaded` (Part 8).
#
# Part 3's rule was "at least one CAD file present", which the two-file world made
# reasonable. It is now a VALID cad_zip and nothing else. A legacy cad_pdf on its own no
# longer advances an attempt: the whole point of the zip is that QC gets the PDF and the
# DWG together, validated at upload, and accepting a lone legacy file would reopen the
# gap the zip closes. Legacy rows stay readable — they just do not satisfy this gate,
# and no attempt still in flight has one, since the legacy kinds can no longer be
# uploaded.
PROGRESSION_CAD_KINDS = (DESIGN_FILE_CAD_ZIP,)

# Every kind design_artifact_upload() accepts. A `kind` outside this tuple is refused
# rather than silently defaulted — the whitelist is the enforcement point, not the
# select element in the template.
#
# The legacy CAD kinds are ABSENT: they are readable but no longer uploadable, which is
# enforced here rather than by hiding the option in the template.
UPLOADABLE_KINDS = (DESIGN_FILE_CAD_ZIP,
                    DESIGN_FILE_BOQ_EXCEL, DESIGN_FILE_BOQ_PDF)

KIND_LABELS = dict(DESIGN_FILE_KIND_CHOICES)

# ── Part 4 ─────────────────────────────────────────────────────────────────────
# Statuses during which a PM change request is permitted. The window OPENS at QC start
# (settled decision 3) — before that, `qc_started_at` is null and a change is a
# conversation, not a system action — and closes at release, because BOQ locking (the
# real close condition) is Part 6 and does not exist yet.
#
# `qc_failed` is included: the package is back with the designer, and a PM who spots a
# requirement change at that moment should not have to wait for the next QC round to
# say so. `released` is excluded, which is the close condition standing in for the lock.
#
# PART 9 added the two waiting-room statuses. `awaiting_head_qc` genuinely needs to be
# here: the package has passed Design QC and is sitting with the Head, `qc_started_at` is
# set and the site is not released, so the window is open by every term of the rule above.
# `awaiting_head_arka` is included for completeness and is unreachable in practice —
# `qc_started_at` is still null at the Arka stage, so the check below refuses it first.
CHANGE_REQUEST_STATUSES = (DESIGN_IN_QC, DESIGN_QC_FAILED, DESIGN_IN_DESIGN,
                           DESIGN_ARKA_SUBMITTED, DESIGN_ARKA_REJECTED,
                           DESIGN_ARTIFACTS_UPLOADED,
                           DESIGN_AWAITING_HEAD_ARKA, DESIGN_AWAITING_HEAD_QC)


# ── Part 9 ─────────────────────────────────────────────────────────────────────
# THE TWO GATES, AND THE FOUR CHECKS EVERY VERDICT ENDPOINT MAKES.
#
# Design QC reviews first, the Design Head second. Both the Arka and the QC package go
# through both gates, and all four verdict endpoints enforce the same four conditions
# server-side, so a direct POST is refused exactly as a hidden button would have been:
#
#   1. the actor holds the correct FLAG for that gate      permissions.user_can_*_gate_design
#   2. the actor is not assignment.assigned_to             (same helpers — decision 3)
#   3. the actor did not record the OTHER gate's verdict   _other_gate_actor_conflict()
#      on THIS artifact                                    (decision 2)
#   4. the PRECEDING gate has passed                       the per-artifact target resolvers
#
# Checks 1 and 2 are about a user and live in permissions.py. Checks 3 and 4 are about a
# ROW and live here, because they need the ArkaSubmission or DesignAttempt in hand.
#
# WHY CHECK 3 EXISTS AT ALL: one person holding both flags could otherwise clear a site
# with two clicks, and two clicks by one person is not a second gate. The refusal is per
# ARTIFACT, not per user — a dual-flag holder may still record the Head verdict on a site
# whose QC verdict somebody else recorded, which is the normal way a Head with the QC flag
# actually works.
#
# STATUS AFTER A HEAD ARKA APPROVAL IS `arka_submitted`, NOT a new value. Part 3 settled
# that there is no status for "Arka approved, artifacts outstanding" — the verdict on the
# current Arka disambiguates, and design_metrics._classify() already splits the two. Part 9
# keeps that exactly: QC approval moves the site to `awaiting_head_arka`, and the Head's
# approval returns it to `arka_submitted`, now carrying head_verdict='approved'. The site
# is not going backwards; `awaiting_head_arka` means strictly "the Head has not ruled", so
# the site must leave it the moment he does.


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _opex_site(project_id):
    """Fetch a non-deleted OPEX site or 404. Residential and CAPEX are unreachable
    through every view in this module."""
    project = get_object_or_404(Project, project_id=project_id, is_deleted=False)
    if project.project_type != 'OPEX':
        raise Http404('Design workflow applies to RESCO sites only.')
    return project


def _get_or_create_assignment(project):
    """The DesignAssignment is created lazily by the first survey upload, so seeded or
    imported sites do not all carry an empty row from day one."""
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None:
        assignment = DesignAssignment.objects.create(project=project)
    return assignment


def _arka_matches_status(arka, status):
    """Whether the current Arka `arka` is in the state `status` says it is in. STRICT.

        arka_submitted      QC and Head both pending (Design QC owes the verdict), OR both
                            approved (the "artifacts outstanding" state of the Part 9 note)
        awaiting_head_arka  QC approved, Head pending

    Every other combination, a missing Arka included, is False. Through the product a
    verdict cannot land while the site is `survey_returned`: _verdict_target() and
    _head_verdict_target() both test the status first. A mismatch here therefore means an
    out-of-band edit, and ArkaSubmissionAdmin can make one — `verdict`, `head_verdict` and
    `is_current` are all editable there. The lift does not guess what that edit meant.
    """
    if arka is None:
        return False
    if status == DESIGN_ARKA_SUBMITTED:
        return ((arka.verdict == ARKA_PENDING and arka.head_verdict == ARKA_PENDING)
                or (arka.verdict == ARKA_APPROVED and arka.head_verdict == ARKA_APPROVED))
    if status == DESIGN_AWAITING_HEAD_ARKA:
        return arka.verdict == ARKA_APPROVED and arka.head_verdict == ARKA_PENDING
    return False


def _arka_status_before_hold(assignment):
    """The Arka review status this site's current Design Hold was taken from, if lifting it
    should RESTORE that status. None means "derive, as before". §D18.

    THE HOLD'S OWN LEDGER ROW is the evidence: design_mark_blocked() writes through
    apply_design_status(), so a hold placed since fc24728 (5 Sep 2026, migration 0079) left
    a StatusTransition to `survey_returned` whose `from_status` is the status it interrupted.
    Read through latest_design_transition(), as that function's docstring asked, in one
    query that also reads the site's LATEST ledger row of any kind.

    FOUR ANSWERS, in this order:

      1. THE LATEST LEDGER ROW IS NOT THE HOLD -> None, with a warning. Either no row exists
         (a hold placed before the ledger) or the latest row moved the site somewhere else,
         so the hold now being lifted has no row of its own. Using an OLDER hold's row would
         restore a status this hold never interrupted. The lift is never refused.
      2. THE HOLD WAS TAKEN FROM A STATUS WHERE A HOLD IS NOW REFUSED -> None, with a
         warning. Only `in_qc`, `artifacts_uploaded` and `awaiting_head_qc` can carry such a
         ledgered row: the ledger began after the refusal at `released` (57ae66f) and
         before §D13's (969eed4), and `awaiting_pm_approval` and `pm_rejected` were
         refused by the commits that added them (c212043, 9104ee7). Deriving returns such a
         site to `in_design` on the same
         attempt, which is the §D13 defect; it is logged for repair, not repeated silently.
      3. THE HOLD WAS TAKEN FROM ANYTHING BUT THE TWO ARKA STATUSES -> None, silently. The
         derivation reads the current rows and is right for them (a date approved during
         the hold, say), where the stored from-status could be stale.
      4. THE CURRENT ARKA DOES NOT MATCH THAT STATUS -> None, with a warning (see
         _arka_matches_status). Otherwise, the from-status.
    """
    hold = (DesignAssignment.objects.filter(pk=assignment.pk)
            .annotate(hold_from=latest_design_transition(
                          'from_status', to_status=DESIGN_SURVEY_RETURNED),
                      last_to=latest_design_transition('to_status'))
            .values('hold_from', 'last_to')
            .first())
    site = assignment.project.project_id

    if hold is None or hold['last_to'] != DESIGN_SURVEY_RETURNED:
        logger.warning('Design Hold on %s has no ledger row of its own (a pre-ledger hold); '
                       'lifting it to the derived status (§D18 R3).', site)
        return None

    from_status = hold['hold_from']
    if from_status in DESIGN_NOT_WITH_DESIGNER_STATUSES:
        logger.warning('Design Hold on %s was taken from %s, where a hold has been refused '
                       'since §D13 (only in_qc, artifacts_uploaded or awaiting_head_qc can '
                       'carry such a row): pre-§D13 hold; needs repair. Lifting it to the '
                       'derived status.', site, from_status)
        return None

    if from_status not in (DESIGN_ARKA_SUBMITTED, DESIGN_AWAITING_HEAD_ARKA):
        return None

    arka = _current_arka(_current_attempt(assignment))
    if not _arka_matches_status(arka, from_status):
        state = (f'v{arka.version} qc={arka.verdict} head={arka.head_verdict}'
                 if arka is not None else 'missing')
        logger.warning('Design Hold on %s was taken from %s, but the current Arka is %s; '
                       'lifting it to the derived status (§D18 R2).', site, from_status, state)
        return None

    return from_status


def _status_after_unblock(assignment):
    """Status to restore when the Head clears a Design Hold by uploading a replacement
    survey or recording a survey folder link. THE ONE PLACE THAT DECIDES; both lift views
    call it.

    §D18 — A HOLD TAKEN FROM AN ARKA REVIEW STATUS RESTORES THAT STATUS. Until 17 Sep 2026
    every lift was derived, so a hold at `arka_submitted` or `awaiting_head_arka` came back
    at `in_design` on the same attempt: the pending verdict could no longer be recorded,
    the Arka left the review queue, and the designer's only way on was a resubmission that
    stood the old version down with its verdict pending forever. When the hold's ledger row
    and the current Arka both say the site was in review, it goes back into review — see
    _arka_status_before_hold() for the conditions and its three logged fallbacks.

    The two restorable statuses are returned by NAME below, not as the value read from the
    ledger, so tests_design_pm_gate_live's parse can still resolve every status a lift can
    write.

    OTHERWISE DERIVED, exactly as before — Part 2 adds no schema. The prior state is
    recoverable from the rows that already exist: no designer means the site never left the
    allocation queue; an APPROVED commitment means design was under way.

    PART 8 reads the approved commitment rather than the `is_current` one. A site can now
    be on hold with an extension request pending, and `is_current` would then be the
    unapproved row — restoring such a site to `due_date_proposed` would drop it back to a
    pre-design stage it had long since left. An approved date is the evidence design had
    started, and a pending request does not undo it.
    """
    before_hold = _arka_status_before_hold(assignment)
    if before_hold == DESIGN_ARKA_SUBMITTED:
        return DESIGN_ARKA_SUBMITTED
    if before_hold == DESIGN_AWAITING_HEAD_ARKA:
        return DESIGN_AWAITING_HEAD_ARKA

    if assignment.assigned_to_id is None:
        return DESIGN_AWAITING_ALLOCATION
    if _effective_commitment(assignment) is not None:
        return DESIGN_IN_DESIGN
    # No approved date has ever existed for this site — it was allocated under the Part 2
    # handshake and never got past it. Those rows are the only ones that can still land
    # here; nothing allocated under Part 8 can reach this line.
    return DESIGN_ALLOCATED


def _deny(request, message, redirect_to):
    messages.error(request, message)
    return redirect(redirect_to)


# ---------------------------------------------------------------------------
# The OPEX Design mirror — Session E (audit A-2.3)
#
# THREE FUNCTIONS, ONE PER QUESTION, and they are separate on purpose:
#
#   derive_design_mirror_state()  WHAT should the mirror read, given a design status
#   _design_mirror_task()         WHICH Task row is the Design mirror on this site
#   apply_mirror_status()         HOW a mirror's status is written at all
#   sync_design_mirror()          the composition, and the only thing callers touch
#
# The split is what lets the SAME mapping serve two callers that have nothing else in
# common — the hook at the end of apply_design_status(), and the reconcile inside
# utils.attach_opex_template(). The audit's §3.4 finding is why there are two: design
# work runs entirely BEFORE activation and nothing sequences them, so on today's data
# the hook alone would fire for zero sites and the first activation would mint a Design
# mirror reading Not Started against a source that had already moved on. A mirror that
# disagrees with its source is the single failure the whole mirror design exists to
# prevent, so the reconcile is not a nicety.
#
# THAT IS ALSO WHY THE MAPPING IS A FUNCTION OF THE STATUS AND NOT OF THE TRANSITION.
# The reconcile has no transition to read — it has a stored value and nothing else.
# ---------------------------------------------------------------------------

# The Design mirror's stable identity. `template_task.code`, not `task_name` and not
# `assigned_role`:
#
#   * `task_name` resolves uniquely TODAY and would work TODAY. It is the pre-2.4
#     pattern this codebase has already migrated away from once (see
#     ChecklistTaskLink's docstring): rewording a template label silently detaches a
#     name-keyed lookup, and a mirror hook that stops finding its row does not raise —
#     it just quietly stops updating. `code` survives the rewording by construction.
#   * `assigned_role` is NOT a discriminator. Two OPEX mirrors carry role=Design —
#     this one and As-Built Drawings — and As-Built does NOT follow DesignAssignment
#     (it is post-commissioning, and nothing in the design workspace records it).
#     Reaching for the role because it is one join shorter gets the wrong row half the
#     time.
DESIGN_MIRROR_CODE = 'DESIGN'


# The mapping. A pure function of the STORED design status — see the block comment
# above for why it cannot be a function of the transition.
#
# Coarse on purpose.
# That is OPEX spec §2 rule 7 working as intended — "portfolio metrics read the source
# object, never the mirror". The mirror answers one question, "has the PM's site got
# its design", and the per-status detail belongs to the design dashboard, which
# already has design_metrics._classify() for it.
#
# EVERY status in the vocabulary is listed, including the three that nothing writes any
# more (`allocated`, `due_date_proposed`, `qc_failed`). They remain legal `choices`
# values and rows carrying them are legal rows, so a mapping that is a pure function of
# a stored value has to answer for them. Omitting them and letting the fallback catch
# them would silently map a real state to Not Started.
DESIGN_MIRROR_STATE_MAP = {
    # ── Nothing is under way. ──────────────────────────────────────────────
    # No survey yet, or the Head holds the survey and has not handed the site to a
    # designer. `awaiting_allocation` is the load-bearing one: 82 of the 87 assignments
    # that exist sit here, so this row is what the Design mirror reads on essentially
    # the entire tender. It is Not Started because that is TRUE — nobody is doing design
    # work — and because In Progress on 82 idle sites destroys the mirror's only job,
    # which is to tell a PM whether their site's design is moving.
    #
    # The discomfort this leaves (a PM sees Phase 1 at Not Started with no action
    # available to them) is real and is NOT this mapping's to fix: it is spec §2 rule 6,
    # mirror ageing — "Design — In Progress, 41 days" — which is not built. No choice
    # here substitutes for it, and R-21 already softens it by excluding mirrors from
    # current_phase().
    DESIGN_AWAITING_SURVEY:     Task.NOT_STARTED,
    DESIGN_AWAITING_ALLOCATION: Task.NOT_STARTED,

    # ── Somebody owns the work. ────────────────────────────────────────────
    # Allocation is the line: from the moment a site has a designer, design is moving,
    # whoever is currently holding it. Which of the nine it is says WHO owes the next
    # move — the designer, the Arka reviewer, the QC reviewer, the Head — and that is a
    # design-dashboard question, not a mirror question.
    DESIGN_ALLOCATED:           Task.IN_PROGRESS,
    DESIGN_DUE_DATE_PROPOSED:   Task.IN_PROGRESS,
    DESIGN_IN_DESIGN:           Task.IN_PROGRESS,
    DESIGN_ARKA_SUBMITTED:      Task.IN_PROGRESS,
    DESIGN_AWAITING_HEAD_ARKA:  Task.IN_PROGRESS,
    DESIGN_ARKA_REJECTED:       Task.IN_PROGRESS,
    DESIGN_ARTIFACTS_UPLOADED:  Task.IN_PROGRESS,
    DESIGN_IN_QC:               Task.IN_PROGRESS,
    DESIGN_AWAITING_HEAD_QC:    Task.IN_PROGRESS,
    DESIGN_QC_FAILED:           Task.IN_PROGRESS,
    # PROMPT 3.1a — the PM approval waiting room. In Progress, the same as
    # `awaiting_head_qc`: a design with the PM is not released, and the mirror answers "has
    # the PM's site got its design" — not yet. It is listed so derive_design_mirror_state()
    # can never raise on it.
    DESIGN_AWAITING_PM_APPROVAL: Task.IN_PROGRESS,
    # The PM rejected it and it is back with the Design Head. In Progress for the same
    # reason: not released, so the PM's site has not got its design. Listed so
    # derive_design_mirror_state() cannot raise.
    DESIGN_PM_REJECTED:          Task.IN_PROGRESS,

    # ── Held. ──────────────────────────────────────────────────────────────
    # `survey_returned` is the DESIGN HOLD flag (Part 8 renamed the label, never the
    # stored value): the assigned designer has stopped over an inadequate survey, which
    # halts their clock and surfaces to the Head. Blocked is the honest reading of that,
    # and _status_after_unblock() restores the pre-hold status cleanly, so the mirror
    # comes back to In Progress by itself with no special case here.
    #
    # THIS IS THE FIRST BLOCKED TASK IN THE SYSTEM WITH NO `Issue` BEHIND IT, and that
    # was checked before it was written rather than after. The human Blocked path
    # auto-creates an Issue (views.py:4367); a derived Blocked writes only the status
    # (see apply_mirror_status). Every consumer of "blocked" was read: not one joins
    # Task.status=Blocked to Issue, or reaches an Issue through a blocked task. The
    # Issue queries are independent — they filter on project or delivery_challan, and
    # Issue.task is nullable with a SET_NULL, so an issue-less blocked row is a shape
    # the schema already permits and project-level issues already produce the converse.
    # task_detail.html guards with `{% if task_issues %}`. What DOES change is recorded
    # at the two read sites that do not exclude mirrors — see the note in
    # sync_design_mirror().
    DESIGN_SURVEY_RETURNED:     Task.BLOCKED,

    # ── Delivered. ─────────────────────────────────────────────────────────
    # NOT terminal, and that is what makes spec rule 3 — "mirrors follow their source in
    # both directions" — achievable here. One reopen route exists (a PM change request
    # accepted by the Head, design_change_request_accept -> _open_next_attempt), which
    # moves released -> in_design and therefore Done -> In Progress with no special case.
    DESIGN_RELEASED:            Task.DONE,
}


def derive_design_mirror_state(design_status):
    """The Design mirror's status, derived from `DesignAssignment.status`.

    A PURE FUNCTION and deliberately nothing more — no query, no write, no clock. Both
    callers (the hook and the reconcile) need the same answer from the same input, and
    a caller that could not test this in isolation would test it through a view.

    An unknown value raises rather than defaulting. A status this does not know is
    either a new choice somebody added without reading here, or a corrupt row;
    both are defects, and Not Started is a plausible-looking wrong answer that would
    hide either one for months. DESIGN_MIRROR_STATE_MAP lists every choice, so the only
    way to reach this line is to have added one.
    """
    try:
        return DESIGN_MIRROR_STATE_MAP[design_status]
    except KeyError:
        raise ValueError(
            f"derive_design_mirror_state(): no mirror state defined for design status "
            f"{design_status!r}. Add it to DESIGN_MIRROR_STATE_MAP — a design status "
            f"the mirror cannot read is a mirror that silently stops following its "
            f"source."
        )


def _design_mirror_task(project):
    """The Design mirror `Task` on one site, or None.

    THE LOOKUP IS THE PROJECT-TYPE GUARD, and it does the job better than an explicit
    `if project.project_type != 'OPEX'` would. `code` is unique within a TEMPLATE, not
    globally, and the Residential template has a task whose code is also 'DESIGN'
    (`(phase, code)` is the uniqueness constraint, so this is legal and expected):

        RESIDENTIAL  phase=DESIGN  code=DESIGN  label='Design'  is_mirror=False
        OPEX         phase=DESIGN  code=DESIGN  label='Design'  is_mirror=True

    TWO INDEPENDENT CLAUSES ALREADY EXCLUDE THE RESIDENTIAL ROW, and both are stated
    because either one alone would be an accident:

      * `phase__project=project` cannot cross templates at all — one project's tasks
        come from one template. This is the scoping the collision actually needs.
      * `is_mirror=True` excludes it a second time, on the property that MATTERS: the
        Residential Design task is a human's task and must never be written by a
        derivation. The Residential template has no mirrors at all, so this clause
        makes a Residential project return None here without the function ever asking
        what type it is.

    A `project_type` join on `template_task__phase__template` — the third guard
    `_checklist_task_link_for()` carries — is deliberately NOT added. That lookup is
    genuinely portfolio-wide (a ChecklistTaskLink has no project to scope by) and needs
    it; this one is handed a project and cannot be portfolio-wide by construction.
    Adding a redundant join and a comment saying it is redundant is worse than saying
    why it is not there. If this lookup is ever lifted out of a per-project context, the
    join comes with it.

    Returns None rather than raising: "no mirror" is the CORRECT and COMMON case today —
    every OPEX site with design under way is still in Draft with no tasks at all (96
    sites, 0 phases). The caller decides what to make of it.
    """
    return (Task.objects
            .filter(phase__project=project,
                    is_mirror=True,
                    template_task__code=DESIGN_MIRROR_CODE)
            .first())


def apply_mirror_status(task, new_status, actor, reason_code):
    """THE ONE PLACE A MIRROR `Task`'s STATUS IS WRITTEN. Caller owns the atomic block.

    THE EXACT INVERSE OF RUNG 0. `_apply_task_status_change()` opens by refusing every
    `is_mirror` task to every human (views.py:4240); this function opens by refusing
    every NON-mirror task to every derivation. Between them the two statements are
    total: every `Task` row in the database is writable by exactly one of them, and
    neither needs to know anything about the other's callers.

    THIS SESSION WEAKENS RUNG 0 IN NO WAY. It was considered and refused: the audit's
    task 1 re-read the refusal at current state and confirmed it is unconditional —
    the predicate is `task.is_mirror` and nothing else, and none of the five parameters
    could carry "this call is a derivation, not a person" without inventing it. A bypass
    flag would turn "read one line" into "audit every caller, forever". So the refusal
    stays absolute and the derivation gets its own door. `_apply_task_status_change()`
    is not called from here, not imported here, and is unmodified by this session.

    WHAT IT DELIBERATELY DOES NOT DO, each because the human path does it and a
    derivation must not:

      * NO `Issue`. The human Blocked branch auto-creates one; a derived Blocked writes
        only the status. Conflating a Design Hold with the project issue log is exactly
        what OPEX_task_template_spec.md:197 dropped the Punch Points mirror to avoid.
      * NO `due_date`, ever, in either direction. Mirrors seed with due_date NULL,
        calculate_due_dates() is deliberately not called for OPEX (B18), and R-20 keeps
        mirrors out of every overdue count. The human path's "In Progress requires a due
        date" guard is a rule about PEOPLE, not about rows, and carrying it across would
        make an undated mirror unable to follow its source.
      * NO notification. The payment-milestone notification is keyed on
        `is_payment_milestone`, which no mirror carries. Notifying on a derived state is
        a separate product decision.
      * NO TRANSITION TABLE. `VALID_TRANSITIONS` forbids humans Done -> In Progress to
        stop somebody un-completing their own work; a mirror has no such actor, and
        reopen (released -> in_design after an accepted change request) is a REAL and
        supported move that has to reverse the mirror out of Done. Spec rule 3 requires
        it: mirrors follow their source in BOTH directions. This is a genuine divergence
        between the two writers and it is correct — stated here rather than left for
        whoever notices the tables differ.

    IDEMPOTENT, AND THAT IS LOAD-BEARING RATHER THAN TIDY. Many consecutive design
    transitions map to the same mirror state — in_design -> arka_submitted ->
    awaiting_head_arka -> artifacts_uploaded -> in_qc is five design events that are all
    "In Progress". Writing unconditionally would produce four StatusTransition rows
    reading `In Progress -> In Progress`, and a row claiming `x -> x` is a history of
    something that did not happen (R-3). Note this guard is NOT implied by the one at
    the end of apply_design_status(): that one says the DESIGN row moved, this one says
    the DERIVED state differs. Both are needed and they answer different questions.

    `filter(pk=...).update()` rather than `save()`, for the reason apply_design_status()
    gives for itself: named columns survive a concurrent write to any column not listed.

    `record_transition()` raises where log_activity() swallows, and is called WITHOUT
    exception handling on purpose — see apply_design_status(). A mirror that moved with
    no record of why is worse than a design action that visibly failed.

    Returns True if the row was written, False if it already read `new_status`.
    """
    if not task.is_mirror:
        raise ValueError(
            f"apply_mirror_status(): task {task.pk} ({task.task_name!r}) is not a "
            f"mirror. This function is the derivation door and writes ONLY derived "
            f"rows; a human's task goes through _apply_task_status_change(), which "
            f"applies the permission checks, the transition table and the due-date "
            f"guard that a derivation deliberately skips."
        )

    if task.status == new_status:
        return False

    fields = {'status': new_status}
    # Parity with _apply_task_status_change()'s update_kwargs (views.py:4325-4332). A
    # mirror reaching Done with a NULL completed_at is invisible to every completion
    # metric in the app.
    if new_status == Task.DONE:
        fields['completed_at'] = timezone.now()
    elif task.status == Task.DONE:
        # ONE DELIBERATE DIVERGENCE FROM THE HUMAN PATH, which does not clear this. It
        # never had to: Done is near-terminal for a human, and VALID_TRANSITIONS lets
        # them leave it only for Blocked. A MIRROR reverses out of Done as normal
        # business — that is the reopen route above — and a `completed_at` left standing
        # on a row that is no longer done is a false date in a column people group by.
        fields['completed_at'] = None
    # Same two rules the human path applies: stamp on entering Blocked so the CEO aged
    # KPI can measure the wait, clear on leaving so a re-block ages from zero rather
    # than from the first one. (Mirrors are excluded from that KPI today —
    # human_owned_tasks_q() sits on the base queryset at views.py:2153 — so this column
    # is currently written and not read. It is written anyway: the day rule 6's mirror
    # ageing is built, it needs a truthful date to have been kept all along.)
    if new_status == Task.BLOCKED and task.status != Task.BLOCKED:
        fields['blocked_since'] = timezone.now()
    elif new_status != Task.BLOCKED and task.status == Task.BLOCKED:
        fields['blocked_since'] = None

    from_status = task.status
    Task.objects.filter(pk=task.pk).update(**fields)
    # Keep the in-memory row in step with the row on disk, exactly as
    # apply_design_status() does and for the same reason: a queryset update leaves the
    # instance untouched, and callers read it afterwards.
    for name, value in fields.items():
        setattr(task, name, value)

    record_transition(
        task, to_status=new_status, from_status=from_status,
        actor=actor, reason_code=reason_code,
    )
    return True


def sync_design_mirror(project, design_status, actor):
    """Bring one OPEX site's Design mirror into line with its `DesignAssignment.status`.

    THE COMPOSITION, and the only one of these four functions anything outside this
    block calls. Two callers, and they are the whole feature:

        apply_design_status()          the HOOK — a design status just moved
        utils.attach_opex_template()   the RECONCILE — a site just got its tasks, and
                                       its design had a head start of up to months

    `actor` is the SOURCE EVENT's actor, per OPEX spec §2.8: the Design Head who
    released, the designer who submitted. The ledger then reads truthfully instead of
    attributing every mirror move to a system user. The reconcile passes None, which
    record_transition() spells ACTOR_ROLE_SYSTEM — correct and not a shortcut: nobody
    moved design at activation time, the mirror is catching up to a status somebody else
    set earlier, and naming the activating PM there would be a lie in the one column
    that exists to answer "who".

    Caller owns the atomic block, matching record_transition() and apply_design_status().

    WHAT A PM SEES WHEN THIS WRITES Blocked, recorded because a derived Blocked has no
    `Issue` behind it and two read sites do not exclude mirrors:

      * views.py:1945 `blocked_subq` -> the CEO project card's red "Blocked" badge, and
        the `proj_blocked` count above it. A site on Design Hold gets that badge with no
        issue in its issue list. That is arguably right — the site IS held — but it is a
        behaviour change and it is not silent.
      * views.py:833 `blocked_tasks_list` is a DEAD context key: no template reads it
        (searched all of projects/templates/). It counts nothing and renders nothing.

      Everything else that counts or lists blocked work already excludes mirrors through
      human_owned_tasks_q() on its base queryset — the PM dashboard's blocked_tasks and
      per-project blocked_count and its five-row evidence list, the SE dashboard's
      blocked_count, the CEO blocked_open / blocked_aged_7d pair, and the daily user
      report's blocked column. The task rows themselves (_task_row.html,
      _task_detail_status.html, project_detail.html) render a red "Blocked" badge and,
      because the row is a mirror, the READ-ONLY badge instead of the status control —
      so the one thing a PM cannot do with it is exactly the thing rung 0 refuses.

    Returns True if the mirror was written, False if it was already correct or absent.
    """
    task = _design_mirror_task(project)
    if task is None:
        # NOT SILENT, AND NOT ALL ONE THING. Two very different situations reach here
        # and lumping them into one log line would bury the defect under the routine
        # case, which is precisely the trap _checklist_task_link_for() logs its own
        # fallback to avoid.
        if not Task.objects.filter(phase__project=project).exists():
            # ROUTINE AND, TODAY, UNIVERSAL. Design runs entirely before activation and
            # nothing sequences the two: 87 sites hold a DesignAssignment and 0 hold a
            # task. A design status moving on a site that has not been activated has
            # nowhere to write and nothing is wrong. This is why the reconcile exists —
            # attach_opex_template() calls this function the moment the rows appear.
            logger.debug(
                'Design mirror sync skipped: %s has no tasks yet (not activated).',
                project.project_id,
            )
        else:
            # A REAL DEFECT. The site HAS tasks, so the template was attached, and the
            # Design mirror still cannot be found by its code. Either the template was
            # re-versioned with the code changed, or the row was deleted. The mirror has
            # silently stopped following its source and nothing else would say so.
            logger.warning(
                'Design mirror NOT FOUND on %s, which has tasks: no Task with '
                'is_mirror=True and template_task__code=%r. The Design mirror has '
                'stopped following its DesignAssignment on this site.',
                project.project_id, DESIGN_MIRROR_CODE,
            )
        return False

    return apply_mirror_status(
        task,
        derive_design_mirror_state(design_status),
        actor,
        REASON_MIRROR_DERIVED,
    )


# ---------------------------------------------------------------------------
# The delivery mirrors — the SECOND derivation, and the second caller of the writer
# ---------------------------------------------------------------------------
#
# WHY THIS LIVES IN THE DESIGN MODULE, which is otherwise nothing to do with delivery.
# It is here because `apply_mirror_status()` is here, and that function is THE door
# through which a mirror `Task` is written — the exact inverse of rung 0. A second door
# opened next to the delivery views would be a second thing to audit forever. The file
# boundary is therefore the wrong one to reason about: what matters is that every
# mirror write goes through one writer, and every caller of that writer is named.
#
# Nothing else about the design module is involved. This function reads DCLineItem and
# writes a Task; it touches no DesignAssignment, no attempt, no Arka submission.

# The four mirror codes, derived from the mapping rather than restated beside it —
# a second literal list is a second thing to keep in step when T4 extends the dict.
DELIVERY_MIRROR_CODES = frozenset(DC_CATEGORY_TO_MIRROR_CODE.values())


def _delivery_mirror_tasks(project):
    """The delivery mirror `Task` rows on one site, keyed by template code.

    THE LOOKUP IS THE PROJECT-TYPE GUARD, exactly as `_design_mirror_task()` explains at
    length for its own case, and here it is stronger still: the Residential template has
    no task carrying any of these four codes AND has no mirrors at all, so a Residential
    project returns `{}` here without the function ever asking what type it is. That
    covers both halves of the fail-safe requirement — "not OPEX" and "no delivery
    mirrors" are one condition to this lookup, not two, and neither raises.

    `code__in` and not `assigned_role='SCM'`: role is not a discriminator (several OPEX
    tasks carry SCM), and `code` survives a template relabel where `label` would not.

    Returns a dict so the caller can ask for one bucket without a second query, and so a
    site missing one of the four is simply a bucket the caller skips rather than a
    KeyError. Partially-seeded sites are a real state — see the count logged below.
    """
    return {
        task.template_task.code: task
        for task in (Task.objects
                     .filter(phase__project=project,
                             is_mirror=True,
                             template_task__code__in=DELIVERY_MIRROR_CODES)
                     .select_related('template_task'))
    }


def sync_delivery_mirrors(project):
    """Derive the four OPEX delivery mirror task statuses from delivery challan lines.
    Read-derived: reflects DC/GRN state, never the reverse.

    THE SECOND DERIVATION, built to the shape `sync_design_mirror()` set: resolve the
    row, compute the state, hand both to `apply_mirror_status()`. It writes no status
    itself and knows nothing about `completed_at`, transitions or actors — that is all
    the writer's, and there is deliberately no second copy of it here.

    WHAT EACH BUCKET MEANS, across ALL challans on the site:

        no DC lines in the category            Not Started
        lines exist, any not yet GRN-confirmed In Progress
        all confirmed, every line green        Done
        all confirmed, any short or damaged    In Progress

    THE LAST ROW IS DELIBERATE AND IS THE POINT OF THE FUNCTION. A bucket reading Done
    while material is missing is worse than one reading In Progress: Done is what a PM
    scans for to stop worrying about a category. The shortfall itself is carried by the
    delivery issue (`create_delivery_issue`), not by this status — this status only
    declines to say the delivery finished, because it did not.

    `_dc_item_severity()` (models.py) IS THE PREDICATE, reused rather than restated.
    Its 'green' is precisely "received in full, no damage" and its 'amber'/'red' are
    precisely "short, or damaged, or both" — the same rule `recalculate_dc_status()`
    rolls up into the DC's own status. A second spelling of that rule here would be two
    definitions of a full delivery, and they would drift the first time either moved.
    The dependency is named in this sentence so it is visible from this end too.

    NO CHALLAN STATUS FILTER, AND THAT IS NOT AN OVERSIGHT. `DeliveryChallan` has no
    cancelled state: its four statuses are Expected / Partially Received / Received /
    Rejected, and `Rejected` is REPURPOSED (see recalculate_dc_status) to mean severe
    delivery failure — shortfall AND damage, or nothing received at all. Filtering it
    out would make the worst deliveries in the portfolio read as though nothing had
    been ordered, which inverts the meaning of the status. So a Rejected challan HOLDS
    its buckets at In Progress, and that is correct: material was ordered and did not
    properly arrive. If a real cancellation is ever added, it is excluded HERE.

    IDEMPOTENT, and it needs no guard of its own to be so. `apply_mirror_status()`
    returns False without writing when the row already reads the derived status, so a
    second call in a row produces no `StatusTransition`; and that function calls no
    `log_activity()` at all, so it produces no `ActivityLog` row either, first call or
    tenth. Both deltas are structurally zero rather than tested-to-be-zero.

    IT MOVES BACKWARDS OUT OF Done, AND MAY SAFELY DO SO. The state is recomputed from
    the live line set every time rather than accumulated, so a bucket leaves Done only
    when the lines genuinely stop supporting it — a new challan line in that category
    is unconfirmed, which is "some confirmed, some not". `apply_mirror_status()` clears
    `completed_at` on the way out for exactly this case. No forward-only clamp is
    needed and none is applied.

    ACTOR IS ALWAYS None, i.e. ACTOR_ROLE_SYSTEM, and this is where it diverges from
    `sync_design_mirror()` — which takes the source event's actor because one design
    status moved and one person moved it. A bucket here aggregates every line on every
    challan on the site: confirming a GRN on one challan can move a bucket whose other
    lines were confirmed by a different SE weeks earlier. Naming whoever happened to
    trigger the recalculation would be a lie in the one column that exists to answer
    "who". The `ActivityLog` entries at the GRN and DC endpoints already record the
    person, and that is the right place for it.

    Caller owns the atomic block, matching `apply_mirror_status()` and
    `record_transition()`.

    Returns the number of mirror rows actually written — 0 when nothing moved, when the
    project is not OPEX, and when it has no delivery mirrors. Never raises.
    """
    tasks = _delivery_mirror_tasks(project)
    if not tasks:
        # Silent for the two routine cases and loud for the one that is not, the same
        # three-way `sync_design_mirror()` makes. Lumping them together would bury a
        # broken template under the ordinary Residential call.
        # The literal, because there is no `Project.OPEX` constant — `PROJECT_TYPE_CHOICES`
        # is bare strings and every other site in this codebase spells it this way.
        if project.project_type != 'OPEX':
            # THE COMMON CASE BY FAR. Every Residential project reaches here on every
            # DC it raises — 96 of them — and nothing is wrong. Not logged at all: a
            # debug line per Residential GRN is noise that would hide the case below.
            pass
        elif not Task.objects.filter(phase__project=project).exists():
            # An OPEX site still in Draft. Delivery before activation is unusual but
            # not impossible, and there is nowhere to write.
            logger.debug(
                'Delivery mirror sync skipped: %s has no tasks yet (not activated).',
                project.project_id,
            )
        else:
            # A REAL DEFECT, and the only way to hear about it. The site HAS tasks, so
            # the template was attached, and not one of the four delivery mirrors can
            # be found by its code. Either the template was re-versioned with the codes
            # changed or the rows were deleted; either way the buckets have silently
            # stopped following the material.
            logger.warning(
                'Delivery mirrors NOT FOUND on %s, which has tasks: no Task with '
                'is_mirror=True and template_task__code in %r. The delivery mirrors '
                'have stopped following the delivery challans on this site.',
                project.project_id, sorted(DELIVERY_MIRROR_CODES),
            )
        return 0

    # ONE QUERY FOR THE WHOLE SITE, across every challan (H7: a project may carry
    # several, and one category may span them). `values_list` rather than model
    # instances because the only thing wanted is four numbers per row, and a site with
    # a long delivery history should not build a DCLineItem for each.
    lines = (DCLineItem.objects
             .filter(challan__project=project)
             .values_list('boq_category', 'received_quantity',
                          'ordered_quantity', 'damaged_quantity'))

    # Bucket by MIRROR CODE, not by category — this is what lets T4's sixteen OPEX
    # categories fan into the same four buckets with no change to the logic below.
    buckets = {code: [] for code in tasks}
    for category, received, ordered, damaged in lines:
        code = DC_CATEGORY_TO_MIRROR_CODE.get(category)
        # An unmapped category is skipped rather than raised on. During T4 the form may
        # legitimately offer a category this dict has not been extended to cover yet,
        # and a delivery that cannot be classified must not take the GRN down with it.
        if code in buckets:
            buckets[code].append(
                _dc_item_severity(received, ordered, damaged))

    written = 0
    for code, severities in buckets.items():
        if not severities:
            state = Task.NOT_STARTED
        elif all(sev == 'green' for sev in severities):
            # `_dc_item_severity()` returns None for an unconfirmed line, so this is
            # "every line confirmed AND every one of them received in full undamaged"
            # in a single test — an unconfirmed line is not green and cannot pass here.
            state = Task.DONE
        else:
            state = Task.IN_PROGRESS

        if apply_mirror_status(tasks[code], state, None, REASON_MIRROR_DERIVED):
            written += 1

    return written


def apply_design_status(assignment, new_status, actor, detail, action_code,
                        extra_fields=None, entity_type='DesignAssignment',
                        entity_id=None, reason_code='', remark=''):
    """THE ONE PLACE `DesignAssignment.status` IS WRITTEN. Session C (audit A-2.2 §5.1).

    Before this existed the field was written in eighteen places across sixteen
    functions, each setting the attribute and saving in its own way. Six of them saved
    the WHOLE ROW; the rest named `update_fields`. That spread is what made a Design
    mirror hook impossible to add without eighteen copies of it, and it is what this
    function exists to end.

    WHAT IT IS NOT. It is not a state machine and deliberately carries no transition
    table. Whether a move is legal from the current state remains each caller's own job,
    exactly as before — `_qc_guard()`, `_verdict_target()`, `_head_verdict_target()`,
    `REALLOCATABLE_STATUSES`, `ARKA_SUBMITTABLE_STATUSES` and the per-view status tests
    are the guards, and they are per-gate for good reason. Two shapes in this module
    cannot be expressed as edges between status names at all: `design_arka_head_approve`
    moves `awaiting_head_arka -> arka_submitted`, which reads backwards and is forwards
    (the Arka now carries head_verdict='approved'), and `_status_after_unblock()` returns
    one of three computed values rather than a literal. It is also not a permission
    check: those need `request`, which this never takes.

    filter().update(), NOT save(). The reason is written out at design_qc_assign() and
    applies identically here: two people acting on one site would otherwise each write a
    whole row from their own stale copy, and the loser's view of every other column would
    silently win. `update()` names its columns, so a concurrent write to any column not
    listed survives. `updated_at` is passed explicitly because `auto_now` does not fire on
    a queryset update.

    `extra_fields` is for the companion columns that must land in the SAME write as the
    status: `released_at`/`released_by`, the `survey_returned_*` triple, the survey file
    or link stamps, the allocation stamps, `current_attempt_number`. Splitting them into
    a second save would reintroduce the very torn write this function removes.

    `new_status=None` means "no transition — write the companion fields and log, and
    leave `status` alone". Two branches need it (a survey file or folder link REPLACED on
    a site whose status is already correct); passing the current status instead would
    write a value read from a stale in-memory row for no reason.

    The caller owns the transaction, matching `record_transition()` and
    `_open_next_attempt()`. `DesignAttempt` open/close is likewise NOT folded in: see
    `_open_next_attempt()`, which is its own chokepoint and calls this one.

    Returns the status the row was on before the write, so callers can message on it.
    """
    from_status = assignment.status

    fields = dict(extra_fields or {})
    if new_status is not None:
        fields['status'] = new_status
    fields['updated_at'] = timezone.now()

    DesignAssignment.objects.filter(pk=assignment.pk).update(**fields)
    # Keep the in-memory row in step with the row on disk. Callers read `assignment`
    # afterwards for their success messages and for the next step in the same
    # transaction, and a queryset update leaves the instance untouched.
    for name, value in fields.items():
        setattr(assignment, name, value)

    log_activity(assignment.project, actor, detail,
                 entity_type=entity_type,
                 entity_id=assignment.pk if entity_id is None else entity_id,
                 action_code=action_code)

    # ── MIRROR HOOK ATTACHMENT POINT — BOTH DERIVATIONS NOW ATTACHED. ──────────
    #
    # Session C left one marker here; Session D attached the first of the two things it
    # anticipated and Session E attached the second. Keeping them apart still matters,
    # because they are not the same derivation and they fail differently:
    #
    #   * THE STATE LEDGER (attached below, Session D). A `StatusTransition` row about
    #     the assignment ITSELF. It answers "who moved this site, from what, and when".
    #   * THE OPEX DESIGN MIRROR (attached below, Session E). A mirror `Task` whose
    #     state is DERIVED from this field. It writes a DIFFERENT subject through
    #     `record_transition()` and does NOT enter `_apply_task_status_change()`, which
    #     exists to refuse humans (rung 0, R-18/R-20) and is untouched by Session E.
    #     `sync_design_mirror()` is its door; see the block above that function.
    #
    # Everything either one needs is in scope right here:
    #
    #     assignment    the subject, and `assignment.project` through it
    #     from_status   the status the row was on before this write
    #     new_status    the status it is on now (None when no transition happened)
    #     actor         the UserProfile that caused it
    #
    # The mirror fires per WRITE, so a caller that writes twice would notify twice — the
    # one place that used to do that (`qc_failed`) no longer does; see design_qc_fail().
    #
    # A NO-TRANSITION CALL IS NOT A TRANSITION, and the guard below says so once for
    # both. `new_status=None` is the companion-field write documented above, and
    # `new_status == from_status` re-states the value the row already carries — neither
    # moved anything, and a row claiming `x -> x` is a history of something that did not
    # happen (R-3). It is a statement about what a transition IS, not error handling.
    #
    # DELIBERATELY NOT WRAPPED IN try/except, AND DO NOT ADD ONE. `record_transition()`
    # raises rather than swallowing, on purpose, and the caller owns the atomic block
    # this runs inside — so a ledger failure rolls the status change back with it. That
    # is the intended outcome: a design status that moved with no record of who moved it
    # is worse than an action that visibly failed and can be retried. Wrapping this in a
    # bare `except` would give you the first while looking like defensive coding.
    #
    # The `log_activity()` call above is a SEPARATE ledger and is unchanged. The feed and
    # the ledger are different things and are allowed to fail differently — that one
    # catches, this one does not (R-3).
    # ───────────────────────────────────────────────────────────────────────────
    if new_status is not None and new_status != from_status:
        record_transition(
            assignment, new_status, from_status=from_status, actor=actor,
            reason_code=reason_code, remark=remark,
        )
        # THE MIRROR, under the SAME guard and after the ledger. Same guard because the
        # question is the same one — did this design row actually move — and a mirror
        # write on a call that moved nothing would announce a transition that never
        # happened. After the ledger so that the two rows a single design move produces
        # land in the order they happened: the source first, then what follows from it.
        #
        # `assignment.status` rather than `new_status`, and they are equal here: the
        # loop above has already written the new value onto the instance, and reading
        # the FIELD makes it plain that the mirror derives from a STATE, not from this
        # transition. That is what lets attach_opex_template() call the same function
        # with nothing but a stored value in hand.
        #
        # UNGUARDED, like the ledger call above it, and for the same reason: this runs
        # inside the caller's atomic block, so a mirror that cannot be written rolls the
        # design status change back with it rather than leaving a site whose mirror
        # silently disagrees with it. Do not add a try/except here either.
        sync_design_mirror(assignment.project, assignment.status, actor)

    return from_status


def latest_design_transition(field, outer_ref='pk', **match):
    """A Subquery: `field` of the most recent StatusTransition on a DesignAssignment that
    matches `**match`, correlated to the outer query's `outer_ref`.

    THE LEDGER, READ BACK — and read the only way a list screen may: as an annotation on
    the query it already runs, so a screen of 86 rows is still ONE query, never 87. Do not
    call this per row. `outer_ref` is the ORM path from the outer model to the
    DesignAssignment's pk: 'pk' when annotating DesignAssignment itself,
    'design_assignment__pk' when annotating Project.

    Served by sttrans_subject_idx (subject_type, subject_id, occurred_at). `-pk` breaks a
    tie between two rows written in the same instant.

    FOUR LIVE CONSUMERS:
      1. design_head_sites — the PM's rejection remark and time (prompt 3.1b-2b).
      2. design_qc_review — the same, beside the Head's two actions (prompt 3.1b-2c, §D25).
      3. design_pm_approval_queue — the latest arrival at the PM, and the Head's return
         remark (prompt 3.1b-2c, §D22).
      4. _arka_status_before_hold — the status a Design Hold was taken FROM
         (field='from_status', to_status=DESIGN_SURVEY_RETURNED) and the site's latest
         ledger row, so that lifting a hold can restore an Arka review status (§D18). It
         annotates a one-row queryset rather than a list.
    """
    return Subquery(
        StatusTransition.objects
        .filter(subject_type=SUBJECT_DESIGN_ASSIGNMENT, subject_id=OuterRef(outer_ref),
                **match)
        .order_by('-occurred_at', '-pk')
        .values(field)[:1])


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------

@login_required
def design_head_sites(request, pk):
    """Design Head's working screen for one tender: every site, its status, designer,
    current due date and blocked flag, with the actions for each."""
    if not user_has_design_head_authority(request.user):
        return HttpResponseForbidden('Design Head only.')

    program = get_object_or_404(Program, pk=pk, is_deleted=False, program_type='OPEX')
    sites = (program.sites.filter(is_deleted=False)
             .select_related('design_assignment', 'design_assignment__assigned_to__user',
                             'design_assignment__qc_assigned_to__user')
             # Session B: the QC-assignable flag needs the current attempt's gate-1
             # reviewer. Prefetched rather than reached through _current_attempt() in the
             # loop, which runs its own query per site.
             #
             # TWO NESTED PREFETCHES HANG OFF IT, for the Pending-at column. Both are
             # batched across every attempt on the screen — TWO QUERIES FOR THE WHOLE
             # TABLE, not two per row — which is the only reason that column can be
             # derived here at all. Measured: SCMPILOT 26 -> 28 queries, MPUVNL
             # 266 -> 268. The +2 is flat; it does not move with the row count.
             #
             # `to_attr` on both, deliberately: a filtered Prefetch written back into the
             # relation's own cache makes `attempt.arka_submissions.all()` quietly mean
             # "the current one" for every later reader in this request. Under their own
             # names the filtered lists cannot be mistaken for the full relations, and
             # _current_arka()/_approved_arka() keep working unchanged anywhere else.
             .prefetch_related(Prefetch(
                 'design_assignment__attempts',
                 queryset=DesignAttempt.objects.prefetch_related(
                     Prefetch('arka_submissions',
                              queryset=ArkaSubmission.objects.filter(is_current=True),
                              to_attr='current_arka_rows'),
                     # Part 4.6: a raised request writes NO status, so a pending one is
                     # invisible to anything that reads `status` alone. The Pending-at
                     # column is the first thing on this screen to say so.
                     Prefetch('change_requests',
                              queryset=DesignChangeRequest.objects.filter(
                                  verdict=CHANGE_REQUEST_PENDING),
                              to_attr='pending_change_request_rows'))))
             # PROMPT 3.1b-2b — the PM's latest rejection, read off the ledger IN THIS
             # QUERY: two correlated subqueries on the same SELECT, no query per row.
             .annotate(
                 pm_rejection_remark=latest_design_transition(
                     'remark', 'design_assignment__pk',
                     reason_code=REASON_DESIGN_PM_REJECTED),
                 pm_rejected_at=latest_design_transition(
                     'occurred_at', 'design_assignment__pk',
                     reason_code=REASON_DESIGN_PM_REJECTED))
             .order_by('project_id'))

    # ONE query for the whole screen, evaluated once with list(). The per-row designer
    # exclusion below is a comprehension over this list — a queryset here would re-run
    # .exclude() per site and put the N+1 straight back.
    #
    # SESSION B.1 — the SAME people as the designer selector. Gate-1 assignment draws from
    # every active designer, not from `is_design_qc` holders, because assignment overrides
    # the open pool instead of drawing from it. The two selectors on this screen therefore
    # offer one list, built once and used twice. `designers` moved up from below the loop
    # for that reuse; the queryset itself is unchanged.
    designers = (UserProfile.objects.select_related('user')
                 .filter(role='Design', is_active=True)
                 .order_by('user__first_name', 'user__username'))
    qc_reviewers = list(designers)

    rows = []
    for site in sites:
        assignment = getattr(site, 'design_assignment', None)
        current = pending = None
        gate1_decided = False
        # The two facts design_pending_at() cannot fetch for itself. Defaults chosen so a
        # site with no assignment, or one whose attempts have not been opened yet, answers
        # "Survey" rather than raising.
        current_arka = None
        change_request_pending = False
        if assignment is not None:
            # The AGREED date, not the is_current row — a pending extension must not
            # change what this screen says the site is committed to (Part 8).
            current = _effective_commitment(assignment)
            pending = _pending_extension(assignment)
            # .all() reads the prefetch cache; filtering here would issue a fresh query.
            attempt = next(
                (t for t in assignment.attempts.all()
                 if t.attempt_number == assignment.current_attempt_number), None)
            gate1_decided = _gate1_verdict_recorded(attempt)
            if attempt is not None:
                # Both lists come from the nested Prefetch above and are already filtered
                # (is_current / verdict='pending'), so these are list reads and not
                # queries. Indexing would raise on the empty case; next() is the read that
                # says "there may be none".
                current_arka = next(iter(attempt.current_arka_rows), None)
                change_request_pending = bool(attempt.pending_change_request_rows)
        rows.append({
            'site':          site,
            'assignment':    assignment,
            'status':        assignment.status if assignment else DESIGN_AWAITING_SURVEY,
            'has_survey':    bool(assignment and assignment.has_survey_file),
            'has_link':      bool(assignment and assignment.has_survey_link),
            # Exposed on the row rather than reached through row.assignment in the
            # template, so the Survey cell and the edit form read one source.
            'survey_folder_url': (assignment.survey_folder_url if assignment else ''),
            'current_due':   current,
            'pending_extension': pending,
            'revisions':     (assignment.due_date_commitments.count() - 1) if assignment else 0,
            'is_blocked':    bool(assignment and assignment.status == DESIGN_SURVEY_RETURNED),
            # PROMPT 3.1a — drives the Head's "Change date" control, which head_sites.html
            # used to gate on the literal 'released'. design_due_date_change() refuses the
            # same set, so the screen and the view cannot disagree.
            #
            # The pm_rejected prompt moved BOTH to DESIGN_NOT_WITH_DESIGNER_STATUSES,
            # together, for exactly that reason. The D13 prompt moved both again, together, to
            # DESIGN_CLOCK_STOPPED_STATUSES — the date controls close where the clock stops,
            # and a package under review is still on it — and renamed the key from
            # `design_work_finished` to what it means.
            'clock_stopped': bool(
                assignment and assignment.status in DESIGN_CLOCK_STOPPED_STATUSES),
            # PROMPT 3.1b-2b (§D14) — the row's flag, the PM's words and the Review link
            # they drive ship together; the Head's two actions are behind that link, on
            # design_qc_review. The two annotations are the LATEST PM rejection on the
            # ledger and are shown only while the row is still pm_rejected — an earlier
            # rejection the Head has already answered is history, not the question.
            'pm_rejected':   bool(assignment and assignment.status == DESIGN_PM_REJECTED),
            'pm_rejection_remark': site.pm_rejection_remark,
            'pm_rejected_at': site.pm_rejected_at,
            'allocatable':   bool(assignment and assignment.survey_ready
                                  and assignment.status in REALLOCATABLE_STATUSES),
            # Session B — the SECOND visibility condition for the allocation block, and
            # deliberately wider than `allocatable`: naming a reviewer stays open long
            # after the designer is fixed, right up until gate 1 rules. Computed here
            # rather than in the template so the rule has one home.
            'qc_assignable': bool(assignment and not gate1_decided),
            'qc_reviewer':   assignment.qc_assigned_to if assignment else None,
            # WHO OWES THE NEXT MOVE — derived at render time, stored nowhere. The three
            # arguments are all prefetched above; the helper issues no query of its own,
            # which is what keeps this column free at any row count. A site with no
            # assignment passes None and gets "Survey", matching the Status cell's own
            # hard-coded fallback on the same row.
            'pending_at':    design_pending_at(
                assignment, current_arka, change_request_pending),
            # THE TWO SELECTORS EXCLUDE EACH OTHER'S CURRENT HOLDER, and the symmetry is
            # the point: one person cannot be both the designer and the gate-1 reviewer of
            # the same site, so whichever role is already filled removes that person from
            # the other list. Both are comprehensions over the single `qc_reviewers` list
            # built before the loop — no query per row.
            #
            # Neither is the rule, only its courtesy. _resolve_qc_reviewer() and
            # _allocate_one() each refuse the pairing on POST, so a crafted form gets the
            # same answer as the dropdown.
            'qc_choices':    [r for r in qc_reviewers
                              if not (assignment and assignment.assigned_to_id == r.pk)],
            'designer_choices': [d for d in qc_reviewers
                                 if not (assignment
                                         and assignment.qc_assigned_to_id == d.pk)],
        })

    return render(request, 'projects/design/head_sites.html', {
        'program':   program,
        'rows':      rows,
        'designers': designers,
    })


@login_required
def design_my_sites(request):
    """Designer's own queue: the sites allocated to them, with their actions."""
    profile = getattr(request.user, 'profile', None)
    if profile is None:
        return HttpResponseForbidden('No profile.')

    assignments = (DesignAssignment.objects
                   .filter(assigned_to=profile, project__is_deleted=False)
                   .select_related('project', 'project__program')
                   .order_by('project__project_id'))

    rows = []
    for assignment in assignments:
        current = _effective_commitment(assignment)
        pending = _pending_extension(assignment)
        rows.append({
            'assignment':  assignment,
            'site':        assignment.project,
            'current_due': current,
            'pending_extension': pending,
            # PART 8: the designer asks for an EXTENSION, and only once there is an
            # agreed date to extend, no request already in flight, and the site is
            # still live.
            # Prompt 3.1a: membership, not `!= DESIGN_RELEASED`. The D13 prompt moved it to
            # DESIGN_CLOCK_STOPPED_STATUSES in step with design_due_date_propose(), which it
            # mirrors: a package under review is still on the clock, so its date stays open.
            'can_request_extension': bool(
                current is not None and pending is None
                and assignment.status not in DESIGN_CLOCK_STOPPED_STATUSES),
            'awaiting':    pending is not None,
            'is_blocked':  assignment.status == DESIGN_SURVEY_RETURNED,
            # Read ONLY by the Design Hold control in my_sites.html, which needs
            # "finished" as well as "already on hold" — `is_blocked` is the latter
            # and a released row is neither. Deliberately not the dashboard's
            # `can_mark_blocked`: that flag also folds in the designer identity and
            # two pre-allocation statuses, and this screen's rows are the designer's
            # own allocated sites, so importing it here would carry conditions this
            # list has already answered. The duplication between the two screens is
            # a known finding for the consolidation session.
            'is_released': assignment.status == DESIGN_RELEASED,
            # PROMPT 3.1a — what my_sites.html's Design Hold control now reads instead of
            # `is_released`: "the designer's work is finished", which also covers a design
            # with the PM. `is_released` is left exactly as it was and stays truthful; this
            # screen no longer reads it.
            #
            # The pm_rejected prompt moved it to DESIGN_NOT_WITH_DESIGNER_STATUSES in step
            # with design_mark_blocked(), which it mirrors — a button the endpoint refuses is
            # worse than no button. The D13 prompt renamed the key from `design_work_finished`
            # to what it means, when that set gained the three review statuses.
            'not_with_designer': assignment.status in DESIGN_NOT_WITH_DESIGNER_STATUSES,
            'revisions':   assignment.due_date_commitments.count() - 1,
        })

    return render(request, 'projects/design/my_sites.html', {'rows': rows})


# ---------------------------------------------------------------------------
# 2. Survey upload / view
# ---------------------------------------------------------------------------

@login_required
def design_survey_upload(request, project_id):
    """Design Head uploads (or replaces) the survey file for an OPEX site.

    Upload happens BEFORE any row is written and the row write is inside a transaction,
    so a storage failure can never leave a DesignAssignment pointing at an object that
    does not exist.

    Replacing a survey is how the Head clears a designer's blocked flag. The status it
    returns to is decided by _status_after_unblock(): the Arka review status the hold was
    taken from where the ledger and the current Arka both support it (§D18), otherwise
    derived from the allocation and any approved due date.
    """
    project = _opex_site(project_id)
    if not user_has_design_head_authority(request.user):
        return HttpResponseForbidden('Design Head only.')
    if request.method != 'POST':
        return redirect('design_head_sites', pk=project.program_id)

    upload = request.FILES.get('survey_file')
    if not upload:
        messages.error(request, 'Please choose a survey file to upload.')
        return redirect('design_head_sites', pk=project.program_id)

    assignment = getattr(project, 'design_assignment', None)
    was_blocked = bool(assignment and assignment.status == DESIGN_SURVEY_RETURNED)

    # Allocation locks the survey EXCEPT when clearing a block — otherwise a survey
    # could be swapped underneath a designer who has already started.
    if (assignment and assignment.survey_file_path and not was_blocked
            and assignment.status not in (DESIGN_AWAITING_SURVEY, DESIGN_AWAITING_ALLOCATION)):
        messages.error(
            request,
            f'{project.project_id}: the survey cannot be replaced after allocation '
            f'unless the designer has placed the site on Design Hold.')
        return redirect('design_head_sites', pk=project.program_id)

    path = build_design_path(project.project_id, 'survey', upload.name)
    try:
        bucket, stored_path = upload_design_file(upload, path)
    except DesignStorageError as exc:
        messages.error(request, f'{project.project_id}: {exc}')
        return redirect('design_head_sites', pk=project.program_id)

    profile = request.user.profile
    with transaction.atomic():
        assignment = _get_or_create_assignment(project)
        replacing = bool(assignment.survey_file_path)

        # The file stamps ride WITH the status write, in one update() — see
        # apply_design_status(). They used to ride with a bare save() of the whole row.
        survey_fields = {
            'survey_file_bucket': bucket,
            'survey_file_path':   stored_path,
            'survey_uploaded_by': profile,
            'survey_uploaded_at': timezone.now(),
        }

        if was_blocked:
            # Clearing the block. survey_returned_at / _by / _reason are deliberately
            # LEFT IN PLACE: together with survey_uploaded_at they are the record of how
            # long the clock was stopped, without adding a schema field this session.
            restored = _status_after_unblock(assignment)
            apply_design_status(
                assignment, restored, profile,
                f'Design Hold cleared by replacement survey; status restored to '
                f'{restored}',
                'design_survey_unblocked', extra_fields=survey_fields)
            # §D18: CAD upload and BOQ completion carry no status guard, so a package can
            # become complete DURING the hold, when the progression rule cannot fire (it
            # fires only from `arka_submitted`). Evaluate it on the way back, in the same
            # transaction, exactly as _open_next_attempt() does after a carry-forward.
            if restored == DESIGN_ARKA_SUBMITTED:
                _maybe_advance_to_artifacts_uploaded(
                    assignment, _current_attempt(assignment), profile)
        elif assignment.status == DESIGN_AWAITING_SURVEY:
            apply_design_status(
                assignment, DESIGN_AWAITING_ALLOCATION, profile,
                'Survey uploaded; site ready for allocation',
                'design_survey_uploaded', extra_fields=survey_fields)
        else:
            # NO TRANSITION on this branch and there never was one — replacing a survey
            # on an already-correct site moves nothing. `None` says exactly that; passing
            # the current status would write a value read from this in-memory row for no
            # reason, and lose a concurrent move.
            apply_design_status(
                assignment, None, profile,
                'Survey file replaced',
                'design_survey_replaced', extra_fields=survey_fields)

    if was_blocked:
        messages.success(request, f'{project.project_id}: replacement survey uploaded, '
                                  f'Design Hold cleared ({assignment.get_status_display()}).')
    elif replacing:
        messages.success(request, f'{project.project_id}: survey replaced.')
    else:
        messages.success(request, f'{project.project_id}: survey uploaded — ready to allocate.')
    return redirect('design_head_sites', pk=project.program_id)


def _survey_link_host_allowed(url):
    """True when `url`'s host is one of SURVEY_LINK_ALLOWED_HOSTS.

    The suffix test is anchored on a leading dot, so `sharepoint.com` admits a tenant
    sub-domain like `horizon.sharepoint.com` but NOT the look-alike `evilsharepoint.com`
    that a bare `endswith` would let through. `urlparse().hostname` is already lower-cased
    and already excludes any port and any `user:pass@` prefix, so a crafted authority
    cannot smuggle an allowed name past this.
    """
    host = (urlparse(url).hostname or '').lower()
    return any(host == allowed or host.endswith('.' + allowed)
               for allowed in SURVEY_LINK_ALLOWED_HOSTS)


@login_required
def design_survey_link_set(request, project_id):
    """Design Head records (or edits) the survey FOLDER LINK for an OPEX site.

    The sibling of design_survey_upload. EITHER route satisfies the allocation gate
    (DesignAssignment.survey_ready); both may coexist on one site and neither supersedes
    the other. Everything below mirrors the upload view deliberately — same guards in the
    same order, same lazy row creation, same transaction boundary, same status transition
    and the same hold-clearing — so the two paths cannot drift. The differences are
    confined to URL-vs-file handling.

    THE HOLD-CLEARING EVENT MATTERS. Clearing a Design Hold emits `design_survey_unblocked`
    exactly as a replacement upload does. Hold duration is reconstructed by pairing
    `design_blocked` with `design_survey_unblocked` (design_analytics.m_hold_duration), so
    a link that cleared a hold under any other code would leave that hold counted as open
    forever.

    THE OTHER TWO CODES DELIBERATELY DIFFER from the upload's `design_survey_uploaded` /
    `design_survey_replaced`: nothing reads either of those, and logging "uploaded" for a
    link nobody uploaded would put a false statement in the one record that exists to be
    audited.

    NO REMOVAL PATH (v1), matching uploads: a wrong link is edited, subject to the
    replace-lock below.
    """
    project = _opex_site(project_id)
    if not user_has_design_head_authority(request.user):
        return HttpResponseForbidden('Design Head only.')
    if request.method != 'POST':
        return redirect('design_head_sites', pk=project.program_id)

    url = (request.POST.get('survey_folder_url') or '').strip()
    if not url:
        messages.error(request, 'Please enter the survey folder link.')
        return redirect('design_head_sites', pk=project.program_id)
    try:
        URLValidator()(url)
    except ValidationError:
        messages.error(request, f'{project.project_id}: please enter a valid survey '
                                f'folder link (a full URL, including https://).')
        return redirect('design_head_sites', pk=project.program_id)
    if not _survey_link_host_allowed(url):
        messages.error(
            request,
            f'{project.project_id}: the survey folder link must point at Google Drive, '
            f'OneDrive, SharePoint or Dropbox.')
        return redirect('design_head_sites', pk=project.program_id)

    assignment = getattr(project, 'design_assignment', None)
    was_blocked = bool(assignment and assignment.status == DESIGN_SURVEY_RETURNED)

    # Allocation locks the link EXCEPT when clearing a block — the same rule the file
    # carries, for the same reason: the folder a designer is working from must not change
    # underneath them once they have started.
    #
    # Keyed on survey_folder_url, NOT on survey_ready. Adding a FIRST link to a site that
    # already has a file is not a replace and must not be locked — nothing is being taken
    # away from the designer.
    if (assignment and assignment.survey_folder_url and not was_blocked
            and assignment.status not in (DESIGN_AWAITING_SURVEY, DESIGN_AWAITING_ALLOCATION)):
        messages.error(
            request,
            f'{project.project_id}: the survey folder link cannot be changed after '
            f'allocation unless the designer has placed the site on Design Hold.')
        return redirect('design_head_sites', pk=project.program_id)

    profile = request.user.profile
    with transaction.atomic():
        assignment = _get_or_create_assignment(project)
        replacing = bool(assignment.survey_folder_url)

        # As on the upload path: the link stamps ride WITH the status write in one
        # update(), not in a bare save() of the whole row.
        link_fields = {
            'survey_folder_url':    url,
            'survey_link_added_by': profile,
            'survey_link_added_at': timezone.now(),
        }

        if was_blocked:
            # Clearing the block. survey_returned_at / _by / _reason are deliberately
            # LEFT IN PLACE, exactly as the upload path leaves them: together with
            # survey_link_added_at they are the record of how long the clock was stopped.
            restored = _status_after_unblock(assignment)
            apply_design_status(
                assignment, restored, profile,
                f'Design Hold cleared by survey folder link; status restored to '
                f'{restored}',
                'design_survey_unblocked', extra_fields=link_fields)
            # §D18 — the progression rule, on the way back. See design_survey_upload().
            if restored == DESIGN_ARKA_SUBMITTED:
                _maybe_advance_to_artifacts_uploaded(
                    assignment, _current_attempt(assignment), profile)
        elif assignment.status == DESIGN_AWAITING_SURVEY:
            apply_design_status(
                assignment, DESIGN_AWAITING_ALLOCATION, profile,
                'Survey folder link added; site ready for allocation',
                'design_survey_link_added', extra_fields=link_fields)
        else:
            # No transition — see the matching branch in design_survey_upload().
            apply_design_status(
                assignment, None, profile,
                'Survey folder link updated',
                'design_survey_link_updated', extra_fields=link_fields)

    if was_blocked:
        messages.success(request, f'{project.project_id}: survey folder link saved, '
                                  f'Design Hold cleared ({assignment.get_status_display()}).')
    elif replacing:
        messages.success(request, f'{project.project_id}: survey folder link updated.')
    else:
        messages.success(request, f'{project.project_id}: survey folder link added — '
                                  f'ready to allocate.')
    return redirect('design_head_sites', pk=project.program_id)


@login_required
def design_survey_download(request, project_id):
    """Redirect to a freshly-signed, short-lived URL for the survey file.

    The URL is minted per request and never stored. Visibility is the ordinary project
    visibility rule, so anyone who can see the site can open its survey.
    """
    project = _opex_site(project_id)
    if not user_can_view_design(request.user, project):
        return HttpResponseForbidden('You do not have access to this site.')

    assignment = getattr(project, 'design_assignment', None)
    if assignment is None or not assignment.survey_file_path:
        raise Http404('No survey file for this site.')

    try:
        url = get_design_file_url(assignment.survey_file_bucket, assignment.survey_file_path)
    except DesignStorageError as exc:
        messages.error(request, str(exc))
        return redirect('design_my_sites')
    if not url:
        raise Http404('No survey file for this site.')
    return redirect(url)


# ---------------------------------------------------------------------------
# 3. Allocation
# ---------------------------------------------------------------------------

def _allocate_one(assignment, designer, actor, allocated_on=None):
    """Core allocation, shared by the single and bulk paths so their rules cannot drift.
    Raises ValueError with a user-facing message; callers own the transaction.

    PART 8 — THE DUE DATE IS SET HERE, AUTOMATICALLY, AND IS ALREADY APPROVED.
    This inverts the Part 2 handshake. The designer no longer proposes the initial date
    and the Head no longer approves it; the date is computed from the allocation date
    (`utils.design_due_date` — +2 calendar days, rolled to the next working day) and
    written as a DueDateCommitment that is approved at the moment it is created, with the
    allocating Head as both proposer and approver. The Head IS the approving authority,
    so recording him on both sides is accurate rather than a fudge — the alternative,
    leaving `approved_by` null, would make the row indistinguishable from a pending
    extension request everywhere downstream.

    The status therefore goes `awaiting_allocation` -> `in_design` DIRECTLY, skipping
    `allocated` and `due_date_proposed` entirely on the initial path.

    `allocated_on` is the date the due date is computed from. Bulk allocation passes ONE
    timestamp for the whole batch so every site in it gets the same date; a batch that
    straddles midnight would otherwise silently split across two due dates.

    ALLOCATION ALSO STAMPS Project.assigned_design (Part 4.5, finding F1).

    ALLOCATION ALSO STAMPS Project.assigned_design (Part 4.5, finding F1).
    Two fields name the designer of a site and they used to be free to diverge:
    `DesignAssignment.assigned_to`, which the design workflow allocates, and
    `Project.assigned_design`, which `user_can_edit_project_boq()` gates BOQ authorship
    on and which the Design dashboard keys its cards off. When they disagreed, the Head
    could allocate a site to a designer who then could not enter its BOQ and never saw
    the site on their own dashboard. Measured before this change: 3 of 5 allocated sites
    had diverged.

    Keeping them in step HERE, at the one moment a designer is chosen, is what makes the
    dashboard integration and BOQ entry work for the allocated designer. The Part 0.6
    BOQ helper is deliberately NOT modified — this feeds it the right value instead.
    Migration 0050 backfilled the rows that had already diverged.
    """
    if not assignment.survey_ready:
        raise ValueError('cannot be allocated before its survey is uploaded')
    if assignment.status == DESIGN_SURVEY_RETURNED:
        raise ValueError('is on Design Hold over an inadequate survey — '
                         'upload a replacement first')
    # SESSION B.1 — THE SELF-REVIEW RULE, ENFORCED FROM THE ALLOCATION SIDE.
    #
    # _resolve_qc_reviewer() already refuses naming the site's designer as its reviewer.
    # That covers one order of events and not the other: with no designer yet, naming a
    # reviewer is legal, and allocating that same person as designer afterwards was
    # accepted by this function because it never looked at qc_assigned_to. The result was
    # a site whose designer and gate-1 reviewer were one person — which
    # user_can_qc_gate_design() then refuses forever, since the designer exclusion is
    # tested before the assignment branch. Assigned, and unreviewable.
    #
    # REFUSES RATHER THAN CLEARING THE REVIEWER. Silently dropping a gate-1 assignment as
    # a side effect of allocating a designer would undo a decision the Head made
    # deliberately, on a screen that says nothing about it. He is told instead, and
    # chooses which of the two roles this person keeps.
    #
    # Living HERE covers the bulk path too, which is the whole reason this function
    # exists — and there it correctly aborts the batch, since a partially applied
    # allocation is worse than none.
    if assignment.qc_assigned_to_id and assignment.qc_assigned_to_id == designer.pk:
        raise ValueError(
            f'cannot be allocated to '
            f'{designer.user.get_full_name() or designer.user.username}, who is already '
            f'its Design QC reviewer — nobody reviews their own work, so change the '
            f'reviewer first')
    if assignment.status not in REALLOCATABLE_STATUSES:
        # Reallocation after work has started is out of scope for Part 2.
        raise ValueError(
            f'has already started design work (status {assignment.status}); '
            f'reallocation at this stage is not supported yet')

    now = timezone.now()
    allocated_on = allocated_on or timezone.localdate(now)
    due = design_due_date(allocated_on)

    previous = assignment.assigned_to
    if previous and previous.pk != designer.pk:
        detail = (f'Site reallocated from {previous.user.get_full_name() or previous.user.username} '
                  f'to {designer.user.get_full_name() or designer.user.username}')
        code = 'design_reallocated'
    else:
        detail = f'Site allocated to {designer.user.get_full_name() or designer.user.username}'
        code = 'design_allocated'

    # The three allocation stamps ride WITH the status in one update(). This used to be a
    # bare save() of the whole row, and it is the sharpest case of the six: the bulk path
    # can reach here with an in-memory row read some time earlier, and a Design Hold
    # placed on the same site meanwhile would have been wiped along with everything else.
    apply_design_status(
        assignment, DESIGN_IN_DESIGN, actor, f'{detail}; due {due}', code,
        extra_fields={'assigned_to': designer, 'assigned_by': actor, 'assigned_at': now})

    # The auto-approved commitment. Any earlier row is stood down first — reallocation
    # of an already-allocated site re-runs this, and the partial unique constraint
    # (one is_current row per assignment) rejects the insert otherwise.
    assignment.due_date_commitments.filter(is_current=True).update(is_current=False)
    DueDateCommitment.objects.create(
        assignment=assignment, proposed_date=due,
        proposed_by=actor, approved_by=actor, approved_at=now,
        is_current=True)

    # OPEX ONLY. Residential projects carry assigned_design from project_activate and
    # have no DesignAssignment row, so this can never touch one.
    project = assignment.project
    if project.assigned_design_id != designer.pk:
        project.assigned_design = designer
        project.save(update_fields=['assigned_design'])

    return due


def _resolve_designer(raw_id):
    """A designer must hold role='Design' and be active. Raises ValueError otherwise."""
    if not (raw_id or '').strip():
        raise ValueError('Please choose a designer.')
    try:
        return UserProfile.objects.select_related('user').get(
            pk=raw_id, role='Design', is_active=True)
    except (UserProfile.DoesNotExist, ValueError):
        raise ValueError('Selected user is not an active Design user.')


@login_required
def design_allocate(request, project_id):
    """Allocate one OPEX site to a designer. Design Head only, POST only."""
    project = _opex_site(project_id)
    if not user_has_design_head_authority(request.user):
        return HttpResponseForbidden('Design Head only.')
    if request.method != 'POST':
        return redirect('design_head_sites', pk=project.program_id)

    try:
        designer = _resolve_designer(request.POST.get('designer_id', ''))
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect('design_head_sites', pk=project.program_id)

    assignment = getattr(project, 'design_assignment', None)
    if assignment is None or not assignment.survey_ready:
        messages.error(request, f'{project.project_id} cannot be allocated before its '
                                f'survey is uploaded.')
        return redirect('design_head_sites', pk=project.program_id)

    try:
        with transaction.atomic():
            due = _allocate_one(assignment, designer, request.user.profile)
    except ValueError as exc:
        messages.error(request, f'{project.project_id} {exc}.')
        return redirect('design_head_sites', pk=project.program_id)

    messages.success(request, f'{project.project_id} allocated to '
                              f'{designer.user.get_full_name() or designer.user.username} — '
                              f'due {due}.')
    return redirect('design_head_sites', pk=project.program_id)


@login_required
def design_bulk_allocate(request, pk):
    """Allocate several sites of one tender to a single designer.

    ALL OR NOTHING: one transaction, and the first site that fails any rule aborts the
    whole batch. A partially-applied bulk allocation would be worse than none — the Head
    would have to work out which half landed.
    """
    if not user_has_design_head_authority(request.user):
        return HttpResponseForbidden('Design Head only.')
    program = get_object_or_404(Program, pk=pk, is_deleted=False, program_type='OPEX')
    if request.method != 'POST':
        return redirect('design_head_sites', pk=program.pk)

    try:
        designer = _resolve_designer(request.POST.get('designer_id', ''))
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect('design_head_sites', pk=program.pk)

    project_ids = request.POST.getlist('site_ids')
    if not project_ids:
        messages.error(request, 'Select at least one site to allocate.')
        return redirect('design_head_sites', pk=program.pk)

    actor = request.user.profile
    # ONE allocation date for the whole batch (Part 8). Computed before the loop, not
    # per site: a bulk run that crossed midnight would otherwise hand the first sites a
    # different due date from the last, and the Head has no way to see that happened.
    batch_date = timezone.localdate()
    try:
        with transaction.atomic():
            allocated = []
            due = None
            for project_id in project_ids:
                site = get_object_or_404(
                    Project, project_id=project_id, is_deleted=False,
                    project_type='OPEX', program=program)
                assignment = getattr(site, 'design_assignment', None)
                if assignment is None or not assignment.survey_ready:
                    raise ValueError(f'{site.project_id} cannot be allocated before its '
                                     f'survey is uploaded')
                due = _allocate_one(assignment, designer, actor, allocated_on=batch_date)
                allocated.append(site.project_id)
    except ValueError as exc:
        messages.error(request, f'Nothing was allocated — {exc}.')
        return redirect('design_head_sites', pk=program.pk)

    messages.success(
        request,
        f'{len(allocated)} site(s) allocated to '
        f'{designer.user.get_full_name() or designer.user.username}, due {due}: '
        f'{", ".join(allocated)}.')
    return redirect('design_head_sites', pk=program.pk)


# ---------------------------------------------------------------------------
# 3b. Gate-1 QC allocation (Session B)
#
# WHO REVIEWS THIS SITE AT GATE 1, rather than "whoever gets to it first". The field is
# nullable and null means open pool (see the note on DesignAssignment.qc_assigned_to), so
# this whole section is additive: a site nobody assigns behaves exactly as it did before.
#
# Deliberately NOT folded into _allocate_one(). Allocating a designer computes a due date,
# moves the status to `in_design` and stamps Project.assigned_design; none of that applies
# to naming a reviewer, and a shared function would have to branch on which job it was
# doing at every one of those steps.
# ---------------------------------------------------------------------------

def _gate1_verdict_recorded(attempt):
    """True once a Design QC verdict is stored on `attempt` — the point after which the
    reviewer must not be swapped.

    Reads `qc_reviewed_by_id`, not `qc_verdict`: a closed attempt keeps qc_verdict at
    'pending' forever when a PM change request ended it (see the note at the top of the
    Part 4 section), so the verdict column cannot tell "nobody ruled" from "the row was
    closed unruled". The reviewer FK is written only by design_qc_pass/design_qc_fail and
    is therefore the honest signal.

    SCOPED TO THE PACKAGE GATE, not the Arka gate. An Arka QC verdict lives on
    ArkaSubmission.reviewed_by and does not lock reassignment — today's open pool already
    lets one holder rule on the Arka and another on the package, so requiring one person
    for both would be a new rule, not a preserved one.
    """
    return attempt is not None and attempt.qc_reviewed_by_id is not None


def _resolve_qc_reviewer(raw_id, assignment):
    """The QC reviewer named by `raw_id`, or None to clear. Raises ValueError otherwise.

    ANY DESIGNER MAY BE ASSIGNED (Session B.1), not only an `is_design_qc` holder. The flag
    governs the OPEN POOL — who may pick unclaimed work up — and assignment overrides the
    pool rather than drawing from it, so requiring the flag here would refuse exactly the
    case the field exists to express. permissions.user_can_qc_gate_design() reads the two
    the same way round; the two must agree or the Head can save a state the gate refuses.

    DELEGATES TO _resolve_designer() rather than restating what a designer is. That keeps
    the existence check, the active check and the role test in ONE place for both kinds of
    allocation, and it is the reason this function contains no role string of its own —
    there is no canonical user_is_designer() helper in this codebase, and inventing a
    second one used in two places would have made the drift worse, not better.

    THE ONE RULE THAT IS GENUINELY ITS OWN is the self-review exclusion, and it is why this
    validator needs the assignment in hand when _resolve_designer() needs nothing but a pk.
    Refusing here as well as in user_can_qc_gate_design() is not belt and braces: without
    it the Head can save an assignment the verdict gate will then refuse forever — a site
    named to a reviewer who can never act on it, which reads as assigned and behaves as
    stuck.
    """
    raw_id = (raw_id or '').strip()
    # Empty is a legitimate instruction, not a missing field: it returns the site to the
    # open pool. The template's "Open pool" option posts exactly this.
    if not raw_id:
        return None
    reviewer = _resolve_designer(raw_id)
    if assignment.assigned_to_id and assignment.assigned_to_id == reviewer.pk:
        raise ValueError(
            f'{reviewer.user.get_full_name() or reviewer.user.username} is the designer '
            f'allocated to this site, and nobody reviews their own work at either gate')
    return reviewer


@login_required
def design_assign_qc(request, project_id):
    """Name (or clear) the Design QC reviewer for one OPEX site. Design Head only, POST only.

    Assignment stays editable until a gate-1 verdict is recorded on the current attempt.
    That is deliberately LATER than designer reallocation closes: a designer is fixed once
    work starts (REALLOCATABLE_STATUSES), but the reviewer is not chosen for the work, they
    are chosen for the review, and until the review has produced a verdict there is nothing
    to be inconsistent with.
    """
    project = _opex_site(project_id)
    if not user_has_design_head_authority(request.user):
        return HttpResponseForbidden('Design Head only.')
    if request.method != 'POST':
        return redirect('design_head_sites', pk=project.program_id)

    assignment = getattr(project, 'design_assignment', None)
    if assignment is None:
        messages.error(request, f'{project.project_id}: design has not started on this '
                                f'site, so there is no review to assign.')
        return redirect('design_head_sites', pk=project.program_id)

    if _gate1_verdict_recorded(_current_attempt(assignment)):
        messages.error(request, f'{project.project_id}: Design QC has already recorded a '
                                f'verdict on this attempt — the reviewer cannot be changed '
                                f'now.')
        return redirect('design_head_sites', pk=project.program_id)

    try:
        reviewer = _resolve_qc_reviewer(request.POST.get('qc_id', ''), assignment)
    except ValueError as exc:
        messages.error(request, f'{project.project_id}: {exc}.')
        return redirect('design_head_sites', pk=project.program_id)

    previous = assignment.qc_assigned_to
    if (previous.pk if previous else None) == (reviewer.pk if reviewer else None):
        messages.info(request, f'{project.project_id}: no change — the Design QC reviewer '
                               f'was already '
                               f'{"unassigned" if reviewer is None else reviewer.user.get_full_name() or reviewer.user.username}.')
        return redirect('design_head_sites', pk=project.program_id)

    actor = request.user.profile
    now = timezone.now()
    with transaction.atomic():
        # filter().update() rather than save(): two Heads assigning the same site at once
        # would otherwise each write a whole row from their own stale copy, and the loser's
        # view of every other field would silently win.
        DesignAssignment.objects.filter(pk=assignment.pk).update(
            qc_assigned_to=reviewer, qc_assigned_by=actor, qc_assigned_at=now,
            updated_at=now)

        if reviewer is None:
            detail = (f'Design QC reviewer cleared — gate 1 returns to the open pool '
                      f'(was {previous.user.get_full_name() or previous.user.username})')
            code = 'design_qc_unassigned'
        elif previous is not None:
            detail = (f'Design QC reviewer changed from '
                      f'{previous.user.get_full_name() or previous.user.username} to '
                      f'{reviewer.user.get_full_name() or reviewer.user.username}')
            code = 'design_qc_reassigned'
        else:
            detail = (f'Design QC reviewer assigned: '
                      f'{reviewer.user.get_full_name() or reviewer.user.username}')
            code = 'design_qc_assigned'
        log_activity(project, actor, detail,
                     entity_type='DesignAssignment', entity_id=assignment.pk,
                     action_code=code)

    if reviewer is None:
        messages.success(request, f'{project.project_id}: Design QC reviewer cleared — any '
                                  f'QC reviewer may now take this site.')
    else:
        messages.success(request, f'{project.project_id}: Design QC assigned to '
                                  f'{reviewer.user.get_full_name() or reviewer.user.username}.')
    return redirect('design_head_sites', pk=project.program_id)


# ---------------------------------------------------------------------------
# 4. Due date handshake
# ---------------------------------------------------------------------------

def _current_commitment(assignment):
    """The row the approve/reject views act on — i.e. the pending extension request.

    NOT the date the site is committed to. Use `_effective_commitment` for that; see the
    effective/pending note at the top of design_metrics.
    """
    return assignment.due_date_commitments.filter(is_current=True).first()


def _effective_commitment(assignment):
    """The APPROVED due date in force, ignoring any pending extension request.

    Every read surface must use this. Ordering matches design_metrics.effective_commitment
    — most recently approved wins, pk as the tiebreak.
    """
    return (assignment.due_date_commitments
            .filter(approved_at__isnull=False)
            .order_by('-approved_at', '-pk')
            .first())


def _pending_extension(assignment):
    """The extension request awaiting a verdict, or None."""
    current = _current_commitment(assignment)
    return current if (current is not None and current.approved_at is None) else None


# ---------------------------------------------------------------------------
# PART 8 — the due-date views are now the EXTENSION flow
# ---------------------------------------------------------------------------
# The initial date is no longer proposed by anybody: `_allocate_one` computes it and
# approves it in the same breath. What remains for these views to do is the case that
# still needs two parties — the designer needs MORE TIME than the automatic date gave
# them, and the Head has to agree before the commitment moves.
#
# The views are repurposed rather than replaced, deliberately: the propose/approve/reject
# trio already implements exactly this shape (insert a proposal, stand the old row down,
# let the Head rule on it), and their URLs are already linked from the designer and Head
# screens. Rewriting them would have meant new URLs and new templates for behaviour that
# was already correct.

@login_required
def design_due_date_propose(request, project_id):
    """The DESIGNER requests an EXTENSION to the agreed due date (Part 8).

    Only the assigned designer. The Head does not request extensions from himself — he
    holds the approving authority, so a Head-initiated change is `design_due_date_change`.

    A REASON IS MANDATORY. The automatic date was already granted without anyone asking
    for it; the only thing that justifies moving it is why, and an extension with a blank
    reason is unauditable a month later when the Head is asked why a site slipped.

    THE APPROVED DATE DOES NOT MOVE HERE. This inserts an unapproved row and takes over
    `is_current`, but every surface reads `_effective_commitment` — the site stays
    committed to, and overdue against, the previously approved date until the Head rules.
    """
    project = _opex_site(project_id)
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None or not user_is_assigned_designer(request.user, assignment):
        return HttpResponseForbidden('Only the designer allocated to this site may '
                                     'request an extension.')
    if request.method != 'POST':
        return redirect('design_my_sites')

    approved = _effective_commitment(assignment)
    if approved is None:
        return _deny(request, f'{project.project_id}: there is no agreed due date to extend.',
                     'design_my_sites')
    if _pending_extension(assignment) is not None:
        return _deny(request, f'{project.project_id}: an extension request is already '
                              f'awaiting the Design Head.', 'design_my_sites')
    # Prompt 3.1a: membership, not equality. The pm_rejected prompt moved the test from
    # DESIGN_WORK_FINISHED_STATUSES to DESIGN_NOT_WITH_DESIGNER_STATUSES, and the D13 prompt
    # moved it again to DESIGN_CLOCK_STOPPED_STATUSES: the question is whether the
    # designer's clock has stopped, and a package under review is still on it. The
    # refusal texts for `released` and for the PM gate are unchanged, word for word.
    if assignment.status in DESIGN_CLOCK_STOPPED_STATUSES:
        where = ('released' if assignment.status == DESIGN_RELEASED
                 else 'with the PM for approval' if assignment.status == DESIGN_AWAITING_PM_APPROVAL
                 else 'back with the Design Head after the PM rejected it')
        return _deny(request, f'{project.project_id}: this site is {where} — its due date '
                              f'can no longer be changed.', 'design_my_sites')

    reason = (request.POST.get('change_reason') or '').strip()
    if not reason:
        return _deny(request, f'{project.project_id}: a reason is required to request an '
                              f'extension.', 'design_my_sites')

    # check_typed_date, not a bare parse_date: '2026-02-30' made parse_date raise and
    # this view answer 500. The past-date and later-than-agreed checks below still run.
    proposed, date_error = check_typed_date(request.POST.get('proposed_date'))
    if proposed is None:
        return _deny(request, date_error or 'Please provide a valid date.', 'design_my_sites')
    if proposed < timezone.localdate():
        return _deny(request, 'The requested due date cannot be in the past.', 'design_my_sites')
    if proposed <= approved.proposed_date:
        return _deny(request, f'{project.project_id}: {proposed} is not later than the '
                              f'agreed date {approved.proposed_date} — an extension must '
                              f'move the date out.', 'design_my_sites')

    profile = request.user.profile
    with transaction.atomic():
        # The partial unique constraint permits only one is_current row per assignment,
        # so the approved one is stood down first, in the same transaction. It keeps
        # approved_at, which is what makes it findable as the effective date meanwhile.
        assignment.due_date_commitments.filter(is_current=True).update(is_current=False)
        DueDateCommitment.objects.create(
            assignment=assignment, proposed_date=proposed,
            proposed_by=profile, change_reason=reason, is_current=True)
        # STATUS IS NOT TOUCHED. The site stays in whatever stage the work is actually in;
        # an extension request is not a workflow stage. Moving it to due_date_proposed
        # here would rewind a site that is mid-Arka back to a pre-design stage.
        log_activity(project, profile,
                     f'Extension requested to {proposed} (agreed {approved.proposed_date}): {reason}',
                     entity_type='DesignAssignment', entity_id=assignment.pk,
                     action_code='design_due_date_extension_requested')

    messages.success(request, f'{project.project_id}: extension to {proposed} requested — '
                              f'the agreed date {approved.proposed_date} stands until the '
                              f'Design Head approves.')
    return redirect('design_my_sites')


@login_required
def design_due_date_approve(request, project_id):
    """The HEAD approves the pending extension. The new date becomes effective.

    Approving stamps `approved_at` on the pending row, which is what promotes it past the
    previously approved row in `_effective_commitment`'s ordering. Nothing else has to
    move for the new date to take effect everywhere, and the superseded row keeps its own
    `approved_at` so the history of what was agreed and when stays intact.
    """
    project = _opex_site(project_id)
    if not user_has_design_head_authority(request.user):
        return HttpResponseForbidden('Design Head only.')
    if request.method != 'POST':
        return redirect('design_head_sites', pk=project.program_id)

    assignment = getattr(project, 'design_assignment', None)
    back = project.program_id
    if assignment is None:
        messages.error(request, f'{project.project_id}: no design assignment.')
        return redirect('design_head_sites', pk=back)

    # Gated on a pending ROW, not on a status: an extension can be requested from any
    # working stage, so there is no one status that means "awaiting a due-date verdict".
    commitment = _pending_extension(assignment)
    if commitment is None:
        messages.error(request, f'{project.project_id}: there is no extension request '
                                f'awaiting approval.')
        return redirect('design_head_sites', pk=back)

    profile = request.user.profile
    with transaction.atomic():
        commitment.approved_by = profile
        commitment.approved_at = timezone.now()
        commitment.save(update_fields=['approved_by', 'approved_at'])
        # STATUS IS NOT TOUCHED — see design_due_date_propose. Part 2 moved the site to
        # in_design here because approval was what STARTED the design; under Part 8 the
        # design started at allocation and is already somewhere further on.
        log_activity(project, profile,
                     f'Extension to {commitment.proposed_date} approved',
                     entity_type='DesignAssignment', entity_id=assignment.pk,
                     action_code='design_due_date_extension_approved')

    messages.success(request, f'{project.project_id}: extension approved — the due date is '
                              f'now {commitment.proposed_date}.')
    return redirect('design_head_sites', pk=back)


@login_required
def design_due_date_reject(request, project_id):
    """The HEAD refuses the extension. The PREVIOUS commitment is restored as current.

    The refused row is stood down, not deleted — the record of what was asked for and
    turned down is the point. Restoring the previously approved row to `is_current` is
    not strictly required for the date to be right (`_effective_commitment` already
    ignores `is_current`), but leaving no current row at all would break the partial
    unique constraint's usefulness as "the row under discussion" and would make the next
    extension request harder to reason about. The two are flipped in one transaction.
    """
    project = _opex_site(project_id)
    if not user_has_design_head_authority(request.user):
        return HttpResponseForbidden('Design Head only.')
    if request.method != 'POST':
        return redirect('design_head_sites', pk=project.program_id)

    assignment = getattr(project, 'design_assignment', None)
    back = project.program_id
    if assignment is None:
        messages.error(request, f'{project.project_id}: no design assignment.')
        return redirect('design_head_sites', pk=back)

    commitment = _pending_extension(assignment)
    if commitment is None:
        messages.error(request, f'{project.project_id}: there is no extension request '
                                f'awaiting approval.')
        return redirect('design_head_sites', pk=back)

    reason = (request.POST.get('reason') or '').strip()
    profile = request.user.profile
    with transaction.atomic():
        restored = _effective_commitment(assignment)
        # Stand the refused row down BEFORE restoring, or the two would momentarily both
        # be current and the partial unique constraint would reject the second write.
        assignment.due_date_commitments.filter(is_current=True).update(is_current=False)
        if restored is not None:
            DueDateCommitment.objects.filter(pk=restored.pk).update(is_current=True)
        log_activity(
            project, profile,
            f'Extension to {commitment.proposed_date} rejected'
            + (f': {reason}' if reason else '')
            + (f' — due date remains {restored.proposed_date}' if restored else ''),
            entity_type='DesignAssignment', entity_id=assignment.pk,
            action_code='design_due_date_extension_rejected')

    messages.success(
        request,
        f'{project.project_id}: extension rejected — the due date remains '
        f'{restored.proposed_date}.' if restored else
        f'{project.project_id}: extension rejected.')
    return redirect('design_head_sites', pk=back)


@login_required
def design_due_date_change(request, project_id):
    """The HEAD revises the agreed due date directly. Auto-approved (Part 8).

    Never an in-place edit: a new DueDateCommitment is inserted with a mandatory
    change_reason and the previous row is stood down in the same transaction. The
    revision count is therefore `commitments - 1`, derived by counting rows — there is
    no counter field to drift.

    PART 8 CHANGED WHO MAY USE THIS AND WHAT IT PRODUCES.

    Under Part 2 either party could initiate and the result was a proposal the Head then
    approved. Both halves of that are wrong now. The Head holds the approving authority,
    so routing his own revision through a pending state means he approves his own request
    — a handshake with one hand. And leaving the designer on this path would give the
    system TWO ways to create a pending row, which the partial unique constraint forbids:
    the second would fail with an integrity error rather than a message. So:

        Head      -> here. The new date is approved on creation and takes effect at once.
        Designer  -> design_due_date_propose, which requests an extension for a verdict.

    Unlike an extension this may move the date EARLIER — pulling a date in is a decision
    the Head is entitled to make, and nothing downstream requires dates to be monotonic.
    """
    project = _opex_site(project_id)
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None:
        raise Http404('No design assignment for this site.')

    if not user_has_design_head_authority(request.user):
        if user_is_assigned_designer(request.user, assignment):
            return _deny(request, f'{project.project_id}: request an extension instead — '
                                  f'the Design Head approves any change to the agreed date.',
                         'design_my_sites')
        return HttpResponseForbidden('Only the Design Head may revise an agreed due date.')
    if request.method != 'POST':
        return redirect('design_head_sites', pk=project.program_id)

    def _back(msg, ok=False):
        (messages.success if ok else messages.error)(request, msg)
        return redirect('design_head_sites', pk=project.program_id)

    # Prompt 3.1a: membership, not equality. The pm_rejected prompt moved the test to
    # DESIGN_NOT_WITH_DESIGNER_STATUSES, and the D13 prompt to DESIGN_CLOCK_STOPPED_STATUSES,
    # each time with the screen flag in design_head_sites() that mirrors it. A PM-rejected
    # package's date is settled when the Head sends it back
    # (EXECUTION_MODULE_DEFERRED.md §D15), not before. The refusal texts for `released` and
    # for the PM gate are unchanged, word for word.
    if assignment.status in DESIGN_CLOCK_STOPPED_STATUSES:
        where = ('released' if assignment.status == DESIGN_RELEASED
                 else 'with the PM for approval' if assignment.status == DESIGN_AWAITING_PM_APPROVAL
                 else 'back with the Design Head after the PM rejected it')
        return _back(f'{project.project_id}: this site is {where} — its due date can no '
                     f'longer be changed.')
    if _effective_commitment(assignment) is None:
        return _back(f'{project.project_id}: there is no agreed due date to revise.')

    reason = (request.POST.get('change_reason') or '').strip()
    if not reason:
        return _back(f'{project.project_id}: a reason is required to change an agreed due date.')

    # check_typed_date, not a bare parse_date — see design_due_date_propose.
    proposed, date_error = check_typed_date(request.POST.get('proposed_date'))
    if proposed is None:
        return _back(date_error or 'Please provide a valid date.')
    if proposed < timezone.localdate():
        return _back('The new due date cannot be in the past.')

    profile = request.user.profile
    now = timezone.now()
    with transaction.atomic():
        # Stand the old row down BEFORE inserting, or the partial unique constraint
        # (one is_current row per assignment) rejects the insert. If an extension request
        # was pending, this supersedes it — the Head has ruled on the date by setting it.
        assignment.due_date_commitments.filter(is_current=True).update(is_current=False)
        DueDateCommitment.objects.create(
            assignment=assignment, proposed_date=proposed, proposed_by=profile,
            approved_by=profile, approved_at=now,
            change_reason=reason, is_current=True)
        # Status untouched: the site stays in whatever stage the work is actually in.
        log_activity(project, profile,
                     f'Agreed due date changed to {proposed}: {reason}',
                     entity_type='DesignAssignment', entity_id=assignment.pk,
                     action_code='design_due_date_changed')

    return _back(f'{project.project_id}: due date is now {proposed}.', ok=True)


# ---------------------------------------------------------------------------
# 5. Blocked flag
# ---------------------------------------------------------------------------

@login_required
def design_mark_blocked(request, project_id):
    """The allocated DESIGNER marks the site blocked on an inadequate survey.

    This stops their clock and surfaces on the Head's screen. Resolution happens by
    conversation; the flag is the record. Cleared by the Head uploading a replacement
    survey (see design_survey_upload), which restores the previous status along with the
    allocation and any approved due date.
    """
    project = _opex_site(project_id)
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None or not user_is_assigned_designer(request.user, assignment):
        return HttpResponseForbidden('Only the designer allocated to this site may flag it.')
    if request.method != 'POST':
        return redirect('design_my_sites')

    if assignment.status == DESIGN_SURVEY_RETURNED:
        return _deny(request, f'{project.project_id} is already on Design Hold.',
                     'design_my_sites')

    # A RELEASED DESIGN IS NOT RETURNABLE TO HOLD THROUGH THIS ENDPOINT.
    #
    # Design Hold means "the survey I was given is inadequate, so my clock stops".
    # After release that sentence is not true of anything: the design is finished,
    # the site is downstream in procurement (`ProcurementBatch` forms from released
    # sites), and there is no clock left to stop. What this endpoint would do to a
    # released row is not a hold — it is an undo of the release, wearing a hold's
    # name, with none of a release-reversal's consequences handled.
    #
    # THE TEMPLATES WERE THE ONLY THING PREVENTING THIS, AND ONE OF THEM DID NOT.
    # `_dashboard_design_actions.html` gates on `can_mark_blocked`, which excludes
    # DESIGN_RELEASED; `my_sites.html` gated on `not row.is_blocked`, which is only
    # "not currently on hold" and is TRUE of a released row — so the button rendered
    # and the post succeeded. A rule enforced only by which button a screen draws is
    # not enforced; this is the rule, and the template fix beside it is now the
    # second line of defence rather than the first.
    #
    # NOT A GENERAL RELEASE LOCK. This closes one route on one endpoint. Whether a
    # released design may be reopened AT ALL, and by whom and with what unwinding of
    # the downstream, is a separate question this does not answer or foreclose.
    #
    # PROMPT 3.1a — MEMBERSHIP, NOT EQUALITY. A design with the PM is as finished as a
    # released one, and a hold on it would be the same undo by the same second door; see
    # DESIGN_WORK_FINISHED_STATUSES. The refusal text for `released` is unchanged.
    #
    # The pm_rejected prompt moved the test to DESIGN_NOT_WITH_DESIGNER_STATUSES: a
    # PM-rejected package is not finished, but the designer does not hold it either, and a
    # hold on it would clear straight back to `in_design`.
    #
    # THE D13 PROMPT (13 Sep 2026) ADDED THE THREE REVIEW STATUSES to that set, closing the
    # same door where it was open on live rows — EXECUTION_MODULE_DEFERRED.md §D13. Their
    # refusal is not a permission denial, because no permission exists that would make the
    # hold right: it says why, and names the route that does the job honestly. The reviewer
    # holding the package fails it as "Survey data inadequate or incorrect", which opens
    # attempt N+1 counted as an input problem, not as the designer's rework (B-06). It names
    # the reviewer who can actually act: Design QC fails only from `in_qc` (starting review
    # from `artifacts_uploaded` first), the Head only from `awaiting_head_qc`.
    #
    # The refusal texts for `released`, for the PM gate and for `pm_rejected` are unchanged,
    # word for word.
    if assignment.status in DESIGN_NOT_WITH_DESIGNER_STATUSES:
        under_review = {
            DESIGN_ARTIFACTS_UPLOADED: ('is waiting for Design QC to start review', 'Design QC'),
            DESIGN_IN_QC:              ('is in review with Design QC', 'Design QC'),
            DESIGN_AWAITING_HEAD_QC:   ('has passed Design QC and is with the Design Head',
                                        'the Design Head'),
        }
        if assignment.status in under_review:
            where, reviewer = under_review[assignment.status]
            return _deny(request,
                         f'{project.project_id} {where}, so it cannot go on Design Hold — a '
                         f'hold pauses design work, and this package is no longer being '
                         f'designed. If the survey is inadequate, ask {reviewer} to fail the '
                         f'package with the category "Survey data inadequate or incorrect". '
                         f'It comes back to you as a new attempt, recorded as a survey '
                         f'problem, not as your rework.',
                         'design_my_sites')
        where = ('has already been released' if assignment.status == DESIGN_RELEASED
                 else 'has passed both review gates and is with the PM for approval'
                 if assignment.status == DESIGN_AWAITING_PM_APPROVAL
                 else 'was rejected by the PM and is back with the Design Head')
        return _deny(request,
                     f'{project.project_id} {where} and cannot be '
                     f'placed on Design Hold through this action.',
                     'design_my_sites')

    reason = (request.POST.get('reason') or '').strip()
    if not reason:
        return _deny(request, 'Please say what is inadequate about the survey.',
                     'design_my_sites')

    profile = request.user.profile
    with transaction.atomic():
        # The hold triple rides WITH the status in one update(); a hold recorded without
        # its reason, or a reason recorded without the hold, is not a state this row may
        # ever be in. There is deliberately no clearing path for the three — see
        # design_survey_upload().
        apply_design_status(
            assignment, DESIGN_SURVEY_RETURNED, profile,
            f'Site placed on Design Hold — survey inadequate: {reason}',
            'design_blocked',
            extra_fields={'survey_returned_at':   timezone.now(),
                          'survey_returned_by':   profile,
                          'survey_return_reason': reason})

    messages.success(request, f'{project.project_id} placed on Design Hold. The Design Head '
                              f'can see the reason and your clock is stopped.')
    return redirect('design_my_sites')


# ===========================================================================
# PART 3 — Arka, CAD, BOQ and versioning
# ===========================================================================
#
# THE ONE RULE THIS SECTION EXISTS TO ENFORCE
# -------------------------------------------
# Every CAD file records WHICH Arka version it was drawn against
# (DesignFile.derived_from_arka, NOT NULL from Part 1).
#
# THE SECOND HALF OF THAT RULE IS GONE. It used to read "and no CAD file and no BOQ
# submission may exist until the current Arka is approved". Both halves of THAT are
# now dropped, separately and for different reasons:
#
#   CAD  may be uploaded alongside the Arka submission. The pairing survives — a CAD
#        still names its Arka version — but the version no longer has to be approved.
#        A designer holding a finished drawing while two reviewers work through the
#        layout was queueing, not checking. _require_current_arka() is what is left.
#
#   BOQ  is no longer gated on the Arka at all. It never had an FK to an Arka version
#        the way CAD does — the bill is built from the shared catalogue, and what this
#        module records is a per-attempt COMPLETION STAMP, not a derivation. Gating a
#        thing that does not derive from the layout on the layout's approval was the
#        weaker half of the rule and it has been removed outright.
#
# Enforcement is still HERE, in the view, not by hiding a button: design_artifact_upload
# calls _require_current_arka() before it touches storage, so a direct POST to a bare URL
# is refused exactly as a click would be. design_boq_complete() calls neither, by design.
#
# WHY THE PAIRING STILL MATTERS: a CAD drawn against a superseded Arka is rework that QC
# has no way to detect once the versions have moved on, so the version it was built from
# is recorded at write time rather than inferred later from timestamps. Recording it is
# now the ONLY protection — see the warning in _require_current_arka() about a CAD whose
# Arka is rejected after upload.
#
# ATTEMPTS ARE OPENED LAZILY. Part 2 left `current_attempt_number` at 0 and created
# no DesignAttempt rows, so the first Arka submission opens attempt 1
# (opened_reason='initial'). Attempts 2+ are opened by a QC failure or a PM change
# request, which are Parts 4 and 5 — nothing here ever opens a second attempt.


# ---------------------------------------------------------------------------
# Part 3 helpers
# ---------------------------------------------------------------------------

def _current_attempt(assignment):
    """The attempt design work is currently happening on, or None before the first
    Arka submission. Read from `current_attempt_number` rather than "the latest row",
    so the pointer the rest of the module maintains is the single source of truth."""
    if not assignment.current_attempt_number:
        return None
    return assignment.attempts.filter(
        attempt_number=assignment.current_attempt_number).first()


def _open_first_attempt(assignment):
    """Open attempt 1 for an assignment that has none, and move the pointer.

    Callers own the transaction. Only ever creates attempt 1: a second attempt is a
    rework loop (QC failure / PM change request) and belongs to a later part.
    """
    attempt = DesignAttempt.objects.create(
        assignment=assignment, attempt_number=1,
        opened_reason=ATTEMPT_REASON_INITIAL,
    )
    assignment.current_attempt_number = 1
    assignment.save(update_fields=['current_attempt_number', 'updated_at'])
    return attempt


def _current_arka(attempt):
    """The live Arka version for an attempt. The partial unique constraint from Part 1
    guarantees there is at most one."""
    if attempt is None:
        return None
    return attempt.arka_submissions.filter(is_current=True).first()


def _approved_arka(attempt):
    """The current Arka ONLY IF it has cleared BOTH gates. This is the object CAD and BOQ
    artifacts pair to — deliberately not "the most recently approved version", so a
    superseded approval can never become the parent of a new artifact.

    PART 9 MOVED THE TEST FROM `verdict` TO `head_verdict`, and that is the whole change
    to the Part 3 gate. `verdict='approved'` now means only that Design QC passed it; an
    Arka the Head has not yet approved is not a layout anybody may build against, so CAD
    and BOQ stay locked until the second gate clears. Because the serial rule guarantees
    head_verdict can only be 'approved' after verdict is, testing the Head field alone is
    sufficient and there is no need to test both.
    """
    arka = _current_arka(attempt)
    if arka is not None and arka.head_verdict == ARKA_APPROVED:
        return arka
    return None


def _require_current_arka(attempt):
    """Return the CURRENT Arka whatever verdict it carries, or raise ValueError with the
    message the designer needs to see.

    REPLACES `_require_approved_arka()`, AND THE REPLACEMENT IS THE WHOLE POINT. The old
    helper refused every artifact write until BOTH gates had passed the current Arka.
    That rule is gone: a designer who has the CAD in hand at the moment they submit the
    Arka may upload it in the same sitting rather than holding the file on their desk
    until two reviewers get round to the layout. The waiting was never producing a better
    drawing — it was producing a queue.

    WHAT REMAINS IS THE PAIRING, NOT THE APPROVAL. `DesignFile.derived_from_arka` is still
    NOT NULL, so an Arka version must EXIST for a CAD to record itself against, and this
    helper is what guarantees one does. The recorded version is the current one at upload
    time, exactly as before — only the verdict requirement is dropped.

    WHAT THIS OPENS, said plainly rather than buried: a CAD may now be paired to an Arka
    that is later rejected. Within one attempt a rejection is answered by a NEW ARKA
    VERSION, not a new attempt, and nothing stands the already-uploaded CAD down when
    that happens — so the designer, not the system, is now responsible for re-uploading a
    drawing whose layout moved under it. `REDO_ARKA => REDO_CAD` in _posted_redo_scope()
    does NOT cover this: that rule governs the QC rework loop, and an in-attempt Arka
    rejection never reaches it. See the session report.

    BOQ COMPLETION NO LONGER CALLS THIS AT ALL. It has no FK to an Arka version and never
    did — see design_boq_complete().
    """
    if attempt is None:
        raise ValueError('no design attempt has been opened yet — submit an Arka first')
    arka = _current_arka(attempt)
    if arka is None:
        raise ValueError('no Arka has been submitted yet. A CAD records the Arka version '
                         'it was drawn against, so the Arka has to be submitted first — '
                         'it does not have to be approved')
    return arka


# ---------------------------------------------------------------------------
# Part 9 — shared gate enforcement
# ---------------------------------------------------------------------------

def _other_gate_actor_conflict(profile, other_gate_reviewer_id):
    """Settled decision 2: one person cannot record BOTH verdicts on the same artifact.

    `other_gate_reviewer_id` is the *_reviewed_by_id already stored by the opposite gate
    on this exact row. Returns True if it is this same person, which is the refusal case.

    PER ARTIFACT, NOT PER USER. Somebody holding both flags is entirely legitimate and is
    refused only the SECOND verdict on an artifact they have already ruled on — they may
    record either gate's verdict on any other site, and may record the Head verdict here
    if a different person passed it through Design QC. A flag says what you may be; this
    says what you may not do twice.

    A null reviewer (no verdict recorded at that gate yet, or a historical row backfilled
    by migration 0055 with head_reviewed_by left null) compares False and admits the
    actor — correct in both cases, because no person is being asked to agree with
    themselves.
    """
    if profile is None or other_gate_reviewer_id is None:
        return False
    return other_gate_reviewer_id == profile.pk


def _posted_error_category(request):
    """Return (category, error_message). The category is MANDATORY on every rejection and
    every failure, at BOTH gates (settled decision 7).

    Validated against DESIGN_ERROR_CATEGORIES rather than trusted, because the field is
    not CHECK-constrained (see the note on DesignAttempt.head_failure_category) — this
    function is the only thing standing between a hand-crafted POST and an uncountable
    value in a column the rework multiplier reads.

    ONE category, never several. A multi-select would let a reviewer tag a rejection with
    four causes, and a rejection with four causes cannot be counted under any of them.
    """
    category = (request.POST.get('error_category') or '').strip()
    if not category:
        return '', ('an error category is required — "rejected" on its own cannot be '
                    'counted, coached against, or told apart from a bad survey')
    if category not in DESIGN_ERROR_CATEGORIES:
        return '', 'that error category is not recognised — please choose one from the list'
    return category, ''


#: Human-readable names for the three redo targets, for messages and log lines.
_REDO_LABELS = {REDO_ARKA: 'the Arka', REDO_CAD: 'the CAD', REDO_BOQ: 'the BOQ'}


def _redo_phrase(redo):
    """"the Arka, the CAD and the BOQ" — the designer's side of a scoped failure."""
    names = [_REDO_LABELS[value] for value in DESIGN_REDO_CHOICES if value in redo]
    if len(names) <= 1:
        return names[0] if names else 'nothing'
    return f'{", ".join(names[:-1])} and {names[-1]}'


def _posted_redo_scope(request, attempt):
    """Return (redo_set, error_message) — what the failing reviewer says must be redone.

    Part 9.1. The checkboxes are the reviewer's answer to "what does the designer actually
    have to make again?", pre-ticked from the error category and freely overridable. Two
    rules are enforced server-side, because a hand-crafted POST must not be able to reach
    a state the UI cannot produce:

    AT LEAST ONE. A failure that requires nothing to be redone is not a failure — the
    designer would receive a reopened attempt with nothing to do and no way to progress it.

    REDOING THE ARKA FORCES REDOING THE CAD. Every artifact records the Arka version it
    was drawn against (DesignFile.derived_from_arka), and the whole point of that field is
    that QC never reviews a CAD produced from a superseded layout. Carrying a CAD across a
    new Arka would create exactly that. So the pair is coupled, and it is coupled here
    rather than left to the reviewer to remember.

    A missing checkbox set (an old form, a bare POST) falls back to the category's
    defaults rather than to nothing — the safe direction, and identical to what the form
    would have submitted untouched.
    """
    if 'redo_scope_submitted' not in request.POST:
        category = (request.POST.get('error_category') or '').strip()
        return set(default_redo_for_category(category)), ''

    redo = {value for value in request.POST.getlist('redo')
            if value in DESIGN_REDO_CHOICES}

    if not redo:
        return set(), ('say what the designer has to redo — a failure that requires '
                       'nothing to be re-made leaves them with a reopened attempt and '
                       'nothing to act on')

    if REDO_ARKA in redo and REDO_CAD not in redo:
        return set(), ('a new Arka means new CAD too — every drawing records the Arka '
                       'version it was made from, and QC must never review a CAD drawn '
                       'against a layout that has been replaced')

    return redo, ''


def _maybe_advance_to_artifacts_uploaded(assignment, attempt, actor):
    """Section 5 — evaluate the progression rule after every Part 3 write.

    The attempt moves `arka_submitted` -> `artifacts_uploaded` once it has ALL THREE:
    an approved current Arka, a current cad_zip, and boq_submitted_at set.

    PART 8 TIGHTENED THE CAD CONDITION from "at least one CAD file" to "a valid cad_zip".
    Validity is established at upload — a row of kind cad_zip only exists because
    `_validate_cad_zip` accepted it — so this is a presence check, not a re-validation.
    A legacy cad_pdf/cad_dwg does NOT satisfy it; see PROGRESSION_CAD_KINDS.

    Called explicitly at the end of each action rather than wired to a signal, so the
    transition is visible next to the write that could have caused it. Idempotent —
    it only fires from `arka_submitted`, so calling it twice does nothing the second
    time. Returns True if it advanced.

    Deliberately does NOT continue on to `in_qc`; handing the package to QC is Part 4.
    """
    if assignment.status != DESIGN_ARKA_SUBMITTED:
        return False
    if attempt is None or _approved_arka(attempt) is None:
        return False
    if not attempt.design_files.filter(
            kind__in=PROGRESSION_CAD_KINDS, is_current=True).exists():
        return False
    if attempt.boq_submitted_at is None:
        return False

    apply_design_status(
        assignment, DESIGN_ARTIFACTS_UPLOADED, actor,
        f'Design package complete on attempt {attempt.attempt_number} — '
        f'approved Arka, CAD and BOQ all present',
        'design_artifacts_uploaded',
        entity_type='DesignAttempt', entity_id=attempt.pk)
    return True


def _attempt_files(attempt):
    """Every DesignFile on an attempt, newest version of each kind first, with the Arka
    version it derives from preloaded — the pairing is what the screens exist to show."""
    if attempt is None:
        return []
    return list(attempt.design_files
                .select_related('derived_from_arka', 'uploaded_by__user')
                .order_by('kind', '-version'))


def _boq_provenance(attempt, arka, arka_history):
    """Which Arka version was live when the BOQ was marked complete, and whether that is
    still the current one. Returns (provenance_arka_or_None, is_stale).

    THE BOQ'S ANSWER TO `DesignFile.derived_from_arka`, AND IT IS NOT AS GOOD. A CAD row
    STORES the version it was drawn against, so the artifacts table renders "not the
    current Arka" from a pk comparison that cannot be wrong. The BOQ has no such field and
    is not getting one: what this module records is a completion STAMP on the attempt, and
    a fourth "is the BOQ finished" signal beside the three that already disagree
    (DESIGN_MODULE_DEFERRED J8) would cost more than it explains.

    SO THE PAIRING IS INFERRED, LIVE, FROM THE STAMP'S TIMESTAMP: the highest-versioned
    Arka on this attempt that had already been submitted when the stamp was written. Same
    shape of answer as the CAD's, same freshness — computed at render, stored nowhere —
    but derived rather than recorded, and it should be read as "which layout was on the
    table at the time", not as a claim about what the designer actually priced.

    WHY IT IS WORTH SHOWING ANYWAY. A BOQ completed under Arka v1 stays stamped and stays
    frozen when v1 is rejected and replaced by v2, because an in-attempt Arka rejection
    opens no attempt and so never reaches the redo scoping that would have re-asked for
    it. Nothing else on any screen says so. This is the only signal, it blocks nothing,
    and it closes nothing — it makes an existing silent gap legible to the reviewer who
    is about to judge the bill.

    NO ARKA, NO BADGE — mirroring `{% if arka and ... %}` on the CAD row. With no current
    Arka there is no version for the BOQ to be stale against, and an attempt whose BOQ was
    stamped before any Arka existed is reported as stale only once one arrives.
    """
    if attempt is None or attempt.boq_submitted_at is None or arka is None:
        return None, False
    # `arka_history` is already in hand for the version panel — reuse it rather than
    # issuing a second query per screen. It is newest-version-first.
    submitted_by_then = [a for a in arka_history
                         if a.submitted_at <= attempt.boq_submitted_at]
    provenance = max(submitted_by_then, key=lambda a: a.version, default=None)
    return provenance, (provenance is None or provenance.pk != arka.pk)


def _designer_boq(project):
    """The project's BOQ, or None. Read-only — this module never creates, seeds or
    writes a BOQ row (settled decision 4); `boq_detail` owns all of that."""
    try:
        return project.boq
    except BOQ.DoesNotExist:
        return None


def _boq_review_panel(boq):
    """Read-only context for the BOQ panel on the Part 9 package review screen (Part 11).

    ONLY WHAT IS ON THIS SITE'S SHEET. The reviewer sees this site's bill, not the 207-item
    catalogue it was drawn from — the point of the picker is that the sheet is short and
    the review has to show that same thing.

    NOT EVERY ROW IS THE DESIGNER'S CHOICE, which this used to claim and no longer can.
    Items flagged mandatory on the catalogue are composed onto every OPEX BOQ and cannot be
    removed, so `boq_mandatory_ids` comes back with the rows and the template marks them.
    A reviewer judging whether the designer picked the right items has to be able to tell
    which ones they actually picked.

    A READ, LIKE EVERYTHING ELSE IN THIS MODULE. It builds no form and writes nothing;
    settled decision 4 stands. It borrows the entry screen's own grouping helpers from
    models rather than re-deriving category order, so the reviewer's copy of the sheet
    cannot list categories in a different order from the designer's.
    """
    if boq is None:
        return {'boq_by_category': [], 'boq_off_catalogue': [],
                'boq_row_count': 0, 'boq_quantity_count': 0,
                'boq_mandatory_ids': set()}

    category_order = opex_catalogue_category_order()
    catalogue_ids  = {m.pk for m in get_opex_boq_catalogue()}
    on_rows, off_rows = split_opex_boq_rows(boq, catalogue_ids)

    return {
        'boq_by_category':    group_boq_rows_by_category(on_rows, category_order),
        'boq_off_catalogue':  group_boq_rows_by_category(off_rows, category_order),
        # Only catalogue-linked rows can be mandatory, so this is never consulted for the
        # off-catalogue half — an ad-hoc row has no master to carry the flag.
        'boq_mandatory_ids':  {m.pk for m in get_opex_mandatory_items()},
        'boq_row_count':      len(on_rows) + len(off_rows),
        'boq_quantity_count': sum(1 for row in on_rows + off_rows
                                  if row.boq_quantity and row.boq_quantity > 0),
    }


# ---------------------------------------------------------------------------
# 6. Screens
# ---------------------------------------------------------------------------

def _workspace_context(project, assignment):
    """Shared context for both Part 3 screens, so the designer and the Head are looking
    at the same computed truth rather than two templates deriving it separately."""
    attempt = _current_attempt(assignment)
    arka    = _current_arka(attempt)
    boq     = _designer_boq(project)
    files   = _attempt_files(attempt)
    # Hoisted out of the dict literal so _boq_provenance() can read it without a second
    # query — it was already being fetched for the version panel.
    arka_history = (list(attempt.arka_submissions.select_related(
                        'submitted_by__user', 'reviewed_by__user',
                        'head_reviewed_by__user')
                        .order_by('-version')) if attempt else [])
    boq_provenance_arka, boq_is_stale = _boq_provenance(attempt, arka, arka_history)
    return {
        'project':        project,
        'assignment':     assignment,
        'attempt':        attempt,
        'arka':           arka,
        'arka_approved':  _approved_arka(attempt) is not None,
        'arka_history':   arka_history,
        'files':          files,
        # Reports the PROGRESSION rule, not "any CAD-ish file exists" — a chip saying
        # "CAD: Uploaded" while the gate still refuses to advance would be a lie the
        # designer cannot debug. Legacy files still LIST above; they just do not tick
        # this chip. See PROGRESSION_CAD_KINDS.
        'has_cad':        any(f.kind in PROGRESSION_CAD_KINDS and f.is_current
                              for f in files),
        'boq':            boq,
        'boq_complete':   bool(attempt and attempt.boq_submitted_at),
        # The BOQ's counterpart to the CAD row's "not the current Arka" badge. See
        # _boq_provenance() for why this is inferred rather than read off a field, and for
        # why it is worth showing despite that. `boq_provenance_arka` may be None while
        # `boq_is_stale` is True: that is a BOQ stamped before any Arka existed.
        'boq_provenance_arka': boq_provenance_arka,
        'boq_is_stale':        boq_is_stale,
        'cad_kinds':      [(k, KIND_LABELS[k]) for k in UPLOADABLE_KINDS],
        'status':         assignment.status,
    }


@login_required
def design_site_workspace(request, project_id):
    """The designer's per-site design screen: submit an Arka, read the rejection
    reason, upload CAD, see every uploaded version with the Arka it derives from,
    reach BOQ entry and mark the BOQ complete.

    Read access is the allocated designer or the Design Head — the Head needs to see
    exactly what the designer sees when a question comes up. Every ACTION on the
    screen is separately gated in its own view; being able to load this page confers
    nothing.
    """
    project = _opex_site(project_id)
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None:
        raise Http404('No design assignment for this site.')

    is_designer = user_is_assigned_designer(request.user, assignment)
    # PROMPT 3.1b-1 — the site's PM (or a Coordinator) reads this screen to approve or
    # reject the package: the Arka, the CAD archive listing and the BOQ are all here
    # already, so there is no second, cut-down approval view (D-3). ONE OR TERM, AT THIS
    # GATE ONLY — the two helpers above are shared by the Head's and the designer's
    # endpoints and neither is widened. Conditioned on the status, so it opens exactly
    # while the PM owes a verdict.
    pm_reviewing = (assignment.status == DESIGN_AWAITING_PM_APPROVAL
                    and can_approve_design_release(request.user, project))
    if not (is_designer or user_has_design_head_authority(request.user) or pm_reviewing):
        return HttpResponseForbidden('Only the allocated designer or the Design Head '
                                     'may open this site\'s design workspace.')

    boq_group_locked = project_boq_is_group_locked(project)

    ctx = _workspace_context(project, assignment)
    ctx.update({
        'is_designer':  is_designer,
        # PROMPT 3.1b-2b (§D10) — the banner and the back link tell the site's PM the truth.
        # Passed, not re-derived: a status test alone would show the PM's text to the Design
        # Head at the same status, since he opens this screen too.
        'pm_reviewing': pm_reviewing,
        # Part 4: the designer reads the QC verdict, the QC remarks and any PM change
        # request off the shared attempt-history partial, so they see the same record of
        # what happened as the Head does on the QC screen.
        'history':      _attempt_history(assignment),
        'can_submit_arka': is_designer and assignment.status in ARKA_SUBMITTABLE_STATUSES,
        # The BOQ link is only useful if the existing Part 0.6 gate lets this user in.
        # Surfaced rather than hidden: a designer allocated to a site whose
        # `assigned_design` names someone else is locked out of BOQ entry, and being
        # told so beats a bare 403 from boq_detail.
        # Part 6: the same caller-side AND the BOQ views apply. A designer whose site has
        # been picked up into a locked procurement group is not locked out by authority —
        # the quantities are simply final, and the banner below says which group froze them.
        'can_edit_boq': (user_can_edit_project_boq(request.user, project)
                         and not boq_group_locked),
        'boq_group_locked': boq_group_locked,
    })
    return render(request, 'projects/design/site_workspace.html', ctx)


@login_required
def design_head_review(request, project_id):
    """The Arka review screen for one site, serving BOTH gates (Part 9).

    One screen rather than two, because the reviewers are looking at exactly the same
    thing — the Arka, its capacity, its link and its version history. What differs is
    which action they may take, and that is decided per gate below and rendered as at most
    one action block. A user with neither authority gets the read-only view if they can
    see the site at all.

    `can_qc_verdict` and `can_head_verdict` are mutually exclusive by construction: they
    require different statuses. Each additionally carries the settled-decision-2 check, so
    a dual-flag holder who recorded gate 1 sees no gate-2 form — the refusal is visible
    before the click, and re-checked server-side when it happens anyway.
    """
    project = _opex_site(project_id)
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None:
        raise Http404('No design assignment for this site.')

    can_qc_gate   = user_can_qc_gate_design(request.user, assignment)
    can_head_gate = user_can_head_gate_design(request.user, assignment)
    if not (can_qc_gate or can_head_gate or user_has_design_head_authority(request.user)):
        return HttpResponseForbidden(
            'The Arka review screen is for Design QC, the Design Head, or his named deputy.')

    ctx = _workspace_context(project, assignment)
    arka = ctx['arka']
    profile = getattr(request.user, 'profile', None)

    # Gate 1 — Design QC owes a verdict.
    ctx['can_qc_verdict'] = bool(
        can_qc_gate
        and arka is not None
        and arka.verdict == ARKA_PENDING
        and assignment.status == DESIGN_ARKA_SUBMITTED
        and not _other_gate_actor_conflict(profile, arka.head_reviewed_by_id)
    )
    # Gate 2 — the Head owes a verdict on what QC passed.
    ctx['can_head_verdict'] = bool(
        can_head_gate
        and arka is not None
        and arka.verdict == ARKA_APPROVED
        and arka.head_verdict == ARKA_PENDING
        and assignment.status == DESIGN_AWAITING_HEAD_ARKA
        and not _other_gate_actor_conflict(profile, arka.reviewed_by_id)
    )
    # Rendered as an explanation when the Head is looking at an Arka he passed through
    # Design QC himself. Without it the screen simply shows no buttons and he has no way
    # to tell "already decided" from "you are refused, and here is why".
    ctx['blocked_by_own_qc_verdict'] = bool(
        can_head_gate
        and arka is not None
        and assignment.status == DESIGN_AWAITING_HEAD_ARKA
        and arka.head_verdict == ARKA_PENDING
        and _other_gate_actor_conflict(profile, arka.reviewed_by_id)
    )
    ctx['error_categories'] = DESIGN_ERROR_CATEGORY_CHOICES
    ctx['is_design_qc']     = can_qc_gate
    return render(request, 'projects/design/head_review.html', ctx)


# ---------------------------------------------------------------------------
# 7. Arka submission
# ---------------------------------------------------------------------------

@login_required
def design_arka_submit(request, project_id):
    """The allocated DESIGNER submits an Arka version.

    Permission is `assignment.assigned_to == profile` and nobody else — explicitly
    including the Design Head, who reviews these and must not be able to author one.

    VERSIONING: version is max(version for this attempt) + 1, starting at 1, and any
    previous submission is flipped to is_current=False BEFORE the insert, in the same
    transaction. The partial unique constraint `uniq_current_arka_per_attempt` rejects
    the insert otherwise — the ordering is load-bearing, not stylistic.

    A rejected version stays is_current=True until this replacement lands, so the
    designer can read what was rejected and why right up to the moment they fix it.
    """
    project = _opex_site(project_id)
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None or not user_is_assigned_designer(request.user, assignment):
        return HttpResponseForbidden('Only the designer allocated to this site may '
                                     'submit an Arka.')
    if request.method != 'POST':
        return redirect('design_site_workspace', project_id=project.project_id)

    def _back(msg, ok=False):
        (messages.success if ok else messages.error)(request, msg)
        return redirect('design_site_workspace', project_id=project.project_id)

    if assignment.status not in ARKA_SUBMITTABLE_STATUSES:
        return _back(f'{project.project_id}: an Arka can only be submitted while the '
                     f'site is in design or its previous Arka was rejected '
                     f'(this site is "{assignment.get_status_display()}").')

    # ── capacity: recorded, NOT validated against anything (settled decision 5) ──
    # Positive-number validation only. There is deliberately no comparison against
    # tendered capacity, no mismatch gate and no rollup — that is Part 5 reporting and
    # a separate commercial decision.
    raw_capacity = (request.POST.get('capacity_kw') or '').strip()
    try:
        capacity = Decimal(raw_capacity)
    except (InvalidOperation, ValueError):
        return _back('Please enter the designed capacity in kW as a number.')
    if capacity <= 0:
        return _back('Designed capacity must be greater than zero.')

    arka_link = (request.POST.get('arka_link') or '').strip()
    if not arka_link:
        return _back('An Arka link is required.')
    try:
        URLValidator()(arka_link)
    except ValidationError:
        return _back('Please enter a valid Arka link (a full URL, including https://).')

    # Optional note for the reviewer. Never validated and never rejected — an empty
    # value stores '' and the submission proceeds exactly as before.
    remarks = (request.POST.get('remarks') or '').strip()

    profile = request.user.profile
    with transaction.atomic():
        attempt = _current_attempt(assignment)
        if attempt is None:
            attempt = _open_first_attempt(assignment)

        # Stand the previous version down BEFORE inserting — see the docstring.
        attempt.arka_submissions.filter(is_current=True).update(is_current=False)
        next_version = (attempt.arka_submissions.aggregate(
            m=Max('version'))['m'] or 0) + 1

        arka = ArkaSubmission.objects.create(
            attempt=attempt, version=next_version,
            capacity_kw=capacity, arka_link=arka_link,
            submitted_by=profile, remarks=remarks,
            verdict=ARKA_PENDING, is_current=True,
        )
        apply_design_status(
            assignment, DESIGN_ARKA_SUBMITTED, profile,
            f'Arka v{arka.version} submitted ({capacity} kW) for approval',
            'design_arka_submitted',
            entity_type='ArkaSubmission', entity_id=arka.pk)

    return _back(f'{project.project_id}: Arka v{next_version} submitted — awaiting '
                 f'Design Head approval.', ok=True)


# ---------------------------------------------------------------------------
# 8. Arka verdict
# ---------------------------------------------------------------------------

def _verdict_target(request, project):
    """The Arka a DESIGN QC (gate 1) verdict may be recorded against, or an error message.

    Shared by the QC approve and reject views so the two cannot disagree about which
    submission is reviewable. The designer cannot reach either endpoint: both are gated on
    `user_can_qc_gate_design`, which refuses the assigned designer.
    """
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None:
        return None, None, f'{project.project_id}: no design assignment for this site.'
    if assignment.status != DESIGN_ARKA_SUBMITTED:
        return None, None, (f'{project.project_id}: there is no Arka awaiting a Design QC '
                            f'verdict (status "{assignment.get_status_display()}").')
    attempt = _current_attempt(assignment)
    arka = _current_arka(attempt)
    if arka is None:
        return None, None, f'{project.project_id}: no current Arka submission found.'
    if arka.verdict != ARKA_PENDING:
        return None, None, (f'{project.project_id}: Arka v{arka.version} has already '
                            f'been {arka.get_verdict_display().lower()} by Design QC.')
    return assignment, arka, None


def _head_verdict_target(request, project):
    """The Arka a DESIGN HEAD (gate 2) verdict may be recorded against, or an error message.

    THIS IS WHERE THE SERIAL RULE IS ENFORCED for the Arka. The status test does the work:
    `awaiting_head_arka` is reachable only from a Design QC approval, so a Head arriving
    before QC has ruled finds the site still at `arka_submitted` and is refused with a
    message naming what he is waiting for. The explicit `verdict != ARKA_APPROVED` test
    below is a belt-and-braces re-check against the row itself rather than the cached
    status, in the same spirit as _package_is_complete().
    """
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None:
        return None, None, f'{project.project_id}: no design assignment for this site.'
    if assignment.status != DESIGN_AWAITING_HEAD_ARKA:
        return None, None, (
            f'{project.project_id}: there is no Arka awaiting your verdict — Design QC '
            f'must approve it first (status "{assignment.get_status_display()}").')
    attempt = _current_attempt(assignment)
    arka = _current_arka(attempt)
    if arka is None:
        return None, None, f'{project.project_id}: no current Arka submission found.'
    if arka.verdict != ARKA_APPROVED:
        return None, None, (f'{project.project_id}: Arka v{arka.version} has not been '
                            f'approved by Design QC, so there is nothing for you to '
                            f'countersign.')
    if arka.head_verdict != ARKA_PENDING:
        return None, None, (f'{project.project_id}: Arka v{arka.version} has already '
                            f'been {arka.get_head_verdict_display().lower()} by the '
                            f'Design Head.')
    return assignment, arka, None


@login_required
def design_arka_approve(request, project_id):
    """GATE 1 — DESIGN QC approves the current Arka version.

    Moves the site to `awaiting_head_arka`. It unlocks nothing, and neither does gate 2
    any more — CAD travels with the Arka submission and the BOQ is not gated on the Arka
    at all. What `head_verdict='approved'` still decides is whether the PACKAGE may
    complete (see _approved_arka, still consulted by
    _maybe_advance_to_artifacts_uploaded).
    """
    project = _opex_site(project_id)
    assignment_pre = getattr(project, 'design_assignment', None)
    if not user_can_qc_gate_design(request.user, assignment_pre):
        return HttpResponseForbidden(
            'The Arka Design QC review is for a Design QC reviewer, and never for the '
            'designer allocated to this site.')
    if request.method != 'POST':
        return redirect('design_head_review', project_id=project.project_id)

    assignment, arka, error = _verdict_target(request, project)
    if error:
        messages.error(request, error)
        return redirect('design_head_review', project_id=project.project_id)

    profile = request.user.profile
    # Decision 2, gate 1 side: refuse if this same person already recorded the HEAD
    # verdict on this Arka. Unreachable through the UI (the Head verdict cannot be first)
    # but checked anyway — every gate enforces the rule from its own side, so neither
    # depends on the other having run.
    if _other_gate_actor_conflict(profile, arka.head_reviewed_by_id):
        messages.error(request, f'{project.project_id}: you have already recorded the '
                                f'Design Head verdict on Arka v{arka.version}. Two '
                                f'verdicts by one person is not a second review.')
        return redirect('design_head_review', project_id=project.project_id)

    with transaction.atomic():
        arka.verdict     = ARKA_APPROVED
        arka.reviewed_by = profile
        arka.reviewed_at = timezone.now()
        arka.save(update_fields=['verdict', 'reviewed_by', 'reviewed_at'])
        apply_design_status(
            assignment, DESIGN_AWAITING_HEAD_ARKA, profile,
            f'Arka v{arka.version} passed Design QC ({arka.capacity_kw} kW) — '
            f'awaiting Design Head approval',
            'design_arka_qc_approved',
            entity_type='ArkaSubmission', entity_id=arka.pk)

    # NAMES WHO HOLDS THE ARKA, AND NOTHING ELSE. It used to end "...before the designer
    # can upload CAD or enter the BOQ", which is no longer true of either: CAD travels
    # with the Arka submission and the BOQ is not gated on the Arka at all. What the
    # Head's approval still decides is whether the PACKAGE can complete.
    messages.success(request, f'{project.project_id}: Arka v{arka.version} passed Design '
                              f'QC — it now needs the Design Head\'s approval before the '
                              f'design package can be completed.')
    return redirect('design_head_review', project_id=project.project_id)


@login_required
def design_arka_reject(request, project_id):
    """GATE 1 — DESIGN QC rejects the current Arka version.

    A reason AND an error category are both MANDATORY (settled decision 7). The reason is
    checked here so the designer gets a usable message, and the Part 1 CHECK constraint
    `rejection_reason_required_when_rejected` enforces the same rule at the database level
    for any writer that bypasses this view. The category is view-enforced only — see the
    note on DesignAttempt.head_failure_category for why it is not a CHECK.

    The rejected submission stays is_current=True — it is the record of what was
    rejected — and is stood down only when the designer submits the replacement.
    """
    project = _opex_site(project_id)
    assignment_pre = getattr(project, 'design_assignment', None)
    if not user_can_qc_gate_design(request.user, assignment_pre):
        return HttpResponseForbidden(
            'The Arka Design QC review is for a Design QC reviewer, and never for the '
            'designer allocated to this site.')
    if request.method != 'POST':
        return redirect('design_head_review', project_id=project.project_id)

    assignment, arka, error = _verdict_target(request, project)
    if error:
        messages.error(request, error)
        return redirect('design_head_review', project_id=project.project_id)

    profile = request.user.profile
    if _other_gate_actor_conflict(profile, arka.head_reviewed_by_id):
        messages.error(request, f'{project.project_id}: you have already recorded the '
                                f'Design Head verdict on Arka v{arka.version}. Two '
                                f'verdicts by one person is not a second review.')
        return redirect('design_head_review', project_id=project.project_id)

    reason = (request.POST.get('rejection_reason') or '').strip()
    if not reason:
        messages.error(request, f'{project.project_id}: a rejection reason is required '
                                f'— the designer cannot act on "rejected" alone.')
        return redirect('design_head_review', project_id=project.project_id)

    category, cat_error = _posted_error_category(request)
    if cat_error:
        messages.error(request, f'{project.project_id}: {cat_error}.')
        return redirect('design_head_review', project_id=project.project_id)

    with transaction.atomic():
        arka.verdict             = ARKA_REJECTED
        arka.rejection_reason    = reason
        arka.qc_failure_category = category
        arka.reviewed_by         = profile
        arka.reviewed_at         = timezone.now()
        arka.save(update_fields=['verdict', 'rejection_reason', 'qc_failure_category',
                                 'reviewed_by', 'reviewed_at'])
        apply_design_status(
            assignment, DESIGN_ARKA_REJECTED, profile,
            f'Arka v{arka.version} rejected at Design QC '
            f'[{DESIGN_ERROR_CATEGORY_LABELS.get(category, category)}]: {reason}',
            'design_arka_qc_rejected',
            entity_type='ArkaSubmission', entity_id=arka.pk)

    messages.success(request, f'{project.project_id}: Arka v{arka.version} rejected at '
                              f'Design QC — the designer has been asked to submit a new '
                              f'version.')
    return redirect('design_head_review', project_id=project.project_id)


@login_required
def design_arka_head_approve(request, project_id):
    """GATE 2 — the DESIGN HEAD approves an Arka that Design QC has already passed.

    THIS IS NO LONGER THE APPROVAL THAT UNLOCKS UPLOAD — nothing is locked behind it any
    more. It is the approval the PACKAGE waits on: _maybe_advance_to_artifacts_uploaded()
    is evaluated at the end of this view precisely because the CAD and the BOQ may already
    have arrived, in which case this verdict is the event that completes the package.

    Status returns to `arka_submitted`, which carries head_verdict='approved' and therefore
    classifies as "Arka approved, artifacts incomplete" — see the Part 9 note at the top of
    this module for why no new status is invented for that state.
    """
    project = _opex_site(project_id)
    assignment_pre = getattr(project, 'design_assignment', None)
    if not user_can_head_gate_design(request.user, assignment_pre):
        return HttpResponseForbidden(
            'The Design Head Arka review is for the Design Head or his named deputy, and '
            'never for the designer allocated to this site.')
    if request.method != 'POST':
        return redirect('design_head_review', project_id=project.project_id)

    assignment, arka, error = _head_verdict_target(request, project)
    if error:
        messages.error(request, error)
        return redirect('design_head_review', project_id=project.project_id)

    profile = request.user.profile
    if _other_gate_actor_conflict(profile, arka.reviewed_by_id):
        messages.error(request, f'{project.project_id}: you recorded the Design QC verdict '
                                f'on Arka v{arka.version} yourself, so you cannot also '
                                f'countersign it as Design Head. It needs a second pair '
                                f'of eyes.')
        return redirect('design_head_review', project_id=project.project_id)

    with transaction.atomic():
        arka.head_verdict     = ARKA_APPROVED
        arka.head_reviewed_by = profile
        arka.head_reviewed_at = timezone.now()
        # QC approved and the Head approved — the gates agree, so no overturn.
        arka.head_overturned_qc = False
        arka.save(update_fields=['head_verdict', 'head_reviewed_by', 'head_reviewed_at',
                                 'head_overturned_qc'])
        # Backwards by status name, forwards by meaning — see the docstring. This is one
        # of the two shapes that make a transition table the wrong abstraction here.
        apply_design_status(
            assignment, DESIGN_ARKA_SUBMITTED, profile,
            f'Arka v{arka.version} approved by the Design Head '
            f'({arka.capacity_kw} kW)',
            'design_arka_head_approved',
            entity_type='ArkaSubmission', entity_id=arka.pk)
        # A re-approval on an attempt that already carries CAD and a submitted BOQ would
        # otherwise leave the status behind; evaluating here costs one query.
        _maybe_advance_to_artifacts_uploaded(
            assignment, _current_attempt(assignment), profile)

    # As on gate 1: this no longer "unlocks" anything the designer was waiting on. It
    # completes the layout, and it is what lets the package advance once the artifacts
    # are in — which they may already be.
    messages.success(request, f'{project.project_id}: Arka v{arka.version} approved — '
                              f'the design package can complete once the CAD and BOQ '
                              f'are in.')
    return redirect('design_head_review', project_id=project.project_id)


@login_required
def design_arka_head_reject(request, project_id):
    """GATE 2 — the DESIGN HEAD rejects an Arka that Design QC passed.

    Reason and category are both mandatory, exactly as at gate 1, and the site returns to
    the designer identically (settled decision 5). The two rejections are RECORDED
    distinctly — different columns, different action_code — so "how often does the Head
    reject what QC passed" stays answerable.

    THIS IS THE OVERTURN CASE for an Arka: QC approved it and the Head did not, so
    head_overturned_qc is set. See the note on the field for why it is stored rather than
    derived.
    """
    project = _opex_site(project_id)
    assignment_pre = getattr(project, 'design_assignment', None)
    if not user_can_head_gate_design(request.user, assignment_pre):
        return HttpResponseForbidden(
            'The Design Head Arka review is for the Design Head or his named deputy, and '
            'never for the designer allocated to this site.')
    if request.method != 'POST':
        return redirect('design_head_review', project_id=project.project_id)

    assignment, arka, error = _head_verdict_target(request, project)
    if error:
        messages.error(request, error)
        return redirect('design_head_review', project_id=project.project_id)

    profile = request.user.profile
    if _other_gate_actor_conflict(profile, arka.reviewed_by_id):
        messages.error(request, f'{project.project_id}: you recorded the Design QC verdict '
                                f'on Arka v{arka.version} yourself, so you cannot also '
                                f'rule on it as Design Head. It needs a second pair of eyes.')
        return redirect('design_head_review', project_id=project.project_id)

    reason = (request.POST.get('rejection_reason') or '').strip()
    if not reason:
        messages.error(request, f'{project.project_id}: a rejection reason is required '
                                f'— the designer cannot act on "rejected" alone.')
        return redirect('design_head_review', project_id=project.project_id)

    category, cat_error = _posted_error_category(request)
    if cat_error:
        messages.error(request, f'{project.project_id}: {cat_error}.')
        return redirect('design_head_review', project_id=project.project_id)

    with transaction.atomic():
        arka.head_verdict           = ARKA_REJECTED
        arka.head_rejection_reason  = reason
        arka.head_failure_category  = category
        arka.head_reviewed_by       = profile
        arka.head_reviewed_at       = timezone.now()
        # QC approved, the Head rejected — the gates disagree. This is the signal
        # settled decision 6 exists to capture.
        arka.head_overturned_qc     = True
        arka.save(update_fields=['head_verdict', 'head_rejection_reason',
                                 'head_failure_category', 'head_reviewed_by',
                                 'head_reviewed_at', 'head_overturned_qc'])
        apply_design_status(
            assignment, DESIGN_ARKA_REJECTED, profile,
            f'Arka v{arka.version} rejected by the Design Head, overturning '
            f'Design QC '
            f'[{DESIGN_ERROR_CATEGORY_LABELS.get(category, category)}]: {reason}',
            'design_arka_head_rejected',
            entity_type='ArkaSubmission', entity_id=arka.pk)

    messages.success(request, f'{project.project_id}: Arka v{arka.version} rejected — the '
                              f'designer has been asked to submit a new version, and the '
                              f'Design QC approval has been recorded as overturned.')
    return redirect('design_head_review', project_id=project.project_id)


# ---------------------------------------------------------------------------
# 9. CAD and BOQ artifact upload
# ---------------------------------------------------------------------------

@login_required
def design_artifact_upload(request, project_id):
    """The allocated DESIGNER uploads a CAD (pdf/dwg) or optional BOQ (xlsx/pdf) file.

    REFUSED only when no Arka has been submitted yet — the approval requirement is gone,
    the existence requirement is not. A direct POST to this URL against an attempt with
    no Arka gets the same refusal a hidden button would have prevented; a POST against a
    PENDING or even a REJECTED Arka is now accepted, which is the change.

    PAIRING: derived_from_arka is set to the value _require_current_arka() returns, which
    is the CURRENT version whatever its verdict. It is never read off an older submission
    and never inferred from timestamps. Nothing re-points it if that version is later
    superseded — see the warning on the helper.

    VERSIONING is per (attempt, kind). Re-uploading a kind creates version N+1, flips
    the previous row to is_current=False and sets its superseded_by to the new row —
    which is why the new row is created first and the old one updated second.

    ORDERING: the file goes to storage BEFORE the transaction opens, exactly as the
    Part 2 survey upload does, so a storage failure aborts before any row is written
    and no DesignFile can ever point at an object that does not exist.
    """
    project = _opex_site(project_id)
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None or not user_is_assigned_designer(request.user, assignment):
        return HttpResponseForbidden('Only the designer allocated to this site may '
                                     'upload design artifacts.')
    if request.method != 'POST':
        return redirect('design_site_workspace', project_id=project.project_id)

    def _back(msg, ok=False):
        (messages.success if ok else messages.error)(request, msg)
        return redirect('design_site_workspace', project_id=project.project_id)

    kind = (request.POST.get('kind') or '').strip()
    if kind not in UPLOADABLE_KINDS:
        return _back('Choose which kind of file this is.')

    upload = request.FILES.get('artifact_file')
    if not upload:
        return _back('Please choose a file to upload.')

    attempt = _current_attempt(assignment)
    try:
        arka = _require_current_arka(attempt)
    except ValueError as exc:
        return _back(f'{project.project_id}: {exc}.')

    # ARCHIVE VALIDATION BEFORE STORAGE (Part 8). A zip that is unreadable, oversized
    # when expanded, or missing a PDF or a DWG is refused here — so a rejected archive
    # never reaches the bucket and never leaves an orphaned object behind.
    listing = []
    if kind == DESIGN_FILE_CAD_ZIP:
        try:
            listing = validate_cad_zip(upload)
        except DesignStorageError as exc:
            return _back(f'{project.project_id}: {exc}')

    # `kind` is whitelisted above, so it is safe as a storage path segment.
    path = build_design_path(project.project_id, kind, upload.name)
    try:
        bucket, stored_path = upload_design_file(upload, path)
    except DesignStorageError as exc:
        return _back(f'{project.project_id}: {exc}')

    # Optional note for the reviewer, per uploaded version. Never validated.
    remarks = (request.POST.get('remarks') or '').strip()

    profile = request.user.profile
    with transaction.atomic():
        previous = attempt.design_files.filter(kind=kind, is_current=True).first()
        next_version = (attempt.design_files.filter(kind=kind)
                        .aggregate(m=Max('version'))['m'] or 0) + 1

        design_file = DesignFile.objects.create(
            attempt=attempt, kind=kind, version=next_version,
            bucket=bucket, path=stored_path,
            original_filename=(upload.name or '')[:255],
            size_bytes=getattr(upload, 'size', None),
            content_type=(getattr(upload, 'content_type', '') or '')[:100],
            archive_listing=listing,
            derived_from_arka=arka,
            uploaded_by=profile, remarks=remarks, is_current=True,
        )
        if previous is not None:
            previous.is_current    = False
            previous.superseded_by = design_file
            previous.save(update_fields=['is_current', 'superseded_by'])

        log_activity(project, profile,
                     f'{KIND_LABELS[kind]} v{next_version} uploaded, derived from '
                     f'Arka v{arka.version}',
                     entity_type='DesignFile', entity_id=design_file.pk,
                     action_code='design_artifact_uploaded')

        advanced = _maybe_advance_to_artifacts_uploaded(assignment, attempt, profile)

    msg = (f'{project.project_id}: {KIND_LABELS[kind]} v{next_version} uploaded '
           f'(derived from Arka v{arka.version}).')
    if advanced:
        msg += ' The design package is now complete.'
    return _back(msg, ok=True)


@login_required
def design_file_download(request, project_id, pk):
    """Redirect to a freshly-signed, short-lived URL for one DesignFile.

    Same shape as the Part 2 survey download: the URL is minted per request, never
    stored, and visibility is the ordinary project visibility rule. The file is looked
    up THROUGH this project's assignment, so a pk belonging to another site's attempt
    is a 404 rather than a signed link.
    """
    project = _opex_site(project_id)
    if not user_can_view_design(request.user, project):
        return HttpResponseForbidden('You do not have access to this site.')

    design_file = get_object_or_404(
        DesignFile, pk=pk, attempt__assignment__project=project)
    try:
        url = get_design_file_url(design_file.bucket, design_file.path)
    except DesignStorageError as exc:
        messages.error(request, str(exc))
        return redirect('design_site_workspace', project_id=project.project_id)
    if not url:
        raise Http404('No stored object for this file.')
    return redirect(url)


# ---------------------------------------------------------------------------
# 10. BOQ completion
# ---------------------------------------------------------------------------

@login_required
def design_boq_complete(request, project_id):
    """The allocated DESIGNER marks the BOQ complete for the current attempt.

    THE BOQ ITSELF IS NOT DUPLICATED (settled decision 4). Quantities live in the
    existing BOQ / BOQItem rows and are entered through the existing `boq_detail`
    screen under the Part 0.6 permission helpers, neither of which this module touches.
    All that happens here is that the ATTEMPT records boq_submitted_at /
    boq_submitted_by — the design workflow's own note that this step is done.

    NOT GATED ON THE ARKA IN ANY WAY, WHICH IS THE CHANGE. This used to refuse unless the
    current Arka was approved at both gates, "for the same reason CAD is". The reason did
    not actually transfer, and that is why the gate is gone rather than relaxed:

        CAD DERIVES FROM A LAYOUT VERSION. DesignFile.derived_from_arka is a real NOT NULL
        FK, so a CAD is meaningfully "the drawing for Arka v3" and a superseded v3 makes it
        stale. THE BOQ IS NOT. It is assembled from the shared BOQItemMaster catalogue
        against BOQ / BOQItem rows that are project-scoped and carry no Arka reference at
        all — there is no version for it to be paired to and never was. What this view
        writes is a per-attempt COMPLETION STAMP, not a derivation.

        So the old rule was refusing a BOQ that could not go stale, on the grounds that a
        CAD can. A designer who knows the bill of quantities can now record it while the
        layout is still with the reviewers.

    NO ATTEMPT IS REQUIRED EITHER. The stamp lives on DesignAttempt, and before the first
    Arka submission there is no attempt to stamp — so this view opens attempt 1 lazily,
    the same way design_arka_submit() does and through the same helper. It opens it only
    AFTER every refusal below has passed, so a rejected completion never leaves a stray
    attempt row behind on a site that has not started design.

    WHAT THIS DOES NOT CHANGE, deliberately: _maybe_advance_to_artifacts_uploaded() still
    demands an APPROVED Arka before the package is complete. BOQ may now outrun the Arka;
    it cannot carry the site past the gate on its own.

    The "at least one quantity" guard reads the existing BOQ and mirrors the check
    `boq_detail`'s own submit branch applies (views.py — `boq_quantity__gt=0`), so this
    stamp cannot be set on an empty BOQ. It is a READ of BOQ rows; nothing here writes
    one.
    """
    project = _opex_site(project_id)
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None or not user_is_assigned_designer(request.user, assignment):
        return HttpResponseForbidden('Only the designer allocated to this site may mark '
                                     'its BOQ complete.')
    if request.method != 'POST':
        return redirect('design_site_workspace', project_id=project.project_id)

    def _back(msg, ok=False):
        (messages.success if ok else messages.error)(request, msg)
        return redirect('design_site_workspace', project_id=project.project_id)

    # No Arka gate. `attempt` may legitimately be None here — see the docstring — so
    # every test below has to tolerate that rather than assume a row exists.
    attempt = _current_attempt(assignment)

    if attempt is not None and attempt.boq_submitted_at is not None:
        return _back(f'{project.project_id}: the BOQ for attempt '
                     f'{attempt.attempt_number} is already marked complete.')

    boq = _designer_boq(project)
    if boq is None or not boq.items.filter(boq_quantity__gt=0).exists():
        return _back(f'{project.project_id}: enter a quantity for at least one BOQ item '
                     f'before marking the BOQ complete.')

    # AND A QUANTITY ON EVERY MANDATORY ITEM, which is a stricter thing than the guard
    # above and sits beside it rather than replacing it. Without this the flag would be
    # decorative: aggregate_group_boq() filters boq_quantity__gt=0, so a mandatory row left
    # blank contributes nothing to the procurement total and is indistinguishable from the
    # item never having been mandatory at all.
    #
    # SCOPED TO ACTIVE MASTERS through get_opex_mandatory_items(). An item deactivated
    # while still carrying the flag must not strand this BOQ — the picker would not offer
    # it, so the designer could not satisfy a guard that demanded it. One query for the
    # quantified set, not one per row.
    quantified = set(
        boq.items.filter(boq_quantity__gt=0, item_master__isnull=False)
        .values_list('item_master_id', flat=True))
    unquantified = [m for m in get_opex_mandatory_items() if m.pk not in quantified]
    if unquantified:
        return _back(
            f'{project.project_id}: every mandatory item needs a quantity before the BOQ '
            f'can be marked complete — still missing ' +
            ', '.join(f'{m.code} {m.description}' for m in unquantified) + '.')

    # Optional note for the reviewer, captured at the moment of completion and never
    # afterwards. Read HERE rather than in each caller because both submitting controls
    # arrive at this view with their own POST: the site workspace posts straight to it,
    # and opex_boq_entry() delegates by calling it with the SAME request object. One
    # read therefore covers both, and the draft-save branch never reaches this line.
    boq_remarks = (request.POST.get('boq_remarks') or '').strip()

    profile = request.user.profile
    with transaction.atomic():
        # LAST, not first: every refusal above has already returned, so this only ever
        # runs on a completion that is actually going to be recorded.
        if attempt is None:
            attempt = _open_first_attempt(assignment)
        attempt.boq_submitted_at = timezone.now()
        attempt.boq_submitted_by = profile
        attempt.boq_remarks      = boq_remarks
        # boq_remarks MUST be in this list. A field assigned above but missing from
        # update_fields is silently not written — nothing raises and nothing logs.
        attempt.save(update_fields=['boq_submitted_at', 'boq_submitted_by',
                                    'boq_remarks'])
        log_activity(project, profile,
                     f'BOQ marked complete for attempt {attempt.attempt_number}',
                     entity_type='DesignAttempt', entity_id=attempt.pk,
                     action_code='design_boq_submitted')
        advanced = _maybe_advance_to_artifacts_uploaded(assignment, attempt, profile)

    msg = f'{project.project_id}: BOQ marked complete.'
    if advanced:
        msg += ' The design package is now complete.'
    return _back(msg, ok=True)


# ===========================================================================
# PART 4 — QC review, attempt lifecycle, deputy, PM change requests, release
# ===========================================================================
#
# TWO REWORK LOOPS, COUNTED SEPARATELY AND NEVER COLLAPSED
# --------------------------------------------------------
# A new attempt opens for exactly two reasons, and which one it was is the whole
# point of recording it:
#
#   opened_reason='qc_failed'          the package was wrong    — execution error
#   opened_reason='pm_change_request'  the brief changed        — moving requirement
#
# A tender whose rework is mostly `qc_failed` has a design quality problem. One whose
# rework is mostly `pm_change_request` has a requirements problem, and no amount of
# designer coaching will fix it. Merging the two fields would erase the only signal
# that tells them apart, so:
#
#   AN ATTEMPT CLOSED BY A PM CHANGE REQUEST KEEPS qc_verdict='pending'.
#   It was never judged. Writing 'failed' there would inflate the QC failure rate with
#   rework the designer did not cause. This is enforced in design_change_request()
#   below, which touches closed_at and nothing else on the outgoing attempt.
#
# WHO MAY DO WHAT
# ---------------
# QC is `user_can_qc_design()` — Head-or-deputy AND NOT the allocated designer. The
# self-QC exclusion is in the permission helper, not here, so the two QC entry points
# (start and verdict) cannot drift. Change requests are
# `user_can_request_design_change()`, which routes to the untouched
# `user_can_manage_project()`.


# ---------------------------------------------------------------------------
# 11. Attempt lifecycle — the one place a new attempt is opened
# ---------------------------------------------------------------------------

def _carry_forward_artifacts(old_attempt, new_attempt, redo):
    """Copy onto `new_attempt` whatever `redo` did NOT name — Part 9.1.

    `redo` is the set of artifacts the failing reviewer ticked. Anything absent from it
    survived the review, so re-making it is pure churn: the designer re-submits an Arka
    nobody disputed, re-uploads a byte-identical archive, and BOTH gates re-approve an
    Arka they already approved. Four verdicts to fix a BOQ line.

    ROWS ARE COPIED, NEVER RE-POINTED. Moving the original row onto the new attempt would
    erase what attempt N actually contained, and attempt N is the row a failed QC verdict
    is recorded against. So each carried artifact is a NEW row on the new attempt with
    `carried_forward_from` pointing back at its original, which is what lets every screen
    say "carried forward from attempt N" instead of implying a verdict was recorded twice.

    THE CAD COPY SHARES THE STORED OBJECT — `bucket` and `path` verbatim. Nothing is
    re-uploaded and no second copy of a 25 MB archive is written.

    Returns a list of the human-readable things carried, for the log line.
    """
    carried = []

    old_arka = _current_arka(old_attempt)
    arka_for_new = None

    if REDO_ARKA not in redo and old_arka is not None:
        arka_for_new = ArkaSubmission.objects.create(
            attempt=new_attempt, version=1,
            capacity_kw=old_arka.capacity_kw, arka_link=old_arka.arka_link,
            submitted_by=old_arka.submitted_by,
            # The designer's submission note travels with the copy. Omitting it here
            # would blank it on the new attempt with no error and no log line.
            remarks=old_arka.remarks,
            # Both gates' verdicts travel with it. `carried_forward_from` is what keeps
            # that honest — see the note on the field.
            verdict=old_arka.verdict,
            rejection_reason=old_arka.rejection_reason,
            qc_failure_category=old_arka.qc_failure_category,
            reviewed_by=old_arka.reviewed_by, reviewed_at=old_arka.reviewed_at,
            head_verdict=old_arka.head_verdict,
            head_rejection_reason=old_arka.head_rejection_reason,
            head_failure_category=old_arka.head_failure_category,
            head_reviewed_by=old_arka.head_reviewed_by,
            head_reviewed_at=old_arka.head_reviewed_at,
            head_overturned_qc=old_arka.head_overturned_qc,
            carried_forward_from=old_arka, is_current=True,
        )
        carried.append(f'Arka v{old_arka.version}')

    # CAD can only be carried if the Arka was: `derived_from_arka` must point at an Arka
    # on the SAME attempt, and a CAD paired to a superseded layout is precisely what that
    # field exists to prevent. The view refuses the combination before reaching here; this
    # guard is the backstop.
    if REDO_CAD not in redo and arka_for_new is not None:
        for old_file in old_attempt.design_files.filter(is_current=True):
            DesignFile.objects.create(
                attempt=new_attempt, kind=old_file.kind, version=1,
                bucket=old_file.bucket, path=old_file.path,
                original_filename=old_file.original_filename,
                size_bytes=old_file.size_bytes, content_type=old_file.content_type,
                archive_listing=old_file.archive_listing,
                derived_from_arka=arka_for_new,
                uploaded_by=old_file.uploaded_by,
                # As above — the upload note travels with the copy.
                remarks=old_file.remarks,
                carried_forward_from=old_file, is_current=True,
            )
            carried.append(f'{KIND_LABELS.get(old_file.kind, old_file.kind)}'
                           f' ({old_file.original_filename or "file"})')

    # The BOQ's line items are project-scoped and never reset; what carries here is the
    # per-attempt COMPLETION STAMP, so the designer does not have to re-tick a BOQ that
    # was not in question.
    if REDO_BOQ not in redo and old_attempt.boq_submitted_at is not None:
        new_attempt.boq_submitted_at = old_attempt.boq_submitted_at
        new_attempt.boq_submitted_by = old_attempt.boq_submitted_by
        new_attempt.save(update_fields=['boq_submitted_at', 'boq_submitted_by'])
        carried.append('BOQ completion')

    return carried


def _open_next_attempt(assignment, reason, actor, detail, redo=None, extra_fields=None):
    """Close the current attempt and open the next one. THE ONLY PLACE THIS HAPPENS.

    Both rework loops call this with a different `reason`; the mechanics are identical
    and are deliberately written once. Duplicating them across the QC-fail and
    change-request views is exactly how the two would drift into disagreeing about
    which fields get set.

    Caller owns the transaction — both call sites already have one open for their own
    writes, and closing one attempt while failing to open the next would be the worst
    possible partial state.

    What it does NOT touch is as important as what it does:
      * `qc_verdict` on the outgoing attempt — the caller owns that. QC-fail sets it to
        'failed' before calling; a change request leaves it 'pending' forever.
      * `assigned_to`, the survey, and the approved DueDateCommitment — all preserved.
        Rework does not reopen the allocation or renegotiate the due date; the site
        goes back to the same designer under the same commitment (settled decision 2).

    PART 9.1 — `redo` SCOPES THE REWORK. Pass the set of artifacts the failing reviewer
    ticked and everything else is carried forward with its approvals; pass None (the
    default) and the attempt starts empty, which is Part 4's behaviour exactly.

    A PM CHANGE REQUEST PASSES None ON PURPOSE. The brief moved, so nothing drawn against
    the old brief can be assumed to still hold — carrying an Arka forward there would
    preserve work against a requirement that no longer exists.

    THE OPENING STATUS FOLLOWS THE ARKA. With no carried Arka the site returns to
    `in_design` and the designer submits one. With an Arka carried forward and approved at
    both gates, the site opens at `arka_submitted` — Part 3's "artifacts outstanding"
    state — so the designer can go straight to whatever actually failed.

    `extra_fields` (3.1c-i) rides on the status write below, for companion columns a caller
    needs moved in the same write. Only design_change_request_accept() passes it, to clear
    the release stamp. `current_attempt_number` is set here and cannot be overridden by it.

    Returns the new DesignAttempt.
    """
    now = timezone.now()
    current = _current_attempt(assignment)
    if current is not None and current.closed_at is None:
        current.closed_at = now
        current.save(update_fields=['closed_at'])

    next_number = (assignment.attempts.aggregate(m=Max('attempt_number'))['m'] or 0) + 1
    new_attempt = DesignAttempt.objects.create(
        assignment=assignment, attempt_number=next_number, opened_reason=reason,
    )

    carried = []
    if redo is not None and current is not None:
        carried = _carry_forward_artifacts(current, new_attempt, redo)

    carried_arka = _current_arka(new_attempt)
    opening_status = (DESIGN_ARKA_SUBMITTED
                      if (carried_arka is not None
                          and carried_arka.head_verdict == ARKA_APPROVED)
                      else DESIGN_IN_DESIGN)

    detail_suffix = f' — carried forward: {", ".join(carried)}' if carried else ''

    # NESTED, NOT MERGED (audit A-2.2 §5.2). This function stays the only place an
    # attempt is closed and the next opened; apply_design_status() stays the only place
    # the status is written. The coupling is not one-to-one in either direction —
    # design_head_qc_pass() closes an attempt without opening one, design_qc_pass()
    # closes nothing — so folding them together would give the merged function two
    # reasons to change. `current_attempt_number` rides with the status because the two
    # must never be observable apart.
    apply_design_status(
        assignment, opening_status, actor,
        f'Attempt {next_number} opened ({new_attempt.get_opened_reason_display()}): '
        f'{detail}{detail_suffix}',
        f'design_attempt_opened_{reason}',
        extra_fields={**(extra_fields or {}), 'current_attempt_number': next_number},
        entity_type='DesignAttempt', entity_id=new_attempt.pk)

    # Everything may have carried forward — a failure whose fix was entirely inside the
    # BOQ line items, for instance. Evaluate the progression rule so the new attempt does
    # not sit at `arka_submitted` with a complete package waiting for a nudge.
    if carried:
        _maybe_advance_to_artifacts_uploaded(assignment, new_attempt, actor)

    return new_attempt


def _pending_change_requests(attempt):
    """Change requests on this attempt that the Design Head has not yet triaged.

    PART 4.6 REDEFINED "UNRESOLVED". It used to mean `resulting_attempt__isnull=True`,
    which worked only because Part 4 set that field in the same transaction as the row.
    A REJECTED request resolves with `resulting_attempt` still null, so reading that
    field would suspend a review forever over a request the Head has already refused.
    The verdict is now the authority and this is the one query that says so.

    A pending request blocks a verdict at BOTH gates: judging a package that may be about
    to be reworked wastes the review and produces a verdict about a design nobody has
    decided to build. The suspension lifts either way — acceptance opens a new attempt to
    review instead, rejection lets this one resume.
    """
    if attempt is None:
        return DesignChangeRequest.objects.none()
    return attempt.change_requests.filter(verdict=CHANGE_REQUEST_PENDING)


def _package_is_complete(attempt):
    """Whether an attempt actually has a reviewable package: approved current Arka, a
    current cad_zip, and a BOQ marked complete.

    Same three conditions Part 3's _maybe_advance_to_artifacts_uploaded() evaluates,
    re-checked here at QC start rather than trusting the status alone — the status is a
    cached conclusion, these rows are the evidence. It must use the SAME kind tuple as
    that function or the two would disagree and a package could pass the gate that let it
    in and then fail the gate at QC.
    """
    if attempt is None:
        return False
    if _approved_arka(attempt) is None:
        return False
    if not attempt.design_files.filter(
            kind__in=PROGRESSION_CAD_KINDS, is_current=True).exists():
        return False
    return attempt.boq_submitted_at is not None


# ---------------------------------------------------------------------------
# 12. QC review
# ---------------------------------------------------------------------------

def _qc_guard(request, project, required_statuses, gate='qc'):
    """Shared entry checks for the five package-review endpoints, at either gate.

    Returns (assignment, attempt, error), where `error` is:
        None  -> refuse with 403. The caller has no authority at this gate.
        ''    -> proceed.
        str   -> refuse with this message and a redirect. Authorised, wrong state.

    `gate` selects the predicate: 'qc' -> user_can_qc_gate_design (the is_design_qc flag),
    'head' -> user_can_head_gate_design (Head authority or named deputy). Both refuse the
    assigned designer, so a designer reviewing their own package is refused identically at
    all five endpoints regardless of what flags they hold.

    ORDER IS DELIBERATE: authority is decided FIRST, before anything about the site's
    state is revealed. A user with no authority at this gate gets an identical 403 whether
    the site is mid-review, already released, or has no design assignment at all — the
    refusal never doubles as a state oracle.

    The SERIAL RULE is carried by `required_statuses`, not by a separate test: the Head's
    endpoints require `awaiting_head_qc`, which is reachable only from a Design QC pass.
    """
    assignment = getattr(project, 'design_assignment', None)

    # `assignment` may be None here; both predicates return False for that, which is the
    # correct answer — there is nothing to review and nobody may review it.
    allowed = (user_can_head_gate_design(request.user, assignment) if gate == 'head'
               else user_can_qc_gate_design(request.user, assignment))
    if not allowed:
        return None, None, None
    if assignment.status not in required_statuses:
        return assignment, None, (
            f'{project.project_id}: this review is not available at this stage '
            f'(status "{assignment.get_status_display()}").')
    return assignment, _current_attempt(assignment), ''


#: The 403 text for each gate. Kept together so the two refusals stay parallel and neither
#: leaks which gate the site is actually sitting at.
_GATE_FORBIDDEN = {
    'qc':   ('Package Design QC is for a Design QC reviewer, and never for the designer '
             'allocated to this site.'),
    'head': ('The Design Head package review is for the Design Head or his named deputy, '
             'and never for the designer allocated to this site.'),
}


@login_required
def design_qc_start(request, project_id):
    """GATE 1 — a DESIGN QC reviewer takes a completed package into review.

    `artifacts_uploaded` -> `in_qc`, and `qc_started_at` is stamped on the attempt.

    STAMPING qc_started_at IS WHAT OPENS THE PM CHANGE REQUEST WINDOW (settled
    decision 3). Before this moment a PM asking for a change is a conversation; after
    it, it is a system action that suspends this review and goes to the Design Head, who
    decides whether it opens a new attempt (Part 4.6).

    The three package conditions are re-checked from the rows rather than inferred from
    the status — a site could reach `artifacts_uploaded` and then have its only CAD file
    superseded by nothing, and QC should refuse that rather than review an empty package.
    """
    project = _opex_site(project_id)
    assignment, attempt, error = _qc_guard(request, project, (DESIGN_ARTIFACTS_UPLOADED,))
    if error is None:
        return HttpResponseForbidden(_GATE_FORBIDDEN['qc'])
    if request.method != 'POST':
        return redirect('design_qc_review', project_id=project.project_id)
    if error:
        messages.error(request, error)
        return redirect('design_qc_queue')

    if not _package_is_complete(attempt):
        messages.error(request, f'{project.project_id}: the package is incomplete — QC '
                                f'needs an approved Arka, at least one current CAD file '
                                f'and a BOQ marked complete.')
        return redirect('design_qc_queue')

    profile = request.user.profile
    with transaction.atomic():
        attempt.qc_started_at = timezone.now()
        attempt.save(update_fields=['qc_started_at'])
        apply_design_status(
            assignment, DESIGN_IN_QC, profile,
            f'QC started on attempt {attempt.attempt_number}',
            'design_qc_started',
            entity_type='DesignAttempt', entity_id=attempt.pk)

    messages.success(request, f'{project.project_id}: QC started on attempt '
                              f'{attempt.attempt_number}. The PM can now raise a change '
                              f'request against this package.')
    return redirect('design_qc_review', project_id=project.project_id)


def _blocking_change_request(project, attempt):
    """The refusal message if a PENDING PM change request blocks a verdict, else ''.

    Part 4.6: a pending request means the Design Head has not yet decided whether this
    package is about to be reworked, so judging it would record a verdict about a design
    nobody has committed to. Applies identically at BOTH gates, which is why it is a
    function rather than four copies. An accepted or rejected request blocks nothing —
    acceptance moved the review to a new attempt, rejection settled that this one stands.
    """
    if _pending_change_requests(attempt).exists():
        return (f'{project.project_id}: a PM change request on this attempt is awaiting '
                f'the Design Head\'s decision — the review is suspended until he accepts '
                f'or rejects it.')
    return ''


@login_required
def design_qc_pass(request, project_id):
    """GATE 1 — DESIGN QC passes the package. THE SITE IS NOT RELEASED BY THIS.

    PART 9 CHANGED WHAT THIS DOES. In Part 4 a QC pass released the site; it now hands the
    package to the Design Head. `in_qc` -> `awaiting_head_qc`, `head_started_at` is
    stamped, and release happens at design_pm_approve() or not at all —
    design_head_qc_pass() hands the package to the PM (prompt 3.1b-2c).

    The attempt is deliberately NOT closed here. It is still live — the Head has not
    ruled — and closing it would make the package look finished to every surface that
    reads closed_at.
    """
    project = _opex_site(project_id)
    assignment, attempt, error = _qc_guard(request, project, (DESIGN_IN_QC,))
    if error is None:
        return HttpResponseForbidden(_GATE_FORBIDDEN['qc'])
    if request.method != 'POST':
        return redirect('design_qc_review', project_id=project.project_id)
    if error:
        messages.error(request, error)
        return redirect('design_qc_queue')

    blocked = _blocking_change_request(project, attempt)
    if blocked:
        messages.error(request, blocked)
        return redirect('design_qc_review', project_id=project.project_id)

    profile = request.user.profile
    # Decision 2, gate 1 side. Unreachable through the UI but enforced from both sides.
    if _other_gate_actor_conflict(profile, attempt.head_reviewed_by_id):
        messages.error(request, f'{project.project_id}: you have already recorded the '
                                f'Design Head verdict on attempt {attempt.attempt_number}.')
        return redirect('design_qc_review', project_id=project.project_id)

    now = timezone.now()
    with transaction.atomic():
        attempt.qc_verdict     = QC_PASSED
        attempt.qc_reviewed_by = profile
        attempt.qc_reviewed_at = now
        attempt.head_started_at = now
        attempt.save(update_fields=['qc_verdict', 'qc_reviewed_by', 'qc_reviewed_at',
                                    'head_started_at'])
        apply_design_status(
            assignment, DESIGN_AWAITING_HEAD_QC, profile,
            f'Design QC passed attempt {attempt.attempt_number} — awaiting '
            f'Design Head review',
            'design_qc_passed',
            entity_type='DesignAttempt', entity_id=attempt.pk)

    messages.success(request, f'{project.project_id}: Design QC passed on attempt '
                              f'{attempt.attempt_number} — the package now needs the '
                              f'Design Head\'s review before the site can be released.')
    return redirect('design_qc_review', project_id=project.project_id)


@login_required
def design_qc_fail(request, project_id):
    """GATE 1 — DESIGN QC fails the package: the attempt closes and N+1 opens for rework.

    `qc_remarks` AND `qc_failure_category` are both mandatory (settled decision 7). The
    remarks are checked here so the reviewer gets a usable message, and enforced
    underneath by the Part 1 CHECK constraint `qc_remarks_required_when_qc_failed` for any
    writer that bypasses this view. The category is view-enforced only.

    A QC FAILURE ENDS THE ATTEMPT — the Head never sees it, and head_verdict stays
    'pending' forever on this row, meaning "not judged" exactly as it does on an attempt
    closed by a PM change request.

    THE CATEGORY'S GROUP DECIDES WHOSE REWORK THIS IS. A Group A failure counts toward the
    designer's multiplier; Group B and C do not (settled decision 8). Nothing is computed
    here — the category is stored, and design_metrics reads it back through
    error_category_group().
    """
    project = _opex_site(project_id)
    assignment, attempt, error = _qc_guard(request, project, (DESIGN_IN_QC,))
    if error is None:
        return HttpResponseForbidden(_GATE_FORBIDDEN['qc'])
    if request.method != 'POST':
        return redirect('design_qc_review', project_id=project.project_id)
    if error:
        messages.error(request, error)
        return redirect('design_qc_queue')

    blocked = _blocking_change_request(project, attempt)
    if blocked:
        messages.error(request, blocked)
        return redirect('design_qc_review', project_id=project.project_id)

    profile = request.user.profile
    if _other_gate_actor_conflict(profile, attempt.head_reviewed_by_id):
        messages.error(request, f'{project.project_id}: you have already recorded the '
                                f'Design Head verdict on attempt {attempt.attempt_number}.')
        return redirect('design_qc_review', project_id=project.project_id)

    remarks = (request.POST.get('qc_remarks') or '').strip()
    if not remarks:
        messages.error(request, f'{project.project_id}: QC remarks are required to fail '
                                f'a package — the designer cannot act on "failed" alone.')
        return redirect('design_qc_review', project_id=project.project_id)

    category, cat_error = _posted_error_category(request)
    if cat_error:
        messages.error(request, f'{project.project_id}: {cat_error}.')
        return redirect('design_qc_review', project_id=project.project_id)

    redo, redo_error = _posted_redo_scope(request, attempt)
    if redo_error:
        messages.error(request, f'{project.project_id}: {redo_error}.')
        return redirect('design_qc_review', project_id=project.project_id)

    now = timezone.now()
    with transaction.atomic():
        attempt.qc_verdict          = QC_FAILED
        attempt.qc_remarks          = remarks
        attempt.qc_failure_category = category
        attempt.qc_reviewed_by      = profile
        attempt.qc_reviewed_at      = now
        attempt.redo_required       = sorted(redo)
        attempt.save(update_fields=['qc_verdict', 'qc_remarks', 'qc_failure_category',
                                    'qc_reviewed_by', 'qc_reviewed_at', 'redo_required'])
        # THE FAILURE IS A LOG LINE, NOT A STATUS. It used to be both: `qc_failed` was
        # written here and overwritten by _open_next_attempt() two statements later,
        # inside this same atomic block, so no query anywhere could ever observe it. The
        # log line was always the thing that made the failure visible in the trail — the
        # old comment here said so — and it is untouched. Session C dropped the write
        # itself: a status nothing can read is not a state the site was in, and a mirror
        # hook firing on it would announce a transition that never happened.
        #
        # `qc_failed` remains a legal DESIGN_ASSIGNMENT_STATUS_CHOICES value and both its
        # readers (design_metrics.stage_of, views.TENDER_DESIGN_SUBMITTED_STATUSES) are
        # left alone — they read committed rows, and no committed row ever carried it.
        log_activity(project, profile,
                     f'Design QC failed attempt {attempt.attempt_number} '
                     f'[{DESIGN_ERROR_CATEGORY_LABELS.get(category, category)}] '
                     f'— redo: {", ".join(sorted(redo))}: {remarks}',
                     entity_type='DesignAttempt', entity_id=attempt.pk,
                     action_code='design_qc_failed')

        new_attempt = _open_next_attempt(
            assignment, ATTEMPT_REASON_QC_FAILED, profile,
            f'Design QC failure on attempt {attempt.attempt_number}', redo=redo)

    messages.success(request, f'{project.project_id}: Design QC failed — attempt '
                              f'{new_attempt.attempt_number} opened and the site is back '
                              f'with the designer to redo {_redo_phrase(redo)}.')
    return redirect('design_qc_review', project_id=project.project_id)


@login_required
def design_head_qc_pass(request, project_id):
    """GATE 2 — the DESIGN HEAD passes a package Design QC has already passed. A HANDOVER
    TO THE SITE'S PM, NOT A RELEASE.

    PROMPT 3.1b-2c CHANGED WHAT THIS DOES. It released the site until then; it now moves
    it to `awaiting_pm_approval`, with REASON_DESIGN_HEAD_PASSED on the ledger row, and the
    site is released at design_pm_approve() or not at all. `released_at` / `released_by`
    are NOT stamped here any more — the PM's approval stamps them, because released_at is
    SCM's age clock and must mean released to SCM (EXECUTION_MODULE_DEFERRED.md §D11).

    This is still where the attempt closes: both review gates have ruled on it. If the PM
    rejects the package, the Head answers from `pm_rejected` without reopening this attempt
    (design_head_return_to_pm / design_head_send_back).
    """
    project = _opex_site(project_id)
    assignment, attempt, error = _qc_guard(request, project, (DESIGN_AWAITING_HEAD_QC,),
                                           gate='head')
    if error is None:
        return HttpResponseForbidden(_GATE_FORBIDDEN['head'])
    if request.method != 'POST':
        return redirect('design_qc_review', project_id=project.project_id)
    if error:
        messages.error(request, error)
        return redirect('design_qc_queue')

    blocked = _blocking_change_request(project, attempt)
    if blocked:
        messages.error(request, blocked)
        return redirect('design_qc_review', project_id=project.project_id)

    profile = request.user.profile
    if _other_gate_actor_conflict(profile, attempt.qc_reviewed_by_id):
        messages.error(request, f'{project.project_id}: you passed attempt '
                                f'{attempt.attempt_number} through Design QC yourself, so '
                                f'you cannot also release it as Design Head. It needs a '
                                f'second pair of eyes.')
        return redirect('design_qc_review', project_id=project.project_id)

    now = timezone.now()
    with transaction.atomic():
        attempt.head_verdict     = QC_PASSED
        attempt.head_reviewed_by = profile
        attempt.head_reviewed_at = now
        attempt.closed_at        = now
        # Both gates passed — they agree, so no overturn.
        attempt.head_overturned_qc = False
        attempt.save(update_fields=['head_verdict', 'head_reviewed_by', 'head_reviewed_at',
                                    'closed_at', 'head_overturned_qc'])
        # THE HANDOVER, NOT THE RELEASE (prompt 3.1b-2c). No released_at / released_by here:
        # design_pm_approve() stamps both in the same write as `released`, so the rule that
        # a row says `released` exactly when it carries them still holds.
        #
        # THE DEPUTY ASYMMETRY IS DELIBERATE. `_qc_guard(..., gate='head')` above admits the
        # Design Head OR his named deputy to pass QC — a technical review, and a deputy is
        # who covers it. The PM gate this hands to has NO deputy
        # (permissions.can_approve_design_release: the site's PM or its Coordinators only,
        # prompt 3.1b-1): accepting a design for the site is the site owner's act, not the
        # design department's. This commit puts the two acts side by side for the first
        # time; the difference is the design, not an oversight.
        apply_design_status(
            assignment, DESIGN_AWAITING_PM_APPROVAL, profile,
            f'Design Head passed attempt {attempt.attempt_number} — with the PM for approval',
            'design_head_qc_passed',
            entity_type='DesignAttempt', entity_id=attempt.pk,
            reason_code=REASON_DESIGN_HEAD_PASSED)

    # PROMPT 3.1b-3 — the PM's turn. IN-APP ONLY, named here; after the atomic block, so no
    # send can unwind the handover. See the block above design_gate_next_actors().
    try:
        who = profile.user.get_full_name() or profile.user.username
        for recipient in design_gate_next_actors(assignment, GATE_HEAD_PASSED):
            send_notification(
                recipient=recipient,
                message=(f'{project.project_id}: a design is waiting for your approval. '
                         f'{who} (Design Head) passed attempt {attempt.attempt_number} '
                         f'through both review gates. Approve it to release it to SCM, or '
                         f'reject it with a remark for the Design Head.'),
                channels=['in_app'],
                link=reverse('design_pm_approval_queue'),
                template='design_gate_head_passed',  # a NotificationLog label, not Interakt
                related_project=project, actor=profile)
    except Exception:
        logger.exception('design_head_qc_pass: the in-app notification for %s failed; the '
                         'handover to the PM stands.', project.project_id)

    messages.success(request, f'{project.project_id}: both review gates passed on attempt '
                              f'{attempt.attempt_number} — the design is now with the site\'s '
                              f'PM for approval before release.')
    return redirect('design_qc_review', project_id=project.project_id)


@login_required
def design_head_qc_fail(request, project_id):
    """GATE 2 — the DESIGN HEAD fails a package Design QC passed. THE OVERTURN CASE.

    Remarks and category are both mandatory, and the attempt closes and reopens exactly as
    a gate-1 failure does (settled decision 5) — the designer's experience is identical.
    What differs is where it is recorded: head_remarks / head_failure_category, a distinct
    action_code, and `head_overturned_qc=True`.

    THAT FLAG IS THE POINT OF THE SECOND GATE (settled decision 6). If after two tenders
    the Head never overturns Design QC, the second gate is a formality and this column is
    the only thing that will say so. It cannot be reconstructed later from the verdicts
    alone once the attempt has been superseded, which is why it is written now.
    """
    project = _opex_site(project_id)
    assignment, attempt, error = _qc_guard(request, project, (DESIGN_AWAITING_HEAD_QC,),
                                           gate='head')
    if error is None:
        return HttpResponseForbidden(_GATE_FORBIDDEN['head'])
    if request.method != 'POST':
        return redirect('design_qc_review', project_id=project.project_id)
    if error:
        messages.error(request, error)
        return redirect('design_qc_queue')

    blocked = _blocking_change_request(project, attempt)
    if blocked:
        messages.error(request, blocked)
        return redirect('design_qc_review', project_id=project.project_id)

    profile = request.user.profile
    if _other_gate_actor_conflict(profile, attempt.qc_reviewed_by_id):
        messages.error(request, f'{project.project_id}: you passed attempt '
                                f'{attempt.attempt_number} through Design QC yourself, so '
                                f'you cannot also rule on it as Design Head. It needs a '
                                f'second pair of eyes.')
        return redirect('design_qc_review', project_id=project.project_id)

    remarks = (request.POST.get('head_remarks') or '').strip()
    if not remarks:
        messages.error(request, f'{project.project_id}: remarks are required to fail a '
                                f'package — the designer cannot act on "failed" alone.')
        return redirect('design_qc_review', project_id=project.project_id)

    category, cat_error = _posted_error_category(request)
    if cat_error:
        messages.error(request, f'{project.project_id}: {cat_error}.')
        return redirect('design_qc_review', project_id=project.project_id)

    redo, redo_error = _posted_redo_scope(request, attempt)
    if redo_error:
        messages.error(request, f'{project.project_id}: {redo_error}.')
        return redirect('design_qc_review', project_id=project.project_id)

    now = timezone.now()
    with transaction.atomic():
        attempt.head_verdict          = QC_FAILED
        attempt.head_remarks          = remarks
        attempt.head_failure_category = category
        attempt.head_reviewed_by      = profile
        attempt.head_reviewed_at      = now
        # Design QC passed this package and the Head did not. Recorded, countable.
        attempt.head_overturned_qc    = True
        attempt.redo_required         = sorted(redo)
        attempt.save(update_fields=['head_verdict', 'head_remarks', 'head_failure_category',
                                    'head_reviewed_by', 'head_reviewed_at',
                                    'head_overturned_qc', 'redo_required'])
        # No status write here either — see the matching note in design_qc_fail(). The
        # gate-2 failure passed through `qc_failed` on its way to the new attempt for
        # exactly as long as gate 1's did, which is to say never observably.
        log_activity(project, profile,
                     f'Design Head failed attempt {attempt.attempt_number}, overturning '
                     f'Design QC '
                     f'[{DESIGN_ERROR_CATEGORY_LABELS.get(category, category)}] '
                     f'— redo: {", ".join(sorted(redo))}: {remarks}',
                     entity_type='DesignAttempt', entity_id=attempt.pk,
                     action_code='design_head_qc_failed')

        new_attempt = _open_next_attempt(
            assignment, ATTEMPT_REASON_QC_FAILED, profile,
            f'Design Head failure on attempt {attempt.attempt_number}', redo=redo)

    messages.success(request, f'{project.project_id}: the Design Head failed the package, '
                              f'overturning Design QC — attempt '
                              f'{new_attempt.attempt_number} opened and the site is back '
                              f'with the designer to redo {_redo_phrase(redo)}.')
    return redirect('design_qc_review', project_id=project.project_id)


# ---------------------------------------------------------------------------
# 12b. PM release approval — gate 3 (prompt 3.1b-1)
#
# LIVE SINCE PROMPT 3.1b-2c, 13 Sep 2026. Everything in this section acts on
# `awaiting_pm_approval`, which design_head_qc_pass() writes when the Head passes a package
# and design_head_return_to_pm() when he overrules a rejection. The surface was built
# FIRST, in prompt 3.1b-1, so that the moment a site could park here, somebody could see
# it and act on it.
#
# AUTHORITY: permissions.can_approve_design_release() — the site's PM or one of its
# Project Coordinators, and no deputy. ORDER, exactly as _qc_guard(): authority first (a
# 403 that reveals nothing about the site's state), then the method, then the status
# (a message and a redirect — never a 500, never a blank page).
# ---------------------------------------------------------------------------

#: The 403 text for both PM verdict endpoints.
_PM_GATE_FORBIDDEN = ('Design release approval is for the site\'s Project Manager or one of '
                      'its Project Coordinators.')


# ---------------------------------------------------------------------------
# 12b-n. Whose turn it is — the PM gate's notifications (prompt 3.1b-3)
#
# THE DESIGN MODULE'S FIRST NOTIFICATIONS, AND THEY ARE IN-APP ONLY. Each of the four call
# sites names channels=['in_app'] itself. It does not lean on send_notification()'s
# default, and it does not lean on SystemSettings.email_enabled being off: that switch is
# ON in production, and UserProfile.email_notifications defaults to True, so a site that
# named 'email' would send real mail to everyone it reached. Nothing can switch these off
# either: in_app_notifications_enabled is read by no send path
# (EXECUTION_MODULE_DEFERRED.md §D37). tests_design_gate_notifications pins the channel
# at every call site by parse.
#
# EVERY SEND RUNS AFTER ITS VIEW'S ATOMIC BLOCK, INSIDE A try/except. send_notification()
# catches its own failures, and that is what makes it dangerous INSIDE the block: a caught
# failed INSERT leaves the Postgres transaction aborted, and the status change is rolled
# back at commit, silently, under a success message. After the block the transition has
# already committed and no send can undo it; the except stops a failure from turning the
# view's redirect into a 500. A notification outage must never become a workflow outage.
#
# LINKS ARE RELATIVE, from reverse(), AND NO MESSAGE CARRIES A URL. The bell renders
# `link` as an href, so a relative path lands on whatever host the user is already on.
# The absolute hosts elsewhere in the codebase are wrong for production (§D38).
#
# SCM IS NOT TOLD WHEN A DESIGN IS RELEASED. Deliberately: a new audience (§D39).
#
# DESIGN CHANGE REQUESTS EMAIL AS WELL (session 3.1c-ii, decisions N-a to N-e). The three
# change-request call sites name channels=['in_app', 'email']; the four gate sites above
# are unchanged and stay in-app only (§D44 records the asymmetry). Each is still one call
# per recipient, after the atomic block, inside a try/except. send_notification() uses ONE
# `message` as both the bell text and the email's plain-text part, so that message carries
# no URL. The email's absolute link lives only in `html_message`, rendered from
# projects/email/design_change_request.html (autoescaped) with request.build_absolute_uri(),
# so the host is whichever host the ACTOR is on (§D44).
# ---------------------------------------------------------------------------

GATE_HEAD_PASSED   = 'head_passed'     # design_head_qc_pass      -> the site's PM
GATE_PM_REJECTED   = 'pm_rejected'     # design_pm_reject         -> the Design Head
GATE_HEAD_RETURNED = 'head_returned'   # design_head_return_to_pm -> the site's PM
GATE_SENT_BACK     = 'sent_back'       # design_head_send_back    -> the designer

# Session 3.1c-ii. Raise -> PM, coordinators, Design Heads; accept -> requester, PM,
# coordinators, designer; reject -> requester. The actor is never among them.
GATE_CHANGE_RAISED   = 'change_request_raised'    # design_change_request
GATE_CHANGE_ACCEPTED = 'change_request_accepted'  # design_change_request_accept
GATE_CHANGE_REJECTED = 'change_request_rejected'  # design_change_request_reject


def _change_request_audience(people, actor):
    """The change-request branches' filter: drop a missing person, anyone whose UserProfile
    OR auth.User is inactive (D5), and the actor (N-c). De-duplication stays in the shared
    tail of design_gate_next_actors(). The four gate branches do not call this, so their
    audiences are exactly what they were (§D44: they still reach an inactive assigned PM)."""
    return [person for person in people
            if person is not None and person.is_active and person.user.is_active
            and (actor is None or person.pk != actor.pk)]


def design_gate_next_actors(assignment, transition, *, change=None, actor=None):
    """Whose turn it is after `transition` on `assignment`: the UserProfiles to notify,
    deduplicated by pk. Call it ONCE per transition, after the atomic block. Never per row.

    GATE_HEAD_PASSED, GATE_HEAD_RETURNED -> project_managers(): the site's PM plus its
        active Coordinators, PM first. That is the audience of can_approve_design_release(),
        so everyone who can act on the queue hears about it, and only they do. Two queries.

    GATE_SENT_BACK -> the allocated designer, `assigned_to`. _open_next_attempt() never
        writes it, so it still names the designer after attempt N+1 opens. One query.

    GATE_PM_REJECTED -> the ONE person whose decision the rejection answers, by a chain:
        1. the actor on the LATEST ledger transition INTO `awaiting_pm_approval`. On a first
           rejection that is whoever passed the package, the Head or his deputy. After a
           Head return it is whoever RETURNED it, and head_reviewed_by would name the wrong
           person here, because design_head_return_to_pm writes nothing to the attempt.
        2. else attempt N's head_reviewed_by;
        3. else every active is_design_head holder, AND THAT FALL-THROUGH IS LOGGED: a
           fan-out to several people nobody chose should be findable later.
        A step is skipped when its person is null or no longer active. One query on the
        normal path, three on the full fall-through.

    THE CHANGE-REQUEST KEYS (session 3.1c-ii) take `change` (the DesignChangeRequest) and
    `actor` (the UserProfile who acted). Each drops the actor and every inactive person,
    profile or auth.User, through _change_request_audience():

    GATE_CHANGE_RAISED -> project_managers(), then every active is_design_head holder. NOT
        the Heads' deputies (D4), so a raise will not reach a deputy once one is named.
    GATE_CHANGE_ACCEPTED -> change.requested_by, project_managers(), then `assigned_to`.
        _open_next_attempt() never writes `assigned_to`, so it names the designer of the
        attempt the acceptance just opened. The SCM owner of a group the site left is added
        by design_change_request_accept() itself, from the membership it removed (D6).
    GATE_CHANGE_REJECTED -> change.requested_by only.
    """
    if transition in (GATE_HEAD_PASSED, GATE_HEAD_RETURNED):
        people = project_managers(assignment.project)
    elif transition == GATE_SENT_BACK:
        people = [assignment.assigned_to] if assignment.assigned_to is not None else []
        if not people:
            logger.warning('design_gate_next_actors: %s has no allocated designer, so the '
                           'send-back notifies nobody.', assignment.project.project_id)
    elif transition == GATE_PM_REJECTED:
        arrival = (StatusTransition.objects
                   .filter(subject_type=SUBJECT_DESIGN_ASSIGNMENT, subject_id=assignment.pk,
                           to_status=DESIGN_AWAITING_PM_APPROVAL)
                   .select_related('actor')
                   .order_by('-occurred_at', '-pk')
                   .first())
        attempt = None
        if arrival is not None and arrival.actor is not None and arrival.actor.is_active:
            people = [arrival.actor]
        else:
            attempt = (assignment.attempts.select_related('head_reviewed_by')
                       .filter(attempt_number=assignment.current_attempt_number).first())
            passer = attempt.head_reviewed_by if attempt is not None else None
            if passer is not None and passer.is_active:
                logger.warning('design_gate_next_actors: %s — nobody active on the ledger '
                               'handed this package to the PM; notifying the Head who '
                               'passed attempt %s (%s) instead.',
                               assignment.project.project_id, attempt.attempt_number,
                               passer.user.username)
                people = [passer]
            else:
                people = list(UserProfile.objects
                              .filter(is_design_head=True, is_active=True)
                              .select_related('user').order_by('pk'))
                log = logger.warning if people else logger.error
                log('design_gate_next_actors: %s — no single Design Head answers this PM '
                    'rejection (the ledger and attempt %s name nobody active); notifying '
                    'ALL %d active Design Head flag-holders: %s.',
                    assignment.project.project_id, assignment.current_attempt_number,
                    len(people), ', '.join(p.user.username for p in people) or 'none')
    elif transition == GATE_CHANGE_RAISED:
        heads = list(UserProfile.objects.filter(is_design_head=True, is_active=True)
                     .select_related('user').order_by('pk'))
        people = _change_request_audience(project_managers(assignment.project) + heads, actor)
    elif transition == GATE_CHANGE_ACCEPTED:
        people = _change_request_audience(
            [change.requested_by, *project_managers(assignment.project),
             assignment.assigned_to], actor)
    elif transition == GATE_CHANGE_REJECTED:
        people = _change_request_audience([change.requested_by], actor)
    else:
        raise ValueError(f'design_gate_next_actors: unknown transition {transition!r}')

    seen, unique = set(), []
    for person in people:
        if person.pk not in seen:
            seen.add(person.pk)
            unique.append(person)
    return unique


def _pm_gate_guard(request, project):
    """Shared entry checks for the two PM verdict endpoints.

    Returns (assignment, error), where `error` is:
        None  -> refuse with 403. The user may not approve this site's design.
        ''    -> proceed.
        str   -> refuse with this message and a redirect. Authorised, wrong state.
    """
    if not can_approve_design_release(request.user, project):
        return None, None
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None:
        return None, f'{project.project_id}: this site has no design to approve.'
    if assignment.status != DESIGN_AWAITING_PM_APPROVAL:
        return assignment, (f'{project.project_id}: this design is not awaiting your '
                            f'approval (status "{assignment.get_status_display()}").')
    return assignment, ''


def _lock_parked(assignment):
    """Re-read the row under a row lock and return it if it is STILL with the PM, else None.

    A site has a PM and possibly several Coordinators, all with the same authority. Two of
    them clicking at once would otherwise both pass _pm_gate_guard() on a stale copy and
    write two ledger rows for one transition. The caller owns the atomic block.
    """
    locked = DesignAssignment.objects.select_for_update().get(pk=assignment.pk)
    if locked.status != DESIGN_AWAITING_PM_APPROVAL:
        return None
    return locked


@login_required
def design_pm_approval_queue(request):
    """The PM's worklist: designs both review gates have passed, waiting for the site's PM
    or a Coordinator to accept them before release.

    Same shape as design_qc_queue(): a DesignAssignment filter on status, scoped to the
    user, one row dict per site. NO ROLE GATE, like design_my_sites(): the queryset IS the
    scope, so anybody else gets the empty state rather than a 403 — the nav link is shown
    to PMs and Coordinators only, and the verdict endpoints re-check authority per site.

    SCOPED BY manageable_projects_q(), the queryset form of can_approve_design_release()
    (both are user_can_manage_project()). `.distinct()` IS REQUIRED, not tidy: that Q
    traverses the coordinators M2M, so a site with two Coordinators would otherwise appear
    twice to its PM. No OPEX site has a Coordinator today, which is exactly why a missing
    distinct() would pass every check on current data.
    """
    profile = getattr(request.user, 'profile', None)
    if profile is None:
        return HttpResponseForbidden('No profile.')

    assignments = (DesignAssignment.objects
                   .filter(status=DESIGN_AWAITING_PM_APPROVAL)
                   .filter(manageable_projects_q(profile, 'project__'),
                           project__is_deleted=False, project__project_type='OPEX')
                   .select_related('project', 'project__program', 'assigned_to__user')
                   # PROMPT 3.1b-2c (§D22) — HOW THE PACKAGE CAME TO BE HERE, read off the
                   # ledger in this same query: the LATEST transition INTO this status. Keyed
                   # on the arrival, not on the Head's reason code: a package the Head
                   # returned, that the PM then rejected again, that went back to the designer
                   # and came here through a second Head pass, still has the old return row —
                   # filtering on the reason would show that stale remark on a fresh package.
                   .annotate(
                       arrived_reason=latest_design_transition(
                           'reason_code', to_status=DESIGN_AWAITING_PM_APPROVAL),
                       arrived_remark=latest_design_transition(
                           'remark', to_status=DESIGN_AWAITING_PM_APPROVAL),
                       arrived_at=latest_design_transition(
                           'occurred_at', to_status=DESIGN_AWAITING_PM_APPROVAL))
                   .distinct()
                   .order_by('project__program__name', 'project__project_id'))

    rows = []
    for assignment in assignments:
        attempt = _current_attempt(assignment)
        returned = assignment.arrived_reason == REASON_DESIGN_HEAD_RETURNED_TO_PM
        rows.append({
            'assignment': assignment,
            'site':       assignment.project,
            'attempt':    attempt,
            'arka':       _current_arka(attempt),
            # The Design Head overruled this PM's rejection: his reason, and when.
            'head_return_remark': assignment.arrived_remark if returned else '',
            'head_returned_at':   assignment.arrived_at if returned else None,
        })

    return render(request, 'projects/design/pm_approval_queue.html', {'rows': rows})


@login_required
def design_pm_approve(request, project_id):
    """GATE 3 — the site's PM accepts the package. RELEASE.

    `awaiting_pm_approval` -> `released`, stamping the release and the PM's approval in
    ONE write through apply_design_status(), with its StatusTransition row in the same
    transaction (R-2) carrying REASON_DESIGN_PM_APPROVED. A remark is optional here.

    THIS IS THE ONLY NEW WRITER OF `released`.
    """
    project = _opex_site(project_id)
    assignment, error = _pm_gate_guard(request, project)
    if error is None:
        return HttpResponseForbidden(_PM_GATE_FORBIDDEN)
    if request.method != 'POST':
        return redirect('design_pm_approval_queue')
    if error:
        messages.error(request, error)
        return redirect('design_pm_approval_queue')

    profile = request.user.profile
    remark = (request.POST.get('remark') or '').strip()
    now = timezone.now()
    with transaction.atomic():
        locked = _lock_parked(assignment)
        if locked is None:
            messages.error(request, f'{project.project_id}: this design is no longer '
                                    f'awaiting your approval.')
            return redirect('design_pm_approval_queue')
        # released_at IS STAMPED HERE, AT THE PM'S APPROVAL, AND NOT AT THE HEAD'S PASS.
        # It is the SCM pool's age clock (post_qc_pool orders by it, _age_days reads it)
        # and the end point of every cycle-time figure, so it has to mean RELEASED TO SCM.
        # Left stamped at the Head's pass, a site would age in SCM's queue while it was
        # still with the PM and invisible to SCM. `released_by` therefore becomes the
        # approving PM, where the five rows released before this gate existed hold the
        # Design Head. That discontinuity is recorded (EXECUTION_MODULE_DEFERRED.md D11),
        # not migrated: those rows are true about who released them at the time.
        apply_design_status(
            locked, DESIGN_RELEASED, profile,
            'PM approved the design for release' + (f': {remark}' if remark else ''),
            'design_pm_approved',
            extra_fields={'released_at': now, 'released_by': profile,
                          'pm_approved_at': now, 'pm_approved_by': profile},
            reason_code=REASON_DESIGN_PM_APPROVED, remark=remark)

    messages.success(request, f'{project.project_id}: design approved and released to SCM.')
    return redirect('design_pm_approval_queue')


@login_required
def design_pm_reject(request, project_id):
    """GATE 3 — the site's PM rejects the package before release. Back to the Design Head.

    `awaiting_pm_approval` -> `pm_rejected`, with a StatusTransition row carrying
    REASON_DESIGN_PM_REJECTED and a MANDATORY remark. Nothing else is written.

    THE REMARK IS ENFORCED HERE, IN THE VIEW, because R-9 is not enforced centrally for
    design rows: REMARK_REQUIRED_SUBJECT_TYPES is empty (EXECUTION_MODULE_DEFERRED.md D12).
    A blank or whitespace-only remark is refused before the transaction opens, so it
    writes nothing at all. The Head reads it where he decides — design_qc_review — and on
    his sites screen, both through latest_design_transition().

    NO ATTEMPT, NO REASON VALUE, NO APPROVAL STAMP, NO released_at CHANGE. Whether this
    rejection warrants a designer attempt is the DESIGN HEAD'S call, made from `pm_rejected`
    with design_head_return_to_pm() (he disagrees) or design_head_send_back() (he agrees,
    and classifies whose fault it was) — a PM does not get to charge a designer a rework
    directly. The attempt the Head passed stays closed with its verdict intact.

    AND IT IS NOT A CHANGE REQUEST, AND MUST NEVER BE MERGED WITH ONE. A PM rejection is
    "the PM never accepted this". A post-release change request (design_change_request
    and its family, prompt 3.1c) is "the PM accepted it and later changed their mind".
    They share no status, no path and no row, so that both questions stay answerable.
    """
    project = _opex_site(project_id)
    assignment, error = _pm_gate_guard(request, project)
    if error is None:
        return HttpResponseForbidden(_PM_GATE_FORBIDDEN)
    if request.method != 'POST':
        return redirect('design_pm_approval_queue')
    if error:
        messages.error(request, error)
        return redirect('design_pm_approval_queue')

    remark = (request.POST.get('remark') or '').strip()
    if not remark:
        messages.error(request, f'{project.project_id}: a remark is required to reject a '
                                f'design — the Design Head cannot act on "rejected" alone.')
        return redirect('design_pm_approval_queue')

    profile = request.user.profile
    with transaction.atomic():
        locked = _lock_parked(assignment)
        if locked is None:
            messages.error(request, f'{project.project_id}: this design is no longer '
                                    f'awaiting your approval.')
            return redirect('design_pm_approval_queue')
        apply_design_status(
            locked, DESIGN_PM_REJECTED, profile,
            f'PM rejected the design before release: {remark}',
            'design_pm_rejected',
            reason_code=REASON_DESIGN_PM_REJECTED, remark=remark)

    # PROMPT 3.1b-3 — the Design Head's turn: the one whose decision this answers.
    # IN-APP ONLY, named here; after the atomic block, so no send can unwind the rejection.
    try:
        who = profile.user.get_full_name() or profile.user.username
        for recipient in design_gate_next_actors(locked, GATE_PM_REJECTED):
            send_notification(
                recipient=recipient,
                # The PM's own words go LAST and unpunctuated: a remark ending in its own
                # full stop must not be followed by a second one.
                message=(f'{project.project_id}: the PM rejected the design and it is back '
                         f'with you. Return it to the PM unchanged, or send it back to the '
                         f'designer. {who} rejected attempt {locked.current_attempt_number} '
                         f'before release: "{remark}"'),
                channels=['in_app'],
                link=reverse('design_qc_review', kwargs={'project_id': project.project_id}),
                template='design_gate_pm_rejected',  # a NotificationLog label, not Interakt
                related_project=project, actor=profile)
    except Exception:
        logger.exception('design_pm_reject: the in-app notification for %s failed; the '
                         'rejection stands.', project.project_id)

    messages.success(request, f'{project.project_id}: design rejected and returned to the '
                              f'Design Head with your remark.')
    return redirect('design_pm_approval_queue')


# ---------------------------------------------------------------------------
# 12c. The Design Head's answer to a PM rejection (prompt 3.1b-2b)
#
# LIVE SINCE PROMPT 3.1b-2c, 13 Sep 2026. Both views require `pm_rejected`, which
# design_pm_reject() writes. tests_design_pm_gate_live (a) pins the writer set of each gate
# status, by parse.
#
# AUTHORITY, ORDER AND REFUSALS are _qc_guard()'s at the head gate: the Design Head or his
# deputy, never the site's own designer; a 403 that reveals nothing first, then the method,
# then the status as a message and a redirect — never a 500, never a blank page. The two
# forms are on design_qc_review, and every refusal lands back there.
# ---------------------------------------------------------------------------

def _lock_pm_rejected(assignment):
    """Re-read the row under a row lock and return it if it is STILL pm_rejected, else None.

    The Head and his deputy hold the same authority here. Two clicks at once would
    otherwise both pass _qc_guard() on a stale copy and act twice. The caller owns the
    atomic block — the same shape as _lock_parked() at the PM's gate.
    """
    locked = DesignAssignment.objects.select_for_update().get(pk=assignment.pk)
    if locked.status != DESIGN_PM_REJECTED:
        return None
    return locked


def _extend_due_date_for_pm_review(assignment, attempt, actor):
    """§D15, decision (c): move the agreed date out by the WHOLE DAYS since attempt N's
    head_reviewed_at, so a reopened attempt is not overdue for the PM's timing.

    WHY THAT SPAN. is_overdue() stops the designer's clock at the Head's pass —
    `awaiting_pm_approval` and `pm_rejected` are both clock-stopped — and the send-back
    starts it again. The span between is exactly the time the package spent with the PM and
    back with the Head. Moving the date by it gives the designer back the margin they had
    when the Head passed the package, no more and no less: a designer who was already late
    at that moment is still late by the same amount.

    THE SAME WRITE design_due_date_change() MAKES: the current row stood down, a new row
    approved on creation, with a change_reason that says why. Never an in-place edit. The
    caller has already refused an open extension request, so the row stood down here is
    the approved one.

    TWO KNOWN COSTS, recorded and deliberately not special-cased
    (EXECUTION_MODULE_DEFERRED.md §D23):
      * The new row IS a revision. `revisions` (rows - 1) goes up by one, on the Head's
        sites screen and in attention_list()'s "revised >= 3 times" band, so a site the PM
        has rejected more than once reads as a designer who keeps needing more time.
      * A null head_reviewed_at adds nothing — see that branch below.

    Returns (old_date, new_date, days), or None when nothing was written.
    """
    agreed = _effective_commitment(assignment)
    if agreed is None:
        # No agreed date was ever set: nothing to move, and nothing to be overdue against.
        return None
    if attempt.head_reviewed_at is None:
        # KNOWN COST, NOT A BUG. With no Head-pass time there is no span to measure, so
        # nothing is added and the reopened attempt may be overdue on arrival. A package
        # reaches the PM only through the Head's pass, which stamps head_reviewed_at, so
        # this is a row written some other way (a fixture, a backfill). Inventing a span
        # would be a date nobody agreed to.
        return None
    days = (timezone.localdate() - timezone.localtime(attempt.head_reviewed_at).date()).days
    if days <= 0:
        # Sent back the same day the Head passed it: no time was lost, and a revision row
        # moving the date by nothing would still count as a revision.
        return None

    new_date = agreed.proposed_date + timedelta(days=days)
    # Stand the current row down BEFORE inserting — the partial unique constraint permits
    # one is_current row per assignment. It keeps its approved_at, so the history of what
    # was agreed and when stays intact.
    assignment.due_date_commitments.filter(is_current=True).update(is_current=False)
    DueDateCommitment.objects.create(
        assignment=assignment, proposed_date=new_date, proposed_by=actor,
        approved_by=actor, approved_at=timezone.now(), is_current=True,
        change_reason=(f'Moved out {days} day(s): the time the package spent with the PM '
                       f'and the Design Head after attempt {attempt.attempt_number} passed '
                       f'review, before the PM rejection was sent back to the designer.'))
    log_activity(assignment.project, actor,
                 f'Agreed due date moved from {agreed.proposed_date} to {new_date} — '
                 f'{days} day(s) with the PM and the Design Head',
                 entity_type='DesignAssignment', entity_id=assignment.pk,
                 action_code='design_due_date_extended_pm_review')
    return agreed.proposed_date, new_date, days


@login_required
def design_head_return_to_pm(request, project_id):
    """The Design Head OVERRULES the PM: `pm_rejected` -> `awaiting_pm_approval`.

    THIS IS THE HEAD DISAGREEING WITH THE PM, AND THE PACKAGE RETURNS UNCHANGED. It writes
    NOTHING to the attempt — no category, no remarks, no redo, no head_* field, no new
    attempt. On the Head's reading the designer did nothing wrong, so nothing is charged and
    nothing reopens; the same package goes back in front of the same PM.

    The Head's reason therefore has one home: this transition's ledger row, carrying
    REASON_DESIGN_HEAD_RETURNED_TO_PM. The remark is MANDATORY and enforced here, since R-9
    is not enforced centrally for design rows (§D12); a blank one is refused before the
    transaction opens. The PM's queue shows it on the returned row, read back with
    latest_design_transition() (§D22).

    released_at and the PM approval stamps are untouched: the PM has approved nothing.
    """
    project = _opex_site(project_id)
    assignment, _attempt, error = _qc_guard(request, project, (DESIGN_PM_REJECTED,),
                                            gate='head')
    if error is None:
        return HttpResponseForbidden(_GATE_FORBIDDEN['head'])
    if request.method != 'POST':
        return redirect('design_qc_review', project_id=project.project_id)
    if error:
        messages.error(request, error)
        return redirect('design_qc_review', project_id=project.project_id)

    remark = (request.POST.get('remark') or '').strip()
    if not remark:
        messages.error(request, f'{project.project_id}: a remark is required to return a '
                                f'design to the PM — the PM rejected it, and needs to read '
                                f'why you disagree.')
        return redirect('design_qc_review', project_id=project.project_id)

    profile = request.user.profile
    with transaction.atomic():
        locked = _lock_pm_rejected(assignment)
        if locked is None:
            messages.error(request, f'{project.project_id}: this design is no longer '
                                    f'waiting for your answer to a PM rejection.')
            return redirect('design_qc_review', project_id=project.project_id)
        apply_design_status(
            locked, DESIGN_AWAITING_PM_APPROVAL, profile,
            f'Design Head returned the design to the PM unchanged: {remark}',
            'design_head_returned_to_pm',
            reason_code=REASON_DESIGN_HEAD_RETURNED_TO_PM, remark=remark)

    # PROMPT 3.1b-3 — the PM's turn again. IN-APP ONLY, named here; after the atomic block,
    # so no send can unwind the return.
    try:
        who = profile.user.get_full_name() or profile.user.username
        for recipient in design_gate_next_actors(locked, GATE_HEAD_RETURNED):
            send_notification(
                recipient=recipient,
                # The Head's words last, as at design_pm_reject.
                message=(f'{project.project_id}: a design is back for your approval, '
                         f'unchanged. Approve it to release it to SCM, or reject it again. '
                         f'{who} (Design Head) disagrees with the rejection: "{remark}"'),
                channels=['in_app'],
                link=reverse('design_pm_approval_queue'),
                template='design_gate_head_returned',  # a NotificationLog label, not Interakt
                related_project=project, actor=profile)
    except Exception:
        logger.exception('design_head_return_to_pm: the in-app notification for %s failed; '
                         'the return to the PM stands.', project.project_id)

    messages.success(request, f'{project.project_id}: design returned to the PM unchanged, '
                              f'with your remark.')
    return redirect('design_qc_review', project_id=project.project_id)


@login_required
def design_head_send_back(request, project_id):
    """The Design Head AGREES with the PM: the package goes back to the designer.

    `pm_rejected` -> a new attempt, opened by _open_next_attempt() — at `in_design` when
    the redo scope includes the Arka, at `arka_submitted` when the Arka is carried forward
    (it is approved at both gates, so it carries with its verdicts).

    THE HEAD CLASSIFIES WHOSE FAULT IT WAS, ON ATTEMPT N — pm_rejection_category,
    pm_rejection_remarks and redo_required — exactly as a gate failure is recorded on the
    attempt that failed. The category decides whose rework attempt N+1 is
    (classify_attempt_causes): Group A charges the designer, Groups B and C do not.

    IT NEVER TOUCHES head_verdict, head_failure_category OR head_remarks. Attempt N carries
    head_verdict='passed' and keeps it: the Head did pass this package and the PM caught
    something he did not. design_analytics._failure_rows() reads the head fields as errors
    the HEAD caught, which is why migration 0086 added separate columns.

    CATEGORY AND REMARKS ARE BOTH MANDATORY, BOTH REFUSED HERE BEFORE ANY WRITE. A blank
    category would read as uncategorised and charge the designer — the opposite of what
    B-06 fixed. A category with blank remarks is refused by the database
    (pm_rejection_remarks_required_with_category); refusing it here first means that
    constraint never reaches the Head as an IntegrityError.

    AN OPEN EXTENSION REQUEST REFUSES THE SEND-BACK (product decision, 13 Sep 2026). The
    designer can ask for more time while the package is in review, and nothing since has
    ruled on it. The send-back writes a new agreed date, which would overrule that request
    without a verdict — so the Head rules on it first, from the Extension button on the
    tender's sites screen, and then sends back.

    THE DUE DATE MOVES OUT by the time the package spent with the PM and the Head — see
    _extend_due_date_for_pm_review() for why that span, and its two costs.

    The ledger row for the move is written inside _open_next_attempt(), with no reason_code
    and no remark: that function takes neither and is shared with three live paths, so it
    is called here and not edited. Its from_status (`pm_rejected`) identifies the row; the
    Head's words are on attempt N.
    """
    project = _opex_site(project_id)
    assignment, attempt, error = _qc_guard(request, project, (DESIGN_PM_REJECTED,),
                                           gate='head')
    if error is None:
        return HttpResponseForbidden(_GATE_FORBIDDEN['head'])
    if request.method != 'POST':
        return redirect('design_qc_review', project_id=project.project_id)
    if error:
        messages.error(request, error)
        return redirect('design_qc_review', project_id=project.project_id)

    def _back(msg):
        messages.error(request, f'{project.project_id}: {msg}')
        return redirect('design_qc_review', project_id=project.project_id)

    if attempt is None:
        return _back('this site has no design attempt to send back.')

    remarks = (request.POST.get('pm_rejection_remarks') or '').strip()
    if not remarks:
        return _back('remarks are required to send a design back — the designer cannot act '
                     'on "the PM rejected it" alone.')

    category, cat_error = _posted_error_category(request)
    if cat_error:
        return _back(f'{cat_error}.')

    redo, redo_error = _posted_redo_scope(request, attempt)
    if redo_error:
        return _back(f'{redo_error}.')

    profile = request.user.profile
    with transaction.atomic():
        locked = _lock_pm_rejected(assignment)
        if locked is None:
            return _back('this design is no longer waiting for your answer to a PM rejection.')
        # UNDER THE LOCK, so the answer cannot change between this check and the date write.
        pending = _pending_extension(locked)
        if pending is not None:
            return _back(f'the designer\'s extension request to {pending.proposed_date} is '
                         f'still open. Approve or reject it from the Extension button on the '
                         f'tender\'s sites screen first — sending the design back moves the '
                         f'due date, and would overrule that request without a verdict.')

        attempt.pm_rejection_category = category
        attempt.pm_rejection_remarks = remarks
        attempt.redo_required = sorted(redo)
        attempt.save(update_fields=['pm_rejection_category', 'pm_rejection_remarks',
                                    'redo_required'])
        log_activity(project, profile,
                     f'Design Head sent the PM rejection of attempt {attempt.attempt_number} '
                     f'back to the designer '
                     f'[{DESIGN_ERROR_CATEGORY_LABELS.get(category, category)}] '
                     f'— redo: {", ".join(sorted(redo))}: {remarks}',
                     entity_type='DesignAttempt', entity_id=attempt.pk,
                     action_code='design_head_sent_back_to_designer')

        moved = _extend_due_date_for_pm_review(locked, attempt, profile)

        new_attempt = _open_next_attempt(
            locked, ATTEMPT_REASON_PM_REJECTED, profile,
            f'Design Head sent the PM rejection of attempt {attempt.attempt_number} back '
            f'to the designer', redo=redo)

    date_note = (f' The due date moved from {moved[0]:%d %b %Y} to {moved[1]:%d %b %Y}.'
                 if moved else '')

    # PROMPT 3.1b-3 — the designer's turn. IN-APP ONLY, named here; after the atomic block,
    # so no send can unwind the send-back or attempt N+1.
    try:
        who = profile.user.get_full_name() or profile.user.username
        for recipient in design_gate_next_actors(locked, GATE_SENT_BACK):
            send_notification(
                recipient=recipient,
                # The Head's words last, as at design_pm_reject.
                message=(f'{project.project_id}: the design is back with you, and attempt '
                         f'{new_attempt.attempt_number} is open to redo '
                         f'{_redo_phrase(redo)}.{date_note} The PM rejected attempt '
                         f'{attempt.attempt_number} and {who} (Design Head) sent it back: '
                         f'"{remarks}"'),
                channels=['in_app'],
                link=reverse('design_site_workspace',
                             kwargs={'project_id': project.project_id}),
                template='design_gate_sent_back',  # a NotificationLog label, not Interakt
                related_project=project, actor=profile)
    except Exception:
        logger.exception('design_head_send_back: the in-app notification for %s failed; the '
                         'send-back stands.', project.project_id)

    messages.success(request, f'{project.project_id}: sent back to the designer — attempt '
                              f'{new_attempt.attempt_number} opened to redo '
                              f'{_redo_phrase(redo)}.{date_note}')
    return redirect('design_qc_review', project_id=project.project_id)


# ---------------------------------------------------------------------------
# 13. Design change requests — the PM's, a coordinator's, or SCM's
#
# SESSION 3.1c-i — THE WINDOW IS ASKED IN ONE PLACE, PER USER. design_change_request()
# (the POST), design_change_request_form() (the GET), pm_change_request_targets() (the PM
# dashboard link) and the two SCM group screens all ask change_request_window_open().
# Before this the POST and the GET each computed `in_draft_group`, spelled differently
# (EXECUTION_MODULE_DEFERRED.md §B1), and the dashboard link used a third rule that
# skipped every released site.
# ---------------------------------------------------------------------------

# The 403 both change-request views return. Authority only — a refusal about the WINDOW is
# a message and a redirect, never a 403.
CHANGE_REQUEST_FORBIDDEN = ("Only the site's PM, one of its coordinators, or SCM may request "
                            "a design change.")


def _pre_release_window_open(assignment, attempt):
    """Part 4's window: QC has started on the current attempt, and the status is one of
    CHANGE_REQUEST_STATUSES. `released` is not in that tuple, so this is before release
    only."""
    return bool(attempt is not None and attempt.qc_started_at is not None
                and assignment.status in CHANGE_REQUEST_STATUSES)


def change_request_window_open(user, assignment):
    """Whether `user` may raise a design change request against `assignment` now.

    Two audiences, two windows (3.1c-i, Q1):

      the PM or a coordinator (user_can_manage_project)
          Part 4's pre-release window, OR permissions.design_change_window_open() —
          released and not in a locked procurement group (D-a).
      SCM (user_may_raise_design_change_as_scm)
          design_change_window_open() ONLY. SCM may not raise before release.

    Anybody else gets False. Somebody holding both authorities gets the PM's window, which
    contains SCM's.

    THIS IS THE WINDOW, NOT THE 403. Every caller asks user_can_request_design_change()
    first; the group screens ask it first so that a viewer who may not raise at all costs
    no window query.
    """
    if assignment is None:
        return False
    project = assignment.project
    if user_can_manage_project(user, project):
        return (design_change_window_open(assignment)
                or _pre_release_window_open(assignment, _current_attempt(assignment)))
    if user_may_raise_design_change_as_scm(user):
        return design_change_window_open(assignment)
    return False


def _change_request_refusal(user, assignment, attempt):
    """Why the window is closed for `user`, as (code, message).

    Call only where change_request_window_open() has returned False. The POST flashes the
    message; the form renders its branch from the code. One function, so the two screens
    cannot give different reasons for the same closed window.

    Checked in the order the POST always checked them — the lock first, because a locked
    BOQ is the one refusal no wait will lift.
    """
    project = assignment.project
    pid = project.project_id
    if project_boq_is_group_locked(project):
        membership = active_group_membership(project, GROUP_TYPE_PROCUREMENT)
        name = membership.group.name if membership is not None else ''
        return 'locked', (
            f'{pid}: change request refused — the BOQ is locked. This site is in the '
            f'locked procurement group "{name}", so its quantities are committed to a '
            f'purchase. A change now needs a variance against the order, which this system '
            f'does not handle yet.')
    # Released and not locked is open to both audiences, so from here on the design is
    # not released — which is a window SCM does not have.
    if not user_can_manage_project(user, project):
        return 'scm_before_release', (
            f'{pid}: SCM may raise a design change request only once the design is '
            f'released. This site is at "{assignment.get_status_display()}".')
    if attempt is None or attempt.qc_started_at is None:
        return 'qc_not_started', (
            f'{pid}: QC has not started on this package yet, so there is nothing settled '
            f'to raise a change against. Talk to the Design Head — a change at this stage '
            f'does not need a formal request.')
    return 'stage', (
        f'{pid}: a change request cannot be raised at this stage (status '
        f'"{assignment.get_status_display()}").')


# ---------------------------------------------------------------------------
# 13a-n. Who hears about a change request (session 3.1c-ii)
#
# The recipients are design_gate_next_actors()'s three change-request keys; these helpers
# only word and link the message. See the block above that function for the channels, and
# why the absolute link lives in the HTML part alone.
# ---------------------------------------------------------------------------

def _change_request_raiser_label(user, project):
    """The capacity a change request was raised in, for the message (D7). The order is the
    authority's own: the site's assigned PM, else a manager of the site (a Project
    Coordinator), else SCM. "requester" is the fall-through for anyone else; today
    user_can_request_design_change() admits nobody else, so a raise never reaches it."""
    profile = user.profile
    if project.assigned_pm_id == profile.pk:
        return 'PM'
    if user_can_manage_project(user, project):
        return 'Project Coordinator'
    if user_may_raise_design_change_as_scm(user):
        return 'SCM'
    return 'requester'


def _change_request_decider_label(user):
    """Who triaged: the Design Head, or his named deputy (user_has_design_head_authority
    admits both to the two triage views)."""
    return 'Design Head' if user_is_design_head(user) else "Design Head's deputy"


def _change_request_email(request, subject, body, quote_label, quote, link):
    """The HTML part: autoescaped, so a reason typed as markup arrives as text, and the one
    place the absolute URL appears. `link` is the relative in-app link; the host is the
    request's own, never a literal."""
    return render_to_string('projects/email/design_change_request.html', {
        'subject': subject, 'body': body, 'quote_label': quote_label, 'quote': quote,
        'url': request.build_absolute_uri(link),
    })


def _change_request_group_owner(membership):
    """D6. The SCM user to tell that an accepted request took a site out of their draft
    procurement group: whoever added the site, if active, else whoever created the group,
    if active. `membership` is the row design_change_request_accept() removed in its own
    transaction, or None, in which case nobody is added."""
    if membership is None:
        return None
    for person in (membership.added_by, membership.group.created_by):
        if person is not None and person.is_active and person.user.is_active:
            return person
    return None


@login_required
def design_change_request(request, project_id):
    """Raise a design change request: the site's PM, one of its coordinators, or SCM.

    PART 4.6 — RAISING A REQUEST NOW DOES ONE THING: IT CREATES A PENDING ROW.

    No attempt is opened, no status moves, the designer is not pulled off anything. The
    Design Head triages it (design_change_request_accept / _reject) and only ACCEPTANCE
    opens attempt N+1. Part 4 opened it here, automatically, which meant any assigned PM
    could push rework into a queue the Head owns without the Head being consulted.

    THE WINDOW is change_request_window_open(), per user (3.1c-i):

      PM / coordinator  QC started and not yet released (Part 4, settled decision 3), OR
                        released and not in a locked procurement group (D-a).
      SCM               released and not in a locked procurement group, only.

    Every closed window is a message and a redirect, not a 403; _change_request_refusal()
    words it. The 403 is authority alone.

    ONE PENDING REQUEST PER ATTEMPT. A second raise while one is untriaged is refused
    here with a message, and by a partial unique constraint underneath — two pending
    requests would give the Head two verdicts to record against one suspension.

    IF QC IS IN FLIGHT, IT IS SUSPENDED (settled decision 4), but the package STAYS at the
    gate it was at. The attempt keeps whatever verdicts it had; no verdict may be recorded
    while the request is pending, and if the Head rejects it the review resumes on the
    same attempt. Marking anything 'failed' here would charge the designer with a rework
    loop the PM caused, which is the reason both `opened_reason` values exist.

    THE GROUP, SINCE 3.1c-i:

      LOCKED procurement group -> REFUSED, for everybody. The quantities are committed to
                                  a purchase; the correction is a variance against that
                                  order, which is a separate feature.
      DRAFT procurement group  -> the request proceeds AND THE SITE STAYS IN THE GROUP.
      no group                 -> the request proceeds. Part 6 §4 refused this case as "a
                                  new scope of work"; D-a opened it.

    THE SITE NO LONGER LEAVES ITS GROUP HERE (3.1c-i, Q2 — reverses Part 4.6's choice).
    It leaves when the Design Head ACCEPTS, inside design_change_request_accept()'s
    transaction; a rejection leaves it where it was, so SCM never re-adds a site nobody
    changed. While the request is pending the site stays in its group's aggregate, and
    site_group_lock() refuses to lock a group holding it. Recorded in
    docs/execution-model.md §12.
    """
    project = _opex_site(project_id)
    assignment = getattr(project, 'design_assignment', None)
    if not user_can_request_design_change(request.user, project):
        return HttpResponseForbidden(CHANGE_REQUEST_FORBIDDEN)
    if request.method != 'POST':
        return redirect('design_change_request_form', project_id=project.project_id)

    def _back(msg, ok=False):
        (messages.success if ok else messages.error)(request, msg)
        return redirect('design_change_request_form', project_id=project.project_id)

    if assignment is None:
        return _back(f'{project.project_id}: design has not started on this site yet.')

    reason = (request.POST.get('reason') or '').strip()
    if not reason:
        return _back('Please say what needs to change — a reason is required.')

    attempt = _current_attempt(assignment)
    if attempt is None:
        return _back(f'{project.project_id}: design has not started on this site yet.')

    # The one window question, asked the way the form and both dashboards ask it.
    if not change_request_window_open(request.user, assignment):
        return _back(_change_request_refusal(request.user, assignment, attempt)[1])

    # PROCUREMENT: only a procurement group commits a BOQ, and the window above has already
    # refused a locked one — so a membership here is a DRAFT group, named in the message.
    membership = active_group_membership(project, GROUP_TYPE_PROCUREMENT)

    # PART 4.6 — one untriaged request at a time. The message is here; the partial unique
    # constraint underneath is what makes the rule true against a double submit or a
    # second PM posting concurrently, and its IntegrityError is caught below.
    existing = _pending_change_requests(attempt).first()
    if existing is not None:
        return _back(f'{project.project_id}: a change request raised by '
                     f'{existing.requested_by.user.get_full_name() or existing.requested_by.user.username} '
                     f'on {timezone.localtime(existing.requested_at):%d %b %Y} is already '
                     f'with the Design Head. Wait for his decision before raising another.')

    profile = request.user.profile
    # PART 9: a review is "in flight" at EITHER gate. A package sitting at
    # `awaiting_head_qc` has passed Design QC and is with the Head, so the request
    # suspends his review exactly as it suspends gate 1's — the message must say so.
    was_in_qc = assignment.status in (DESIGN_IN_QC, DESIGN_AWAITING_HEAD_QC)
    # Who is raising it, for the activity feed only. An SCM request is still a
    # `pm_change_request` everywhere else — no new reason value (3.1c-i).
    raised_as = ('PM change request raised' if user_can_manage_project(request.user, project)
                 else 'Change request raised by SCM')
    try:
        with transaction.atomic():
            # NO GROUP REMOVAL HERE ANY MORE (3.1c-i, Q2). A draft-group site stays in its
            # group until the Design Head accepts — see design_change_request_accept().
            change = DesignChangeRequest.objects.create(
                attempt=attempt, requested_by=profile, reason=reason,
                verdict=CHANGE_REQUEST_PENDING)
            log_activity(project, profile,
                         f'{raised_as} on attempt '
                         f'{attempt.attempt_number} — awaiting the Design Head: {reason}'
                         + (' (review in progress — suspended)' if was_in_qc else ''),
                         entity_type='DesignChangeRequest', entity_id=change.pk,
                         action_code='design_change_requested')
    except IntegrityError:
        # The database refused it — two raises raced past the pre-check above. The
        # constraint is the authority and this is the message that says so.
        return _back(f'{project.project_id}: refused by the database — only one change '
                     f'request at a time may await the Design Head on an attempt.')

    # SESSION 3.1c-ii — the Design Heads, the site's PM and its coordinators hear about it,
    # in-app AND by email. After the atomic block, so no send can unwind the request.
    try:
        who = profile.user.get_full_name() or profile.user.username
        capacity = _change_request_raiser_label(request.user, project)
        subject = f'{project.project_id}: design change request raised'
        body = (f'{project.project_id}: a design change request is waiting for the Design '
                f'Head. {who} ({capacity}) raised it on attempt {attempt.attempt_number}.'
                + (' The review in progress is suspended until it is decided.'
                   if was_in_qc else ''))
        for recipient in design_gate_next_actors(assignment, GATE_CHANGE_RAISED,
                                                 change=change, actor=profile):
            # The triage buttons are on the QC review screen, which the PM, a coordinator
            # and SCM cannot open; they get the change-request screen instead.
            if user_has_design_head_authority(recipient.user):
                link = reverse('design_qc_review', kwargs={'project_id': project.project_id})
            else:
                link = reverse('design_change_request_form',
                               kwargs={'project_id': project.project_id})
            send_notification(
                recipient=recipient,
                message=f'{body} Reason: "{reason}"',
                channels=['in_app', 'email'],
                link=link,
                subject=subject,
                html_message=_change_request_email(request, subject, body, 'Reason',
                                                   reason, link),
                template='design_change_request_raised',  # a NotificationLog label
                related_project=project, actor=profile)
    except Exception:
        logger.exception('design_change_request: the notification for %s failed; the '
                         'change request stands.', project.project_id)

    # NOTHING ELSE HAPPENED, and the message must not imply otherwise. No attempt was
    # opened and the site did not move; the designer is still on whatever they were on.
    msg = (f'{project.project_id}: change request raised on attempt '
           f'{attempt.attempt_number} and sent to the Design Head. No new attempt has '
           f'been opened — he decides whether this becomes rework.')
    if was_in_qc:
        msg += (' The review in progress is suspended: no verdict can be recorded until '
                'he rules.')
    if membership is not None:
        msg += (f' The site stays in procurement group "{membership.group.name}" while he '
                f'decides, and the group cannot be locked until he does. It leaves the '
                f'group only if he accepts.')
    return _back(msg, ok=True)


# ---------------------------------------------------------------------------
# 13b. Design Head triage of PM change requests (Part 4.6)
#
# THE HEAD OWNS THE QUEUE, SO THE HEAD OWNS WHAT ENTERS IT. Every other route into the
# design queue — allocation, a QC failure, a due-date extension — already passes through
# him. A PM change request was the one that did not, and "any PM may force rework at will"
# is not a workload model.
#
# DESIGN QC IS NOT ADMITTED. QC judges whether a package is right; it does not decide
# whether the brief may move or who absorbs the rework. Permission is the Part 4
# head-authority helper, deputy included, and nothing else.
#
# BOTH ACTIONS RE-READ THE VERDICT INSIDE THE TRANSACTION. Two Heads on the same request,
# or a double-submitted form, must not produce two decisions — the second finds it already
# triaged and is told so.
# ---------------------------------------------------------------------------

def _change_request_or_404(pk):
    return get_object_or_404(
        DesignChangeRequest.objects.select_related(
            'attempt__assignment__project', 'requested_by__user'),
        pk=pk, attempt__assignment__project__is_deleted=False)


def _triage_guard(request, change):
    """Shared front half of both triage views: permission and method.

    Returns (project, refusal_response_or_None). Written once because the two views must
    refuse identically — a reject endpoint that admits somebody the accept endpoint
    refuses is the same hole either way round.
    """
    project = change.attempt.assignment.project
    if not user_has_design_head_authority(request.user):
        return project, HttpResponseForbidden(
            'Only the Design Head or his named deputy may action a PM change request.')
    if request.method != 'POST':
        return project, redirect('design_qc_review', project_id=project.project_id)
    return project, None


def _triage_redirect(request, project):
    """Where a triage action lands. `next` is honoured so the Head working the dashboard
    queue stays on it, but ONLY if it is a local path — a POSTed absolute URL is an open
    redirect, and this endpoint is reachable by anyone with head authority."""
    target = (request.POST.get('next') or '').strip()
    if target.startswith('/') and not target.startswith('//'):
        return target
    return reverse('design_qc_review', kwargs={'project_id': project.project_id})


@login_required
def design_change_request_accept(request, pk):
    """The Head agrees the brief moved. THIS is what opens attempt N+1.

    It calls `_open_next_attempt()` — the one shared attempt-opening function, the same
    one both QC-failure loops use — with `opened_reason='pm_change_request'` and `redo`
    left at None. None is correct and deliberate: the brief moved, so nothing drawn
    against the old brief can be assumed to still hold, and carrying an Arka forward
    would preserve work against a requirement that no longer exists.

    THE CLOSED ATTEMPT KEEPS BOTH VERDICTS AT 'pending'. It was interrupted, not judged.
    Writing 'failed' at either gate would charge the designer with a rework loop the PM
    caused and inflate the QC failure rate with work nobody found fault in — the exact
    corruption `opened_reason` exists to prevent.

    SESSION 3.1c-i ADDS THREE THINGS, all inside the one transaction:

      * A RE-CHECK OF THE LOCK. If the site's BOQ has been committed to a purchase since the
        request was raised — project_boq_is_group_locked() — the acceptance is refused and
        nothing is written; the request stays pending for the Head to reject with a reason.
        Asked whichever window the request was raised in: a locked BOQ is the one thing
        that forbids reopening at any stage.
      * THE GROUP DEPARTURE (Q2). A site in a draft procurement group leaves it HERE, not at
        raise, through remove_from_group() — so a rejected request leaves the group as it
        was.
      * THE RELEASE STAMP IS CLEARED. `released_at` / `released_by` go to None in the SAME
        apply_design_status() write that moves the status, through _open_next_attempt()'s
        `extra_fields`. The StatusTransition row keeps the history; the stamps describe a
        release that no longer stands. Only this caller passes it — the QC-fail and
        send-back loops are unchanged.

    LOCK ORDER: the request row, then the group row (if the site has a live procurement
    membership), then the assignment row. site_group_lock() takes no row lock of its own
    (EXECUTION_MODULE_DEFERRED.md §D43), so the re-check narrows the race with a concurrent
    lock rather than closing it.
    """
    change = _change_request_or_404(pk)
    project, refusal = _triage_guard(request, change)
    if refusal is not None:
        return refusal

    assignment = change.attempt.assignment
    profile = request.user.profile

    def _back(msg, ok=False):
        (messages.success if ok else messages.error)(request, msg)
        return redirect(_triage_redirect(request, project))

    with transaction.atomic():
        # Re-read under the transaction; the row may have been triaged since the page
        # that rendered this button was drawn.
        change = (DesignChangeRequest.objects.select_for_update()
                  .select_related('attempt__assignment').get(pk=change.pk))
        if change.verdict != CHANGE_REQUEST_PENDING:
            return _back(f'{project.project_id}: that change request has already been '
                         f'{change.get_verdict_display().lower()}.')

        membership = active_group_membership(project, GROUP_TYPE_PROCUREMENT)
        if membership is not None:
            SiteGroup.objects.select_for_update().get(pk=membership.group_id)
            # Re-read once the group is held: SCM may have removed the site or locked the
            # group between the read above and the lock.
            membership = active_group_membership(project, GROUP_TYPE_PROCUREMENT)
        assignment = DesignAssignment.objects.select_for_update().get(pk=assignment.pk)

        if project_boq_is_group_locked(project):
            name = membership.group.name if membership is not None else ''
            return _back(f'{project.project_id}: change request not accepted — the BOQ was '
                         f'locked in procurement group "{name}" after this request was '
                         f'raised, so it can no longer reopen the design. Nothing was '
                         f'changed. Reject it with a reason to close it.')

        change.verdict    = CHANGE_REQUEST_ACCEPTED
        change.decided_by = profile
        change.decided_at = timezone.now()
        change.save(update_fields=['verdict', 'decided_by', 'decided_at'])

        left_group = None
        # The membership removed IN THIS TRANSACTION, carried out of the block for the
        # notification (D6) rather than re-read after it.
        left_membership = None
        if membership is not None:
            remove_from_group(membership, profile, CHANGE_REQUEST_REMOVAL_REASON)
            left_group = membership.group.name
            left_membership = membership

        # The outgoing attempt's verdicts are deliberately untouched — see the docstring.
        # _open_next_attempt() sets closed_at and nothing else on it.
        new_attempt = _open_next_attempt(
            assignment, ATTEMPT_REASON_PM_CHANGE_REQUEST, profile,
            f'change request accepted on attempt {change.attempt.attempt_number}',
            extra_fields={'released_at': None, 'released_by': None})

        change.resulting_attempt = new_attempt
        change.save(update_fields=['resulting_attempt'])

        log_activity(project, profile,
                     f'PM change request accepted — attempt '
                     f'{new_attempt.attempt_number} opened: {change.reason}',
                     entity_type='DesignChangeRequest', entity_id=change.pk,
                     action_code='design_change_request_accepted')

    # SESSION 3.1c-ii — the requester, the site's PM and coordinators, the designer and, if
    # the site left a draft group here, that group's SCM owner. In-app AND email, after
    # the atomic block. The Design Heads are not told: one of them made the decision.
    try:
        who = profile.user.get_full_name() or profile.user.username
        label = _change_request_decider_label(request.user)
        requester = change.requested_by
        requester_name = requester.user.get_full_name() or requester.user.username
        subject = (f'{project.project_id}: design change request accepted — attempt '
                   f'{new_attempt.attempt_number} opened')
        recipients = design_gate_next_actors(assignment, GATE_CHANGE_ACCEPTED,
                                             change=change, actor=profile)
        owner = _change_request_group_owner(left_membership)
        if (owner is not None and owner.pk != profile.pk
                and all(person.pk != owner.pk for person in recipients)):
            recipients.append(owner)
        for recipient in recipients:
            whose = ('your design change request' if recipient.pk == requester.pk
                     else f'the design change request {requester_name} raised')
            body = (f'{project.project_id}: {who} ({label}) accepted {whose}. Attempt '
                    f'{new_attempt.attempt_number} is open and the site is back with the '
                    f'designer.'
                    + (f' The site left procurement group "{left_group}".'
                       if left_group is not None else ''))
            if recipient.pk == assignment.assigned_to_id:
                link = reverse('design_site_workspace',
                               kwargs={'project_id': project.project_id})
            else:
                link = reverse('design_change_request_form',
                               kwargs={'project_id': project.project_id})
            send_notification(
                recipient=recipient,
                message=f'{body} Request: "{change.reason}"',
                channels=['in_app', 'email'],
                link=link,
                subject=subject,
                html_message=_change_request_email(request, subject, body, 'Request',
                                                   change.reason, link),
                template='design_change_request_accepted',  # a NotificationLog label
                related_project=project, actor=profile)
    except Exception:
        logger.exception('design_change_request_accept: the notification for %s failed; '
                         'the acceptance stands.', project.project_id)

    msg = (f'{project.project_id}: change request accepted — attempt '
           f'{new_attempt.attempt_number} opened and the site is back with the designer.')
    if left_group is not None:
        msg += f' It left procurement group "{left_group}".'
    return _back(msg, ok=True)


@login_required
def design_change_request_reject(request, pk):
    """The Head refuses: the current version stands, and he says why.

    THE REASON IS MANDATORY, and enforced by a CHECK constraint as well as by this view.
    A rejection without a justification is a rubber stamp, and a rubber stamp is worse
    than no triage at all — it adds a step and removes nothing.

    NOTHING REOPENS. No attempt, no status change, no appeal path. If the PM still wants
    the change they raise a new request, which is a fresh row with a fresh reason rather
    than an escalation of this one. A review that was suspended by this request may now
    resume on the SAME attempt — `_pending_change_requests()` stops matching it, so both
    gates unblock by themselves.
    """
    change = _change_request_or_404(pk)
    project, refusal = _triage_guard(request, change)
    if refusal is not None:
        return refusal

    profile = request.user.profile

    def _back(msg, ok=False):
        (messages.success if ok else messages.error)(request, msg)
        return redirect(_triage_redirect(request, project))

    reason = (request.POST.get('rejection_reason') or '').strip()
    if not reason:
        return _back(f'{project.project_id}: say why the current version stands — a '
                     f'rejection reason is required.')

    with transaction.atomic():
        change = (DesignChangeRequest.objects.select_for_update()
                  .select_related('attempt__assignment').get(pk=change.pk))
        if change.verdict != CHANGE_REQUEST_PENDING:
            return _back(f'{project.project_id}: that change request has already been '
                         f'{change.get_verdict_display().lower()}.')

        change.verdict          = CHANGE_REQUEST_REJECTED
        change.rejection_reason = reason
        change.decided_by       = profile
        change.decided_at       = timezone.now()
        change.save(update_fields=['verdict', 'rejection_reason',
                                   'decided_by', 'decided_at'])

        log_activity(project, profile,
                     f'PM change request rejected on attempt '
                     f'{change.attempt.attempt_number} — the current version stands: '
                     f'{reason}',
                     entity_type='DesignChangeRequest', entity_id=change.pk,
                     action_code='design_change_request_rejected')

    # SESSION 3.1c-ii — the requester alone, with the Head's reason. In-app AND email,
    # after the atomic block.
    try:
        who = profile.user.get_full_name() or profile.user.username
        label = _change_request_decider_label(request.user)
        subject = f'{project.project_id}: design change request rejected'
        body = (f'{project.project_id}: {who} ({label}) rejected your design change request '
                f'on attempt {change.attempt.attempt_number}. The current design stands and '
                f'no new attempt was opened.')
        link = reverse('design_change_request_form', kwargs={'project_id': project.project_id})
        for recipient in design_gate_next_actors(change.attempt.assignment,
                                                 GATE_CHANGE_REJECTED,
                                                 change=change, actor=profile):
            send_notification(
                recipient=recipient,
                message=f'{body} Reason: "{reason}"',
                channels=['in_app', 'email'],
                link=link,
                subject=subject,
                html_message=_change_request_email(request, subject, body, 'Reason',
                                                   reason, link),
                template='design_change_request_rejected',  # a NotificationLog label
                related_project=project, actor=profile)
    except Exception:
        logger.exception('design_change_request_reject: the notification for %s failed; '
                         'the rejection stands.', project.project_id)

    return _back(f'{project.project_id}: change request rejected — the current version '
                 f'stands and no new attempt was opened. Any review suspended by it can '
                 f'now resume.', ok=True)


# ---------------------------------------------------------------------------
# 14. Part 4 screens
# ---------------------------------------------------------------------------

def _attempt_history(assignment):
    """Every attempt on an assignment, oldest first, with the change requests that
    closed each one. The two rework loops must be tellable apart at a glance, which is
    what `opened_reason` renders as on the screens."""
    return list(assignment.attempts
                .select_related('qc_reviewed_by__user', 'head_reviewed_by__user',
                                'boq_submitted_by__user')
                .prefetch_related('change_requests__requested_by__user',
                                  # Part 4.6 — the partial renders the triager and the
                                  # attempt an acceptance opened; both would be N+1
                                  # queries per change request without this.
                                  'change_requests__decided_by__user',
                                  'change_requests__resulting_attempt',
                                  'arka_submissions')
                .order_by('attempt_number'))


def _qc_scope(user):
    """The Q() limiting a QC reviewer's worklist to sites that are theirs to review.

    THREE ANSWERS, matching the three kinds of person who can reach this screen:

        Head authority      -> Q()    unscoped, the whole gate
        is_design_qc holder -> their own assignments PLUS the open pool
        plain designer      -> their own assignments ONLY

    HEAD AUTHORITY IS DELIBERATELY UNSCOPED. The Head runs the gate; hiding sites he has
    assigned to somebody else would remove from his screen exactly the work he assigned and
    is accountable for. The narrowing exists so a reviewer is never shown a colleague's
    queue, not to partition the gate.

    A user holding BOTH the QC flag and Head authority gets the unscoped view: he is
    admitted here as the Head, and the wider of two overlapping claims is the honest one.

    THE THIRD BRANCH IS SESSION B.1 AND IT IS LOAD-BEARING. A plain designer previously
    fell through to Q() — harmless only because every read gate refused them entry, so the
    branch was unreachable. user_can_view_qc_queue() now admits them, and an unscoped Q()
    would show a designer with one assignment every reviewable site in the system. They get
    the open pool arm removed, not merely narrowed: the pool is what `is_design_qc` grants
    and they do not hold it.

    THIS SCOPES VISIBILITY, NOT AUTHORITY. Whether a row's buttons are live is still
    user_can_qc_gate_design() per row, which applies the same assignment rule and the two
    exclusions this cannot see. A screen filter is not a gate.
    """
    if user_has_design_head_authority(user):
        return Q()
    profile = getattr(user, 'profile', None)
    if profile is None:
        return Q()
    if user_is_design_qc(user):
        return Q(qc_assigned_to__isnull=True) | Q(qc_assigned_to=profile)
    return Q(qc_assigned_to=profile)


@login_required
def design_qc_queue(request):
    """The review worklist for BOTH artifacts and BOTH gates.

    Deliberately NOT the Design Head dashboard — no metrics, no workload, no capacity, no
    overdue logic. It is a worklist of the five reviewable statuses and nothing else; the
    dashboards are Part 5 and Part 9 §6.

    PART 9 — ONE QUEUE, TWO AUDIENCES, TWO ARTIFACTS.

    Design QC and the Head share this screen and see the same rows, because knowing what is
    stacked up at the other gate is exactly the information a reviewer needs. What differs
    is which row each can ACT on, and that is computed PER ROW because both the self-review
    exclusion and the one-person-two-verdicts rule are per site.

    IT CARRIES ARKAS AS WELL AS PACKAGES, and it has to. The Arka is the FIRST thing
    Design QC reviews, and until Part 9 the only route to `design_head_review` was the
    Head's per-tender site list — a screen a Design QC reviewer cannot open, because it is
    gated on Head authority. Without the Arka section here, a QC reviewer's dashboard could
    correctly report "2 Arka awaiting your verdict" with nowhere to click, which is exactly
    the failure Part 4.5 called out about screens reachable only by typing a URL.

    Two querysets rather than one: the two artifacts live at different statuses, need
    different verdict URLs, and read better as separate sections than as one list where
    half the rows have no package to open.
    """
    # Session B.1 — the queue's OWN gate, one case wider than the dashboard's: a plain
    # designer who has been assigned at least one site gets in. Safe because _qc_scope()
    # below hands them their assignments and nothing else — the door and what is behind it
    # are decided together, and neither is sufficient alone.
    if not user_can_view_qc_queue(request.user):
        return HttpResponseForbidden(
            'Design QC, Design Head, named deputy, or a designer assigned to review a site.')

    profile = getattr(request.user, 'profile', None)

    # Session B — a QC reviewer sees their own sites plus the open pool, never a
    # colleague's. Head authority is unscoped: the Head's job is to see the whole gate,
    # and scoping him would hide work he is accountable for. See _qc_scope().
    scope = _qc_scope(request.user)

    # ── Arkas: awaiting gate 1, or passed gate 1 and awaiting gate 2 ──────────
    arka_assignments = (DesignAssignment.objects
                        .filter(scope,
                                status__in=(DESIGN_ARKA_SUBMITTED,
                                            DESIGN_AWAITING_HEAD_ARKA),
                                project__is_deleted=False)
                        .select_related('project', 'project__program', 'assigned_to__user')
                        .order_by('status', 'project__project_id'))

    arka_rows = []
    for assignment in arka_assignments:
        attempt = _current_attempt(assignment)
        arka = _current_arka(attempt)
        if arka is None:
            continue
        # A site at `arka_submitted` whose Arka the Head has ALREADY approved is not in
        # anybody's review queue — it is with the designer, owing CAD and BOQ. See the
        # Part 9 status note: that combination is the "artifacts outstanding" state.
        if arka.head_verdict == ARKA_APPROVED:
            continue
        awaiting_head = assignment.status == DESIGN_AWAITING_HEAD_ARKA
        can_qc_gate   = user_can_qc_gate_design(request.user, assignment)
        can_head_gate = user_can_head_gate_design(request.user, assignment)
        own_qc_verdict = _other_gate_actor_conflict(profile, arka.reviewed_by_id)
        arka_rows.append({
            'assignment':    assignment,
            'site':          assignment.project,
            'attempt':       attempt,
            'arka':          arka,
            'awaiting_head': awaiting_head,
            'can_qc':        (can_qc_gate and not awaiting_head
                              and arka.verdict == ARKA_PENDING),
            'can_head':      (can_head_gate and awaiting_head
                              and arka.head_verdict == ARKA_PENDING
                              and not own_qc_verdict),
            'blocked_own':   (can_head_gate and awaiting_head
                              and arka.head_verdict == ARKA_PENDING
                              and own_qc_verdict),
        })

    # ── Packages: awaiting gate 1, in gate 1, or awaiting gate 2 ──────────────
    # `pm_rejected` IS DELIBERATELY NOT IN THIS TUPLE (prompt 3.1b-2b, §D14). It is not a
    # review: both gates have passed, and the Head's answer — return to the PM, or send back
    # to the designer — lives on design_qc_review, reached from design_head_sites. Added
    # here, it would make `can_qc` below True for a QC reviewer on a package that is not
    # theirs to judge.
    assignments = (DesignAssignment.objects
                   .filter(scope,
                           status__in=(DESIGN_ARTIFACTS_UPLOADED, DESIGN_IN_QC,
                                       DESIGN_AWAITING_HEAD_QC),
                           project__is_deleted=False)
                   .select_related('project', 'project__program', 'assigned_to__user')
                   .order_by('status', 'project__project_id'))

    rows = []
    for assignment in assignments:
        attempt = _current_attempt(assignment)
        awaiting_head = assignment.status == DESIGN_AWAITING_HEAD_QC
        can_qc_gate   = user_can_qc_gate_design(request.user, assignment)
        can_head_gate = user_can_head_gate_design(request.user, assignment)
        rows.append({
            'assignment':  assignment,
            'site':        assignment.project,
            'attempt':     attempt,
            'arka':        _current_arka(attempt),
            'in_qc':       assignment.status == DESIGN_IN_QC,
            'awaiting_head': awaiting_head,
            'open_crs':    list(_pending_change_requests(attempt)),
            # Gate 1 actions: start review, then pass/fail. Not offered once the package
            # has moved past Design QC.
            'can_qc':      can_qc_gate and not awaiting_head,
            # Gate 2 actions. Withheld when this user recorded the QC verdict themselves —
            # settled decision 2, shown as a disabled state rather than a silent absence.
            'can_head':    (can_head_gate and awaiting_head and attempt is not None
                            and not _other_gate_actor_conflict(
                                profile, attempt.qc_reviewed_by_id)),
            'blocked_own': (can_head_gate and awaiting_head and attempt is not None
                            and _other_gate_actor_conflict(
                                profile, attempt.qc_reviewed_by_id)),
        })

    return render(request, 'projects/design/qc_queue.html', {
        'arka_rows': arka_rows,
        'rows':      rows,
        'is_deputy': user_is_design_head_deputy(request.user) and not user_is_design_head(request.user),
        'is_design_qc': user_is_design_qc(request.user),
        'has_head_authority': user_has_design_head_authority(request.user),
    })


@login_required
def design_qc_review(request, project_id):
    """Head / deputy: the full package for one site — Arka link and capacity, CAD and
    BOQ files by signed URL, BOQ link, attempt history — with the QC actions."""
    project = _opex_site(project_id)
    # PROMPT 3.1b-2c (§D25) — the PM's rejection remark, read HERE, where the Head decides
    # what to do about it. This query REPLACES the `project.design_assignment` read that used
    # to fetch the row, so the remark costs no query: two correlated subqueries on the one
    # SELECT that was already being issued. Filtering on the reason alone is exact at
    # `pm_rejected`, because design_pm_reject() is the only writer of that status — the
    # latest PM-rejection row is the one that put the package here.
    assignment = (DesignAssignment.objects.filter(project=project)
                  .annotate(
                      pm_rejection_remark=latest_design_transition(
                          'remark', reason_code=REASON_DESIGN_PM_REJECTED),
                      pm_rejected_at=latest_design_transition(
                          'occurred_at', reason_code=REASON_DESIGN_PM_REJECTED))
                  .first())
    if assignment is None:
        raise Http404('No design assignment for this site.')
    # Seat the row in the reverse-relation cache, so the helpers below that reach it through
    # `project.design_assignment` (the BOQ-lock and correction predicates) read this instance
    # instead of issuing the query this one replaced.
    project.design_assignment = assignment
    # Session B.1 — PER SITE, and that is the whole difference from the queue's gate. A
    # plain designer reaches this screen for the one site they were named on and no other;
    # an unassigned site refuses them exactly as it did before B.1.
    if not (user_can_view_design_qc_dashboard(request.user)
            or user_is_assigned_qc_reviewer(request.user, project)):
        return HttpResponseForbidden(
            'Design QC, Design Head, named deputy, or the designer assigned to review '
            'this site.')

    ctx = _workspace_context(project, assignment)
    attempt  = ctx['attempt']
    open_crs = list(_pending_change_requests(attempt))
    profile  = getattr(request.user, 'profile', None)

    can_qc_gate   = user_can_qc_gate_design(request.user, assignment)
    can_head_gate = user_can_head_gate_design(request.user, assignment)
    awaiting_head = assignment.status == DESIGN_AWAITING_HEAD_QC

    # Settled decision 2, evaluated once and reused: did this actor record the OTHER
    # gate's verdict on this exact attempt?
    own_qc_verdict = bool(attempt is not None and _other_gate_actor_conflict(
        profile, attempt.qc_reviewed_by_id))

    ctx.update({
        'history':       _attempt_history(assignment),
        'open_crs':      open_crs,
        'can_qc':        can_qc_gate,
        'can_start_qc':  can_qc_gate and assignment.status == DESIGN_ARTIFACTS_UPLOADED
                         and _package_is_complete(attempt),
        # Gate 1 verdict form.
        'can_verdict':   can_qc_gate and assignment.status == DESIGN_IN_QC and not open_crs,
        # Gate 2 verdict form. Mutually exclusive with the above by status.
        'can_head_verdict': (can_head_gate and awaiting_head and not open_crs
                             and not own_qc_verdict),
        # Why the gate-2 form is absent, when it is absent for this reason and not
        # because of the status. An empty screen explains nothing.
        'blocked_by_own_qc_verdict': can_head_gate and awaiting_head and own_qc_verdict,
        # PROMPT 3.1b-2b — the Design Head's two answers to a PM rejection: return it to the
        # PM unchanged, or send it back to the designer. Gate-2 authority, so the assigned
        # designer is refused even when they hold Head authority; both endpoints re-check it
        # through _qc_guard(). Set here, never derived in the template (R-13).
        'can_resolve_pm_rejection': can_head_gate and assignment.status == DESIGN_PM_REJECTED,
        # PROMPT 3.1b-2c (§D25) — the PM's words beside the two actions that answer them.
        # Shown only at `pm_rejected`: an earlier rejection the Head has already answered is
        # history (the ledger), not the question in front of him.
        'pm_rejection_remark': (assignment.pm_rejection_remark
                                if assignment.status == DESIGN_PM_REJECTED else ''),
        'pm_rejected_at': (assignment.pm_rejected_at
                           if assignment.status == DESIGN_PM_REJECTED else None),
        'awaiting_head': awaiting_head,
        'is_self_qc':    user_is_assigned_designer(request.user, assignment),
        # Part 4.6 — drives the triage buttons on the pending-change-request banner.
        # Head authority, NOT gate-2 authority: triaging a change request is not a review
        # of the package, so the self-QC and one-person-two-verdicts rules do not apply.
        'has_head_authority': user_has_design_head_authority(request.user),
        'is_deputy':     user_is_design_head_deputy(request.user) and not user_is_design_head(request.user),
        'released':      assignment.status == DESIGN_RELEASED,
        'error_categories': DESIGN_ERROR_CATEGORY_CHOICES,
        # Part 9.1 — drives the checkbox pre-ticking on the two fail forms. Serialised
        # here rather than assembled in the template so the mapping has exactly one home
        # (models.DEFAULT_REDO_BY_CATEGORY) and the JS reads it rather than repeating it.
        'redo_defaults_json': json.dumps(
            {category: default_redo_for_category(category)
             for category in DESIGN_ERROR_CATEGORIES}),
        # What is actually available to carry, so the form can say "carried forward"
        # against a real artifact rather than offering an empty promise.
        'has_approved_arka_to_carry': bool(
            ctx['arka'] is not None and ctx['arka'].head_verdict == ARKA_APPROVED),
    })
    ctx.update(_boq_review_panel(ctx['boq']))
    # Session B - whether this reviewer may CORRECT the bill they are reading, rather than
    # only fail the attempt over it. Taken from the permission helper rather than spelled
    # as `can_qc_gate or can_head_gate` here: the rule has one home, and the screen must
    # not be able to offer a link the endpoint would refuse.
    ctx['can_correct_boq'] = user_can_correct_boq(request.user, project)
    return render(request, 'projects/design/qc_review.html', ctx)


# ---------------------------------------------------------------------------
# 15. Dashboard integration (Part 4.5)
#
# The OPEX design workflow's screens were URL-reachable only through Parts 2-4, which
# is unusable: nobody types a URL. These helpers give views.py the per-site context it
# needs to render design state and ONE contextual action inside the tender card that
# already exists on the Design dashboard.
#
# THEY COMPUTE CONTEXT AND NOTHING ELSE. No status is written, no row is created, no
# permission is decided here — the action a helper offers is only ever a link or a form
# pointing at a Part 2-4 endpoint that re-checks authority for itself. A button this
# code chooses to render is a convenience; the view behind it is the gate.
# ---------------------------------------------------------------------------

# One primary action per assignment status, from the allocated designer's point of view.
# `kind` tells the template what to render:
#   'propose_due'  — inline date form posting to design_due_date_propose
#   'link'         — a button to the screen carrying that step's form
#   'none'         — nothing to do; `waiting` explains who is holding the ball
#
# EXACTLY ONE ACTION IS OFFERED AT A TIME. Showing every button always is what makes a
# workflow screen unreadable, and the status already determines which single step is legal —
# every other endpoint would refuse anyway.
_DESIGNER_ACTIONS = {
    DESIGN_AWAITING_SURVEY:     ('none', '', 'The Design Head has not provided the survey yet.'),
    DESIGN_AWAITING_ALLOCATION: ('none', '', 'Waiting for the Design Head to allocate this site.'),
    DESIGN_ALLOCATED:           ('propose_due', 'Propose due date', ''),
    DESIGN_DUE_DATE_PROPOSED:   ('none', '', 'Waiting for the Design Head to approve your proposed date.'),
    DESIGN_IN_DESIGN:           ('link', 'Submit Arka', ''),
    DESIGN_ARKA_REJECTED:       ('link', 'Submit revised Arka', ''),
    DESIGN_ARTIFACTS_UPLOADED:  ('none', '', 'Package complete — waiting for Design QC to start.'),
    DESIGN_IN_QC:               ('none', '', 'In review with Design QC.'),
    DESIGN_RELEASED:            ('none', '', 'Design released. Nothing further to do.'),
    DESIGN_SURVEY_RETURNED:     ('none', '', 'Design Hold — waiting for a replacement survey.'),
    # PART 9 — the two waiting rooms. Both name WHICH reviewer is holding the ball, which
    # is the entire reason they are separate statuses rather than a flag.
    DESIGN_AWAITING_HEAD_ARKA:  ('none', '', 'Arka passed Design QC — waiting for the '
                                             'Design Head to approve it.'),
    DESIGN_AWAITING_HEAD_QC:    ('none', '', 'Package passed Design QC — waiting for the '
                                             'Design Head\'s review.'),
    # PROMPT 3.1a — no action, and says who holds the ball. Without this entry the
    # .get() default renders a card with no action AND no text, which is the failure
    # this table exists to prevent.
    DESIGN_AWAITING_PM_APPROVAL: ('none', '', 'Design passed both review gates — with the '
                                              'PM for approval.'),
    # No action: the designer does not hold the site —
    # the Design Head decides whether it goes back to the PM or comes back to them.
    DESIGN_PM_REJECTED:          ('none', '', 'Returned by the PM — the Design Head is '
                                              'reviewing the rejection.'),
}


def designer_dashboard_context(profile, projects):
    """Per-site design context for the Design dashboard's tender cards.

    `projects` is the queryset the dashboard already fetched — this issues no project
    query of its own. Returns {project_pk: context_dict} covering ONLY OPEX sites that
    have a DesignAssignment; every other project is absent from the mapping, so the
    template renders nothing extra for Residential and the existing cards are untouched.

    `profile` is the viewing user. Actions are offered only where they are the allocated
    designer — a Design Head looking at somebody else's site gets the state and no
    buttons.
    """
    out = {}
    for project in projects:
        if project.project_type != 'OPEX':
            continue
        assignment = getattr(project, 'design_assignment', None)
        if assignment is None:
            continue

        is_designer = assignment.assigned_to_id == profile.pk
        attempt     = _current_attempt(assignment)
        arka        = _current_arka(attempt)
        commitments = list(assignment.due_date_commitments.all())
        # PART 8: the dashboard shows the AGREED date. Reading the is_current row here
        # would blank the due date on every card the moment its designer asked for an
        # extension — and would show the requested date as though it had been granted.
        current_due = effective_commitment(commitments)
        pending_due = pending_extension(commitments)

        # `arka_submitted` is the one status whose next step depends on the Arka verdict
        # rather than on the status alone, so it is resolved here instead of in the table.
        kind, label, waiting = _DESIGNER_ACTIONS.get(
            assignment.status, ('none', '', ''))
        # THE VERDICT NO LONGER DECIDES WHAT THE DESIGNER MAY DO, ONLY WHAT THE PACKAGE
        # NEEDS. This branch used to read the Arka's head_verdict FIRST and offer nothing
        # at all until both gates had passed — correct while CAD and BOQ were gated on the
        # approval, and a card that hides real work now that they are not. A designer at
        # `arka_submitted` with a pending Arka may upload the CAD (it records the current
        # version, which needs no verdict) and may complete the BOQ (which never derived
        # from the layout). So the outstanding ARTIFACT is what the card offers, and the
        # verdict only changes what it says while waiting.
        if assignment.status == DESIGN_ARKA_SUBMITTED:
            # UNCHANGED, DELIBERATELY: CAD_KINDS, not PROGRESSION_CAD_KINDS. A lone legacy
            # cad_pdf satisfies this test but does NOT satisfy the progression rule, so a
            # site carrying one is told there is nothing to upload while
            # _maybe_advance_to_artifacts_uploaded() still waits for a zip. That mismatch
            # predates this change and is left alone rather than quietly altered here —
            # the legacy kinds can no longer be uploaded, so no attempt still in flight
            # can enter the state.
            has_cad  = bool(attempt and attempt.design_files.filter(
                kind__in=CAD_KINDS, is_current=True).exists())
            boq_done = bool(attempt and attempt.boq_submitted_at)
            approved = arka is not None and arka.head_verdict == ARKA_APPROVED

            if not has_cad:
                kind, label, waiting = 'link', 'Upload CAD', ''
            elif not boq_done:
                kind, label, waiting = 'link', 'Enter BOQ', ''
            elif approved:
                # Reachable only as a belt-and-braces case: the write that completed the
                # package would already have advanced the status off `arka_submitted`.
                kind, label, waiting = 'none', '', 'Package complete — waiting for Design QC.'
            else:
                # Everything the designer owes is in; the Arka verdict is the only thing
                # left, and now it genuinely IS the thing being waited on.
                kind, label, waiting = ('none', '', 'CAD and BOQ are in — waiting for the '
                                                   'Arka verdict to complete the package.')

            # THE ARKA-PENDING FACT IS NOT DROPPED WHEN AN ACTION IS OFFERED. The card
            # renders `waiting` only when no action is available (see
            # _dashboard_design_actions.html), but the same card's status chip already
            # reads "Design: Arka awaiting Design QC" from _dashboard_design_chips.html —
            # so the designer sees both the button and who is holding the Arka, and this
            # key stays populated for any other reader of the context.
            if not approved and not waiting:
                waiting = 'Arka is with Design QC — your CAD and BOQ do not wait for it.'

        # The remarks the designer has to act on: the most recent FAILED attempt, at
        # EITHER gate. Part 9 made this two fields — a package failed by the Head carries
        # head_remarks and an empty qc_remarks, and reading only the QC field would leave
        # the designer with a reopened attempt and no visible reason for it.
        # Read off the attempts already loaded rather than re-querying per card.
        last_failed = None
        for a in sorted(assignment.attempts.all(), key=lambda a: a.attempt_number, reverse=True):
            if ((a.qc_verdict == QC_FAILED and a.qc_remarks)
                    or (a.head_verdict == QC_FAILED and a.head_remarks)):
                last_failed = a
                break

        out[project.pk] = {
            'assignment':     assignment,
            'status':         assignment.status,
            'status_label':   assignment.get_status_display(),
            'is_designer':    is_designer,
            'designer':       assignment.assigned_to,
            'attempt':        attempt,
            'attempt_number': assignment.current_attempt_number,
            # Only shown when the attempt is rework — an 'initial' attempt needs no label.
            'attempt_reason': (attempt.get_opened_reason_display()
                               if attempt and attempt.opened_reason != ATTEMPT_REASON_INITIAL
                               else ''),
            'arka':           arka,
            'due_date':       current_due.proposed_date if current_due else None,
            'due_approved':   bool(current_due and current_due.approved_at),
            # The requested date, shown as a pending chip beside the agreed one. It is
            # NOT what 'due_date' reports and never feeds an overdue calculation.
            'due_extension_requested': (pending_due.proposed_date if pending_due else None),
            # Revisions are derived by counting commitment rows, exactly as Part 2 does —
            # there is no stored counter to drift.
            'due_revised':    max(len(commitments) - 1, 0),
            'is_blocked':     assignment.status == DESIGN_SURVEY_RETURNED,
            'block_reason':   assignment.survey_return_reason,
            'qc_failed_attempt': last_failed,
            'action_kind':    kind if is_designer else 'none',
            'action_label':   label if is_designer else '',
            'waiting':        waiting,
            # Prompt 3.1a: `released` is excluded ONCE, through a set rather than by name in
            # the tuple as well. The pm_rejected prompt moved that set to
            # DESIGN_NOT_WITH_DESIGNER_STATUSES in step with design_mark_blocked(), which
            # this flag mirrors. The D13 prompt added the three review statuses to that set,
            # so the button is gone from a package under review as the endpoint refuses it.
            'can_mark_blocked': (is_designer
                                 and assignment.status not in (
                                     DESIGN_SURVEY_RETURNED, DESIGN_AWAITING_SURVEY,
                                     DESIGN_AWAITING_ALLOCATION)
                                 and assignment.status not in DESIGN_NOT_WITH_DESIGNER_STATUSES),
        }
    return out


def design_head_dashboard_counts(user):
    """The three queue sizes a Design Head can see for free — one COUNT each.

    NOT METRICS. No workload, no capacity, no overdue arithmetic, no sorting, no
    per-designer breakdown — that is Part 5 and is deliberately absent. These are the
    sizes of three worklists that already exist as screens, shown so the Head knows
    whether opening them is worth it.

    Returns None for a user without Head authority, so the template can test one value.

    It also carries the list of OPEX tenders, because the Head's per-tender screen
    (design_head_sites) is where survey upload and allocation live and he has no other
    way to reach it: `program_list` is @role_required(['Admin','PM','CEO']) and the real
    Design Head holds role='Design', so that nav link 403s for him. Modifying that
    decorator is out of scope, so the tenders are linked directly from here instead.
    """
    if not user_has_design_head_authority(user):
        return None
    base = DesignAssignment.objects.filter(project__is_deleted=False)
    return {
        'awaiting_allocation': base.filter(status=DESIGN_AWAITING_ALLOCATION).count(),
        # Gate 1's Arka queue — still keyed on the QC verdict, which is what `verdict`
        # now means. The status term alone would be enough; the verdict term is kept as
        # the same belt-and-braces re-check it has always been.
        'awaiting_arka':       base.filter(status=DESIGN_ARKA_SUBMITTED,
                                           attempts__arka_submissions__is_current=True,
                                           attempts__arka_submissions__verdict=ARKA_PENDING
                                           ).distinct().count(),
        # PART 9 — gate 2's two queues, counted separately from gate 1's. Merging them
        # would tell the Head how much work exists without telling him how much is his.
        'awaiting_head_arka':  base.filter(status=DESIGN_AWAITING_HEAD_ARKA).count(),
        'awaiting_head_qc':    base.filter(status=DESIGN_AWAITING_HEAD_QC).count(),
        'awaiting_qc':         base.filter(status=DESIGN_ARTIFACTS_UPLOADED).count(),
        'in_qc':               base.filter(status=DESIGN_IN_QC).count(),
        # PROMPT 3.1b-2b (§D14) — packages the PM rejected, back with the Head. A number of
        # its own, NOT folded into awaiting_head_qc: that tile's worklist is the review
        # queue, which deliberately does not carry these, so a folded count would promise
        # rows the queue cannot show. The Head acts on them from design_head_sites.
        'pm_rejected':         base.filter(status=DESIGN_PM_REJECTED).count(),
        'programs':            list(Program.objects.filter(is_deleted=False,
                                                           program_type='OPEX')
                                    .order_by('name')),
    }


def design_qc_dashboard_counts(user):
    """The two queue sizes a DESIGN QC reviewer can see for free — one COUNT each.

    THE GATE-1 COUNTERPART OF design_head_dashboard_counts(), and it exists for the reason
    Part 4.5 gives for that one: a screen that is URL-reachable only is unusable, because
    nobody types a URL. Without this the Design QC dashboard and review queue would have no
    entry point anywhere in the product.

    Returns None for a user with no claim on gate 1 at all, so the template can test one
    value — and deliberately NOT for a Head who lacks the QC flag, since his own strip
    already carries every number this one would.

    SESSION B.1 — WHO GETS A STRIP, AND WHAT IS ON IT:

        is_design_qc holder            -> every key, including `open_pool`
        plain designer, >=1 assignment -> no `open_pool` KEY AT ALL
        plain designer, 0 assignments  -> None, so the strip does not render

    The middle case omits the key rather than setting it to zero, and that is the whole
    point. A zero would tell a designer there is an open pool and that it happens to be
    empty; the truth is that the pool is not theirs to see, and a number they can watch
    change is a worse lie than no number. The template renders that block only
    `{% if qc_counts.open_pool is not None %}`, so absence is what hides it.

    NOT METRICS. Two counts and a tender list; the dashboard is design_qc_dashboard().
    """
    profile = getattr(user, 'profile', None)
    has_flag = user_is_design_qc(user)
    # A designer with no flag earns a strip only by having been handed work. Checked
    # against the same FK the queue scopes on, so the strip appears exactly when
    # user_can_view_qc_queue() would let them through to the screen it links to.
    has_assignments = bool(
        profile is not None
        and DesignAssignment.objects.filter(qc_assigned_to=profile,
                                            project__is_deleted=False).exists())
    if not (has_flag or has_assignments):
        return None
    # SESSION B — the same _qc_scope() the queue applies, so the numbers on this strip and
    # the rows behind them cannot disagree. A header saying 4 above a list of 2 is worse
    # than no header, and two counting paths is how that happens.
    base = DesignAssignment.objects.filter(_qc_scope(user), project__is_deleted=False)
    # The two Session B numbers decompose the three below by WHOSE the work is rather than
    # by stage, so this must be the UNION of exactly those three sets and nothing wider.
    #
    # The Arka arm carries the same is_current/pending filter as `awaiting_arka` below, and
    # it has to: a site at `arka_submitted` whose Arka the Head has already approved is not
    # in anybody's review queue — it is with the designer, owing CAD and BOQ — and
    # design_qc_queue() skips exactly those rows. Counting on status alone would put them
    # in these two numbers and not in the list the numbers head.
    #
    # The gate-2 statuses are deliberately absent. These count gate-1 work owed, and
    # `qc_assigned_to` says nothing about who clears gate 2.
    gate1_owed = base.filter(
        Q(status=DESIGN_ARKA_SUBMITTED,
          attempts__arka_submissions__is_current=True,
          attempts__arka_submissions__verdict=ARKA_PENDING)
        | Q(status__in=(DESIGN_ARTIFACTS_UPLOADED, DESIGN_IN_QC))
    ).distinct()
    counts = {
        'awaiting_arka': base.filter(status=DESIGN_ARKA_SUBMITTED,
                                     attempts__arka_submissions__is_current=True,
                                     attempts__arka_submissions__verdict=ARKA_PENDING
                                     ).distinct().count(),
        'awaiting_qc':   base.filter(status=DESIGN_ARTIFACTS_UPLOADED).count(),
        'in_qc':         base.filter(status=DESIGN_IN_QC).count(),
        'assigned_to_me': (gate1_owed.filter(qc_assigned_to=profile).count()
                           if profile is not None else 0),
    }
    # TWO KEYS ADDED ONLY FOR A FLAG HOLDER — see the docstring. A plain designer's dict
    # carries neither, which is what keeps both blocks off their strip.
    #
    # `open_pool`: setting it to zero would render a number about a pool they have no claim
    # on. Absence, not zero, is what the template tests.
    #
    # `programs`: those buttons link to design_qc_dashboard, which is gated on
    # user_can_view_design_qc_dashboard() and therefore 403s for a plain designer —
    # deliberately, since a per-tender dashboard is portfolio data they were never handed.
    # Offering a button that refuses the person who clicks it is worse than offering none,
    # so the key goes with the authority. (Same reason as the Head's strip: `program_list`
    # is @role_required(['Admin','PM','CEO']) and a QC holder is role='Design', so the nav
    # link 403s for them and tenders are linked from here instead.)
    if has_flag:
        counts['open_pool'] = gate1_owed.filter(qc_assigned_to__isnull=True).count()
        counts['programs'] = list(Program.objects.filter(is_deleted=False,
                                                         program_type='OPEX')
                                  .order_by('name'))
    return counts


def pm_change_request_targets(user, projects):
    """Which of `projects` the PM may raise a design change request against right now.

    Returns {project_pk: True} for OPEX sites where change_request_window_open() is True
    for `user` — the same predicate design_change_request() enforces on POST. Until 3.1c-i
    this skipped every released site, so a released site in a draft group was
    change-requestable by URL and offered no link anywhere.
    """
    out = {}
    for project in projects:
        if project.project_type != 'OPEX':
            continue
        assignment = getattr(project, 'design_assignment', None)
        if assignment is None:
            continue
        if not user_can_request_design_change(user, project):
            continue
        if change_request_window_open(user, assignment):
            out[project.pk] = True
    return out


# ---------------------------------------------------------------------------
# 16. Design Head tender dashboard (Part 5) — READ ONLY
#
# A NEW SCREEN RATHER THAN AN EXTENSION OF design_head_sites().
# Settled decision 8 rules out a per-site table as the landing view, and head_sites IS
# that table — bolting metrics on top of it would make them an appendix to the thing the
# decision forbids. So this becomes the landing view for a tender and drills DOWN into a
# filtered list rendered on the same page; head_sites stays exactly as it was and remains
# the full editable site list, linked from here.
#
# This view writes nothing. Every number comes from design_metrics, which holds no write
# of any kind.
# ---------------------------------------------------------------------------

@login_required
def design_tender_dashboard(request, pk):
    """The Design Head's operational view of one tender.

    Permission is the Part 4 helper, so a named deputy gets in and the 'Design Head' role
    string is not consulted. A designer is refused outright — the workload table shows
    every designer's rework multiplier side by side, and on samples this small that number
    is noisy enough that the first reaction to a bad one is to argue with the metric
    rather than the work.

    DRILL-DOWN, NOT A LANDING TABLE. `?stage=` and `?designer=` filter a compact list
    rendered beneath the panels, from the rows already in memory — neither adds a query.
    """
    if not user_has_design_head_authority(request.user):
        return HttpResponseForbidden(
            'The tender design dashboard is for the Design Head or his named deputy.')

    program = get_object_or_404(Program, pk=pk, is_deleted=False, program_type='OPEX')
    metrics = tender_metrics(program)

    # Drill-down is filtered in Python over `metrics['sites']`, which is already loaded.
    stage    = (request.GET.get('stage') or '').strip()
    designer = (request.GET.get('designer') or '').strip()
    drill, drill_label = [], ''
    if stage in STAGE_LABELS:
        drill = [s for s in metrics['sites'] if s['stage'] == stage]
        drill_label = STAGE_LABELS[stage]
    elif designer.isdigit():
        did = int(designer)
        drill = [s for s in metrics['sites']
                 if s['designer'] and s['designer'].pk == did]
        drill_label = (drill[0]['designer'].user.get_full_name()
                       or drill[0]['designer'].user.username) if drill else 'designer'

    return render(request, 'projects/design/tender_dashboard.html', {
        'program':      program,
        'm':            metrics,
        'drill':        drill,
        'drill_label':  drill_label,
        'drill_stage':  stage if stage in STAGE_LABELS else '',
        'drill_designer': designer if designer.isdigit() else '',
        'is_deputy':    user_is_design_head_deputy(request.user)
                        and not user_is_design_head(request.user),
    })


# ---------------------------------------------------------------------------
# 17. Design QC tender dashboard (Part 9 §6) — READ ONLY, AND A STRICT SUBSET
#
# A SUBSET OF design_tender_dashboard(), NOT A SECOND DASHBOARD. It calls the same
# tender_metrics() — same batched reads, same `sites` list — and then passes THREE of its
# keys to the template. What it deliberately does not pass is the point of the screen:
#
#   workload     per-designer rework multipliers. PERFORMANCE DATA, HEAD ONLY. Part 5
#                already refuses designers this table because on samples this small the
#                first reaction to a bad number is to argue with the metric; a QC reviewer
#                holding their colleagues' multipliers is the same problem with a
#                reporting line attached.
#   capacity     designed-versus-tendered. COMMERCIAL, HEAD ONLY. A reviewer's job is
#                whether this design is right, not whether the tender is profitable.
#   attention    the full list, which leads on overdue sites, Design Holds and due-date
#                revisions — all of which are about how the tender is being RUN. QC gets
#                `qc_attention` instead: only items awaiting a QC action.
#
# The exclusion is enforced HERE, by not putting the data in the context, rather than by a
# template `{% if %}`. A key that is absent cannot be rendered by a future template edit;
# a key that is present and conditionally hidden can.
# ---------------------------------------------------------------------------

@login_required
def design_qc_dashboard(request, pk):
    """Design QC's operational view of one tender: stage counts, their own queue, and the
    items awaiting a QC action.

    The Design Head is admitted too — he sees everything Design QC sees plus his own full
    dashboard, so refusing him a narrower view of his own data would be an access rule
    with nothing behind it. A designer, PM, SCM or Site Engineer is refused outright.
    """
    if not user_can_view_design_qc_dashboard(request.user):
        return HttpResponseForbidden(
            'The Design QC dashboard is for Design QC, the Design Head, or his named deputy.')

    program = get_object_or_404(Program, pk=pk, is_deleted=False, program_type='OPEX')
    metrics = tender_metrics(program)

    # Same drill-down mechanism as the Head's dashboard, filtered in Python over rows
    # already in memory. The `designer=` filter is deliberately NOT offered: browsing a
    # tender by designer is the workload table by another route.
    stage = (request.GET.get('stage') or '').strip()
    drill, drill_label = [], ''
    if stage in STAGE_LABELS:
        drill = [s for s in metrics['sites'] if s['stage'] == stage]
        drill_label = STAGE_LABELS[stage]

    return render(request, 'projects/design/qc_dashboard.html', {
        'program':     program,
        # A DELIBERATELY NARROWED dict, not `metrics`. See the section note above.
        'm': {
            'program':          metrics['program'],
            'today':            metrics['today'],
            'total_sites':      metrics['total_sites'],
            'assigned_sites':   metrics['assigned_sites'],
            'unassigned_sites': metrics['unassigned_sites'],
            'stages':           metrics['stages'],
            'queue':            metrics['qc_queue'],
            'attention':        metrics['qc_attention'],
        },
        'drill':       drill,
        'drill_label': drill_label,
        'drill_stage': stage if stage in STAGE_LABELS else '',
        'is_head':     user_has_design_head_authority(request.user),
    })


@login_required
def design_change_request_form(request, project_id):
    """Raise a change request on a site — the PM, a coordinator or SCM — and see the history
    of the ones already raised. GET only — the POST target is design_change_request().

    The window is change_request_window_open(), the predicate the POST enforces, so the
    form cannot offer a button that refuses or hide one that would have worked. When it is
    closed, `closed_reason` is the code _change_request_refusal() gives the POST, and the
    template renders its explanation from that code rather than re-deriving it.
    """
    project = _opex_site(project_id)
    if not user_can_request_design_change(request.user, project):
        return HttpResponseForbidden(CHANGE_REQUEST_FORBIDDEN)

    assignment = getattr(project, 'design_assignment', None)
    if assignment is None:
        raise Http404('Design has not started on this site yet.')

    attempt = _current_attempt(assignment)
    window_open = change_request_window_open(request.user, assignment)
    closed_reason = ('' if window_open
                     else _change_request_refusal(request.user, assignment, attempt)[0])

    # PROCUREMENT — for the template's note only: a draft-group site stays in its group
    # until the Head accepts (3.1c-i, Q2), and the PM should know that before raising.
    membership = active_group_membership(project, GROUP_TYPE_PROCUREMENT)
    draft_group = (membership.group if membership is not None
                   and membership.group.status == SITE_GROUP_DRAFT else None)

    return render(request, 'projects/design/change_request.html', {
        'project':     project,
        'assignment':  assignment,
        'attempt':     attempt,
        'history':     _attempt_history(assignment),
        'window_open': window_open,
        'closed_reason': closed_reason,
        'draft_group':   draft_group,
        'requests':    list(DesignChangeRequest.objects
                            .filter(attempt__assignment=assignment)
                            .select_related('requested_by__user', 'attempt',
                                            'resulting_attempt', 'decided_by__user')
                            .order_by('-requested_at')),
    })


# ===========================================================================
# PART 6 — site groups, aggregated BOQ, BOQ lock, SCM handoff
# ===========================================================================
#
# WHY A GROUP EXISTS AT ALL
# -------------------------
# Procurement never happens for a whole tender. A tender runs for months and its sites
# release in dribs and drabs; an order placed for all of them would either wait for the
# last site or be raised against quantities that are still moving. So SCM batches a set
# of released sites, prices that batch, and orders it. The batch is this module's
# SiteGroup, and forming one is a COMMERCIAL judgement — order economics, lead times,
# what a vendor will quote for — which is why SCM forms it and Design does not.
#
# THE AGGREGATE IS COMPUTED, NEVER STORED
# ---------------------------------------
# Nothing here writes a BOQ row, copies one, or snapshots one. The aggregate is a Sum
# over `BOQItem.boq_quantity` grouped by `item_master` across the member sites, run at
# read time. That is not a performance compromise, it is the requirement: the per-site
# BOQ has to survive intact underneath the aggregate for per-site profitability and
# expense tracking later, and a stored roll-up is exactly how the two drift apart.
#
# THE LOCK IS ENFORCED AT THE CALLER, NOT IN THE PERMISSION HELPER
# ---------------------------------------------------------------
# `permissions.project_boq_is_group_locked()` is a separate predicate ANDed beside
# `user_can_edit_project_boq()` at every BOQ write path. The Part 0.6 helper answers
# "is this person the designer" and is NOT modified. See that predicate's docstring.
#
# NO save() OVERRIDES, NO SIGNALS. Both transitions — locking a group, soft-removing a
# membership — are explicit assignments in the views below, inside a transaction, next
# to the permission check that authorises them. Same rule as every prior part.
# ---------------------------------------------------------------------------

# The reason string stamped on a membership pulled out by a change request. A constant
# because two places must agree on it exactly: design_change_request_accept() writes it
# (at acceptance since 3.1c-i, at raise before), and the group screen reads it back to
# explain to SCM why a site left. The stored value still says "PM" for an SCM-raised
# request; rows already carry it, so it is not reworded.
CHANGE_REQUEST_REMOVAL_REASON = 'PM change request'


def active_group_membership(project, group_type):
    """The site's one live membership OF `group_type`, or None.

    "One" is guaranteed by the partial unique constraint on SiteGroupMembership, not by
    this query — but only once the type is named. The constraint is
    `uniq_active_site_group_membership_per_type` over `(project, group_type)` where
    `removed_at IS NULL`, so a project may hold one live PROCUREMENT membership and one
    live EXECUTION membership at the same time (D-1). `.first()` here is picking the
    only row that can exist FOR THIS TYPE, not the first of several; without the type
    filter it would be picking the first of up to two, in undefined order.

    `group_type` IS REQUIRED AND IS DELIBERATELY NOT DEFAULTED to procurement. A default
    is precisely the failure D-1 introduces: a future execution caller that forgets the
    argument would get a procurement membership back, silently, and every fact computed
    from it — lock state, group name, `in_draft_group` — would be an answer to a
    question it did not ask. Required means every call site states its intent in
    writing, and a new one cannot be written without deciding.

    Pass a GROUP_TYPE_* constant, not a literal.

    THE FILTER IS THE MEMBERSHIP'S OWN COLUMN, NOT `group__group_type`. The two always
    agree — `save()` copies one from the other and refuses any later change — but only
    the local column is the one the constraint is written over, so filtering on it makes
    this query and the uniqueness guarantee it relies on state the same condition. It
    also costs no join.
    """
    return (project.group_memberships
            .filter(removed_at__isnull=True, group_type=group_type)
            .select_related('group', 'group__program').first())


def remove_from_group(membership, actor, reason):
    """Soft-remove one membership and log it. THE ONLY PLACE A SITE LEAVES A GROUP.

    Both callers — SCM removing a site by hand, and a PM change request pulling one out
    (settled decision 6) — go through here, so the two cannot drift into disagreeing
    about which fields get stamped or whether the departure is logged at all. A silent
    removal would leave SCM reading an aggregate that quietly changed under them.

    The caller owns the transaction. The change-request path has one open for its own
    writes and the removal must share it.

    NOT NARROWED BY TYPE, DELIBERATELY (prompt 1.1b, D-1). This function scopes nothing
    — it is handed a row that a caller already resolved, and both of today's callers now
    resolve procurement explicitly. Its mechanism is type-agnostic: stamp three fields,
    log, return. An execution removal will want exactly this, and giving it a second
    copy is how the two drift into disagreeing about whether a departure is logged.

    WHAT DID NEED FIXING IS THE LOG TEXT. It hardcoded the word "procurement", which was
    true only because procurement was the only type that could exist. That made it a
    sentence guaranteed to become false rather than to fail — an execution removal would
    have written "procurement group" into the activity feed and nothing would have
    complained. It now comes from the row. The procurement wording is unchanged.
    """
    membership.removed_at     = timezone.now()
    membership.removed_by     = actor
    membership.removal_reason = reason
    membership.save(update_fields=['removed_at', 'removed_by', 'removal_reason'])
    kind = membership.get_group_type_display().lower()
    log_activity(membership.project, actor,
                 f'Removed from {kind} group "{membership.group.name}": {reason}',
                 entity_type='SiteGroupMembership', entity_id=membership.pk,
                 action_code='site_group_site_removed')
    return membership


def _group_member_ids(group):
    """Primary keys of the sites currently in `group` — active memberships only."""
    return list(group.memberships
                .filter(removed_at__isnull=True)
                .values_list('project_id', flat=True))


def aggregate_group_boq(member_ids):
    """Sum BOQ quantities across `member_ids`, grouped by catalogue item.

    THE JOIN IS `item_master`, WHICH IS WHY BOQItemMaster EXISTS (Part 0.5). Every item
    aggregates the same way — there is no per-item rule on the master and none is
    invented here.

    `boq_quantity__gt=0` mirrors the guard `boq_detail`'s submit branch and
    `design_boq_complete()` both apply, so "a quantity was entered" means the same thing
    on this screen as on the two that produced it. A null or zero row contributes
    nothing to a sum, but counting it would inflate `site_count` into a claim that a site
    contributed to a line when it did not.

    UNLINKED ROWS ARE RETURNED, NOT DROPPED. A `BOQItem` with a null `item_master` cannot
    join, so its quantity is missing from the total — and a total that is silently short
    is worse than no total. They come back in `unlinked` for the template to shout about.
    Measured at build time: 0 such rows on OPEX sites, 2 on legacy Residential ones
    (deferred finding B1).

    Returns a dict; `contributions` maps item_master_id -> [(project_id, quantity)] so the
    per-line site count can be checked against the sites that produced it without a query
    per line.
    """
    lines = list(
        BOQItem.objects
        .filter(boq__project_id__in=member_ids, boq_quantity__gt=0,
                item_master__isnull=False)
        .values('item_master', 'item_master__code', 'item_master__description',
                'item_master__unit', 'item_master__sort_order')
        .annotate(total_quantity=Sum('boq_quantity'),
                  site_count=Count('boq__project', distinct=True))
        .order_by('item_master__sort_order', 'item_master__code')
    )

    # Per-site breakdown, one query for the whole table. Lets the screen show WHICH sites
    # are behind each line, which is what makes the aggregate auditable rather than a
    # number SCM has to trust.
    contributions = {}
    for row in (BOQItem.objects
                .filter(boq__project_id__in=member_ids, boq_quantity__gt=0,
                        item_master__isnull=False)
                .values('item_master', 'boq__project__project_id', 'boq_quantity')
                .order_by('boq__project__project_id')):
        contributions.setdefault(row['item_master'], []).append(
            (row['boq__project__project_id'], row['boq_quantity']))
    for line in lines:
        line['contributions'] = contributions.get(line['item_master'], [])

    unlinked = list(
        BOQItem.objects
        .filter(boq__project_id__in=member_ids, boq_quantity__gt=0,
                item_master__isnull=True)
        .select_related('boq__project')
        .order_by('boq__project__project_id', 'serial_no')
    )

    return {
        'lines':      lines,
        'unlinked':   unlinked,
        'item_count': len(lines),
        'site_count': len(member_ids),
    }


def _age_days(stamp, now):
    """Whole days between `stamp` and `now`, or None. Used for pool ageing only."""
    if stamp is None:
        return None
    return (now - stamp).days


def post_qc_pool(program, now=None):
    """Released sites in this tender that are in NO group, oldest release first.

    THIS QUEUE IS THE POINT OF THE SCREEN. Without it, sites pass QC and pile up while
    procurement receives nothing — the failure is silent on both sides, because Design
    has finished and SCM was never told. Age is days since `released_at`.

    Sites with a LIVE PROCUREMENT membership are dropped; a site whose membership was
    REMOVED is back in the pool, which is what makes settled decision 6 recoverable — a
    change request returns the site to the queue rather than losing it.

    PROCUREMENT, AND THIS IS THE NARROWING THAT MATTERS (prompt 1.1b, D-1). The question
    this screen asks is "has SCM taken this site yet", and only a procurement membership
    answers it. An execution membership means the PM has put the site in a delivery
    batch — which says nothing about whether it has been procured, and under D-1 may
    exist alongside a procurement membership or entirely without one. Left unnarrowed,
    the first execution group ever created would silently delete its sites from this
    queue: they would be released, unprocured, and invisible to the only screen that
    would have said so. That is precisely the failure the paragraph above says this
    function exists to prevent, arriving by a second route.

    THE EXCLUSION IS AN EXPLICIT SUBQUERY ON project_id, NOT
    `.exclude(project__group_memberships__removed_at__isnull=True)`. That spelling is
    wrong and silently empties this screen. Django compiles it to

        NOT EXISTS (SELECT 1 FROM project U1
                    LEFT OUTER JOIN sitegroupmembership U2 ON U1.id = U2.project_id
                    WHERE U2.removed_at IS NULL AND U1.id = assignment.project_id)

    — a LEFT JOIN, so a project with NO membership rows still produces one phantom row
    whose U2.removed_at is NULL. The condition matches it, EXISTS is true, and the
    project is excluded. The result is a pool that omits precisely the sites it exists to
    show: every released site that has never been in a group. It only ever returned the
    sites that had once been grouped and removed, which is why it looked correct against
    data that had exactly that shape.

    Filtering `SiteGroupMembership` directly and excluding on `project__in` compiles to a
    plain subquery over rows that actually exist, with no outer join and no phantom row.
    """
    now = now or timezone.now()
    rows = list(
        DesignAssignment.objects
        .filter(project__program=program, project__is_deleted=False,
                status=DESIGN_RELEASED)
        .exclude(project__in=SiteGroupMembership.objects
                 .filter(removed_at__isnull=True,
                         group_type=GROUP_TYPE_PROCUREMENT).values('project_id'))
        .select_related('project', 'released_by__user')
        .order_by('released_at')
    )
    for a in rows:
        a.age_days = _age_days(a.released_at, now)
    return rows


def tender_release_completeness(program):
    """(released, total) for a tender — 'X of Y sites are released'.

    Shown beside every aggregate. SCM has to know whether they are looking at a final
    quantity or a running total, and the aggregate itself cannot tell them: a group of
    three sites looks identical whether the tender has three sites or thirty.

    Counts DesignAssignment rows, never `Project.status` (settled decision 9) — OPEX
    sites are `Draft` and stay `Draft`.
    """
    total = program.sites.filter(is_deleted=False).count()
    released = DesignAssignment.objects.filter(
        project__program=program, project__is_deleted=False,
        status=DESIGN_RELEASED).count()
    return released, total


def pending_change_requests_for(member_ids):
    """Change requests on these sites still awaiting the Design Head's decision.

    Same definition as `_pending_change_requests()` (Part 4.6): `verdict='pending'`.
    Locking a group over one of these would freeze a BOQ that may be about to be
    reworked, and the Head has not yet said whether it will be.

    PART 4.6 MADE THIS GUARD REACHABLE. Part 4 set `resulting_attempt` in the same
    transaction that created the row, so no unresolved request could exist and this fired
    only against rows made in Django admin (deferred finding G6). A pending request is now
    an ordinary state that lasts as long as the Head takes to triage it. A REJECTED
    request no longer blocks anything — the current version stands, so its BOQ is settled.
    """
    return list(DesignChangeRequest.objects
                .filter(attempt__assignment__project_id__in=member_ids,
                        verdict=CHANGE_REQUEST_PENDING)
                .select_related('attempt__assignment__project', 'requested_by__user'))


def _group_rows(program):
    """Every PROCUREMENT group under a tender with its member count and lock state,
    newest first.

    PROCUREMENT: this feeds SCM's group screen and the SCM dashboard, and both render a
    Lock button beside every row. Listing a PM's execution batch there would offer SCM
    an action D-1 says does not exist for it, on a grouping they do not own.
    """
    return list(
        program.site_groups
        .filter(group_type=GROUP_TYPE_PROCUREMENT)
        .select_related('created_by__user', 'locked_by__user')
        .annotate(member_count=Count(
            'memberships', filter=Q(memberships__removed_at__isnull=True)))
        .order_by('-created_at')
    )


def _tender_or_404(pk):
    return get_object_or_404(Program, pk=pk, is_deleted=False, program_type='OPEX')


def _group_or_404(pk):
    """Resolve one SCM PROCUREMENT batch by pk, or 404.

    THE HIGHEST-LEVERAGE NARROWING IN THIS FILE (audit Task B). Six views resolve their
    group through here, including both write paths — adding sites and locking. Every one
    of them is procurement UI: the lock is procurement-only by D-1, and an execution
    group reaching `site_group_lock` through a hand-typed pk would freeze a BOQ that no
    purchase order was ever raised against.

    Narrowing HERE rather than in each view is deliberate. It is also why
    `site_group_detail`, `site_group_remove_site` and `_group_member_ids` need no type
    filter of their own — they are scoped to a group this function already vouched for,
    and duplicating the check in them would create four places for it to drift.
    """
    return get_object_or_404(
        SiteGroup, pk=pk, program__is_deleted=False, program__program_type='OPEX',
        group_type=GROUP_TYPE_PROCUREMENT)


# ---------------------------------------------------------------------------
# 17. Group formation
# ---------------------------------------------------------------------------

@login_required
def site_group_list(request, pk):
    """SCM's group screen for one tender: the groups, the post-QC pool, and the form
    that creates the next group.

    READ is SCM, Admin and Design Head authority; WRITE is SCM alone (Part 6 §1 and §3).
    The Head can see what became of the sites he released and nothing more — he does not
    own the order, so he does not form the batch.
    """
    if not user_can_view_site_groups(request.user):
        return HttpResponseForbidden(
            'Procurement groups are visible to SCM, Admin and the Design Head.')

    program = _tender_or_404(pk)
    released, total = tender_release_completeness(program)
    pool = post_qc_pool(program)

    return render(request, 'projects/design/site_groups.html', {
        'program':      program,
        'groups':       _group_rows(program),
        'pool':         pool,
        'released':     released,
        'total_sites':  total,
        'can_manage':   user_can_manage_site_groups(request.user),
        'change_request_project_ids': change_request_link_ids(request.user, pool),
    })


def change_request_link_ids(user, assignments):
    """Project pks among `assignments` whose "Request design change" link renders for
    `user` (3.1c-i, Q3).

    THE SAME TWO QUESTIONS THE POST ASKS, decided here so a template only tests membership
    and never re-derives the window: user_can_request_design_change(), then
    change_request_window_open(). Authority first, so Admin and the Design Head — who read
    these screens and may not raise — cost no window query. A locked-group row fails the
    window, which is why no locked row carries the link.

    None entries are skipped: a group member always has an assignment, but the relation is
    nullable and a missing one is no reason to fail the page.
    """
    return {a.project_id for a in assignments
            if a is not None
            and user_can_request_design_change(user, a.project)
            and change_request_window_open(user, a)}


@login_required
def site_group_create(request, pk):
    """SCM creates a group under a tender and optionally seeds it with sites.

    Sites are optional at creation — a named empty draft is a legitimate intermediate
    state while SCM decides what goes in it. Locking an empty group is not (see
    site_group_lock).
    """
    if not user_can_manage_site_groups(request.user):
        return HttpResponseForbidden('Only SCM may create a procurement group.')
    program = _tender_or_404(pk)
    if request.method != 'POST':
        return redirect('site_group_list', pk=program.pk)

    name = (request.POST.get('name') or '').strip()
    if not name:
        messages.error(request, 'Give the group a name — SCM will be reading it on a '
                                'purchase order.')
        return redirect('site_group_list', pk=program.pk)

    profile = request.user.profile
    with transaction.atomic():
        # PROCUREMENT, STATED RATHER THAN INHERITED. This endpoint is SCM's and only
        # SCM's — `user_can_manage_site_groups` above is the procurement authority — so
        # what it creates is a procurement batch. The model default says the same thing,
        # but a default is a safety net for rows nobody thought about; this row was
        # thought about. When an execution-group creator is written it will sit beside
        # this one and pass the other constant, and neither will be relying on which way
        # the default happens to point.
        group = SiteGroup.objects.create(
            program=program, name=name, status=SITE_GROUP_DRAFT,
            group_type=GROUP_TYPE_PROCUREMENT,
            created_by=profile, notes=(request.POST.get('notes') or '').strip())

    added, refused = _add_sites(group, request.POST.getlist('project_ids'), profile)
    messages.success(request, f'Group "{group.name}" created.'
                              + (f' {len(added)} site(s) added.' if added else ''))
    for line in refused:
        messages.error(request, line)
    return redirect('site_group_detail', pk=group.pk)


def _add_sites(group, project_ids, actor):
    """Add each id to `group`, one at a time. Returns (added, refused_messages).

    PER-SITE, NOT BULK, AND EACH IN ITS OWN SAVEPOINT. Two reasons, both deliberate:

      * SCM selects ten sites and one of them is already spoken for. Failing the whole
        batch teaches them to add sites one at a time, which is worse than telling them
        which one it was.
      * The exclusivity rule is enforced by a PARTIAL UNIQUE CONSTRAINT in the database
        (settled decision 2), not by the pre-check above it. The pre-check exists for the
        error message; the `IntegrityError` catch is what makes the rule true under a
        concurrent add. Without the savepoint, one IntegrityError would poison the whole
        transaction and take the successful adds down with it.
    """
    added, refused = [], []
    for raw in project_ids:
        if not str(raw).isdigit():
            continue
        project = Project.objects.filter(pk=int(raw), is_deleted=False).first()
        if project is None:
            refused.append('One selected site no longer exists.')
            continue

        if project.program_id != group.program_id:
            refused.append(f'{project.project_id}: belongs to a different tender.')
            continue

        assignment = getattr(project, 'design_assignment', None)
        if assignment is None or assignment.status != DESIGN_RELEASED:
            state = assignment.get_status_display() if assignment else 'design not started'
            refused.append(f'{project.project_id}: not released ({state}) — only released '
                           f'sites can be grouped for procurement.')
            continue

        # PROCUREMENT: the exclusivity being pre-checked is procurement exclusivity —
        # the constraint below refuses a second live PROCUREMENT membership, and this
        # pre-check exists only to produce a better message than the IntegrityError
        # would. Asking a wider question here would refuse a legitimate procurement add
        # because the PM had put the site in an execution batch, which the database
        # would have allowed.
        existing = active_group_membership(project, GROUP_TYPE_PROCUREMENT)
        if existing is not None:
            refused.append(f'{project.project_id}: already in group '
                           f'"{existing.group.name}" ({existing.group.status}).')
            continue

        try:
            with transaction.atomic():
                membership = SiteGroupMembership.objects.create(
                    group=group, project=project, added_by=actor)
                log_activity(project, actor,
                             f'Added to procurement group "{group.name}"',
                             entity_type='SiteGroupMembership', entity_id=membership.pk,
                             action_code='site_group_site_added')
        except IntegrityError:
            # The database refused it. Reachable when two adds race, or when the view's
            # pre-check above is somehow bypassed — either way the constraint is the
            # authority and this is the message that says so.
            refused.append(f'{project.project_id}: refused by the database — a site may '
                           f'be in only one group at a time.')
            continue

        added.append(project)
    return added, refused


@login_required
def site_group_add_sites(request, pk):
    """SCM adds released, ungrouped sites to a DRAFT group."""
    if not user_can_manage_site_groups(request.user):
        return HttpResponseForbidden('Only SCM may add sites to a procurement group.')
    group = _group_or_404(pk)
    if request.method != 'POST':
        return redirect('site_group_detail', pk=group.pk)

    if group.status != SITE_GROUP_DRAFT:
        messages.error(request, f'"{group.name}" is locked — its membership is final.')
        return redirect('site_group_detail', pk=group.pk)

    added, refused = _add_sites(group, request.POST.getlist('project_ids'),
                                request.user.profile)
    if added:
        messages.success(request, f'{len(added)} site(s) added to "{group.name}".')
    for line in refused:
        messages.error(request, line)
    if not added and not refused:
        messages.error(request, 'No sites were selected.')
    return redirect('site_group_detail', pk=group.pk)


@login_required
def site_group_remove_site(request, pk):
    """SCM removes one site from a DRAFT group, with a reason."""
    if not user_can_manage_site_groups(request.user):
        return HttpResponseForbidden('Only SCM may remove a site from a procurement group.')
    group = _group_or_404(pk)
    if request.method != 'POST':
        return redirect('site_group_detail', pk=group.pk)

    if group.status != SITE_GROUP_DRAFT:
        messages.error(request, f'"{group.name}" is locked — its membership is final.')
        return redirect('site_group_detail', pk=group.pk)

    raw = request.POST.get('membership_id', '')
    if not raw.isdigit():
        messages.error(request, 'No site was selected.')
        return redirect('site_group_detail', pk=group.pk)

    # `group=group` scopes the membership to THIS group — an id from another group is a
    # 404, not a silent no-op reported as success.
    membership = get_object_or_404(
        SiteGroupMembership, pk=int(raw), group=group, removed_at__isnull=True)
    reason = (request.POST.get('reason') or '').strip() or 'Removed by SCM'

    with transaction.atomic():
        remove_from_group(membership, request.user.profile, reason)

    messages.success(request, f'{membership.project.project_id} removed from '
                              f'"{group.name}". Its own BOQ is unchanged.')
    return redirect('site_group_detail', pk=group.pk)


# ---------------------------------------------------------------------------
# 18. Aggregated BOQ and the lock
# ---------------------------------------------------------------------------

@login_required
def site_group_detail(request, pk):
    """One group: its members, its aggregated BOQ, what left it and why, and the lock.

    The consolidated requirement is what SCM raises an order against, so everything that
    qualifies it is on the same screen — how complete the tender is, which sites are
    behind each line, and any BOQ row that could not be aggregated.
    """
    if not user_can_view_site_groups(request.user):
        return HttpResponseForbidden(
            'Procurement groups are visible to SCM, Admin and the Design Head.')

    group = _group_or_404(pk)
    memberships = list(group.memberships
                       .filter(removed_at__isnull=True)
                       .select_related('project', 'project__design_assignment',
                                       'added_by__user')
                       .order_by('project__project_id'))
    removed = list(group.memberships
                   .filter(removed_at__isnull=False)
                   .select_related('project', 'removed_by__user')
                   .order_by('-removed_at'))

    member_ids = [m.project_id for m in memberships]
    agg = aggregate_group_boq(member_ids)
    released, total = tender_release_completeness(group.program)
    pool = (post_qc_pool(group.program)
            if group.status == SITE_GROUP_DRAFT else [])

    return render(request, 'projects/design/site_group_detail.html', {
        'program':      group.program,
        'group':        group,
        'memberships':  memberships,
        'removed':      removed,
        'agg':          agg,
        'released':     released,
        'total_sites':  total,
        'blockers':     (pending_change_requests_for(member_ids)
                         if group.status == SITE_GROUP_DRAFT else []),
        'pool':         pool,
        'can_manage':   user_can_manage_site_groups(request.user),
        'change_request_reason': CHANGE_REQUEST_REMOVAL_REASON,
        # 3.1c-i, Q3 — members and pool rows alike. A locked group's members fail the
        # window, so they never appear in this set.
        'change_request_project_ids': change_request_link_ids(
            request.user,
            [getattr(m.project, 'design_assignment', None) for m in memberships]
            + list(pool)),
    })


@login_required
def site_group_lock(request, pk):
    """SCM locks a group. The BOQ of every member site becomes read-only from here on.

    THERE IS NO UNLOCK, DELIBERATELY. Once quantities are committed to a purchase, the
    correction is a variance against the order, not an edit to the BOQ it was raised
    from. Building half of a variance process as an "unlock" button would be worse than
    the honest gap — it would let a quantity move after an order was placed against it
    with nothing recording that it had.

    Two refusals before the write, and both are about not freezing something meaningless:
    an empty group locks nothing, and a member with an unresolved change request is
    already known to need rework.
    """
    if not user_can_manage_site_groups(request.user):
        return HttpResponseForbidden('Only SCM may lock a procurement group.')
    group = _group_or_404(pk)
    if request.method != 'POST':
        return redirect('site_group_detail', pk=group.pk)

    def _back(msg, ok=False):
        (messages.success if ok else messages.error)(request, msg)
        return redirect('site_group_detail', pk=group.pk)

    if group.status == SITE_GROUP_LOCKED:
        return _back(f'"{group.name}" is already locked.')

    member_ids = _group_member_ids(group)
    if not member_ids:
        return _back(f'"{group.name}" has no sites — there is nothing to lock.')

    blockers = pending_change_requests_for(member_ids)
    if blockers:
        sites = ', '.join(sorted({b.attempt.assignment.project.project_id
                                  for b in blockers}))
        return _back(f'"{group.name}" cannot be locked: {sites} '
                     f'{"has" if len(blockers) == 1 else "have"} a change request '
                     f'awaiting the Design Head. Wait for his decision, or remove the '
                     f'site from the group first.')

    profile = request.user.profile
    now = timezone.now()
    with transaction.atomic():
        group.status    = SITE_GROUP_LOCKED
        group.locked_by = profile
        group.locked_at = now
        group.save(update_fields=['status', 'locked_by', 'locked_at'])

        # One log line per member site, on the site itself. The group is not a project, so
        # ActivityLog cannot hang the event off it — and the site is where a PM or designer
        # will look to find out why the BOQ stopped accepting edits.
        for project in Project.objects.filter(pk__in=member_ids):
            log_activity(project, profile,
                         f'BOQ locked — site group "{group.name}" locked for procurement',
                         entity_type='SiteGroup', entity_id=group.pk,
                         action_code='site_group_locked')

    return _back(f'"{group.name}" locked. The BOQ of {len(member_ids)} site(s) is now '
                 f'read-only. There is no unlock — a later change needs a variance '
                 f'against the order.', ok=True)


# ---------------------------------------------------------------------------
# 19. SCM handoff — the OPEX section of the SCM dashboard
# ---------------------------------------------------------------------------

def scm_opex_tender_rows(now=None):
    """Per-tender procurement rows for the SCM dashboard's OPEX section.

    KEYED OFF SiteGroup AND DesignAssignment, NEVER `Project.status` (settled decision 9).
    OPEX sites are created `Draft` and nothing promotes them (deferred finding H1), so a
    status filter would return an empty section forever. No existing dashboard queryset is
    read, widened or modified — this is its own query set from its own tables.

    Returns one row per OPEX tender that has at least one design assignment, so tenders
    where design has not started do not pad the section.
    """
    now = now or timezone.now()
    rows = []
    programs = (Program.objects
                .filter(is_deleted=False, program_type='OPEX')
                .order_by('name'))
    for program in programs:
        assignments = (DesignAssignment.objects
                       .filter(project__program=program, project__is_deleted=False)
                       .count())
        if not assignments:
            continue
        released, total = tender_release_completeness(program)
        groups = _group_rows(program)
        pool = post_qc_pool(program, now)
        rows.append({
            'program':      program,
            'total_sites':  total,
            'in_design':    assignments,
            'released':     released,
            'groups':       groups,
            'group_count':  len(groups),
            'locked_count': sum(1 for g in groups if g.status == SITE_GROUP_LOCKED),
            'pool':         pool,
            'pool_count':   len(pool),
            'oldest_pool_age': pool[0].age_days if pool else None,
        })
    return rows


# ---------------------------------------------------------------------------
# 20. Part 10 — the Design Head's quality analytics
#
# READ-ONLY EXCEPT FOR THE SELECTION. The GET view runs arithmetic over rows other parts
# created and writes nothing; the two POST endpoints write one DesignAnalyticsPreference
# row and nothing else. No workflow model is touched by any of the three.
#
# HEAD ONLY, AND user_is_design_head() RATHER THAN user_has_design_head_authority().
# Every other screen in this module calls the wider helper so a named deputy can act. This
# one deliberately does not, and the reason is what is on the page: per-person error rates
# for named designers, and an overturn rate for named QC reviewers. The deputy field is a
# plain FK to any UserProfile — a Head can and often would name a senior DESIGNER as
# deputy so review queues keep moving during an absence. That is the right person to clear
# an Arka queue and the wrong person to hold their colleagues' failure rates, and settled
# decision 3 says no designer sees another designer's figures. Covering QC for a week does
# not carry the team's performance data with it.
#
# So: Design QC is refused, designers are refused, PMs are refused, the deputy is refused,
# and a Django superuser without the flag is refused too — the flag is the rule, exactly as
# it is for the eighteen other design views, and there is no second authority path.
# ---------------------------------------------------------------------------

#: Where an unauthorised caller is told to go. One string, so the GET view and both POST
#: endpoints refuse identically — a probe must not be able to tell them apart.
_ANALYTICS_FORBIDDEN = ('Design quality analytics is for the Design Head only.')


def _analytics_scope(pk):
    """The tenders in scope: one OPEX tender, or every OPEX tender combined.

    `pk=None` is the all-tenders view. Both paths return a LIST, so compute() and every
    metric below it are identical for the two — there is no "all tenders" branch anywhere
    past this function, and therefore no way for the two views to drift apart.
    """
    if pk is None:
        return list(Program.objects
                    .filter(is_deleted=False, program_type='OPEX')
                    .order_by('name')), None
    program = get_object_or_404(Program, pk=pk, is_deleted=False, program_type='OPEX')
    return [program], program


def _analytics_preference(user):
    """The caller's stored selection, or None. Does NOT create a row.

    A Head who has never opened the selector has no row, and that is indistinguishable
    from a row holding the default. get_or_create() here would write on a GET, which is
    exactly what verification item 13 checks does not happen.
    """
    profile = getattr(user, 'profile', None)
    if profile is None:
        return None
    return DesignAnalyticsPreference.objects.filter(profile=profile).first()


@login_required
def design_quality_analytics(request, pk=None):
    """Quality analytics for one tender, or for every OPEX tender combined.

    READS ONLY. Nothing on this page writes, including the preference row — the selector
    posts to design_analytics_configure() below.
    """
    if not user_is_design_head(request.user):
        return HttpResponseForbidden(_ANALYTICS_FORBIDDEN)

    programs, program = _analytics_scope(pk)
    preference = _analytics_preference(request.user)
    selected   = selected_metric_keys(preference)
    result     = compute_analytics(programs, selected)

    return render(request, 'projects/design/quality_analytics.html', {
        'program':      program,
        'programs':     programs,
        'all_tenders':  program is None,
        # The selector's own list of tenders, which is every OPEX tender regardless of
        # what is currently in scope.
        'tender_choices': (programs if program is None else
                           list(Program.objects
                                .filter(is_deleted=False, program_type='OPEX')
                                .order_by('name'))),
        'selected':     selected,
        'catalogue':    catalogue_for_display(selected),
        'groups':       METRIC_GROUPS,
        'panels':       result['panels'],
        'site_count':   result['site_count'],
        'released_count': result['released_count'],
        'attempt_count':  result['attempt_count'],
        'optional_on':  sum(1 for k in selected if k in OPTIONAL_METRICS),
        'optional_total': len(OPTIONAL_METRICS),
        'scope_pk':     pk,
    })


def _analytics_redirect(request):
    """Back to whichever scope the selector was posted from.

    The scope travels in the POST body rather than being remembered server-side: it is a
    property of the page the Head was looking at, not of the Head, and storing it would
    mean a second piece of state to keep in step with the URL.
    """
    scope = (request.POST.get('scope') or '').strip()
    if scope.isdigit():
        return redirect('design_quality_analytics_tender', pk=int(scope))
    return redirect('design_quality_analytics')


@login_required
def design_analytics_configure(request):
    """Store which OPTIONAL metrics this Head wants. THE ONLY WRITE IN PART 10.

    CORE KEYS ARE DISCARDED, not stored, even if the browser posts them. They are always
    on, so a stored core key would be a row that could later be edited to switch one off —
    through the admin, an import, or a view that forgets the rule. What is never written
    cannot be unset. Anything outside OPTIONAL_METRICS is dropped for the same reason the
    error category is validated at the gates rather than trusted: the checkbox names come
    from the client.
    """
    if request.method != 'POST':
        return redirect('design_quality_analytics')
    if not user_is_design_head(request.user):
        return HttpResponseForbidden(_ANALYTICS_FORBIDDEN)

    chosen = [k for k in request.POST.getlist('metrics') if k in OPTIONAL_METRICS]
    # Deduplicated and ordered by the catalogue, so the stored list is stable no matter
    # what order the form serialised the boxes in.
    chosen = [k for k in OPTIONAL_METRICS if k in set(chosen)]

    DesignAnalyticsPreference.objects.update_or_create(
        profile=request.user.profile, defaults={'metrics': chosen},
    )
    messages.success(
        request,
        f'Metric selection saved — {len(chosen)} optional metric'
        f'{"" if len(chosen) == 1 else "s"} on, plus the locked core set.')
    return _analytics_redirect(request)


@login_required
def design_analytics_reset(request):
    """Back to the default: core metrics only.

    Deletes the row rather than storing an empty list. Both read identically through
    selected_metric_keys(), and deleting means "this Head has never configured the page"
    stays the same state as "this Head reset it", which is what the word reset means.
    """
    if request.method != 'POST':
        return redirect('design_quality_analytics')
    if not user_is_design_head(request.user):
        return HttpResponseForbidden(_ANALYTICS_FORBIDDEN)

    DesignAnalyticsPreference.objects.filter(profile=request.user.profile).delete()
    messages.success(request, 'Metric selection reset to the core set.')
    return _analytics_redirect(request)


# ---------------------------------------------------------------------------
# Part 12 — the OPEX BOQ catalogue, owned by the Design Head
#
# A PARALLEL SCREEN, NOT A WIDENED GATE. The Admin already has a catalogue screen at
# /portal-admin/boq-items/ covering both templates (37 Residential + 207 OPEX). This is a
# SECOND entry point onto the SAME model and the SAME form, scoped to OPEX, reached by a
# different person through a different URL. The Admin's four views, its template and its
# sidebar are untouched — which is the whole reason this shape was chosen over widening
# @role_required. A permission-aware admin sidebar would have put all nine Admin screens
# in the blast radius of a catalogue feature; this puts none of them there.
#
# THE DUPLICATION IS DELIBERATE. It is roughly sixty lines and it should not be factored
# together with the Admin views. The two already differ — this one is OPEX-scoped at every
# query, drops two form fields instead of one, and scopes its own sort_order suggestion —
# and a shared implementation would drag Admin code into the diff of every future change
# to either screen. They answer the same question for two different people, and only one
# of them is allowed near the Residential rows.
#
# HEAD ONLY, AND user_is_design_head() RATHER THAN user_has_design_head_authority().
# The precedent is Part 10 (see the block above design_quality_analytics), and the reason
# here has the same shape. A deputy is named so review queues keep moving through an
# absence, and a senior DESIGNER is the obvious person to name. Clearing an Arka queue for
# a week is not the same authority as retiring an item every site in the tender buys from,
# or adding one they will all be offered. The absence ends; the catalogue does not.
#
# OPEX-SCOPED AT EVERY QUERY, NOT ONLY THE LIST. The 37 Residential rows pre-populate every
# new Residential BOQ (models.get_standard_boq_items), so they are the Admin's alone and
# must stay unreachable from here by ANY route — including a hand-typed URL carrying a
# Residential item_id. That is why the object lookup filters on project_type as well as pk
# and 404s: a 403 would confirm the row exists, and a plain pk lookup would hand the Head
# an ITM- row to edit on a form that would happily save it.
# ---------------------------------------------------------------------------

#: Where an unauthorised caller is told to go. ONE string, so all four endpoints refuse
#: identically — a probe must not be able to tell them apart, and must not be able to tell
#: "refused" from "no such row" by the wording either.
_CATALOGUE_FORBIDDEN = 'The RESCO BOQ catalogue is for the Design Head only.'


def _opex_catalogue_qs():
    """The rows this screen manages, and the ONLY rows any of its four views may touch.

    Every queryset in this section starts here rather than at BOQItemMaster.objects, so
    the OPEX term cannot be present on three paths and forgotten on the fourth. That is
    the entire reason this one-line function exists.
    """
    return BOQItemMaster.objects.filter(project_type='OPEX')


def _opex_catalogue_item(item_id):
    """One OPEX catalogue row, or 404.

    THE project_type TERM IS THE BOUNDARY, not a convenience filter. A Residential item_id
    typed into one of these URLs must behave exactly as a pk that does not exist.
    """
    return get_object_or_404(_opex_catalogue_qs(), pk=item_id)


@login_required
def design_boq_catalogue(request):
    """The OPEX BOQ catalogue — every item the picker offers, and how many BOQ rows have
    been built from each. Design Head only.

    NO TYPE FILTER AND NO TYPE COLUMN, unlike the Admin screen. Everything here is OPEX by
    construction, so both would carry one value on every row. The ACTIVE filter is kept:
    it is the only axis on which these rows differ.

    Ordering is (sort_order, code) with no project_type term. The Admin screen needs one
    because it interleaves two templates whose sort_order both restart at 1; this screen
    holds only one of them, so the model's own ordering would already be right and the
    explicit clause is here to say so rather than to change anything.
    """
    if not user_is_design_head(request.user):
        return HttpResponseForbidden(_CATALOGUE_FORBIDDEN)

    items = (_opex_catalogue_qs()
             .annotate(linked_count=Count('boq_items'))
             .order_by('sort_order', 'code'))

    active_filter = request.GET.get('active', '')
    if active_filter == '1':
        items = items.filter(is_active=True)
    elif active_filter == '0':
        items = items.filter(is_active=False)

    return render(request, 'projects/design/boq_catalogue.html', {
        'items':         items,
        'active_filter': active_filter,
        # Counts are of the whole OPEX catalogue, NOT of the filtered list — they are the
        # denominator the filter chips are read against, so filtering must not move them.
        'active_count':  _opex_catalogue_qs().filter(is_active=True).count(),
        'total_count':   _opex_catalogue_qs().count(),
    })


@login_required
def design_boq_catalogue_create(request):
    """Add one OPEX catalogue item. Design Head only.

    `project_type` IS DELETED FROM THE FORM AND SET ON THE INSTANCE. Not hidden, not
    disabled, not merely overwritten after binding — DELETED, so a POST carrying
    project_type=Residential is never read at all. The field is editable on the shared
    BOQItemMasterForm because the Admin screen needs it there; here it is the one value
    that must not be user-supplied, and `del` is what makes that true rather than merely
    intended. It is the same idiom the Admin edit view uses to keep `code` create-only.
    """
    if not user_is_design_head(request.user):
        return HttpResponseForbidden(_CATALOGUE_FORBIDDEN)

    if request.method == 'POST':
        form = BOQItemMasterForm(request.POST)
        del form.fields['project_type']
        if form.is_valid():
            item = form.save(commit=False)
            item.project_type = 'OPEX'
            item.save()
            log_activity(None, request.user.profile,
                         f"Created RESCO BOQ catalogue item "
                         f"'{item.code} — {item.description}'",
                         entity_type='BOQItemMaster', entity_id=item.pk,
                         action_code='design_boq_item_created')
            messages.success(request, f'Catalogue item "{item.code}" added.')
            return redirect('design_boq_catalogue')
    else:
        # The next slot at the end of the OPEX template, scoped to OPEX. The Admin screen's
        # equivalent is unscoped and is logged as a deferred finding; it is not touched
        # here, but this is new code and copying the unscoped form would have imported the
        # bug rather than inherited it.
        next_order = (_opex_catalogue_qs()
                      .aggregate(m=Max('sort_order'))['m'] or 0) + 1
        form = BOQItemMasterForm(initial={'sort_order': next_order, 'is_active': True})
        del form.fields['project_type']

    return render(request, 'projects/design/boq_catalogue_form.html', {
        'form':  form,
        'title': 'Add RESCO Catalogue Item',
        'item':  None,
    })


@login_required
def design_boq_catalogue_edit(request, item_id):
    """Edit one OPEX catalogue item. Design Head only.

    TWO FIELDS ARE DROPPED, FOR TWO DIFFERENT REASONS.

    `code` is create-only — it is the stable identifier existing BOQ rows are grouped
    under, and reassigning it would break the grouping. The Admin edit view drops it for
    exactly this reason and this one follows.

    `project_type` is dropped because this screen is OPEX-only. Left bound, it would let
    the Head move a row onto the Residential template, where it would immediately
    pre-populate every new Residential BOQ. That is the single edit on this form with
    cross-type blast radius, and it is not his to make.

    Existing BOQItem rows are never modified: BOQItem.description is a point-in-time
    snapshot taken at BOQ creation, and nothing here writes to BOQItem.
    """
    if not user_is_design_head(request.user):
        return HttpResponseForbidden(_CATALOGUE_FORBIDDEN)

    item = _opex_catalogue_item(item_id)

    if request.method == 'POST':
        form = BOQItemMasterForm(request.POST, instance=item)
        del form.fields['code']
        del form.fields['project_type']
        if form.is_valid():
            form.save()
            log_activity(None, request.user.profile,
                         f"Updated RESCO BOQ catalogue item '{item.code}' "
                         f"(active={item.is_active})",
                         entity_type='BOQItemMaster', entity_id=item.pk,
                         action_code='design_boq_item_updated')
            messages.success(request, f'Catalogue item "{item.code}" saved.')
            return redirect('design_boq_catalogue')
    else:
        form = BOQItemMasterForm(instance=item)
        del form.fields['code']
        del form.fields['project_type']

    return render(request, 'projects/design/boq_catalogue_form.html', {
        'form':         form,
        'title':        f'Edit RESCO Catalogue Item — {item.code}',
        'item':         item,
        'linked_count': item.boq_items.count(),
    })


@login_required
def design_boq_catalogue_toggle(request, item_id):
    """Deactivate / reactivate one OPEX catalogue item. Design Head only. POST only.

    DEACTIVATE IS THE ONLY REMOVAL, AND IT IS NOT A DELETE. BOQItem.item_master is
    SET_NULL, so deleting a catalogue row would null the link on every BOQ line built from
    it and drop those quantities out of aggregate_group_boq(), which joins on
    item_master__isnull=False. Nothing would raise and no screen would say so. There is no
    delete path here, and the request's word "delete" means this.

    NO GUARD ON LINKED ROWS, deliberately. Deactivating leaves existing sheets untouched:
    split_opex_boq_rows() reclassifies those rows as off-catalogue, so they still render
    with their quantities and can still be removed — they simply cannot be re-added, and
    the item stops being offered on new sheets. The linked-row count on the list exists to
    inform that decision, not to block it.
    """
    if request.method != 'POST':
        return redirect('design_boq_catalogue')
    if not user_is_design_head(request.user):
        return HttpResponseForbidden(_CATALOGUE_FORBIDDEN)

    item = _opex_catalogue_item(item_id)

    # A FLAGGED ITEM CANNOT BE DEACTIVATED WHILE STILL FLAGGED, the same rule and the same
    # wording as the Admin screen's toggle. Deactivating would leave a mandatory item the
    # picker never offers, which the completion guard then has to ignore to avoid stranding
    # every BOQ — a contradiction better refused than absorbed. Reactivation is unaffected.
    if item.is_active and item.is_mandatory:
        messages.error(request, f'Catalogue item "{item.code}" is marked mandatory — clear '
                                f'the mandatory flag before deactivating it.')
        return redirect('design_boq_catalogue')

    item.is_active = not item.is_active
    item.save(update_fields=['is_active', 'updated_at'])

    state = 'activated' if item.is_active else 'deactivated'
    log_activity(None, request.user.profile,
                 f"{state.capitalize()} RESCO BOQ catalogue item '{item.code}'",
                 entity_type='BOQItemMaster', entity_id=item.pk,
                 action_code=f'design_boq_item_{state}')
    messages.success(request, f'Catalogue item "{item.code}" {state}.')
    return redirect('design_boq_catalogue')
