"""
DG-B — damaged delivery at GRN: photos, the challan's damage Issue, and the draft that
keeps a refused GRN's typed values.

THE RULE (rulings R1-R11). When confirm_grn (SE) or override_grn (SCM) leaves a line with
damaged_quantity > 0, that line must end the submission with at least one LIVE photo
(already stored, or uploaded now) and at most DC_DAMAGE_PHOTO_MAX_PER_LINE. Lines whose
damage is RECORDED NOW (ruling QE: damaged > 0 and either the number changed or photos
were attached) also need the challan's damage Issue — the open one is reused with a
Comment, or a new one is raised from the submitter's description. A line whose damage is
set back to 0 leaves its photos and the Issue alone and adds a Comment (R7).

THE ORDER, which is what makes it all-or-nothing (R8). The two views call, in turn:

    plan_damage()           reads, writes nothing, uploads nothing; every refusal is here
    upload_planned_photos() before the transaction; a partial run discards itself
    -- transaction.atomic() --
    lock_challan()          the challan row, so two GRNs cannot both raise an Issue
    (the view's line saves)
    record_damage()         Issue / Comment / photo rows, after a fresh Issue lookup
    (recalculate_dc_status, unchanged, then notify_delivery_issue_raised for a new Issue)

and on ANY exception from the block they call discard_stored(). Nothing here changes how
_read_grn_lines parses or clamps, nor how recalculate_dc_status decides a status.

This module imports models, storage, permissions and the notification senders; it is
imported by views.py and never imports it, so there is no cycle.
"""
from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Exists, OuterRef

from .delivery_storage import (
    DAMAGE_PHOTO_STORAGE_OFF, DC_DAMAGE_PHOTO_MAX_PER_LINE, DeliveryStorageError,
    delivery_bucket, discard_unrecorded_damage_photo, upload_damage_photo,
    validate_damage_photo,
)
from .models import (
    Comment, DCLineDamagePhoto, DeliveryChallan, Issue, REASON_CREATED, log_activity,
)
from .notifications import send_notification
from .notify import notify, resolve_recipients
from .permissions import active_scm_users, project_managers
from .utils import record_transition

#: A damage description shorter than this says nothing a reader of the Issue can use.
DAMAGE_DESCRIPTION_MIN = 10

#: The POST field of the damage description, asked only when no damage Issue is open.
DAMAGE_DESCRIPTION_FIELD = 'damage_description'

#: An Issue in one of these statuses, on the challan, with a photo linked, is the
#: challan's damage Issue (R4). Resolved and Closed ones are history: new damage after
#: them raises a new Issue.
OPEN_DAMAGE_ISSUE_STATUSES = (Issue.OPEN, Issue.IN_PROGRESS)

GRN_DRAFT_CONFIRM = 'confirm'
GRN_DRAFT_OVERRIDE = 'override'

# Its own key, not issue_draft's: a refused GRN must never overwrite a refused Issue's
# draft, or the other way round.
_GRN_DRAFT_SESSION_KEY = 'grn_draft'


def photo_field(item):
    """The POST field carrying `item`'s damage photos (one input, `multiple`)."""
    return f'damage_photos_{item.pk}'


def _name(profile):
    return profile.user.get_full_name() or profile.user.username


def _line_label(item):
    return f'{item.boq_category} — {item.item_description}'


# ---------------------------------------------------------------------------
# The draft — a refused GRN keeps what was typed (R11)
# ---------------------------------------------------------------------------
#
# The GRN forms post and redirect, as before, so a refresh never re-posts. A refusal
# parks the raw typed strings in the session, tagged with the challan and the form, and
# delivery_challan_detail takes them out ONCE. Raw strings, not parsed values: a refused
# box is shown back as typed. Files cannot be put back into a file input, which is why
# the page says photos must be attached again.

def stash_grn_draft(request, challan, which, line_items):
    post = request.POST
    request.session[_GRN_DRAFT_SESSION_KEY] = {
        'where': f'grn:{challan.pk}:{which}',
        'lines': {
            str(item.pk): {
                'received': post.get(f'received_qty_{item.pk}', '').strip(),
                'damaged': post.get(f'damaged_qty_{item.pk}', '').strip(),
                'notes': post.get(f'grn_notes_{item.pk}', '').strip(),
            }
            for item in line_items
        },
        'description': post.get(DAMAGE_DESCRIPTION_FIELD, '').strip(),
        'on_behalf_reason': post.get('grn_on_behalf_reason', '').strip(),
    }


def pop_grn_draft(request, challan):
    """The draft stashed for `challan`, with `which` ('confirm' or 'override') added, or
    None. Removed from the session when returned, so only the next render shows it."""
    draft = request.session.get(_GRN_DRAFT_SESSION_KEY)
    if not draft:
        return None
    for which in (GRN_DRAFT_CONFIRM, GRN_DRAFT_OVERRIDE):
        if draft.get('where') == f'grn:{challan.pk}:{which}':
            del request.session[_GRN_DRAFT_SESSION_KEY]
            return dict(draft, which=which)
    return None


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def open_damage_issue(challan):
    """The challan's damage Issue (R4): Open or In Progress, on this challan, with at
    least one photo linked — live or withdrawn (ruling QG). One query. The oldest wins if
    a race or old data left two."""
    return (Issue.objects
            .filter(delivery_challan=challan, status__in=OPEN_DAMAGE_ISSUE_STATUSES)
            .filter(Exists(DCLineDamagePhoto.objects.filter(issue=OuterRef('pk'))))
            .order_by('raised_at', 'pk')
            .first())


def damage_issue_assignee(project):
    """Who a new damage Issue is assigned to: the site's PM (R5), or None.

    Read from project.assigned_pm, the site's PM of record — written only by
    site_pm.change_site_pm(). No helper returns the PM alone: project_managers() leads
    with a coordinator when there is no PM. An inactive PM counts as no PM (ruling QB):
    an Issue assigned to someone who cannot log in is an unassigned Issue that looks
    assigned.
    """
    pm = project.assigned_pm
    if pm is None or not (pm.is_active and pm.user.is_active):
        return None
    return pm


def _photo_counts(challan):
    """{line_id: (live photos, all photos)} for the challan's lines. One query."""
    counts = {}
    rows = (DCLineDamagePhoto.objects.filter(line__challan=challan)
            .values_list('line_id', 'withdrawn_at'))
    for line_id, withdrawn_at in rows:
        live, total = counts.get(line_id, (0, 0))
        counts[line_id] = (live + (withdrawn_at is None), total + 1)
    return counts


# ---------------------------------------------------------------------------
# The plan — every refusal, before any upload
# ---------------------------------------------------------------------------

@dataclass
class DamageLine:
    """A line whose damage is recorded in this submission (ruling QE)."""
    item: object
    damaged: int
    files: list


@dataclass
class DamagePlan:
    recorded: list = field(default_factory=list)   # DamageLine
    cleared: list = field(default_factory=list)    # DCLineItem set to 0 that has photos
    description: str = ''
    # The damage Issue the page-time lookup found. record_damage() looks again under the
    # challan lock; a difference between the two is the race ruling QK settles.
    expected_issue_pk: int = None

    @property
    def files(self):
        return [f for line in self.recorded for f in line.files]


def plan_damage(request, challan, line_items, grn_lines):
    """Decide what this GRN records about damage. Returns (plan, errors).

    Writes nothing and uploads nothing, so a refusal here leaves no trace. `grn_lines` is
    _read_grn_lines()'s output, already free of quantity errors; `line_items` is the same
    evaluated queryset, so iterating it again costs no query. The stored
    damaged_quantity on each item is still the value BEFORE this submission: neither
    view writes a line until after this returns.

    File checks come last (R8): a submission with a missing photo is refused for that,
    not for a file it would have refused anyway.
    """
    read = {item.pk: damaged for item, _received, damaged, _condition, _notes in grn_lines}
    files_by_line = {item.pk: request.FILES.getlist(photo_field(item)) for item in line_items}

    # A GRN with no damage, no prior damage and no photo is the GRN of before DG-B: no
    # photo query, no Issue lookup, nothing else asked.
    if not (any(read.values()) or any(files_by_line.values())
            or any(item.damaged_quantity for item in line_items)):
        return DamagePlan(), []

    counts = _photo_counts(challan)
    errors, plan = [], DamagePlan()
    for item in line_items:
        files = files_by_line[item.pk]
        label = _line_label(item)
        if item.pk not in read:
            # QF: a skipped line (blank received box) records nothing, so its photos
            # would be stored against nothing. Refused rather than dropped silently.
            if files:
                errors.append(f'{label}: photos were attached, but no received quantity '
                              f'was entered. Enter the quantity, or remove the photos.')
            continue
        damaged = read[item.pk]
        live, total = counts.get(item.pk, (0, 0))
        if damaged == 0:
            if files:
                errors.append(f'{label}: photos were attached, but no units are marked '
                              f'damaged. Enter the damaged quantity, or remove the photos.')
            elif item.damaged_quantity > 0 and total:
                plan.cleared.append(item)
            continue
        # R2, the end-state rule: what the line will hold once this submission is saved.
        if live + len(files) == 0:
            errors.append(f'{label}: {damaged} damaged — attach at least one photo of the '
                          f'damage.')
        elif live + len(files) > DC_DAMAGE_PHOTO_MAX_PER_LINE:
            errors.append(f'{label}: at most {DC_DAMAGE_PHOTO_MAX_PER_LINE} photos per line '
                          f'— {live} already on record and {len(files)} attached.')
        if files or damaged != item.damaged_quantity:
            plan.recorded.append(DamageLine(item=item, damaged=damaged, files=files))

    issue = open_damage_issue(challan) if (plan.recorded or plan.cleared) else None
    plan.expected_issue_pk = issue.pk if issue else None
    plan.description = request.POST.get(DAMAGE_DESCRIPTION_FIELD, '').strip()
    if plan.recorded and issue is None and len(plan.description) < DAMAGE_DESCRIPTION_MIN:
        errors.append(f'Describe the damage (at least {DAMAGE_DESCRIPTION_MIN} characters). '
                      f'It becomes the description of the damage issue raised for this '
                      f'challan.')
    if errors:
        return None, errors

    if plan.files and not delivery_bucket():
        return None, [DAMAGE_PHOTO_STORAGE_OFF]
    for upload in plan.files:
        try:
            validate_damage_photo(upload)
        except DeliveryStorageError as exc:
            errors.append(str(exc))
    if errors:
        return None, errors
    return plan, []


# ---------------------------------------------------------------------------
# Storage — before the transaction, discarded on any failure after it
# ---------------------------------------------------------------------------

def upload_planned_photos(plan, project):
    """Upload every planned photo. Returns [(DamageLine, stored dict)]. Raises
    DeliveryStorageError after discarding whatever this run already stored."""
    stored = []
    try:
        for line in plan.recorded:
            for upload in line.files:
                stored.append((line, upload_damage_photo(upload, project)))
    except DeliveryStorageError:
        discard_stored(stored)
        raise
    return stored


def discard_stored(stored):
    """Remove uploads no photo row records — after a rolled-back GRN. Never raises."""
    for _line, upload in stored:
        discard_unrecorded_damage_photo(upload)


# ---------------------------------------------------------------------------
# The writes — inside the views' transaction
# ---------------------------------------------------------------------------

class DamageIssueClosed(Exception):
    """The open damage Issue the page relied on was resolved or closed before this GRN
    took the lock, and no description was given to raise a new one (ruling QK). A
    refusal, raised inside the transaction so the line saves roll back with it."""


@dataclass
class DamageOutcome:
    issue: object
    created: bool


def lock_challan(challan):
    """Lock the challan row for the rest of the transaction.

    RACE: two GRNs on one challan at once (SE and SCM, or a double tap). Both can find no
    damage Issue and both would raise one. With the row locked, the second waits here
    until the first commits, then record_damage() looks again and finds the first one's
    Issue. A no-op on SQLite, which the test suite runs on; proved on Postgres by hand.
    """
    return DeliveryChallan.objects.select_for_update().get(pk=challan.pk)


def record_damage(plan, challan, project, profile, stored):
    """Write the damage Issue or its Comment and the photo rows. Call inside the
    transaction, after lock_challan() and the line saves. Returns a DamageOutcome, or
    None when there was nothing to record or nowhere to record it."""
    if not plan.recorded and not plan.cleared:
        return None
    issue, created, comment_lines = open_damage_issue(challan), False, []
    name = _name(profile)

    if plan.recorded:
        if issue is None:
            if len(plan.description) < DAMAGE_DESCRIPTION_MIN:
                raise DamageIssueClosed(
                    'The damage issue on this challan was resolved or closed while you '
                    'were working. Describe the damage to raise a new one.')
            title = f'Damaged delivery — DC {challan.dc_number}'
            # R5, and QJ: the same Issue, ledger row and activity line create_delivery_issue
            # writes for a delivery issue.
            issue = Issue.objects.create(
                project=project, delivery_challan=challan, task=None, title=title,
                description=plan.description, severity=Issue.HIGH, status=Issue.OPEN,
                raised_by=profile, assigned_to=damage_issue_assignee(project),
            )
            record_transition(issue, to_status=Issue.OPEN, actor=profile,
                              reason_code=REASON_CREATED, remark=title)
            log_activity(project, profile,
                         f'Raised delivery issue: {title} ({Issue.HIGH}) on DC '
                         f'{challan.dc_number}',
                         entity_type='Issue', entity_id=issue.pk, action_code='issue_created')
            created = True
        else:
            # QK: the page found no open Issue and asked for a description, and one was
            # raised meanwhile. The description is kept, in the Comment, not dropped.
            if plan.expected_issue_pk is None and plan.description:
                comment_lines.append(plan.description)
            comment_lines += [
                f'{name} recorded damage on {_line_label(line.item)}: {line.damaged} damaged'
                for line in plan.recorded
            ]
        for line, upload in stored:
            DCLineDamagePhoto.objects.create(line=line.item, issue=issue,
                                             uploaded_by=profile, **upload)

    # R7: damage set back to 0 on a line that has photos. The photos and the Issue are
    # left exactly as they are; the Comment is the record that it changed.
    if plan.cleared and issue is not None:
        comment_lines += [f'damage on {_line_label(item)} set to 0 by {name}'
                          for item in plan.cleared]

    if comment_lines:
        # QL: one Comment per submission, one line per line. Comment creation sends no
        # notification anywhere in the app (no signal; create_issue_comment only logs),
        # so nothing is sent here either; the activity line is create_issue_comment's.
        Comment.objects.create(project=project, task=None, issue=issue, parent=None,
                               author=profile, body='\n'.join(comment_lines))
        log_activity(project, profile, f'Commented on issue: {issue.title}',
                     entity_type='Comment', entity_id=issue.pk)

    return DamageOutcome(issue=issue, created=created) if issue is not None else None


def damage_message(outcome):
    """The sentence the GRN's success message ends with, or ''."""
    if outcome is None:
        return ''
    if not outcome.created:
        return ' The damage was added to the open damage issue for this challan.'
    message = f' Damage issue raised: "{outcome.issue.title}".'
    if outcome.issue.assigned_to_id is None:
        message += ' It is unassigned: this site has no active PM.'
    return message


# ---------------------------------------------------------------------------
# Notifications — a delivery issue raised
# ---------------------------------------------------------------------------

def damage_issue_scm_recipients():
    """Every active SCM user (R6), with the login row joined: resolve_recipients() reads
    profile.user.is_active for each one."""
    return list(active_scm_users().select_related('user'))


def notify_delivery_issue_raised(issue, actor, *, also_tell=()):
    """The notices for a newly raised delivery issue. Shared by create_delivery_issue
    and the GRN damage path.

    MOVED VERBATIM from create_delivery_issue (pre-flight 6): the assignee gets the
    issue_raised event (in-app + email, after commit) and the WhatsApp leg (after
    commit); every other manager an immediate in-app nudge, which is a database row and
    so rolls back with the caller's transaction. With `also_tell` empty — as
    create_delivery_issue calls it — the sends are exactly what they were.

    `also_tell` (the GRN path passes every active SCM user, R6) is told through the same
    issue_raised event, so in-app + email. De-duplicated: the assignee is never told
    twice, a manager who is also in `also_tell` gets the event instead of the nudge, and
    notify() drops the actor and anyone deactivated.
    """
    project, title, profile = issue.project, issue.title, actor
    assigned_to = issue.assigned_to
    raiser_name = profile.user.get_full_name() or profile.user.username
    _ic_link = f'/issues/{issue.pk}/'
    values = {'customer_name': project.customer_name,
              'project_id': project.project_id,
              'issue_title': title, 'raised_by_name': raiser_name}
    if assigned_to and assigned_to != profile:
        # In-app and email through notify() (T3a): wording from the issue_raised event or
        # Admin's active version, sent after commit, raiser and deactivated people dropped.
        # log_label keeps the template_name these rows always carried.
        notify(
            'issue_raised', recipients=[assigned_to], actor=profile,
            related_project=project, link=_ic_link, log_label='issue_created',
            values=values,
        )
        # WhatsApp is still sent here, template and params unchanged, until the WhatsApp
        # phase. resolve_recipients() gives it exactly notify()'s recipients (ruling Q3)
        # and on_commit gives it notify()'s timing, so a rolled-back issue sends nothing.
        _ic_whatsapp = resolve_recipients([assigned_to], profile)

        def _send_issue_whatsapp():
            for _wa in _ic_whatsapp:
                send_notification(
                    recipient=_wa,
                    message=(f'A new issue has been raised on project '
                             f'{project.customer_name}: "{title}". Please login to '
                             f'review and coordinate resolution.'),
                    channels=['whatsapp'],
                    link=_ic_link,
                    template='issue_created',
                    template_params=[project.customer_name,
                                     _wa.user.get_full_name() or _wa.user.username,
                                     project.customer_name, raiser_name],
                    related_project=project,
                    actor=profile,
                )
        transaction.on_commit(_send_issue_whatsapp, robust=True)

    extra = [p for p in also_tell if p != assigned_to]
    extra_pks = {p.pk for p in extra}
    # Notify every project manager (PM + coordinators), skipping the raiser and the
    # assignee (who are notified through other paths). project_managers() dedupes.
    for _mgr in project_managers(project):
        if _mgr != profile and _mgr != assigned_to and _mgr.pk not in extra_pks:
            send_notification(
                recipient=_mgr,
                message=f'Issue "{title}" raised on {project.project_id} — {project.customer_name}.',
                channels=['in_app'],
                link=f'/issues/{issue.pk}/',
                related_project=project,
                actor=profile,
            )
    if extra:
        notify(
            'issue_raised', recipients=extra, actor=profile,
            related_project=project, link=_ic_link, log_label='issue_created',
            values=values,
        )
