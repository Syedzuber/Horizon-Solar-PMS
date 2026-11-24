"""
B-5a — design BOQ groups (rulings 33-35; Zuber's decisions Q1-Q8 and Q-A..Q-F, 23 Nov 2026).

Design users put sites into named groups (a site in one group at most) and read one LIVE
consolidated BOQ per group, on screen and as a three-sheet Excel. Separate from SCM's
procurement groups in every respect: own tables, no lock taken or checked, and SCM never
reads these.

WHAT COUNTS (Q1). Only sites whose design status is in DESIGN_BOQ_COUNTED_STATUSES —
handed in, in either review, with the PM, or released — contribute to the totals. Every
other member shows as "Not counted yet". The one predicate is design_boq_counts().

NO READ IS WIDENED (Q-B). The picker lists only sites the user can already open (the BOQ
read gate, batched as permissions.boq_readable_projects_q); the add POST refuses any other
site with the same words as a site that does not exist; and a group — its screen, its
Excel, and every write on it — is open only to a user who can open EVERY live site in it.
So every per-site figure on these pages is one the user could already read on that site's
own BOQ.

THE TOTALS ARE SCM'S FUNCTION, UNCHANGED. design_views.aggregate_group_boq() takes project
pks and filters on quantity alone (no BOQ status, lock or SCM state), so it is called here
with the counted sites and is not modified. What it leaves out on purpose — blank
quantities — is fetched by one more query and reported, never summed silently (Q5).

NO PRICES anywhere: nothing on these pages, in their context or in the Excel reads a rate,
amount or value.
"""
import re
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q
from django.db.models.functions import Lower
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font

from .accb import ACCB_CONFIGURED_CODE
from .design_views import aggregate_group_boq
from .models import (
    BOQItem, DesignAssignment, DesignBOQGroup, DesignBOQGroupMembership, Project,
    DESIGN_BOQ_COUNTED_STATUSES, log_activity,
)
from .permissions import (
    boq_readable_project_ids, boq_readable_projects_q, user_can_use_design_boq_groups,
)

#: Refusal for anyone who is not a design user (permissions.user_can_use_design_boq_groups).
NOT_A_DESIGN_USER = 'BOQ groups are for design users.'

#: Zuber Q-B: a group holding even one site the user cannot open is refused whole.
GROUP_NOT_OPEN = "This group includes sites you can't open."

#: Zuber Q-B: a site the user can't open gets the SAME words as one that doesn't exist, so
#: the add POST cannot be used to find out which site codes exist.
SITE_NOT_FOUND = "A selected site doesn't exist."

#: Ballast Type Structure and Tin Shade are counted in whole numbers. A fraction on either
#: is almost always a kWp or area typed into a count (Zuber, 23 Nov), so it is warned about.
WHOLE_NUMBER_CODES = ('OPX-037', 'OPX-038')

#: Activity entries name at most this many sites, then "and N more": ActivityLog.action is
#: 255 characters and log_activity() cuts anything longer mid-word (Zuber Q-E).
LOG_SITES_SHOWN = 5

GROUP_NAME_MAX = DesignBOQGroup._meta.get_field('name').max_length

XLSX_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


# ---------------------------------------------------------------------------
# Predicates and small helpers
# ---------------------------------------------------------------------------

def design_boq_counts(assignment):
    """THE ONE PLACE "does this site's BOQ count in a design group's totals" is answered.

    Zuber Q1: by the design status, never BOQ.status (which is 'Draft' on every OPEX BOQ).
    A site with no design assignment never counts.
    """
    return assignment is not None and assignment.status in DESIGN_BOQ_COUNTED_STATUSES


def _assignment(project):
    """The site's design assignment, or None. getattr because the reverse one-to-one
    raises (an AttributeError subclass) when the row is missing."""
    return getattr(project, 'design_assignment', None)


def _person(profile):
    """A UserProfile's display name, or '' for none."""
    if profile is None:
        return ''
    return profile.user.get_full_name() or profile.user.username


def _short_list(codes):
    """'A, B, C' — or the first LOG_SITES_SHOWN codes and 'and N more' (Zuber Q-E)."""
    if len(codes) <= LOG_SITES_SHOWN:
        return ', '.join(codes)
    return f"{', '.join(codes[:LOG_SITES_SHOWN])} and {len(codes) - LOG_SITES_SHOWN} more"


def _clean_group_name(raw, exclude_pk=None):
    """(name, '') or (None, the refusal). Unique ignoring case, as the constraint says.

    The pre-check is for a readable message; uniq_design_boq_group_name_ci is what makes it
    true under two concurrent saves, and both callers catch its IntegrityError.
    """
    name = (raw or '').strip()
    if not name:
        return None, 'Give the group a name.'
    if len(name) > GROUP_NAME_MAX:
        return None, f'Keep the group name to {GROUP_NAME_MAX} characters.'
    # Any group whose name equals this one ignoring case, other than the one being renamed.
    clash = (DesignBOQGroup.objects.filter(name__iexact=name)
             .exclude(pk=exclude_pk).values_list('name', flat=True).first())
    if clash is not None:
        return None, f'A group named "{clash}" already exists. Choose another name.'
    return name, ''


def _live_members(group):
    """The group's memberships whose site is not soft-deleted, with everything the screen,
    the Excel and the access check read — one query.

    A soft-deleted site keeps its row (so it cannot join another group) but is hidden from
    the member list, the counts and the totals (pre-flight P5).
    """
    return list(group.memberships
                .filter(project__is_deleted=False)
                .select_related('project__program',
                                'project__design_assignment__assigned_to__user',
                                'added_by__user')
                .order_by('project__project_id'))


def _readable_q(request):
    """boq_readable_projects_q() for this request's user, built ONCE per request.

    The group screen asks it twice (the group gate and the picker) and the add POST twice
    (the gate and the posted sites). Building it can cost a query (the deputy lookup), so
    it is kept on the request rather than rebuilt; the answer cannot change mid-request.
    """
    if not hasattr(request, '_design_boq_readable_q'):
        request._design_boq_readable_q = boq_readable_projects_q(request.user)
    return request._design_boq_readable_q


def _readable_ids(request, projects):
    """The pks among `projects` whose BOQ this request's user may read. One query."""
    return boq_readable_project_ids(request.user, projects, readable_q=_readable_q(request))


def _group_open_to(request, members):
    """True if the user can open every live site in the group (Zuber Q-B).

    An empty group is open to every design user: there is nothing in it to read.
    One query when the group has members.
    """
    ids = {m.project_id for m in members}
    if not ids:
        return True
    return ids <= _readable_ids(request, Project.objects.filter(pk__in=ids))


def _open_group_or_refuse(request, pk):
    """(group, members, None) for a design user who can open the group, else
    (None, None, a 403 response). Every per-group route starts here, so the screen, the
    Excel and every write on a group answer the same question the same way."""
    # Design users only (ruling 33, Zuber Q-A): SCM has its own groups, nobody else groups.
    if not user_can_use_design_boq_groups(request.user):
        return None, None, HttpResponseForbidden(NOT_A_DESIGN_USER)
    # The creator rides with the group: the screen's header names them.
    group = get_object_or_404(DesignBOQGroup.objects.select_related('created_by__user'), pk=pk)
    members = _live_members(group)
    # The whole group, or nothing (Zuber Q-B): every per-site figure on the group's pages
    # must be one the user could already read on that site's own BOQ.
    if not _group_open_to(request, members):
        return None, None, HttpResponseForbidden(GROUP_NOT_OPEN)
    return group, members, None


# ---------------------------------------------------------------------------
# The consolidated BOQ
# ---------------------------------------------------------------------------

def _member_rows(members):
    """One dict per live member: what the Sites tab, the warnings and the Sites sheet show."""
    rows = []
    for membership in members:
        project = membership.project
        assignment = _assignment(project)
        rows.append({
            'membership': membership,
            'project':    project,
            'code':       project.project_id,
            'tender':     project.program.name if project.program else '',
            'designer':   _person(assignment.assigned_to) if assignment else '',
            'status':     (assignment.get_status_display() if assignment
                           else 'No design assignment'),
            'counted':    design_boq_counts(assignment),
        })
    return rows


def consolidate(member_rows):
    """The live consolidated BOQ of the counted members. 4 queries, whatever the size.

    aggregate_group_boq() supplies the catalogue lines, the ACCB configurations and the
    lines with no catalogue item, exactly as SCM's screen gets them (3 queries). One more
    query fetches every counted line any warning or marker needs:

      * a blank quantity — aggregate_group_boq() drops it (quantity > 0 only), so it is
        listed here as counted as 0 (Q5); a 0 is a statement and is not listed (Q-F);
      * a retired catalogue item — still summed (it is a real BOQ line), marked "Retired";
      * OPX-037 / OPX-038 with a fraction, or a line whose stored unit differs from the
        catalogue unit — listed; the line still counts in the total.
    """
    counted_ids = [r['project'].pk for r in member_rows if r['counted']]
    agg = aggregate_group_boq(counted_ids)

    # Every counted line that is blank, on a retired item, on a whole-number item, or whose
    # unit could differ from its catalogue unit. The unit comparison is done in Python
    # below: a database "<>" against a NULL catalogue unit would make every unlinked line
    # match. The filter only keeps the fetch small.
    flagged = list(
        BOQItem.objects
        .filter(boq__project_id__in=counted_ids)
        .filter(Q(boq_quantity__isnull=True)
                | Q(item_master__is_active=False)
                | Q(item_master__code__in=WHOLE_NUMBER_CODES)
                | ~Q(uom=F('item_master__unit')))
        .values('boq__project__project_id', 'item_master_id', 'item_master__code',
                'item_master__unit', 'item_master__is_active', 'accb_config__config_key',
                'description', 'uom', 'boq_quantity', 'serial_no')
        .order_by('boq__project__project_id', 'serial_no'))

    retired_items = {row['item_master_id'] for row in flagged
                     if row['item_master_id'] is not None
                     and row['item_master__is_active'] is False}
    retired_accb_keys = {row['accb_config__config_key'] for row in flagged
                         if row['accb_config__config_key'] is not None
                         and row['item_master__is_active'] is False}

    blanks, unit_warnings = [], []
    for row in flagged:
        site, code, qty = (row['boq__project__project_id'], row['item_master__code'],
                           row['boq_quantity'])
        if qty is None:
            blanks.append({'site': site, 'code': code or '',
                           'description': row['description']})
        if row['item_master_id'] is None:
            continue
        reasons = []
        if code in WHOLE_NUMBER_CODES and qty is not None and qty != qty.to_integral_value():
            reasons.append('not a whole number')
        if row['uom'] != row['item_master__unit']:
            reasons.append('unit differs from the catalogue')
        if reasons:
            unit_warnings.append({'site': site, 'code': code, 'quantity': qty,
                                  'line_unit': row['uom'],
                                  'catalogue_unit': row['item_master__unit'],
                                  'reasons': reasons})

    # The per-site split, summed per SITE: aggregate_group_boq()'s catalogue contributions
    # are one entry per LINE, so two lines on one item at one site would show twice.
    for line in agg['lines']:
        per_site = {}
        for site, qty in line['contributions']:
            per_site[site] = per_site.get(site, Decimal('0')) + qty
        line['per_site'] = per_site
        line['per_site_list'] = sorted(per_site.items())
        line['retired'] = line['item_master'] in retired_items
    for accb in agg['accb_lines']:
        accb['per_site'] = dict(accb['contributions'])
        accb['per_site_list'] = accb['contributions']
        accb['retired'] = accb['config_key'] in retired_accb_keys
        accb['code'] = ACCB_CONFIGURED_CODE

    return {
        'lines':          agg['lines'],
        'accb_lines':     agg['accb_lines'],
        'unlinked':       agg['unlinked'],
        'counted_codes':  [r['code'] for r in member_rows if r['counted']],
        'not_counted':    [r for r in member_rows if not r['counted']],
        'blanks':         blanks,
        'unit_warnings':  unit_warnings,
    }


def _picker_rows(request, group):
    """Every OPEX site with a design assignment that the user can open, as JSON-ready dicts
    for the add-sites picker (Zuber Q-B: the picker lists only sites the user can open).

    3 queries whatever the number of sites: the readable ids, their assignments with
    site, tender and designer, and every design-group membership among them (to grey out
    sites in another group and name it).
    """
    candidates = Project.objects.filter(project_type='OPEX', is_deleted=False,
                                        design_assignment__isnull=False)
    readable = _readable_ids(request, candidates)
    in_groups = {pid: (gid, name) for pid, gid, name in
                 DesignBOQGroupMembership.objects
                 .filter(project_id__in=readable)
                 .values_list('project_id', 'group_id', 'group__name')}
    rows = []
    for assignment in (DesignAssignment.objects
                       .filter(project_id__in=readable)
                       .select_related('project__program', 'assigned_to__user')
                       .order_by('project__project_id')):
        project = assignment.project
        group_id, group_name = in_groups.get(project.pk, (None, ''))
        rows.append({
            'id':       project.pk,
            'code':     project.project_id,
            'name':     project.site_name or '',
            'tender':   project.program.name if project.program else '',
            'designer': _person(assignment.assigned_to),
            'status':   assignment.get_status_display(),
            'counted':  design_boq_counts(assignment),
            'here':     group_id == group.pk,
            'group':    '' if group_id == group.pk else group_name,
        })
    return rows


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

@login_required
def design_boq_group_list(request):
    """Every design BOQ group: name, sites, counted sites, created by and when, and the
    create form. Access: design users (role Design or Design Head authority)."""
    # Design users only (ruling 33, Zuber Q-A).
    if not user_can_use_design_boq_groups(request.user):
        return HttpResponseForbidden(NOT_A_DESIGN_USER)

    # Each group with its live-site count and its counted-site count in one query. Both
    # Counts run over the same membership join; the joins to the site and its (one-to-one)
    # design assignment cannot multiply rows.
    live = Q(memberships__project__is_deleted=False)
    groups = list(
        DesignBOQGroup.objects
        .select_related('created_by__user')
        .annotate(site_count=Count('memberships', filter=live),
                  counted_count=Count('memberships', filter=live & Q(
                      memberships__project__design_assignment__status__in=(
                          DESIGN_BOQ_COUNTED_STATUSES))))
        .order_by(Lower('name')))

    # Which groups this user may open: every live member must be readable (Zuber Q-B).
    # One query for all memberships and one for the readable subset, whatever the number
    # of groups — never a check per group.
    members_by_group = {}
    for group_id, project_id in (DesignBOQGroupMembership.objects
                                 .filter(project__is_deleted=False)
                                 .values_list('group_id', 'project_id')):
        members_by_group.setdefault(group_id, set()).add(project_id)
    every_member = set().union(*members_by_group.values()) if members_by_group else set()
    readable = (_readable_ids(request, Project.objects.filter(pk__in=every_member))
                if every_member else set())
    for group in groups:
        group.can_open = members_by_group.get(group.pk, set()) <= readable
        group.created_by_name = _person(group.created_by)

    return render(request, 'projects/design/boq_groups.html', {
        'groups':          groups,
        'name_max':        GROUP_NAME_MAX,
        'group_not_open':  GROUP_NOT_OPEN,
    })


@login_required
def design_boq_group_create(request):
    """Create a design BOQ group and open it (straight into the picker, since it is
    empty). Access: design users. POST only; a GET goes back to the list."""
    # Design users only (ruling 33: any design user may create a group).
    if not user_can_use_design_boq_groups(request.user):
        return HttpResponseForbidden(NOT_A_DESIGN_USER)
    if request.method != 'POST':
        return redirect('design_boq_group_list')

    name, error = _clean_group_name(request.POST.get('name'))
    if error:
        messages.error(request, error)
        return redirect('design_boq_group_list')
    profile = request.user.profile
    try:
        with transaction.atomic():
            group = DesignBOQGroup.objects.create(name=name, created_by=profile)
            log_activity(None, profile,
                         f'{_person(profile)} created design BOQ group "{group.name}"',
                         entity_type='DesignBOQGroup', entity_id=group.pk,
                         action_code='design_boq_group_created')
    except IntegrityError:
        # Another group took the name between the pre-check and the insert.
        messages.error(request, f'A group named "{name}" already exists. Choose another name.')
        return redirect('design_boq_group_list')
    return redirect('design_boq_group_detail', pk=group.pk)


@login_required
def design_boq_group_rename(request, pk):
    """Rename a design BOQ group. Access: design users who can open every site in it.
    POST only; a GET goes back to the group."""
    group, _members, refusal = _open_group_or_refuse(request, pk)
    if refusal:
        return refusal
    if request.method != 'POST':
        return redirect('design_boq_group_detail', pk=group.pk)

    name, error = _clean_group_name(request.POST.get('name'), exclude_pk=group.pk)
    if error:
        messages.error(request, error)
        return redirect('design_boq_group_detail', pk=group.pk)
    if name == group.name:
        return redirect('design_boq_group_detail', pk=group.pk)
    old = group.name
    profile = request.user.profile
    try:
        with transaction.atomic():
            group.name = name
            group.save(update_fields=['name'])
            log_activity(None, profile,
                         f'{_person(profile)} renamed design BOQ group "{old}" to "{name}"',
                         entity_type='DesignBOQGroup', entity_id=group.pk,
                         action_code='design_boq_group_renamed')
    except IntegrityError:
        messages.error(request, f'A group named "{name}" already exists. Choose another name.')
        return redirect('design_boq_group_detail', pk=group.pk)
    messages.success(request, f'Renamed to "{name}".')
    return redirect('design_boq_group_detail', pk=group.pk)


@login_required
def design_boq_group_detail(request, pk):
    """One design BOQ group: its sites (with the add-sites picker) and its live
    consolidated BOQ. Access: design users who can open every site in the group."""
    group, members, refusal = _open_group_or_refuse(request, pk)
    if refusal:
        return refusal

    member_rows = _member_rows(members)
    boq = consolidate(member_rows)
    picker = _picker_rows(request, group)
    return render(request, 'projects/design/boq_group_detail.html', {
        'group':        group,
        'member_rows':  member_rows,
        'boq':          boq,
        # The same sentences the Summary sheet carries (warning_lines is shared).
        'warnings':     warning_lines(member_rows, boq),
        'picker_rows':  picker,
        'tenders':      sorted({row['tender'] for row in picker if row['tender']}),
        # Q2: a new, empty group opens straight into the picker.
        'open_picker':  not member_rows,
        'name_max':     GROUP_NAME_MAX,
    })


@login_required
def design_boq_group_add_sites(request, pk):
    """Add the picked sites to a design BOQ group — all of them, or none with a reason for
    each refused site. Access: design users who can open the group and every site added.
    POST only."""
    group, members, refusal = _open_group_or_refuse(request, pk)
    if refusal:
        return refusal
    if request.method != 'POST':
        return redirect('design_boq_group_detail', pk=group.pk)

    # Posted pks, de-duplicated in order. A non-number is a site that doesn't exist.
    raw = request.POST.getlist('project_ids')
    ids, refused = [], []
    for value in raw:
        value = str(value).strip()
        if not value.isdigit():
            refused.append(SITE_NOT_FOUND)
        elif int(value) not in ids:
            ids.append(int(value))
    if not ids and not refused:
        messages.error(request, 'No sites were selected.')
        return redirect('design_boq_group_detail', pk=group.pk)

    # Validation is batched — three queries whatever the number of sites: the sites with
    # their design assignments, the subset the user may open, and any design-group
    # membership they already hold.
    projects = {p.pk: p for p in (Project.objects.filter(pk__in=ids)
                                  .select_related('design_assignment'))}
    readable = _readable_ids(request, Project.objects.filter(pk__in=ids))
    existing = {m.project_id: m for m in (DesignBOQGroupMembership.objects
                                          .filter(project_id__in=ids)
                                          .select_related('group'))}
    to_add = []
    for pid in ids:
        project = projects.get(pid)
        # A site the user can't open is reported exactly like one that doesn't exist (Q-B).
        if project is None or project.is_deleted or pid not in readable:
            refused.append(SITE_NOT_FOUND)
            continue
        code = project.project_id
        if project.project_type != 'OPEX':
            refused.append(f'{code}: not a RESCO site.')
        elif _assignment(project) is None:
            refused.append(f'{code}: has no design assignment.')
        elif pid in existing and existing[pid].group_id == group.pk:
            refused.append(f'{code}: already in this group.')
        elif pid in existing:
            refused.append(f'{code}: already in "{existing[pid].group.name}". A site can be '
                           f'in one design group only.')
        else:
            to_add.append(project)

    # ALL OR NOTHING (pre-flight P9, Zuber): the picker only offers valid sites, so a
    # refusal here means the list changed under the user; adding part of it would leave
    # them guessing which part.
    if refused:
        messages.error(request, 'No sites were added. Fix these and try again:')
        for line in dict.fromkeys(refused):        # each distinct reason once, in order
            messages.error(request, line)
        return redirect('design_boq_group_detail', pk=group.pk)

    profile = request.user.profile
    codes = [p.project_id for p in to_add]
    try:
        with transaction.atomic():
            # bulk_create: one INSERT. The model has no save() logic to bypass.
            DesignBOQGroupMembership.objects.bulk_create([
                DesignBOQGroupMembership(group=group, project=p, added_by=profile)
                for p in to_add])
            log_activity(None, profile,
                         f'{_person(profile)} added {len(codes)} site'
                         f'{"" if len(codes) == 1 else "s"} to design BOQ group '
                         f'"{group.name}": {_short_list(codes)}',
                         entity_type='DesignBOQGroup', entity_id=group.pk,
                         action_code='design_boq_group_sites_added')
    except IntegrityError:
        # uniq_design_boq_group_site refused a site another user added to a group between
        # the check above and this insert. The whole add rolls back.
        messages.error(request, 'No sites were added: one of them was put in a group by '
                                'someone else a moment ago. Open the picker again to see '
                                'where each site is.')
        return redirect('design_boq_group_detail', pk=group.pk)

    not_counted = sum(1 for p in to_add if not design_boq_counts(_assignment(p)))
    note = (f' {not_counted} show{"s" if not_counted == 1 else ""} as not counted yet until '
            f'the BOQ is submitted.' if not_counted else '')
    messages.success(request, f'{len(codes)} site{"" if len(codes) == 1 else "s"} added: '
                              f'{", ".join(codes)}.{note}')
    return redirect('design_boq_group_detail', pk=group.pk)


@login_required
def design_boq_group_remove_site(request, pk):
    """Remove one site from a design BOQ group (its BOQ is not changed). Access: design
    users who can open the group. POST only; the confirmation is on the page."""
    group, _members, refusal = _open_group_or_refuse(request, pk)
    if refusal:
        return refusal
    if request.method != 'POST':
        return redirect('design_boq_group_detail', pk=group.pk)

    raw = request.POST.get('membership_id', '')
    if not raw.isdigit():
        messages.error(request, 'No site was selected.')
        return redirect('design_boq_group_detail', pk=group.pk)
    # Scoped to THIS group: a membership id from another group is a 404, not a removal
    # from a group the user did not open.
    membership = get_object_or_404(DesignBOQGroupMembership.objects.select_related('project'),
                                   pk=int(raw), group=group)
    code = membership.project.project_id
    profile = request.user.profile
    with transaction.atomic():
        membership.delete()
        log_activity(None, profile,
                     f'{_person(profile)} removed {code} from design BOQ group "{group.name}"',
                     entity_type='DesignBOQGroup', entity_id=group.pk,
                     action_code='design_boq_group_site_removed')
    messages.success(request, f'{code} removed from "{group.name}". Its BOQ is unchanged.')
    return redirect('design_boq_group_detail', pk=group.pk)


@login_required
def design_boq_group_excel(request, pk):
    """Download the group's live consolidated BOQ as .xlsx — Summary, By site, Sites
    (Zuber Q6). Access: design users who can open every site in the group. No prices."""
    group, members, refusal = _open_group_or_refuse(request, pk)
    if refusal:
        return refusal

    member_rows = _member_rows(members)
    boq = consolidate(member_rows)
    workbook = build_group_workbook(group, member_rows, boq, timezone.localtime())

    # The group name is typed by a user; it goes into a header, so it is sanitised rather
    # than trusted. The date is the IST date (TIME_ZONE is Asia/Kolkata).
    safe = re.sub(r'[^A-Za-z0-9._ -]', '_', group.name).strip() or 'group'
    response = HttpResponse(content_type=XLSX_TYPE)
    response['Content-Disposition'] = (
        f'attachment; filename="{safe}_live_{timezone.localdate():%Y-%m-%d}.xlsx"')
    workbook.save(response)
    return response


# ---------------------------------------------------------------------------
# The Excel
# ---------------------------------------------------------------------------

SUMMARY_SHEET = 'Summary'
BY_SITE_SHEET = 'By site'
SITES_SHEET = 'Sites'
SUMMARY_HEADERS = ['Code', 'Description', 'Unit', 'Sites', 'Total', 'Note']
UNLINKED_TITLE = 'Lines with no catalogue item (not summed across sites)'
UNLINKED_HEADERS = ['Site', 'Description', 'Unit', 'Quantity']
SITES_HEADERS = ['Site', 'Site name', 'Tender', 'Designer', 'Design status', 'Counted']


def warning_lines(member_rows, boq):
    """The warnings shown above the table and on the Summary sheet, as plain sentences.
    One list for both, so the screen and the file cannot say different things."""
    lines = []
    if boq['not_counted']:
        lines.append('Not counted yet: ' + ', '.join(
            f"{r['code']} ({r['status']})" for r in boq['not_counted']) + '.')
    if boq['blanks']:
        lines.append('Blank quantities counted as 0: ' + ', '.join(
            f"{b['site']} {b['code'] or b['description'][:40]}" for b in boq['blanks']) + '.')
    for w in boq['unit_warnings']:
        qty = 'blank' if w['quantity'] is None else _num(w['quantity'])
        lines.append(f"{w['site']} {w['code']}: quantity {qty}, line unit {w['line_unit']}, "
                     f"catalogue unit {w['catalogue_unit']} — {' and '.join(w['reasons'])}. "
                     f"Still counted in the total.")
    return lines


def _num(value):
    """A Decimal as a plain number for a sentence: 12, 12.5 — never 12.00 or 1.2E+1."""
    if value is None:
        return ''
    text = format(value.normalize(), 'f') if isinstance(value, Decimal) else str(value)
    return text


def _bold_row(ws, row_number):
    for cell in ws[row_number]:
        cell.font = Font(bold=True)


def build_group_workbook(group, member_rows, boq, generated_at):
    """The three-sheet workbook. Pure: reads only what consolidate() returned.

    Quantities are written as numbers (Decimal → float would lose nothing at 2 places, but
    openpyxl writes Decimal natively). ACCB descriptions are written whole: a cell holds up
    to 32,767 characters, and B-3c's text runs past 255.
    """
    wb = Workbook()
    wrap = Alignment(wrap_text=True, vertical='top')
    counted = boq['counted_codes']

    # ---- Summary -----------------------------------------------------------------
    ws = wb.active
    ws.title = SUMMARY_SHEET
    ws.append([f'{group.name} — live consolidated BOQ'])
    ws['A1'].font = Font(bold=True, size=13)
    ws.append([f'Generated {generated_at:%d %b %Y %H:%M} IST from {len(counted)} counted '
               f'site{"" if len(counted) == 1 else "s"} of {len(member_rows)}. '
               f'Quantities only, no prices.'])
    warnings = warning_lines(member_rows, boq)
    if warnings:
        ws.append([])
        ws.append(['Warnings'])
        _bold_row(ws, ws.max_row)
        for line in warnings:
            ws.append([line])
    ws.append([])
    ws.append(SUMMARY_HEADERS)
    _bold_row(ws, ws.max_row)
    header_row = ws.max_row
    for line in boq['lines']:
        ws.append([line['item_master__code'], line['item_master__description'],
                   line['item_master__unit'], line['site_count'], line['total_quantity'],
                   'Retired' if line['retired'] else ''])
    for accb in boq['accb_lines']:
        ws.append([accb['code'], accb['description'], accb['unit'], accb['site_count'],
                   accb['total_quantity'], 'Retired' if accb['retired'] else ''])
    for row in ws.iter_rows(min_row=header_row + 1, min_col=2, max_col=2):
        row[0].alignment = wrap
    if boq['unlinked']:
        ws.append([])
        ws.append([UNLINKED_TITLE])
        _bold_row(ws, ws.max_row)
        ws.append(UNLINKED_HEADERS)
        _bold_row(ws, ws.max_row)
        for item in boq['unlinked']:
            ws.append([item.boq.project.project_id, item.description, item.uom,
                       item.boq_quantity])
    for letter, width in (('A', 14), ('B', 80), ('C', 9), ('D', 8), ('E', 12), ('F', 10)):
        ws.column_dimensions[letter].width = width

    # ---- By site: items × counted sites -----------------------------------------
    by = wb.create_sheet(BY_SITE_SHEET)
    by.append(['Code', 'Description', 'Unit', 'Note'] + counted + ['Total'])
    _bold_row(by, 1)
    for line in boq['lines']:
        by.append([line['item_master__code'], line['item_master__description'],
                   line['item_master__unit'], 'Retired' if line['retired'] else '']
                  + [line['per_site'].get(code) for code in counted]
                  + [line['total_quantity']])
    for accb in boq['accb_lines']:
        by.append([accb['code'], accb['description'], accb['unit'],
                   'Retired' if accb['retired'] else '']
                  + [accb['per_site'].get(code) for code in counted]
                  + [accb['total_quantity']])
    by.freeze_panes = 'E2'
    by.column_dimensions['A'].width = 14
    by.column_dimensions['B'].width = 60

    # ---- Sites ----------------------------------------------------------------------
    st = wb.create_sheet(SITES_SHEET)
    st.append(SITES_HEADERS)
    _bold_row(st, 1)
    for r in member_rows:
        st.append([r['code'], r['project'].site_name or '', r['tender'], r['designer'],
                   r['status'], 'Yes' if r['counted'] else 'No'])
    for letter, width in (('A', 14), ('B', 30), ('C', 28), ('D', 22), ('E', 30), ('F', 9)):
        st.column_dimensions[letter].width = width
    return wb

