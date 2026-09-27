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
"""
import re
import uuid as _uuid

from django.db.models import Q

from .models import (
    Program, Project, SiteGroup, UserProfile, Vendor,
    APPROVAL_PARTY_CHOICES, APPROVAL_PARTY_DESIGN, APPROVAL_STEP_APPROVED,
    GROUP_TYPE_PROCUREMENT,
)
from .permissions import profile_can_be_approval_assignee, user_has_design_head_authority
from .views import _validate_upload_file

#: Files one submission may carry — the vendor-order raise's DOC_SLOTS number. The page
#: may be used again (a resubmit adds more), so this caps a request, not a round.
ATTACHMENT_LIMIT = 5

#: What a proxy decision's evidence may be: a PDF, or a photo or screenshot (a WhatsApp
#: screenshot is PNG or JPEG). Not HEIC or WebP — views._validate_upload_file's MIME map
#: has neither (SECONDARY_FINDINGS, Approvals 2a-2).
EVIDENCE_EXTENSIONS = ['pdf', 'jpg', 'jpeg', 'png']

#: The shortest reason, in non-space characters, for keeping an earlier approval (D-A20).
KEEP_REASON_MIN = 15

_PARTY_LABELS = dict(APPROVAL_PARTY_CHOICES)


def person_name(profile):
    return profile.user.get_full_name() or profile.user.username


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


def parse_create(request):
    """The raise page's POST. Returns (cleaned, errors).

    `cleaned` holds create_approval_request()'s keyword arguments, less kind, raised_by
    and attachments, plus `files` (the validated uploads) and `client_uuid`. Missing
    people, a missing title and the like are NOT errors here: the chokepoint refuses
    them, in its own words.
    """
    post, errors = request.POST, []

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
        'material': {
            'proposed_make': post.get('proposed_make', ''),
            'specification': post.get('specification', ''),
            'quantity_note': post.get('quantity_note', ''),
        },
        'programs':    programs,
        'projects':    projects,
        'site_groups': site_groups,
        'files':       files,
        'client_uuid': parse_client_uuid(post),
    }
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


def parse_revision(post, approval, material, errors):
    """The resubmit page's edited details. Returns the `revision` dict for
    resubmit_approval_request() holding ONLY the keys whose posted value differs from
    what the request holds now — an untouched field is never sent, so the round's change
    list shows exactly what SCM changed.

    Nothing at all unless the page drew the editable fields (hidden revise=1): a POST
    from a page opened before they existed must never read as "every field blanked".

    Scope is compared with the LIVE scope (current_scope_pks), which is all the pickers
    can show. When SCM does change a scope list, the posted list replaces that whole set,
    so a since-deleted site in it is dropped; the chokepoint refuses a deleted one
    anyway. BOQ items and design sign-off are read-only on the page and never sent.
    """
    if post.get('revise') != '1':
        return {}
    revision = {}
    for key in ('title', 'description'):
        if key in post and _text(post[key]) != _text(getattr(approval, key)):
            revision[key] = post[key]
    if 'vendor' in post:
        vendor = _vendor(post, errors, current=approval.vendor)
        if (vendor.pk if vendor else None) != approval.vendor_id:
            revision['vendor'] = vendor

    picked = dict(zip(('programs', 'projects', 'site_groups'), parse_scope(post, errors)))
    current = current_scope_pks(approval)
    for key, rows in picked.items():
        if {row.pk for row in rows} != current[key]:
            revision[key] = rows

    if material is not None:
        for key in ('proposed_make', 'specification', 'quantity_note'):
            if key in post and _text(post[key]) != _text(getattr(material, key)):
                revision[key] = post[key]
    return revision


def keepable_steps(latest):
    """{party: step} for the parties whose approval may be kept (D-A20). `latest` is the
    last row per party of the round that asked for changes — the chokepoint's own rule.
    Only an APPROVED step qualifies: a party that asked for changes, or whose step was
    superseded when the round closed, is offered no keep."""
    return {party: step for party, step in latest.items()
            if step.verdict == APPROVAL_STEP_APPROVED}


def parse_carry(post, keepable, overrides, errors):
    """The ticked "Keep <name>'s approval" boxes. Returns {party: reason}, the `carry`
    argument of resubmit_approval_request().

    FORM RULES, refused here with everything typed kept on the page:
      * a reason of at least KEEP_REASON_MIN non-space characters — the chokepoint only
        refuses an empty one;
      * no keep for a party whose approver this resubmit also changes (`overrides`) —
        the chokepoint refuses it too, but in words about the ledger, not the form.
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
        if len(re.sub(r'\s', '', reason)) < KEEP_REASON_MIN:
            errors.append(f'Say why {name}\'s approval is being kept — at least '
                          f'{KEEP_REASON_MIN} characters, not counting spaces.')
            refused = True
        if not refused:
            carry[party] = reason
    return carry
