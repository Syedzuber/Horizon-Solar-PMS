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
"""
import uuid as _uuid

from django.db.models import Q

from .models import (
    Program, Project, SiteGroup, UserProfile, Vendor,
    APPROVAL_PARTY_DESIGN, GROUP_TYPE_PROCUREMENT,
)
from .permissions import profile_can_be_approval_assignee, user_has_design_head_authority
from .views import _validate_upload_file

#: Files one submission may carry — the vendor-order raise's DOC_SLOTS number. The page
#: may be used again (a resubmit adds more), so this caps a request, not a round.
ATTACHMENT_LIMIT = 5


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


def parse_attachments(request, errors):
    """The files posted under `attachments`, each validated the way every upload is
    (views._validate_upload_file: type, 20 MB, MIME). Touches no storage."""
    files = request.FILES.getlist('attachments')
    if len(files) > ATTACHMENT_LIMIT:
        errors.append(f'Attach at most {ATTACHMENT_LIMIT} files at a time.')
    for upload in files:
        try:
            _validate_upload_file(upload)
        except ValueError as exc:
            errors.append(f'{upload.name}: {exc}.')
    return files


def parse_create(request):
    """The raise page's POST. Returns (cleaned, errors).

    `cleaned` holds create_approval_request()'s keyword arguments, less kind, raised_by
    and attachments, plus `files` (the validated uploads) and `client_uuid`. Missing
    people, a missing title and the like are NOT errors here: the chokepoint refuses
    them, in its own words.
    """
    post, errors = request.POST, []

    vendor = None
    vendor_raw = (post.get('vendor') or '').strip()
    if vendor_raw:
        vendor = (Vendor.objects.filter(pk=int(vendor_raw), is_active=True).first()
                  if vendor_raw.isdigit() else None)
        if vendor is None:
            errors.append('The vendor you chose is no longer available. Choose again.')

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
