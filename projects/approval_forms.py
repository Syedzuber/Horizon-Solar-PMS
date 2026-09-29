"""
Approvals S2a — reading the approval screens' POSTs.

A module of its own rather than more of forms.py, so the approval screens are one set:
approvals.py writes, approval_views.py draws and routes, this module reads.

NO MODELFORMS, DELIBERATELY. Every write goes through approvals.py, so nothing here saves
anything. A function here turns request.POST / request.FILES into the arguments an
approvals.py entry point takes, and says what is wrong with them BEFORE any file reaches
storage. Whether the request may be written at all — the kind rules, the same-person
rule, who may be named — is the chokepoint's decision, never this module's; its
ApprovalRefused is what the person sees.

SCOPE PICKERS are three plain multi-selects (tenders, sites, site groups), filtered the
way order_views._raise_candidates() filters its candidates: live tenders, live sites, and
procurement groups of live tenders. SiteGroup has no soft-delete of its own. The
vendor-order picker is not reused: its wording is about purchases.

Approvals 2a-2 adds the resubmit page's revision (parse_revision — only the fields that
actually changed), its "keep an approval" boxes (parse_carry — the one FORM rule on a
kept approval: a reason of at least KEEP_REASON_MIN non-space characters, and no keep for
a party whose approver is being changed), and the proxy form's evidence files
(parse_evidence_files).

Approvals 3a adds the pre-dispatch kind. Its request names one PO/PI record (a
VendorOrder — "PO/PI record" in every label, never "order") and, optionally, the approved
pre-order approval behind it. THE VENDOR IS DERIVED FROM THE RECORD, never posted: the
raise page has no vendor field for this kind, and parse_revision ignores a posted vendor
on a pre-dispatch request.

Approvals 3b adds which PO/PI records an approved pre-order approval covers: the link
form's picker (order_link_choices, parse_linked_order) and the read-only block on a PO/PI
record's page (pre_order_coverage). Linking is optional; approvals.py writes the links.

Material lines (28 Sep 2026, D-A28) replace the request-wide make, specification and
quantity note on both forms: parse_lines reads the lines table (inputs line-<i>-<field>),
line_rows fills it from a request's live lines. THIS MODULE DOES NOT CHECK A QUANTITY. The
unit rules (a whole number for a count unit, two places for a measured one) are
approvals._clean_lines', which refuses; a second copy here could only disagree with it.

Contractor bills 4a-2 add the bill's raise page: bill_sites (step 1's list, and what a
?project= must be), bill_task_choices, contractor_choices, and parse_bill_create. As for
material, the RULES are the chokepoint's: which vendor may bill, which tasks, the amount,
number and date are approvals._clean_bill's, and the view asks it before anything is
uploaded. This module only reads the POST, and checks the PDF's bytes and the photos'
types — because those must be refused before a file reaches storage, and the chokepoint
only ever sees a file already stored.

Contractor bills 4b-1 add a bill's resubmit (parse_bill_revision): the title,
description, amount, bill number, date, ticked tasks and an optional replacement PDF. The
contractor and site are not drawn; a posted one that differs is still passed on, so the
chokepoint's own refusal is what SCM reads. parse_carry refuses keeping the Site
Engineer's confirmation once the ticked tasks differ from the bill's (B). file_accept
builds every file input's accept attribute: MIME types as well as extensions, so a phone
offers photos in a format the server takes (Q6).
"""
import re
import uuid as _uuid

from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as date_format

# approvals.py imports nothing of this module (nor of views), so no circular import.
from .approvals import BILL_CONTRACTOR_LOCKED
from .bill_storage import BillStorageError, validate_bill_pdf
from .models import (
    ApprovalOrderLink, ApprovalRequest, Program, Project, SiteGroup, Task, UserProfile, Vendor,
    VendorOrder,
    APPROVAL_APPROVED, APPROVAL_KIND_MATERIAL_PRE_DISPATCH, APPROVAL_KIND_MATERIAL_PRE_ORDER,
    APPROVAL_PARTY_CHOICES, APPROVAL_PARTY_DESIGN, APPROVAL_PARTY_SITE_ENGINEER,
    APPROVAL_STEP_APPROVED,
    GROUP_TYPE_PROCUREMENT, VENDOR_BILLABLE_KINDS,
)
from .permissions import (
    profile_can_be_approval_assignee, user_can_view_approval_request,
    user_has_design_head_authority,
)
from .units import format_quantity
from .views import MIME_TYPE_MAP, _validate_upload_file

#: Files one submission may carry — the vendor-order raise's DOC_SLOTS number. The page
#: may be used again (a resubmit adds more), so this caps a request, not a round.
ATTACHMENT_LIMIT = 5

#: What a proxy decision's evidence may be: a PDF, or a photo or screenshot (a WhatsApp
#: screenshot is PNG or JPEG). Not HEIC or WebP — views._validate_upload_file's MIME map
#: has neither (SECONDARY_FINDINGS, Approvals 2a-2).
EVIDENCE_EXTENSIONS = ['pdf', 'jpg', 'jpeg', 'png']

#: What a contractor bill's photos may be (4a-2 ruling 6). Never a PDF: photos go to the
#: PUBLIC bucket as ordinary attachments (D-A37), and a PDF there could be a second copy of
#: the bill — the one document that must only ever sit in the private bills bucket.
BILL_PHOTO_EXTENSIONS = ['jpg', 'jpeg', 'png']

#: The shortest reason, in non-space characters, for keeping an earlier approval (D-A20).
KEEP_REASON_MIN = 15

_PARTY_LABELS = dict(APPROVAL_PARTY_CHOICES)

#: The fields of one row of the lines table, as its inputs name them: line-<i>-<field>.
LINE_FIELDS = ('id', 'description', 'make', 'specification', 'quantity', 'unit')
_LINE_INPUT = re.compile(r'^line-(\d+)-(' + '|'.join(LINE_FIELDS) + r')$')


def person_name(profile):
    return profile.user.get_full_name() or profile.user.username


def file_accept(extensions):
    """A file input's accept attribute for `extensions` (4b-1, Q6): each type's MIME type
    (views.MIME_TYPE_MAP), then each extension — "image/jpeg,image/png,.jpg,.jpeg,.png".
    Naming image/jpeg is meant to make an iPhone hand over a HEIC photo converted to JPEG
    (not yet walked on a real iPhone); the extensions keep a desktop picker filtering by
    name. No `capture`: that would take the
    choice of a photo already in the gallery away on many phones. Only a convenience —
    the server still judges every file (views._validate_upload_file)."""
    mimes = dict.fromkeys(MIME_TYPE_MAP[ext] for ext in extensions if ext in MIME_TYPE_MAP)
    return ','.join(list(mimes) + [f'.{ext}' for ext in extensions])


def _pks(post, name):
    """The posted integer pks under `name`, de-duplicated, in order. Junk is dropped."""
    return [int(raw) for raw in dict.fromkeys(post.getlist(name)) if raw.isdigit()]


def _profile(post, name):
    raw = (post.get(name) or '').strip()
    if not raw.isdigit():
        return None
    return UserProfile.objects.select_related('user').filter(pk=int(raw)).first()


# ---------------------------------------------------------------------------
# Choices
# ---------------------------------------------------------------------------

def _live_programs():
    return Program.objects.filter(is_deleted=False)


def _live_projects():
    return Project.objects.filter(is_deleted=False)


def _live_groups():
    return SiteGroup.objects.filter(group_type=GROUP_TYPE_PROCUREMENT,
                                    program__is_deleted=False)


def scope_choices():
    """The three scope lists the raise page offers. Three queries."""
    return {
        'programs':    list(_live_programs().order_by('name')),
        'projects':    list(_live_projects().only('pk', 'project_id', 'customer_name')
                            .order_by('project_id')),
        'site_groups': list(_live_groups().select_related('program')
                            .order_by('program__name', 'name')),
    }


def vendor_choices():
    """Active vendors. A pre-order approval may name one; it need not."""
    return list(Vendor.objects.filter(is_active=True).order_by('name'))


def _day(moment):
    return date_format(timezone.localtime(moment), 'd M Y')


def po_pi_record_label(order):
    """One PO/PI record as the picker names it: vendor, PO and PI numbers, the date it was
    RECORDED in PMS (there is no PO date field — created_at is when SCM entered it), and
    what it was sized against. VendorOrder.scope_label's last form, "Order #<pk>", says
    "order", so a record naming no site or tender reads "PO/PI record #<pk>" instead."""
    scope = order.scope_label
    if scope == f'Order #{order.pk}':
        scope = f'PO/PI record #{order.pk}'
    return (f'{order.vendor.name} · PO {order.po_number or "—"} · '
            f'PI {order.pi_number or "—"} · recorded {_day(order.created_at)} · {scope}')


def _by_vendor(rows, label):
    """[{vendor, options: [{pk, vendor_pk, label}]}] — one <optgroup> per vendor, vendors
    by name, `rows`' own order kept within each."""
    groups = {}
    for row in rows:
        groups.setdefault(row.vendor_id, {'vendor': row.vendor, 'options': []})
        groups[row.vendor_id]['options'].append(
            {'pk': row.pk, 'vendor_pk': row.vendor_id, 'label': label(row)})
    return sorted(groups.values(), key=lambda g: (g['vendor'].name.lower(), g['vendor'].pk))


def po_pi_record_choices():
    """Every PO/PI record, grouped by vendor, newest first within each vendor. A
    VendorOrder has no soft delete, status or active flag, so nothing is filtered out —
    and a deactivated vendor's record stays offered: goods already ordered can still ship.
    Three queries (records with vendor, sites with projects, programs)."""
    rows = (VendorOrder.objects.select_related('vendor')
            .prefetch_related('sites__project', 'programs__program')
            .order_by('-created_at', '-pk'))
    return _by_vendor(rows, po_pi_record_label)


def pre_order_label(approval):
    return (f'{approval.title} · {approval.vendor.name} · '
            f'approved {_day(approval.closed_at)}')


def pre_order_choices():
    """Approved pre-order approvals that name a vendor, grouped by vendor, most recently
    approved first. One without a vendor is not offered: create_approval_request() refuses
    a link whose vendor differs from the PO/PI record's, and none never matches. One query.
    """
    rows = (ApprovalRequest.objects
            .filter(kind=APPROVAL_KIND_MATERIAL_PRE_ORDER, status=APPROVAL_APPROVED,
                    vendor__isnull=False)
            .select_related('vendor').order_by('-closed_at', '-pk'))
    return _by_vendor(rows, pre_order_label)


def _active_links(approval):
    return ApprovalOrderLink.objects.filter(approval=approval, removed_at__isnull=True)


def order_link_choices(approval):
    """The PO/PI records SCM may link to the approved pre-order `approval` (Approvals
    3b): every record not already actively linked to it, grouped by vendor, newest first
    within each, labelled as 3a's picker labels them. Only the approval's vendor's records
    when it names one; records of every vendor when it names none. Five queries whatever
    the number of records: the records with their vendor (the exclusion is a subquery),
    then sites, their projects, programs and their tenders for the labels."""
    rows = (VendorOrder.objects
            .exclude(pk__in=_active_links(approval).values('vendor_order_id'))
            .select_related('vendor')
            .prefetch_related('sites__project', 'programs__program')
            .order_by('-created_at', '-pk'))
    if approval.vendor_id is not None:
        rows = rows.filter(vendor_id=approval.vendor_id)
    return _by_vendor(rows, po_pi_record_label)


def parse_linked_order(post):
    """The PO/PI record the link form names, or None when missing or unknown. Whether it
    may be linked is the chokepoint's decision (approvals.link_order_to_approval)."""
    pk = (post.get('vendor_order') or '').strip()
    if not pk.isdigit():
        return None
    return VendorOrder.objects.filter(pk=int(pk)).first()


def pre_order_coverage(user, order):
    """The "Covered by pre-order approval" block on `order`'s PO/PI record page (Approvals
    3b): [{title, url}] for each ACTIVE link, most recently linked first. `url` only where
    user_can_view_approval_request() admits `user` to that approval, so no link leads to a
    403; the title is shown either way. [] when there are none, and the page then draws
    nothing. One query when there are none, two when there are some (the approvals'
    steps, for the predicate)."""
    links = list(ApprovalOrderLink.objects.filter(vendor_order=order, removed_at__isnull=True)
                 .select_related('approval')
                 .prefetch_related('approval__steps')
                 .order_by('-linked_at', '-pk'))
    return [{'title': link.approval.title,
             'url': (reverse('approval_detail', args=[link.approval_id])
                     if user_can_view_approval_request(user, link.approval) else None)}
            for link in links]


def assignee_choices(party):
    """Everyone who may be NAMED as the `party` approver, by name.

    profile_can_be_approval_assignee() decides; the query only narrows which active
    profiles it is asked about, so the rule lives in one place.
    """
    candidates = UserProfile.objects.filter(is_active=True).select_related('user')
    if party == APPROVAL_PARTY_DESIGN:
        candidates = candidates.filter(is_design_head=True)
    return sorted((p for p in candidates if profile_can_be_approval_assignee(p, party)),
                  key=person_name)


def design_authority_choices():
    """Everyone who may DECIDE a design step: each Design Head and each Head's named
    deputy. user_has_design_head_authority() decides; the query narrows to the Heads and
    anybody named as a deputy. Offered when SCM records a design decision on someone's
    behalf — whose decision it was, not who was named."""
    candidates = (UserProfile.objects.filter(is_active=True)
                  .filter(Q(is_design_head=True) | Q(deputy_for__is_design_head=True))
                  .select_related('user').distinct())
    return sorted((p for p in candidates if user_has_design_head_authority(p.user)),
                  key=person_name)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_client_uuid(data, name='client_uuid'):
    """The raise page's idempotency key — the hidden field, or the ?key= in the page's
    URL — or None when absent or malformed."""
    try:
        return _uuid.UUID((data.get(name) or '').strip())
    except ValueError:
        return None


def parse_scope(post, errors):
    """The three multi-selects. Returns (programs, projects, site_groups). A pk that no
    longer resolves — deleted since the page was drawn — is an error, never dropped
    silently: a request that quietly names less than SCM ticked says something untrue."""
    picked = []
    for name, queryset, noun in (('program', _live_programs(), 'tender'),
                                 ('project', _live_projects(), 'site'),
                                 ('site_group', _live_groups(), 'site group')):
        wanted = _pks(post, name)
        rows = list(queryset.filter(pk__in=wanted)) if wanted else []
        if len(rows) != len(wanted):
            errors.append(f'A {noun} you chose no longer exists. Choose again.')
        picked.append(rows)
    return tuple(picked)


def _validated_files(request, name, errors, allowed_extensions=None):
    """The files posted under `name`, each validated the way every upload is
    (views._validate_upload_file: type, 20 MB, MIME). Touches no storage."""
    files = request.FILES.getlist(name)
    if len(files) > ATTACHMENT_LIMIT:
        errors.append(f'Attach at most {ATTACHMENT_LIMIT} files at a time.')
    for upload in files:
        try:
            _validate_upload_file(upload, allowed_extensions)
        except ValueError as exc:
            errors.append(f'{upload.name}: {exc}.')
    return files


def parse_attachments(request, errors):
    """The files posted under `attachments`. Touches no storage."""
    return _validated_files(request, 'attachments', errors)


def parse_evidence_files(request, errors):
    """The proxy form's evidence files, under `evidence_files`: PDF and photos only
    (EVIDENCE_EXTENSIONS). Touches no storage."""
    return _validated_files(request, 'evidence_files', errors, EVIDENCE_EXTENSIONS)


def _vendor(post, errors, current=None):
    """The posted vendor: an active one, or `current` — the vendor the request already
    names, still offered "(as before — inactive)" after it was deactivated, so an
    untouched select never reads as a change. None when none is chosen."""
    raw = (post.get('vendor') or '').strip()
    if not raw:
        return None
    if current is not None and raw == str(current.pk):
        return current
    vendor = (Vendor.objects.filter(pk=int(raw), is_active=True).first()
              if raw.isdigit() else None)
    if vendor is None:
        errors.append('The vendor you chose is no longer available. Choose again.')
    return vendor


def _vendor_order(post, errors):
    """The posted PO/PI record, with its vendor. Required: a pre-dispatch request ships
    against exactly one."""
    raw = (post.get('vendor_order') or '').strip()
    if not raw:
        errors.append('Choose the PO/PI record this dispatch is against.')
        return None
    order = (VendorOrder.objects.select_related('vendor').filter(pk=int(raw)).first()
             if raw.isdigit() else None)
    if order is None:
        errors.append('The PO/PI record you chose no longer exists. Choose again.')
    return order


def _pre_order(post, errors, vendor):
    """The posted pre-order approval, or None when none is chosen (the link is optional).
    Whether it may be linked — its kind, that it is approved, its vendor — is the
    chokepoint's rule. The vendor is also compared here, only so the message names the
    PO/PI record the person just chose."""
    raw = (post.get('pre_order_request') or '').strip()
    if not raw:
        return None
    pre_order = (ApprovalRequest.objects.filter(pk=int(raw)).first()
                 if raw.isdigit() else None)
    if pre_order is None:
        errors.append('The pre-order approval you chose no longer exists. Choose again.')
    elif vendor is not None and pre_order.vendor_id != vendor.pk:
        errors.append('The pre-order approval you chose is for a different vendor from '
                      'the PO/PI record.')
    return pre_order


def parse_lines(post):
    """The lines table's rows, from inputs named line-<i>-<field>. Returns
    [{id, description, make, specification, quantity, unit}] in order of i, every value
    as posted (quantity a string), for the chokepoint's `lines` — and, after a refusal,
    for drawing the table again exactly as typed.

    GAPS IN i ARE EXPECTED: removing a row removes its inputs, so line-0 and line-2 may
    arrive without line-1. Rows are ordered by i as a NUMBER (line-10 after line-9), and
    the numbers themselves mean nothing beyond that order.

    A row with nothing typed in it — no description, make, specification or quantity,
    and no id — is a row that was added and left empty; it is dropped, not sent as a
    line to be refused. `id` becomes an int when it is one; anything else is passed on
    as posted, for the chokepoint to refuse."""
    rows = {}
    for key in post:
        match = _LINE_INPUT.match(key)
        if match:
            rows.setdefault(int(match.group(1)), {})[match.group(2)] = post.get(key, '')
    lines = []
    for index in sorted(rows):
        row = {field: rows[index].get(field, '') for field in LINE_FIELDS}
        raw_id = row['id'].strip()
        row['id'] = int(raw_id) if raw_id.isdigit() else (raw_id or None)
        typed = any(row[field].strip() for field in
                    ('description', 'make', 'specification', 'quantity'))
        if typed or row['id'] is not None:
            lines.append(row)
    return lines


def line_rows(material):
    """The lines table's rows for `material` as it stands: its live lines in position
    order, in parse_lines' shape, quantity written as people read it ("120", "2.50").
    [] for a request with no lines (one raised before lines) or no material."""
    if material is None:
        return []
    return [{'id': line.pk, 'description': line.description, 'make': line.make,
             'specification': line.specification,
             'quantity': format_quantity(line.quantity, line.unit), 'unit': line.unit}
            for line in material.lines.order_by('position')]


def parse_create(request, kind=APPROVAL_KIND_MATERIAL_PRE_ORDER):
    """The raise page's POST. Returns (cleaned, errors).

    `cleaned` holds create_approval_request()'s keyword arguments, less kind, raised_by
    and attachments, plus `files` (the validated uploads) and `client_uuid`. Missing
    people, a missing title, a bad quantity and the like are NOT errors here: the
    chokepoint refuses them, in its own words. The material is `lines` (parse_lines).

    Pre-dispatch (Approvals 3a): the PO/PI record is required and the vendor is the
    record's — a posted `vendor` is ignored. The pre-order link is optional.
    """
    post, errors = request.POST, []

    order = pre_order = None
    if kind == APPROVAL_KIND_MATERIAL_PRE_DISPATCH:
        order = _vendor_order(post, errors)
        vendor = order.vendor if order is not None else None
        pre_order = _pre_order(post, errors, vendor)
    else:
        vendor = _vendor(post, errors)

    design = post.get('design_signoff_required') == 'on'
    programs, projects, site_groups = parse_scope(post, errors)
    files = parse_attachments(request, errors)

    cleaned = {
        'title':                   post.get('title', ''),
        'description':             post.get('description', ''),
        'vendor':                  vendor,
        'pm_assignee':             _profile(post, 'pm_assignee'),
        'design_signoff_required': design,
        # Ignored unless the box is ticked, so a Head picked and then un-ticked is not
        # sent to the chokepoint as a contradiction the person can no longer see.
        'design_assignee':         _profile(post, 'design_assignee') if design else None,
        'material':    {},
        'lines':       parse_lines(post),
        'programs':    programs,
        'projects':    projects,
        'site_groups': site_groups,
        'files':       files,
        'client_uuid': parse_client_uuid(post),
    }
    if kind == APPROVAL_KIND_MATERIAL_PRE_DISPATCH:
        cleaned['material'].update(vendor_order=order, pre_order_request=pre_order)
    return cleaned, errors


def parse_proxy(post):
    """A proxy record's three fields: (decided_by, channel, evidence). Unchecked here —
    ProxyDecision's rules are the chokepoint's."""
    return (_profile(post, 'decided_by'), (post.get('channel') or '').strip(),
            post.get('evidence', ''))


def parse_new_assignee(post):
    return _profile(post, 'new_assignee')


def parse_assignee_overrides(post, holders, errors):
    """The resubmit page's per-party approver selects. `holders` is {party: profile} —
    who holds each party now. Returns {party: profile} for the parties CHANGED only, so
    naming the same person again is not sent as an override (no ledger segment)."""
    changed = {}
    for party, holder in holders.items():
        raw = (post.get(f'assignee_{party}') or '').strip()
        if not raw or (raw.isdigit() and int(raw) == holder.pk):
            continue
        profile = _profile(post, f'assignee_{party}')
        if profile is None:
            errors.append('An approver you chose no longer exists. Choose again.')
            continue
        changed[party] = profile
    return changed


# ---------------------------------------------------------------------------
# Resubmit revision and kept approvals (Approvals 2a-2)
# ---------------------------------------------------------------------------

def _text(value):
    """A text as the chokepoint stores it — stripped — with a browser's CRLF read as LF,
    so a textarea posted back untouched is not a change."""
    return (value or '').replace('\r\n', '\n').strip()


def current_scope_pks(approval):
    """The request's scope as the resubmit pickers can show it: LIVE rows only, by the
    same three definitions the pickers use. {revision key: {pk}}. Three queries."""
    return {
        'programs':    set(_live_programs().filter(pk__in=approval.programs.all())
                           .values_list('pk', flat=True)),
        'projects':    set(_live_projects().filter(pk__in=approval.projects.all())
                           .values_list('pk', flat=True)),
        'site_groups': set(_live_groups().filter(pk__in=approval.site_groups.all())
                           .values_list('pk', flat=True)),
    }


def parse_revision(post, approval, errors):
    """The resubmit page's edited details. Returns the `revision` dict for
    resubmit_approval_request() holding ONLY the keys whose posted value differs from
    what the request holds now — an untouched field is never sent, so the round's change
    list shows exactly what SCM changed.

    Nothing at all unless the page drew the editable fields (hidden revise=1): a POST
    from a page opened before they existed must never read as "every field blanked".

    Scope is compared with the LIVE scope (current_scope_pks), which is all the pickers
    can show. When SCM does change a scope list, the posted list replaces that whole set,
    so a since-deleted site in it is dropped; the chokepoint refuses a deleted one
    anyway. Design sign-off is read-only on the page and never sent. The material lines
    are not part of the revision: the view reads them with parse_lines and sends them as
    resubmit_approval_request(lines=...), as one set.

    A pre-dispatch request's vendor is the PO/PI record's (Approvals 3a): the page draws
    it read-only, and a posted `vendor` is ignored here, never sent.
    """
    if post.get('revise') != '1':
        return {}
    revision = {}
    for key in ('title', 'description'):
        if key in post and _text(post[key]) != _text(getattr(approval, key)):
            revision[key] = post[key]
    if 'vendor' in post and approval.kind != APPROVAL_KIND_MATERIAL_PRE_DISPATCH:
        vendor = _vendor(post, errors, current=approval.vendor)
        if (vendor.pk if vendor else None) != approval.vendor_id:
            revision['vendor'] = vendor

    picked = dict(zip(('programs', 'projects', 'site_groups'), parse_scope(post, errors)))
    current = current_scope_pks(approval)
    for key, rows in picked.items():
        if {row.pk for row in rows} != current[key]:
            revision[key] = rows
    return revision


def keepable_steps(latest):
    """{party: step} for the parties whose approval may be kept (D-A20). `latest` is the
    last row per party of the round that asked for changes — the chokepoint's own rule.
    Only an APPROVED step qualifies: a party that asked for changes, or whose step was
    superseded when the round closed, is offered no keep."""
    return {party: step for party, step in latest.items()
            if step.verdict == APPROVAL_STEP_APPROVED}


def parse_carry(post, keepable, overrides, errors, tasks_changed=False):
    """The ticked "Keep <name>'s approval" boxes. Returns {party: reason}, the `carry`
    argument of resubmit_approval_request().

    FORM RULES, refused here with everything typed kept on the page:
      * a reason of at least KEEP_REASON_MIN non-space characters — the chokepoint only
        refuses an empty one;
      * no keep for a party whose approver this resubmit also changes (`overrides`) —
        the chokepoint refuses it too, but in words about the ledger, not the form;
      * `tasks_changed` (a bill whose ticked tasks differ from its own, 4b-1, B): no keep
        of the Site Engineer's confirmation — the chokepoint refuses it too.
    Whether an approval can be kept at all is re-decided by the chokepoint under its lock.
    """
    carry = {}
    for party, label in _PARTY_LABELS.items():
        if post.get(f'keep_{party}') != 'on':
            continue
        step = keepable.get(party)
        if step is None:
            errors.append(f'The {label} step was not approved, so there is no approval '
                          f'to keep.')
            continue
        name = person_name(step.decided_by)
        reason = (post.get(f'keep_reason_{party}') or '').strip()
        refused = False
        if party in overrides:
            errors.append(f'{name}\'s approval cannot be kept while you change the {label} '
                          f'approver. Untick "Keep", or leave the approver as before.')
            refused = True
        if tasks_changed and party == APPROVAL_PARTY_SITE_ENGINEER:
            errors.append(f'{name}\'s confirmation cannot be kept: the tasks on this bill '
                          f'changed, so the Site Engineer must confirm the work again. '
                          f'Untick "Keep".')
            refused = True
        if len(re.sub(r'\s', '', reason)) < KEEP_REASON_MIN:
            errors.append(f'Say why {name}\'s approval is being kept — at least '
                          f'{KEEP_REASON_MIN} characters, not counting spaces.')
            refused = True
        if not refused:
            carry[party] = reason
    return carry


# ---------------------------------------------------------------------------
# Contractor bills (4a-2)
# ---------------------------------------------------------------------------

def bill_sites():
    """The sites a contractor bill may be raised against: live, not Draft, not test data.

    Draft is left out because approvals._clean_bill refuses it (no work can have been done
    on a site never activated); test data by ruling 8 (4a-2). The raise page's step 1 lists
    exactly these, and the same queryset judges a ?project= in the URL, so no address
    reaches a site the list does not offer."""
    return Project.objects.filter(is_deleted=False, is_test=False).exclude(status='Draft')


def bill_site_choices():
    """Step 1's site list, by project id. One query."""
    return list(bill_sites().only('pk', 'project_id', 'customer_name', 'project_type', 'status')
                .order_by('project_id'))


def bill_site(raw):
    """The site `raw` (a ?project= value) names, if it is one bill_sites() offers; else
    None. One query, none for junk."""
    raw = (raw or '').strip()
    if not raw.isdigit():
        return None
    return bill_sites().filter(pk=int(raw)).first()


def contractor_choices():
    """Active vendors recorded as a contractor or both (D-A14, D-A38), by name. The
    chokepoint refuses any other vendor; this only keeps suppliers out of the list."""
    return list(Vendor.objects.filter(is_active=True, kind__in=sorted(VENDOR_BILLABLE_KINDS))
                .order_by('name'))


def bill_task_choices(project):
    """The tasks on `project` a bill may name, in the order the site's workspace shows them
    (phase, then task). Mirror tasks are left out: their status is derived from another
    record and the chokepoint refuses them. The phase is joined because the list is drawn
    under each phase's name. One query."""
    return list(Task.objects.filter(phase__project=project, is_mirror=False)
                .select_related('phase')
                .order_by('phase__phase_order', 'phase__pk', 'task_order', 'pk'))


def _bill_vendor(post, errors):
    """The posted contractor, or None when none is chosen. ANY vendor that still exists is
    returned — whether it may bill (a contractor, active) is approvals._clean_bill's rule,
    asked by the view before any upload, so its words are the ones SCM reads."""
    raw = (post.get('vendor') or '').strip()
    if not raw:
        errors.append('Choose the contractor this bill is from.')
        return None
    vendor = Vendor.objects.filter(pk=int(raw)).first() if raw.isdigit() else None
    if vendor is None:
        errors.append('The contractor you chose no longer exists. Choose again.')
    return vendor


def _bill_tasks(post, errors):
    """The ticked tasks, in the order posted, re-read. Whether each is on the site and not
    a mirror is approvals._clean_bill's rule; one that no longer exists is an error here,
    never dropped — a bill that quietly names less than SCM ticked says something untrue."""
    wanted = _pks(post, 'task')
    rows = Task.objects.in_bulk(wanted) if wanted else {}
    if len(rows) != len(wanted):
        errors.append('A task you chose no longer exists. Choose again.')
    return [rows[pk] for pk in wanted if pk in rows]


def parse_bill_create(request):
    """The contractor bill raise page's POST. Returns (cleaned, errors).

    `cleaned`: title, description, vendor, pm_assignee, site_engineer_assignee, tasks,
    amount, bill_number and bill_date (as typed — the chokepoint parses them), `pdf` (the
    uploaded file, not stored), `photos` (validated, not stored) and `client_uuid`. The
    site is not here: it is the page's ?project=, read by the view.

    Errors here are only what must be settled before a file reaches storage: the bill
    PDF (present, and a real PDF by bill_storage.validate_bill_pdf — its bytes, not its
    name), the photos (jpg/jpeg/png, 20 MB, at most ATTACHMENT_LIMIT), and choices that
    no longer exist. Touches no storage."""
    post, errors = request.POST, []
    vendor = _bill_vendor(post, errors)
    tasks = _bill_tasks(post, errors)
    pdf = request.FILES.get('bill_pdf')
    if pdf is None:
        errors.append('Attach the contractor\'s bill as a PDF.')
    else:
        try:
            validate_bill_pdf(pdf)
        except BillStorageError as exc:
            errors.append(str(exc))
    photos = _validated_files(request, 'attachments', errors, BILL_PHOTO_EXTENSIONS)
    cleaned = {
        'title':                  post.get('title', ''),
        'description':            post.get('description', ''),
        'vendor':                 vendor,
        'pm_assignee':            _profile(post, 'pm_assignee'),
        'site_engineer_assignee': _profile(post, 'site_engineer_assignee'),
        'tasks':                  tasks,
        'amount':                 post.get('amount', ''),
        'bill_number':            post.get('bill_number', ''),
        'bill_date':              post.get('bill_date', ''),
        'pdf':                    pdf,
        'photos':                 photos,
        'client_uuid':            parse_client_uuid(post),
    }
    return cleaned, errors


def parse_bill_revision(request, approval, task_pks, errors):
    """A contractor bill's resubmit POST (4b-1). Returns (revision, pdf, tasks_changed):
    the `revision` for resubmit_approval_request() without its `pdf` (the view stores the
    file first), the replacement PDF as uploaded (not stored) or None, and whether the
    ticked tasks differ from `task_pks` — the bill's own, in its order.

    Nothing at all unless the page drew the bill's fields (hidden revise=1), as
    parse_revision. The title and description are sent only when changed; the amount,
    number, date and ticked tasks always — the chokepoint writes only the columns whose
    value changes, and judges all of them as create does.

    THE CONTRACTOR AND SITE ARE NOT DRAWN, but a posted one is passed on when it differs,
    so SCM reads the chokepoint's refusal (BILL_CONTRACTOR_LOCKED, BILL_SITE_LOCKED); one
    that repeats what the bill holds is dropped. A posted contractor that is blank or no
    longer exists cannot be passed on as a vendor, so it is refused here in the
    chokepoint's own words.

    Errors here are only what must be settled before a file reaches storage: the PDF's
    bytes (bill_storage.validate_bill_pdf) and ticked tasks that no longer exist."""
    post = request.POST
    if post.get('revise') != '1':
        return {}, None, False
    revision = {}
    for key in ('title', 'description'):
        if key in post and _text(post[key]) != _text(getattr(approval, key)):
            revision[key] = post[key]
    raw_vendor = (post.get('vendor') or '').strip() if 'vendor' in post else None
    if raw_vendor is not None and raw_vendor != str(approval.vendor_id):
        vendor = (Vendor.objects.filter(pk=int(raw_vendor)).first()
                  if raw_vendor.isdigit() else None)
        if vendor is None:
            errors.append(BILL_CONTRACTOR_LOCKED)
        else:
            revision['vendor'] = vendor     # another contractor: the chokepoint refuses it
    if any(name in post for name in ('program', 'project', 'site_group')):
        picked = dict(zip(('programs', 'projects', 'site_groups'),
                          parse_scope(post, errors)))
        for name, key in (('program', 'programs'), ('project', 'projects'),
                          ('site_group', 'site_groups')):
            if name in post:
                revision[key] = picked[key]
    revision['amount'] = post.get('amount', '')
    revision['bill_number'] = post.get('bill_number', '')
    revision['bill_date'] = post.get('bill_date', '')
    tasks = _bill_tasks(post, errors)
    revision['tasks'] = tasks
    pdf = request.FILES.get('bill_pdf')
    if pdf is not None:
        try:
            validate_bill_pdf(pdf)
        except BillStorageError as exc:
            errors.append(str(exc))
    return revision, pdf, {task.pk for task in tasks} != set(task_pks)


def parse_bill_photos(request, errors):
    """A bill resubmit's new photos, under `attachments`: jpg/jpeg/png only, as on the
    raise page. Touches no storage."""
    return _validated_files(request, 'attachments', errors, BILL_PHOTO_EXTENSIONS)


def parse_site_photos(request, errors):
    """A Site Engineer's site photos, under `site_photos` (4a-3, D-A53): jpg/jpeg/png
    only, 20 MB each, at most ATTACHMENT_LIMIT. Whether a confirmation has at least one is
    the chokepoint's rule (approvals._site_engineer_refusal), not this function's. Touches
    no storage."""
    return _validated_files(request, 'site_photos', errors, BILL_PHOTO_EXTENSIONS)
