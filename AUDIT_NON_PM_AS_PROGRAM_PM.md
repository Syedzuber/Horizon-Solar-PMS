# AUDIT — Assigning a user whose `UserProfile.role` is not `'PM'` as the PM of a tender

**Mode:** AUDIT-ONLY. Read-only. No fix, no migration, no refactor was made.
**Branch / HEAD:** `main` @ `85b3404`
**Local DB:** `solarpms_local` @ `localhost` — 4 Programs, 148 Projects. Every ORM claim below
was executed against it read-only; nothing in this audit wrote to any database.
**Subject:** `praveen` / `praveen@horizonrenewablepower.com` — `UserProfile` pk=17,
`role='Design'`, `is_design_head=True`, `is_design_qc=False`, `is_active=True`.

## Premise correction carried forward from pre-flight

`projects.Program` has **no** `assigned_pm` field. Program authority is DERIVED UPWARD from
child sites. Q1 and Q3 were re-aimed accordingly (Q1-R, Q3-R, Q3b-R, Q3c-R); Q2 and Q4 stand
as originally written; Q5–Q8 read "the Program's PM" as "the `assigned_pm` of the site(s)
under the Program".

```
$ venv/Scripts/python.exe -c "... print([f.name for f in Program._meta.get_fields()])"
Program fields: ['sites', 'site_groups', 'id', 'program_type', 'name', 'client_name',
 'status', 'short_tender_code', 'total_capacity', 'expected_completion_date',
 'planned_site_count', 'tender_reference_number', 'bid_value', 'award_date',
 'ppa_reference', 'ppa_signed_date', 'ppa_per_unit_rate', 'ppa_escalation_percentage',
 'ppa_escalation_frequency', 'financing_partner_name', 'financing_assistance_type',
 'created_at', 'updated_at', 'created_by', 'is_deleted', 'deleted_at']
```

---

# Q1-R — Where does "a tender cannot be created without a PM" actually live?

## Plain answer

**(c) NOT PRESENT AT ALL.** There is no PM field on `Program`, no PM selector on the
Program-creation form, no PM selector on the Program-creation view, and the flow does not
create, require, or default a child `Project`. A Program can be created with no PM anywhere in
its subtree, and the local DB contains exactly such a Program today (`Mehtab Ahmed`, 0 sites).

The believed constraint does not exist. It is not even UI convention — there is no control on
the screen to observe the convention with.

## Evidence — the form (`ProgramForm` in `projects/forms.py`)

`Meta.fields` in full. No PM field, no user field of any kind:

```python
    class Meta:
        model = Program
        fields = [
            'program_type', 'name', 'client_name', 'status',
            'short_tender_code', 'total_capacity', 'expected_completion_date',
            'planned_site_count',
            # OPEX-specific
            'tender_reference_number', 'bid_value', 'award_date',
            'ppa_reference', 'ppa_signed_date', 'ppa_per_unit_rate',
            'ppa_escalation_percentage', 'ppa_escalation_frequency',
            # CAPEX-specific placeholders
            'financing_partner_name', 'financing_assistance_type',
        ]
```

`ProgramForm.clean()` validates only `short_tender_code` (required for OPEX, reserved-code
guard, soft-delete-aware global uniqueness) and `tender_reference_number` uniqueness. It makes
no assertion about any user.

## Evidence — the view (`program_create` in `projects/views.py`)

```python
@login_required
@role_required(['Admin', 'PM'])
def program_create(request):
    """Create a Program (OPEX tender or multi-site CAPEX contract). Admin/PM only."""
    if request.method == 'POST':
        form = ProgramForm(request.POST)
        if form.is_valid():
            program = form.save(commit=False)
            program.created_by = request.user
            program.save()
            log_activity(
                None, getattr(request.user, 'profile', None),
                f"Created {program.program_type} Program: {program.name}",
                entity_type='Program', entity_id=program.pk, action_code='program_created',
            )
            messages.success(request, f"Program '{program.name}' created successfully.")
            return redirect('program_detail', pk=program.pk)
    else:
        form = ProgramForm()
    return render(request, 'projects/program_form.html', {'form': form, 'action': 'Create'})
```

The only user written is `created_by` (an `auth.User`, not a `UserProfile`, and never read as
an authority except by the `_can_access_program` creator fallback — see Q3b-R). No `Project`
is created. Site creation is an entirely separate, later request (`opex_site_create` /
`opex_site_bulk_upload`).

## Evidence — a real site-less Program exists

```
=== Q3b: Programs and their non-deleted site counts ===
  pk=26 name='SCMPILOT SCM Pilot Walkthrough' type=OPEX  sites=6  created_by=122 deleted=False
  pk=3  name='Mehtab Ahmed'                   type=CAPEX sites=0  created_by=16  deleted=False
  pk=2  name='MPUVNL'                         type=OPEX  sites=86 created_by=15  deleted=False
  pk=1  name='HRP-Test'                       type=OPEX  sites=10 created_by=10  deleted=False
```

## Where the belief probably comes from

**INFERRED:** the PM-shaped feeling of tender creation comes from `@role_required(['Admin',
'PM'])` on `program_create` — *a PM creates the tender* — not from any field. The creator's
identity lands in `created_by`, and `_can_access_program` reads it as an access fallback. That
is the only sense in which a tender "has" a PM at creation, and it is not an assignment.

---

# Q2 — What populates the PM choices

## Program-creation PM selector

**There is none.** See Q1-R. There is no queryset to show.

## `Project.assigned_pm` — every populating path

There are six paths that write `Project.assigned_pm`. Five filter on `role='PM'`; one does not.

### 1. `project_create` (`projects/views.py`) — Residential, self-assignment

No selector at all; the creator is the PM by construction, and the decorator is the filter.

```python
@login_required
@role_required(['PM'])
def project_create(request):
    ...
                project.assigned_pm = request.user.profile
```

Filters on: **role only** (via `@role_required(['PM'])`). Not on `is_active`.

### 2. `create_opex_site` (`projects/views.py`) — the OPEX site core, single-add AND bulk

```python
        # A PM creator owns the site; an Admin creator leaves it unassigned
        # (assign later via admin_assign_pm) — assigned_pm requires a PM profile.
        site.assigned_pm = profile if (profile and profile.role == 'PM') else None
```

Filters on: **role only**, as an explicit string comparison. A `role='Design'` creator does
not raise — the site is silently created with `assigned_pm=None`.

### 3. `admin_assign_pm` (`projects/views.py`) — Admin panel, assign/reassign

Dropdown queryset (from `admin_project_list`, which renders the form):

```python
    pm_users = (
        UserProfile.objects
        .filter(role='PM', is_active=True)
        .select_related('user')
        .order_by('user__first_name')
    )
```

Re-validated on POST — the dropdown is not trusted:

```python
    try:
        pm_profile = UserProfile.objects.select_related('user').get(pk=pm_user_id, role='PM', is_active=True)
    except UserProfile.DoesNotExist:
        messages.error(request, 'Selected user is not a valid active PM.')
        return redirect('admin_project_list')
```

Filters on: **both** `role` and `is_active`, at render and again at write.

### 4. `subadmin_projects` (`projects/views.py`) — System Admin, first-time assignment only

```python
        pm_profile = get_object_or_404(UserProfile, pk=pm_prof_pk, role='PM', is_active=True)
        project.assigned_pm = pm_profile
```
```python
    pm_profiles = (
        UserProfile.objects
        .filter(role='PM', is_active=True)
        .select_related('user')
        .order_by('user__first_name')
    )
```

Filters on: **both**. Also refuses reassignment (`if project.assigned_pm_id: ... error`).

### 5. Django admin — `ProjectAdmin` (`projects/admin.py`)

`assigned_pm` is in the editable fieldset and is NOT in `readonly_fields`:

```python
    readonly_fields = ['project_id', 'status', 'created_at', 'activated_at', 'deleted_at']
    fieldsets = (
        ('Project Info', {
            'fields': ('project_id', 'project_type', 'status', 'assigned_pm', 'created_by')
        }),
```

The dropdown is constrained by the model field itself (`Project` in `projects/models.py`):

```python
    assigned_pm               = models.ForeignKey(
        'UserProfile',
        limit_choices_to={'role': 'PM'},
        related_name='pm_projects',
        on_delete=models.PROTECT,  # Prevent accidental deletion of a PM who owns projects
        null=True,
        blank=True,
    )
```

Filters on: **role only** (`limit_choices_to`). Not on `is_active`.

### 6. The Zoho webhook (`projects/views.py`) — THE ONE PATH WITH NO ROLE FILTER

```python
    pm_email = (deal.get('Assign_PM', '') or '').strip()
    assigned_pm = None
    if pm_email:
        profile_match = UserProfile.objects.filter(user__email__iexact=pm_email).first()
        if profile_match:
            assigned_pm = profile_match
```

Filters on: **neither** `role` nor `is_active`. Email match only. The codebase already knows
this — `user_can_view_project`'s docstring in `projects/permissions.py` says so verbatim:

> a user who ends up as `assigned_pm` despite not holding the PM role (reachable via the Zoho
> webhook, which matches on email with no role filter) can still see their own project.

## Would a `role='Design'` user appear in those dropdowns today?

**No — in every dropdown that exists.** Proven:

```
=== Q2: would a Design-role profile appear in the PM dropdown querysets? ===
  admin_project_list/subadmin pm queryset role='PM',is_active=True -> count: 7
  design head in that queryset? False
  coordinators queryset count: 5 | design head in it? False
```

The single expression that excludes them, and it is the same one in all five filtered paths:

> **`role='PM'`** — as `.filter(role='PM', is_active=True)` in `admin_project_list` /
> `subadmin_projects`, as `.get(pk=..., role='PM', is_active=True)` on their POST paths, as
> `limit_choices_to={'role': 'PM'}` on the model field for the Django admin, and as
> `profile.role == 'PM'` in `create_opex_site`.

The one exception is the Zoho webhook, which has no dropdown at all and no role filter — but it
only ever creates `project_type='Residential'` projects and is not reachable for a tender site.

## `Project.coordinators`

Field definition (`projects/models.py`):

```python
    coordinators              = models.ManyToManyField(
        'UserProfile',
        related_name='coordinated_projects',
        blank=True,
        limit_choices_to={'role': 'Project Coordinator'},
    )
```

The only write path is `assign_coordinators` (`projects/views.py`):

```python
    candidates = (
        UserProfile.objects
        .filter(role='Project Coordinator', is_active=True)
        .select_related('user')
        .order_by('user__first_name')
    )
    ...
        selected_ids = {int(v) for v in request.POST.getlist('coordinator_ids') if v.isdigit()}
        valid_ids    = set(candidates.values_list('pk', flat=True))
        desired      = selected_ids & valid_ids   # ignore anything not an active PC candidate
```

Filters on: **both** `role` and `is_active`, and the POST is intersected with the candidate set
rather than trusted. A `role='Design'` user cannot be made a coordinator either — the excluding
expression is `role='Project Coordinator'`.

**Note for the decision this feeds:** the coordinator route is NOT a back door. It is filtered
exactly as tightly as the PM route.

---

# Q3-R — `Project.assigned_pm`: how it is set, and whether it can be changed

## Field definition

```python
    assigned_pm               = models.ForeignKey(
        'UserProfile',
        limit_choices_to={'role': 'PM'},
        related_name='pm_projects',
        on_delete=models.PROTECT,  # Prevent accidental deletion of a PM who owns projects
        null=True,
        blank=True,
    )
```

FK target `projects.UserProfile`; `null=True`; `blank=True`; `related_name='pm_projects'`;
`on_delete=models.PROTECT`; `limit_choices_to={'role': 'PM'}`.

`limit_choices_to` constrains **form and admin choices only**. It is not a database constraint
and not a `save()` validation — a direct ORM assignment of any `UserProfile` succeeds. That is
exactly how the Zoho webhook can seat a non-PM in the field.

## How `assigned_pm` is populated when a site is created under a Program

Not chosen, not defaulted from the Program, not copied from a sibling site. It is set to **the
creator's own profile, and only if that creator holds `role='PM'`** — otherwise `None`.

`create_opex_site` (`projects/views.py`), the request-independent core:

```python
    with transaction.atomic():
        site = form.save(commit=False)
        site.program = program
        site.project_type = 'OPEX'          # forced — never user-selectable here
        site.customer_name = program.client_name
        site.status = 'Draft'
        # A PM creator owns the site; an Admin creator leaves it unassigned
        # (assign later via admin_assign_pm) — assigned_pm requires a PM profile.
        site.assigned_pm = profile if (profile and profile.role == 'PM') else None
```

Its docstring states the rule for callers:

> `profile`: creator's UserProfile, used for PM auto-assignment; a non-PM (or None)
> profile leaves assigned_pm=None, matching the single-add behavior.

`OpexSiteForm.Meta.fields` confirms there is no PM widget on the site form:

```python
        fields = [
            'site_code', 'customer_contact_person', 'customer_phone', 'customer_email',
            'site_address', 'city', 'state', 'capacity_kw',
        ]
```

`opex_site_create` passes the requesting user's own profile and nothing else:

```python
        profile = getattr(request.user, 'profile', None)
        site, form = create_opex_site(program, request.POST, request.user, profile=profile)
```

## Is `assigned_pm` editable after creation? — YES

**Yes.** Four paths change it after creation. None of them is on the Program.

| # | Path | Location | Gate | Reassign allowed? |
|---|---|---|---|---|
| 1 | `admin_assign_pm` | `projects/views.py` | `@role_required(['Admin'])`, POST only | **Yes** — explicitly logs `(previously <old PM>)` |
| 2 | `subadmin_projects` | `projects/views.py` | `@system_admin_required` | **No** — first-time only, refuses if `assigned_pm_id` is set |
| 3 | Django admin `ProjectAdmin` | `projects/admin.py` | Django admin staff perms | **Yes**, dropdown limited to `role='PM'` |
| 4 | Zoho webhook | `projects/views.py` | unauthenticated webhook | Creation only, but with **no role filter** |

Evidence for (1), the reassignment path:

```python
    old_pm = project.assigned_pm
    project.assigned_pm = pm_profile
    project.save(update_fields=['assigned_pm'])

    log_activity(
        project=project,
        actor=request.user.profile,
        action=(
            f"PM assigned to {pm_profile.user.get_full_name() or pm_profile.user.username}"
            + (f" (previously {old_pm.user.get_full_name() or old_pm.user.username})" if old_pm else "")
        ),
        entity_type='Project',
        entity_id=project.pk,
        action_code='pm_assigned',
    )
```

What CANNOT change it, verified: `ProjectEditForm` (`Meta.fields` has no `assigned_pm`),
`PostActivationFieldEditForm` (three fields only), `assign_coordinators` (M2M `.add()`/
`.remove()` only, and its docstring calls this out as deliberate), and no management command
except the seed scripts (`seed_opex_test_data`, `seed_scm_pilot`), which set it on data they
create themselves.

The full write census — every `assigned_pm` assignment or `update_fields` in application code:

```
$ grep -rn "assigned_pm" --include=*.py . | grep -v "/venv/" | grep -v tests_
./projects/views.py:2702:                project.assigned_pm = request.user.profile     # project_create
./projects/views.py:3466:        site.assigned_pm = profile if (profile and profile.role == 'PM') else None
./projects/views.py:8886:                assigned_pm=assigned_pm,                       # Zoho webhook create
./projects/views.py:11760:    project.assigned_pm = pm_profile                        # admin_assign_pm
./projects/views.py:11761:    project.save(update_fields=['assigned_pm'])
./projects/views.py:12637:        project.assigned_pm = pm_profile                    # subadmin_projects
./projects/views.py:12638:        project.save(update_fields=['assigned_pm'])
./projects/management/commands/seed_opex_test_data.py:465:        project.assigned_pm = pm
```

## Is there any bulk path that sets `assigned_pm` across all sites of a Program at once?

**No. There is none.**

```
$ grep -rn "program.*assigned_pm\|assigned_pm.*program" --include=*.py projects/ | grep -v tests_
(no output)
```

`opex_site_bulk_upload` is a bulk *site-creation* path, not a bulk PM-assignment path — it
reuses `create_opex_site` per row, so every row gets the same creator-derived rule.
`admin_assign_pm` and `subadmin_projects` are strictly one project per POST.

**This does not matter for Dehradun (one site). It matters at forty**, and it matters the day
the Dehradun PM changes: reassigning a tender's PM is N separate Admin-panel POSTs, one per
site, with no transaction spanning them and no screen that shows you missed one.

---

# Q3b-R — The site-less Program window

## Confirmation of the documented behaviour

`user_can_manage_program` (`projects/permissions.py`), in full:

```python
def user_can_manage_program(user, program):
    """Return True if `user` has PM-level management authority over `program`.

    A Program has no assigned_pm / coordinators of its own — its authority is DERIVED
    from its child sites: a user manages the Program if they manage ANY non-deleted
    child site, decided through the one canonical `user_can_manage_project()` path
    (never by re-comparing assigned_pm / coordinators / role strings here). This keeps
    Program access on the exact same comparison rule as project access.

    NOTE (deliberate): this returns False for a Program with no sites the user manages
    (e.g. a brand-new empty Program). Admin / CEO reach Program views through the
    view-layer role gate, not this helper — same split as user_can_manage_project,
    which only decides PM-level authority. The `getattr` guard mirrors the project
    helper (a superuser without a profile is not a manager)."""
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    for site in program.sites.filter(is_deleted=False):
        if user_can_manage_project(user, site):
            return True
    return False
```

Confirmed by ORM against the real site-less Program (`Mehtab Ahmed`, pk=3, 0 sites):

```
=== Q3b: user_can_manage_program on a site-less Program ===
  program 'Mehtab Ahmed': user_can_manage_program(nirankar) = False
  program 'Mehtab Ahmed': user_can_manage_program(mehtab) = False
  program 'Mehtab Ahmed': user_can_manage_program(anand) = False
  program 'Mehtab Ahmed': user_can_manage_program(designhead praveen) = False
```

## Who can see or act on a Program before its first site exists

`created_by` **does** grant something, and this is the gap-filler. `_can_access_program`
(`projects/views.py`) is the view-layer gate every Program screen uses:

```python
def _can_access_program(request, program):
    """View-layer access gate for a SPECIFIC Program (mirrors _pm_owns_project's role
    for projects). Admin / CEO reach every Program (role_required already limits who
    gets here). A PM reaches a Program they manage — canonical user_can_manage_program
    (authority over any child site) — OR one they created (covers a brand-new empty
    Program whose creator has no site to derive authority from yet). The created_by
    fallback is a creator check, NOT a re-implementation of PM/coordinator comparison."""
    role = _get_user_role(request)
    if role in ('Admin', 'CEO'):
        return True
    if user_can_manage_program(request.user, program):
        return True
    return program.created_by_id == request.user.id
```

`program_list` applies the same helper per row for a PM:

```python
    role = _get_user_role(request)
    if role == 'PM':
        programs = [p for p in programs if _can_access_program(request, p)]
    else:
        programs = list(programs)
```

Every Program screen (`program_list`, `program_detail`, `program_edit`, `opex_site_create`,
`opex_site_bulk_upload`, `opex_site_bulk_template`) uses `_can_access_program`, so the
`created_by` fallback covers all of them uniformly.

## Plain answer

**For the creator: no, there is no window.** `_can_access_program`'s `created_by` fallback is
placed there precisely to close it, and the docstring says so.

**But there is a window for everybody else, and it is total.** Between `program_create` and the
first site, the Program is reachable by exactly three sets of people: the creator, Admin, and
CEO. No other PM can see it, because there is no site to derive authority from. If the creator
leaves, is deactivated, or simply hands over, the tender becomes Admin/CEO-only until somebody
creates a site under it.

**For Dehradun's intended PM specifically, the answer is worse and comes from a different
mechanism:** `program_create` is `@role_required(['Admin', 'PM'])` and `program_list` /
`program_detail` are `@role_required(['Admin', 'PM', 'CEO'])`. A `role='Design'` user is refused
by the decorator before `_can_access_program` is ever consulted, so they cannot create the
tender, cannot be its `created_by`, and cannot open it afterwards. Proven in Q7.

---

# Q3c-R — Does anything assume all sites under a Program share one PM

## Plain answer

**No. There are no such hits.** Nothing reads a Program's PM as a singular value, takes the
first site's `assigned_pm`, or reduces `program.sites.values('assigned_pm')` to one value.

## Evidence — the searches

```
$ grep -rn "program.*assigned_pm\|assigned_pm.*program" --include=*.py projects/ | grep -v tests_
(no output)
```

```
$ grep -rn "sites.first()\|\.sites\.\(all\|filter\)" --include=*.py projects/ | grep -v tests_
projects/design_metrics.py:274:    total_sites = program.sites.filter(is_deleted=False).count()
projects/design_views.py:974:    sites = (program.sites.filter(is_deleted=False)
projects/design_views.py:5230:    total = program.sites.filter(is_deleted=False).count()
projects/forms.py:797:        if self.program.sites.filter(site_code=code).exists():
projects/models.py:304:        program.sites.filter(is_deleted=False)
projects/permissions.py:416:    for site in program.sites.filter(is_deleted=False):
projects/views.py:3324:        program.sites.filter(is_deleted=False)
projects/views.py:3402:    active_sites = program.sites.filter(is_deleted=False).count()
```

Every one of the eight is a count, an existence check, a full-list iteration, or a per-site list
render. Not one of them collapses the site set to a single PM:

- `design_metrics` / `design_views` (x2) / `views.program_delete` — `.count()` only.
- `forms.OpexSiteForm.clean` — `site_code` uniqueness `.exists()`.
- `models.get_program_rollup` — groups by `status`, not by PM.
- `permissions.user_can_manage_program` — iterates ALL sites and OR-reduces
  `user_can_manage_project` per site. This is the one place that would break under mixed PMs,
  and it is correct: it returns True if you manage *any* site, which is the right answer for a
  tender whose sites have different PMs.
- `views.program_detail` — renders the site list with
  `.select_related('assigned_pm', 'assigned_pm__user')`, i.e. a per-site PM column. It shows
  each site's own PM, never a tender-level one.

`design_analytics.py` similarly reads PM per site, not per program:

```python
        sites.append({
            'assignment':  a,
            'project':     a.project,
            'program':     a.project.program,
            'designer':    a.assigned_to,
            'pm':          a.project.assigned_pm,
```

**Consequence for the decision:** two sites under one Program having different PMs is a shape
the code already handles correctly everywhere it is read. It is only *unassignable in bulk*
(Q3-R), never misread.

---

# Q4 — Does authority depend on the role string anywhere

## `user_can_manage_project()` in full (`projects/permissions.py`)

```python
def user_can_manage_project(user, project):
    """
    Return True if `user` has PM-level management authority on `project`.

    Authority is the UNCONDITIONAL OR of two additive sources:
        assigned PM  OR  a Project Coordinator on this project.

    This is the one canonical comparison path — every PM-ownership check routes
    through here, so adding coordinator support here gives every call site correct
    behaviour with no further edits.

    INVARIANT (additive-only): the assigned-PM check is evaluated FIRST and never
    gated on whether coordinators exist. Assigning a coordinator can only ever add
    a manager — it can never remove the PM's authority. Do not restructure this as
    "if coordinators: check coordinators else check PM" — that would silently lock
    the PM out. The OR is unconditional and lives here, not at any call site.

    `Project.assigned_pm` and `coordinators` are both to `UserProfile`, so we
    compare against `user.profile`. `getattr` guards a user with no profile
    (e.g. a superuser created via `createsuperuser`). A null `assigned_pm`
    compares False, matching the old `is not None` guards.
    """
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    if project.assigned_pm == profile:          # PM authority — always checked, never gated
        return True
    return project.coordinators.filter(pk=profile.pk).exists()  # additive coordinator authority
```

## Does it consult `UserProfile.role`?

**No — purely assignment fields.** The function reads `user.profile` (identity),
`project.assigned_pm` (FK identity comparison) and `project.coordinators` (M2M membership). The
string `role` does not appear in its body. Its queryset twin, `manageable_projects_q()`, is the
same rule and equally role-free:

```python
    return (Q(**{f'{prefix}assigned_pm': profile})
            | Q(**{f'{prefix}coordinators': profile}))
```

The module docstring makes this the codebase-wide contract:

> `user_can_manage_project()` is the SINGLE canonical place PM-level management
> authority is decided. Do NOT compare `Project.assigned_pm` (or, in a future
> change, coordinators) or role strings directly anywhere else in the codebase —
> call this function instead.

## The critical question — proven with a read-only ORM query

**YES.** A `role='Design'` user who is `assigned_pm` on a project gets `True` from
`user_can_manage_project()`, and `True` from `user_can_view_project()` as well.

Query (the assignment is in memory only; `.save()` is never called, and the DB value is re-read
afterwards to prove nothing was written):

```python
dh = UserProfile.objects.get(pk=17)   # praveen, role='Design', is_design_head=True
proj = Project.objects.filter(is_deleted=False).exclude(assigned_pm=None).first()
print("  design head can manage BEFORE:", user_can_manage_project(dh.user, proj))
proj.assigned_pm = dh          # in-memory only; no .save() anywhere in this script
print("  design head can manage AFTER in-memory assignment:", user_can_manage_project(dh.user, proj))
print("  design head can VIEW after in-memory assignment:", user_can_view_project(dh.user, proj))
print("  (nothing written — DB value still:", Project.objects.get(pk=proj.pk).assigned_pm.user.username, ")")
```

Output:

```
=== Q4: user_can_manage_project with a Design-role profile as assigned_pm (IN MEMORY, NOT SAVED) ===
  project: SCMPILOT06 real assigned_pm: demo.pm PM
  design head can manage BEFORE: False
  design head can manage AFTER in-memory assignment: True
  design head can VIEW after in-memory assignment: True
  (nothing written — DB value still: demo.pm )
```

**This is the crux of the whole audit.** The authority layer is role-blind and would accept
Praveen without complaint. Everything that breaks below breaks *around* it, at the view
decorators and the dashboard entry points — never at the authority check itself.

---

# Q5 — Task routing by role

## Plain answer

**PM tasks are resolved from `project.assigned_pm`. There is no role lookup anywhere in task
assignment.** If the site's `assigned_pm` is a `role='Design'` user, PM tasks on that site are
assigned **to them**, correctly and with no complaint. Not to nobody, and not to some other user.

**There is exactly one resolution site per template path, and they agree.**

## Resolution site 1 — `attach_opex_template` (`projects/utils.py`)

```python
        # Pre-assign the PM's own NON-MIRROR tasks. `is_mirror=False` is the whole
        # difference from the Residential filter below, and it is load-bearing: without
        # it COD and HOTO land in every per-user counter as the PM's work, on 95 sites,
        # protected only by 1.3b's exclusion rather than by being true.
        assign_tasks_to(
            Task.objects.filter(
                phase__project=project,
                assigned_role=Task.PM,
                is_mirror=False,
            ),
            project.assigned_pm,
        )
```

## Resolution site 2 — `attach_residential_template` (`projects/utils.py`)

```python
        # Pre-assign PM tasks to the named PM on this project.
        # SE-role tasks start unassigned — same as Design/SCM/Finance.
        pm_profile = project.assigned_pm

        assign_tasks_to(
            Task.objects.filter(
                phase__project=project,
                assigned_role=Task.PM,
            ),
            pm_profile,
        )
```

Both read `project.assigned_pm` directly. They differ only in the `is_mirror=False` term, which
is about mirrors, not about who the PM is. **They do not disagree.**

## The chokepoint they write through (`projects/utils.py`)

```python
def assign_tasks_to(queryset, user):
    """
    Write Task.assigned_to for a SET of tasks. Returns the number of rows updated.
    ...
    """
    return queryset.update(assigned_to=user)
```

No role validation. `assign_task_to` (the singular form, used by the interactive views) is
likewise a plain identity write.

## There is no `role='PM'` lookup in any task path

The complete census of `role == 'PM'` / `role='PM'` literals in application code (tests and
management commands excluded):

```
$ grep -rn "role='PM'\|== 'PM'" --include=*.py projects/ | grep -v tests_ | grep -v management/commands
projects/permissions.py:167:    # the endpoints' PM-only guard (`role == 'PM' and not user_can_manage_project(...)`)   [COMMENT]
projects/views.py:3291:    if role == 'PM':                                     # program_list scoping branch
projects/views.py:3466:        site.assigned_pm = profile if (profile and profile.role == 'PM') else None
projects/views.py:8244:    if request.user.profile.role == 'PM' and not user_can_manage_project(request.user, project):
projects/views.py:8696:        and role == 'PM'                                 # cascade-scheduling UI gate
projects/views.py:11729:        .filter(role='PM', is_active=True)               # admin_project_list dropdown
projects/views.py:11754:        pm_profile = ...get(pk=pm_user_id, role='PM', is_active=True)   # admin_assign_pm
projects/views.py:12636:        pm_profile = get_object_or_404(UserProfile, pk=pm_prof_pk, role='PM', is_active=True)
projects/views.py:12663:        .filter(role='PM', is_active=True)               # subadmin_projects dropdown
```

**Not one of these is in a task-assignment path.** Four are PM-selector querysets (Q2), one is
the OPEX site creator rule (Q3-R), one is a `program_list` scoping branch, one is a
`set_milestone_amounts` narrowing guard, one is a UI feature gate, one is a comment.

## ORM proof against live rows

```
=== Q5: PM-role tasks vs project.assigned_pm on OPEX sites ===
  SCMPILOT06 assigned_pm=demo.pm(PM) PM-tasks=5
      task='Net Metering Approval'         is_mirror=False assigned_to=demo.pm (PM)
      task='Post-Installation Approvals'   is_mirror=False assigned_to=demo.pm (PM)
      task='COD'                           is_mirror=True  assigned_to=None (None)
      task='CEIG Approval'                 is_mirror=False assigned_to=demo.pm (PM)
  SCMPILOT05 assigned_pm=demo.pm(PM) PM-tasks=5   [identical shape]
  SCMPILOT04 assigned_pm=demo.pm(PM) PM-tasks=5   [identical shape]
```

Every non-mirror PM task carries `assigned_to == project.assigned_pm`; every mirror carries
`assigned_to=None`. The join is by assignment field, never by role.

## The real Q5 risk is upstream, not in the routing

The routing is correct and would put the tasks on Praveen. **The tasks would never be created**,
because the site can never be activated by him — `opex_site_activate` is
`@role_required(['PM', 'Project Coordinator'])`. See Q7, breakage item 1. The failure mode the
prompt anticipated ("tasks are missing, discovered weeks later") is real, but its cause is the
activation gate, not the assignee resolution.

---

# Q6 — Everywhere else that assumes an assigned PM has `role='PM'`

## `_PROFILE_TO_TASK_ROLE` — there is exactly ONE copy, and it is correct

Reported to be duplicated. **It is not.** There is one module-level definition in
`projects/views.py`; every other hit is a read of it or a test asserting on it.

```
$ grep -rn "_PROFILE_TO_TASK_ROLE" --include=*.py . | grep -v "/venv/"
./projects/models.py:345:    # ... and _PROFILE_TO_TASK_ROLE in views.py maps only          [COMMENT]
./projects/tests_opex_template.py:396                                                        [TEST]
./projects/tests_role_mapping.py:5,57,104,118,124,127,132,139,141                            [TESTS]
./projects/views.py:478:_PROFILE_TO_TASK_ROLE = {'BD': 'BD / Sales'}                        [THE DEFINITION]
./projects/views.py:485,2667,3929,4198,4221,4612,5315,8685                                   [READS]
```

The definition, with the comment that explains why it holds only one entry:

```python
# DIFFERENCES ONLY, DELIBERATELY. Every other role's two strings are byte-identical,
# so `.get(x, x)` passes them through unchanged and an identity entry would add a
# line that proves nothing — `{'Foo': 'Foo'}` does not make 'Foo' a real profile
# role. The invariant that actually matters is enforced structurally instead, by
# `tests_role_mapping.py`: every Task.ROLE_CHOICES value, mapped backwards, must
# land on a real UserProfile.ROLE_CHOICES value. A NEW ROLE IS ADDED HERE AND
# NOWHERE ELSE (R-19).
_PROFILE_TO_TASK_ROLE = {'BD': 'BD / Sales'}

# Derived, never written as a second literal — two constants drift, a derived one
# cannot. Strict inverse: the forward map's values are unique, so this round-trips.
# Same idiom as _MILESTONE_TO_FINANCE_TASK above.
_TASK_TO_PROFILE_ROLE = {
    task_role: profile_role
    for profile_role, task_role in _PROFILE_TO_TASK_ROLE.items()
}
```

The map does not touch `'PM'` at all (`'PM'` is byte-identical in both vocabularies), and the
reverse map is derived rather than a second literal. **Agreement is structural. No finding.**

## Dashboard routing after login — chosen by ROLE STRING

`login_view` → `get_post_login_url` → `get_user_dashboard` (`projects/decorators.py`):

```python
ROLE_DASHBOARD = {
    'Admin':         '/dashboard/admin/',
    'System Admin':  '/sub-admin/projects/',
    'PM':            '/dashboard/pm/',
    # Coordinators reuse the PM dashboard (scoped to their coordinated projects).
    # Without this entry the role fell back silently to the Admin dashboard — a
    # role-inappropriate-data-exposure risk, not a cosmetic bug.
    'Project Coordinator': '/dashboard/pm/',
    'Site Engineer': '/dashboard/site-engineer/',
    'Design':        '/dashboard/design/',
    'Finance':       '/dashboard/finance/',
    'SCM':           '/dashboard/scm/',
    'CEO':           '/dashboard/ceo/',
    'BD':            '/dashboard/bd/',
}
```
```python
    try:
        role = user.profile.role
        url = ROLE_DASHBOARD.get(role, '/dashboard/admin/')
```

**Purely role string.** Praveen lands on `/dashboard/design/`, and the `home_url` context
processor puts that same URL behind the nav's Home control on every page.

## The PM dashboard's project list — sources from ASSIGNMENT, but the door is role-gated

The list itself is assignment-driven and would be correct:

```python
    # Projects this user manages: own PM projects OR projects they coordinate.
    managed_project_ids = list(
        Project.objects.filter(
            Q(assigned_pm=pm_profile) | Q(coordinators=pm_profile)
        ).values_list('id', flat=True).distinct()
    )
```

But the view he would need to reach it through is not:

```python
@login_required
@role_required(['PM', 'Project Coordinator'])
def dashboard_pm(request):
```

**This is exactly the scenario the prompt named, and it is confirmed:** Praveen holds the
authority and never sees the tender. Proven by executing the view with his real user:

```
actor: praveen role: Design is_design_head: True
  dashboard_pm     -> status 403
  program_list     -> status 403
  project_create   -> status 403
```

`dashboard_design` — where he actually lands — scopes on `assigned_design` OR holding a task,
never on `assigned_pm`:

```python
    projects_qs = (
        Project.objects.filter(
            Q(assigned_design=design_profile) |
            Q(phases__tasks__assigned_to=design_profile),
            Q(status__in=['Active', 'In Progress']) | Q(project_type='OPEX'),
            is_deleted=False,
        )
```

**INFERRED, and it is a partial mitigation worth naming:** once the site *is* activated by
somebody else, its PM tasks are assigned to Praveen, so the `phases__tasks__assigned_to` leg
would surface the site on his Design dashboard. He would see the site — as a designer's card, in
a designer's dashboard, with none of the PM stat blocks, activation control, or
project-management affordances. Before activation there are no tasks, so the site is invisible
to him entirely.

## Notification routing — goes through the ASSIGNMENT field, no role lookup

`send_notification` (`projects/notifications.py`) takes an explicit `UserProfile`; it never
resolves a recipient itself.

> `recipient must always be a UserProfile, never a raw User.`

The PM-side resolver is `project_managers` (`projects/permissions.py`):

```python
def project_managers(project):
    """
    Return the list of UserProfiles with PM-level authority on `project`:
    the assigned PM plus every active Project Coordinator, deduplicated, PM first.

    Use this everywhere a notification currently targets `project.assigned_pm`, so
    coordinators receive the same operational notifications as the PM. Returns an
    empty list if there is no PM and no coordinators.
    """
    managers = []
    seen = set()
    pm = project.assigned_pm
    if pm is not None:
        managers.append(pm)
        seen.add(pm.pk)
    for coord in project.coordinators.filter(is_active=True):
        if coord.pk not in seen:
            seen.add(coord.pk)
            managers.append(coord)
    return managers
```

Assignment field, no role term. Its call sites (`projects/views.py`): milestone recipients, BOQ
notifications, invoice recipients, three issue-notification blocks, and issue comments.

`assign_task` — `_notify_assignment` (`projects/utils.py`), reached from `assign_task_to`:
notifies the `user` object it was handed. No role lookup.

`assign_project` — two call sites (`admin_assign_pm` and the Zoho webhook), both
`recipient=pm_profile` / the resolved profile. No role lookup.

**One role lookup exists in the notification area, and it is not a PM lookup** — the Zoho
webhook's fallback when no PM could be matched:

```python
    if project.assigned_pm is None:
        # Notify Admin in-app: PM could not be assigned
        try:
            admin_profile = UserProfile.objects.filter(role='Admin').first()
```

**Verdict: notification routing is safe for a Design-role PM.** He would receive every PM
notification correctly.

## EOD digest — groups by role, and would give him the wrong-shaped digest

`send_eod_digest` (`projects/management/commands/`):

```python
        excluded_roles = getattr(settings, 'EOD_DIGEST_EXCLUDED_ROLES', [])
        recipients = list(
            UserProfile.objects
            .filter(is_active=True, user__is_active=True)
            .exclude(role__in=excluded_roles)
            .select_related('user')
            .order_by('user__first_name', 'user__username')
        )
```
```python
        # Coordinator recipients get a role-based template branch (§2) and their own gating
        # inputs (§3). Roles are mutually exclusive, so this is a clean branch, not an overlay.
        COORDINATOR_ROLE = 'Project Coordinator'
        coord_pks = {p.pk for p in recipients if p.role == COORDINATOR_ROLE}
```
```python
            metrics = {
                'assigned':        assigned_map.get(profile.pk, 0),
                ...
                # Coordinator-only fields (§2). Zero/ignored for every other role.
                'coord_projects':    coord_projects_map.get(profile.pk, 0),
```

The five base metrics are own-activity (assignment-driven) and would be correct for Praveen. The
only role-branched content is the Coordinator branch, which he correctly does not get.
**Low severity:** he receives a digest, it counts his real tasks, and no PM-specific digest
content exists to be withheld. The role grouping is a reporting-shape issue, not a data loss.

## Hit table

| File | Function | What goes wrong |
|---|---|---|
| `projects/decorators.py` | `get_user_dashboard` / `ROLE_DASHBOARD` | Lands on `/dashboard/design/`; nav Home never points at the PM dashboard |
| `projects/views.py` | `dashboard_pm` (decorator) | 403. Holds authority, never sees the tender — the named failure |
| `projects/views.py` | `program_list`, `program_detail`, `program_create`, `program_edit` (decorators) | Cannot open, create or edit the tender container at all |
| `projects/views.py` | `opex_site_activate` (decorator) | Cannot start execution on his own site — no tasks ever exist |
| `projects/views.py` | `project_overview` template context | Renders PM controls (`is_assigned_pm` is True) whose targets then 403 |
| `projects/views.py` | `create_opex_site` | A Design creator silently produces `assigned_pm=None` — no error |
| `projects/templates/base.html` | nav | Projects nav hidden: `role == 'Admin' or 'PM' or 'CEO'` |
| `send_eod_digest` | recipient grouping | Correct base metrics; no PM-shaped digest exists (low) |
| `projects/permissions.py` | `project_managers` | **No problem** — assignment-driven |
| `projects/views.py` | `_PROFILE_TO_TASK_ROLE` | **No problem** — one copy, doesn't touch `'PM'` |
| `projects/utils.py` | `attach_opex_template` / `attach_residential_template` | **No problem** — assignment-driven |

---

# Q7 — Screens gated by role rather than by assignment

## The census

Every `@role_required([...])` whose list contains `'PM'`, across `views.py`, `design_views.py`
and `report_views.py`:

```
$ grep -rn "role_required(\[" projects/views.py projects/design_views.py projects/report_views.py | grep "'PM'"
projects/views.py:589:@role_required(['PM', 'Project Coordinator'])
projects/views.py:2672:@role_required(['PM', 'Admin', 'CEO'])
projects/views.py:2690:@role_required(['PM'])
projects/views.py:2759:@role_required(['PM', 'Project Coordinator'])
projects/views.py:2893:@role_required(['PM', 'Project Coordinator'])
projects/views.py:2971:@role_required(['PM', 'Project Coordinator'])
projects/views.py:3079:@role_required(['PM', 'Project Coordinator'])
projects/views.py:3198:@role_required(['PM', 'Project Coordinator'])
projects/views.py:3277:@role_required(['Admin', 'PM', 'CEO'])
projects/views.py:3314:@role_required(['Admin', 'PM', 'CEO'])
projects/views.py:3339:@role_required(['Admin', 'PM'])
projects/views.py:3361:@role_required(['Admin', 'PM'])
projects/views.py:3485:@role_required(['Admin', 'PM'])
projects/views.py:3679:@role_required(['Admin', 'PM'])
projects/views.py:3815:@role_required(['Admin', 'PM'])
projects/views.py:5182:@role_required(['PM', 'Project Coordinator'])
projects/views.py:7807:@role_required(['PM', 'Project Coordinator'])
projects/views.py:8217:@role_required(['BD', 'PM'])
projects/design_views.py:4667:   [COMMENT, not a decorator]
projects/design_views.py:4774:   [COMMENT, not a decorator]
```

**18 real decorators.** `'Design'` appears in none of the 18 lists.

## The table — object-scoped views and whether they also check assignment

| View | Decorator | Object-scoped? | `user_can_manage_project()` also called? |
|---|---|---|---|
| `project_edit` | `['PM','Project Coordinator']` | project | **Yes** — via `_pm_owns_project` |
| `project_activate` | `['PM','Project Coordinator']` | project | **Yes** — via `_pm_owns_project` |
| `opex_site_activate` | `['PM','Project Coordinator']` | project | **Yes** — direct + `_pm_owns_project` |
| `project_recalculate_dates` | `['PM','Project Coordinator']` | project | **Yes** — via `_pm_owns_project` |
| `task_add` | `['PM','Project Coordinator']` | project | **Yes** — via `_pm_owns_project` |
| `task_assign` | `['PM','Project Coordinator']` | project | **Yes** — via `_pm_owns_project` |
| `milestone_create` | `['PM','Project Coordinator']` | project | **Yes** — via `_pm_owns_project` |
| `set_milestone_amounts` | `['BD','PM']` | project | **Yes** — direct, narrowed to `role == 'PM'` |
| `program_detail` | `['Admin','PM','CEO']` | program | via `_can_access_program` → `user_can_manage_program` |
| `program_edit` | `['Admin','PM','CEO']` | program | via `_can_access_program` |
| `opex_site_create` | `['Admin','PM']` | program | via `_can_access_program` |
| `opex_site_bulk_upload` | `['Admin','PM']` | program | via `_can_access_program` |
| `opex_site_bulk_template` | `['Admin','PM']` | program | via `_can_access_program` |
| `program_list` | `['Admin','PM','CEO']` | list | per-row `_can_access_program` |
| `dashboard_pm` | `['PM','Project Coordinator']` | none (list) | no — assignment-scoped queryset instead |
| `project_list` | `['PM','Admin','CEO']` | none (redirect) | no |
| `project_create` | `['PM']` | none (create) | no |
| `program_create` | `['Admin','PM']` | none (create) | no |

`_pm_owns_project` is a thin adapter, not a second rule:

```python
def _pm_owns_project(request, project):
    """Return True if the request user is the assigned PM on this project.

    Thin adapter over the canonical user_can_manage_project() — kept so its
    existing callers stay unchanged. No ownership comparison lives here anymore.
    """
    return user_can_manage_project(request.user, project)
```

## The count

# **18.**

That is the number of screens a Design-role PM would be refused from. **It is not zero.** All 13
object-scoped ones would pass their assignment check — Q4 proves `user_can_manage_project()`
returns True for him — and are refused earlier, by the decorator, with a 403 and no message.

The decorator denies before the view body runs, and denies with 403 rather than a redirect
(`role_required` in `projects/decorators.py`):

```python
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect('/login/')
            try:
                role = request.user.profile.role
            except Exception:
                # No UserProfile — no role, and therefore no access. Deny, never assume.
```

Executed proof against three of the 18 with Praveen's real user:

```
actor: praveen role: Design is_design_head: True
  dashboard_pm     -> status 403
  program_list     -> status 403
  project_create   -> status 403
```

**The remaining 15 were not individually executed.** INFERRED, with high confidence: they share
one decorator implementation and none of their lists contains `'Design'`, so all 18 deny
identically.

---

# Q8 — Design Head and PM as the same person

## Does any check compare the design approver against the project's PM?

**No.** `design_views.py` contains no reference to `assigned_pm` at all:

```
$ grep -n "assigned_pm\|user_can_manage_project" projects/design_views.py
3280:# `user_can_manage_project()`.                                    [COMMENT ONLY]
```

The single mention is a comment noting that change requests route through
`user_can_request_design_change()`, which is a pass-through (`projects/permissions.py`):

```python
def user_can_request_design_change(user, project):
    """Return True if `user` may raise a PM change request against `project`.

    Routed straight through user_can_manage_project() — the one canonical PM-authority
    path, which already covers the assigned PM and every active Project Coordinator
    (settled decision 5). Deliberately NOT re-derived here and that function is NOT
    modified; this wrapper exists only so the change-request view states its rule by
    name like every other view in the design module, rather than reaching for a
    general-purpose helper directly.
    """
    if project is None:
        return False
    return user_can_manage_project(user, project)
```

## Does anything refuse a transition when the acting user recorded the prior verdict?

**Yes — but only WITHIN the design module, between its own two gates.** `apply_design_status()`
itself refuses nothing; it is a writer, and says so:

```python
    WHAT IT IS NOT. It is not a state machine and deliberately carries no transition
    table. Whether a move is legal from the current state remains each caller's own job,
    exactly as before ... It is also not a permission
    check: those need `request`, which this never takes.
```

The refusal lives in `_other_gate_actor_conflict` (`projects/design_views.py`):

```python
def _other_gate_actor_conflict(profile, other_gate_reviewer_id):
    """Settled decision 2: one person cannot record BOTH verdicts on the same artifact.

    `other_gate_reviewer_id` is the *_reviewed_by_id already stored by the opposite gate
    on this exact row. Returns True if it is this same person, which is the refusal case.

    PER ARTIFACT, NOT PER USER. Somebody holding both flags is entirely legitimate and is
    refused only the SECOND verdict on an artifact they have already ruled on — they may
    record either gate's verdict on any other site, and may record the Head verdict here
    if a different person passed it through Design QC. A flag says what you may be; this
    says what you may not do twice.
    """
    if profile is None or other_gate_reviewer_id is None:
        return False
    return other_gate_reviewer_id == profile.pk
```

Enforced at release, in `design_head_qc_pass`:

```python
    profile = request.user.profile
    if _other_gate_actor_conflict(profile, attempt.qc_reviewed_by_id):
        messages.error(request, f'{project.project_id}: you passed attempt '
                                f'{attempt.attempt_number} through Design QC yourself, so '
                                f'you cannot also release it as Design Head. It needs a '
                                f'second pair of eyes.')
        return redirect('design_qc_review', project_id=project.project_id)
```

And two more self-review rules in the same module (`projects/permissions.py`):

```python
def user_can_qc_design(user, assignment):
    """...
        Design Head authority (Head or named deputy)
        AND NOT the designer this site is allocated to

    THE SECOND CONDITION IS THE POINT. Nobody QCs their own package — not a designer,
    not a deputy who happens to be the allocated designer, and not the Head himself if
    he has taken a site on personally (settled decision 1). ...
    """
```
```python
def user_is_assigned_designer(user, assignment):
    """...
    Deliberately strict: it is an identity check against `assignment.assigned_to` and
    nothing else. The Design Head does NOT satisfy it — proposing a due date is the
    designer's act, and the Head approving his own proposal would collapse the
    two-sided handshake into one side."""
```

**Every one of these compares a design actor to another DESIGN actor.** Designer vs QC reviewer;
QC reviewer vs Head. Not one of them compares any design actor to `project.assigned_pm`.

## The one place a two-signature rule DOES exist on the PM side

`task_approve` (`projects/views.py`) — but it is about task submission, not design:

```python
    if task.submitted_by is not None and task.submitted_by == profile:
        messages.error(
            request,
            f"You submitted '{task.task_name}' yourself — a RESCO task must be "
            f"approved by someone other than the person who submitted it. Ask "
            f"another project manager or a QA/QC reviewer to sign it off."
        )
        return _approval_response(request, project, task)
```

Its own comment names the exact situation Dehradun creates and treats it as expected:

```python
    # anyone — on a small site the PM is frequently both the person who did the
    # work and the only manager present, which is exactly the case where a
    # self-signature would be silent and routine.
```

## Plain answer

**YES. If one user is both Design Head and the site's `assigned_pm`, they can release a design
and then act on it as PM with no check firing.**

The design-side gates (`_other_gate_actor_conflict`, `user_can_qc_design`,
`user_is_assigned_designer`) all fire on design-vs-design collisions and would still protect the
QC↔Head boundary on Dehradun. But `released` is where the design module's rules stop —
`design_head_qc_pass`'s own docstring says so:

> Release sets `released_at` / `released_by` on the assignment and moves it to
> `released`. THAT IS ALL IT DOES (Part 4 settled decision 9). It does not lock, group
> or hand over the BOQ — those are Part 6, and reading anything into `released` beyond
> "design is finished" would pre-empt decisions that have not been made.

Downstream, `user_can_request_design_change()` routes to `user_can_manage_project()`, which by Q4
returns True for him. So the same person releases the design as Head and then raises, accepts or
acts on it as PM. There is no cross-module comparison to fire.

**This is a "no check exists" finding, as anticipated. Nothing was built. Recorded only.**

Practical scope, for calibration: Dehradun is one site with one PM. The design gates that DO
exist (QC vs Head) will still demand a second person for the QC↔release pair unless Praveen is
also given `is_design_qc` — he is not (`is_design_qc=False`, verified in the profile dump at the
top of this document). So the two-gate design review remains genuinely two-person. What collapses
is the boundary *between* design release and PM acceptance, which was never guarded in the first
place — for any project, with any PM.

---

# 2. THE ONE-LINE VERDICT

> **Verdict:** Can a user with `role='Design'` be set as `Project.assigned_pm` on a new site
> through the existing UI today — **NO.** The single expression that prevents it is
> **`role='PM'`**: `.filter(role='PM', is_active=True)` in the Admin and System-Admin PM
> dropdowns (and re-checked on their POST paths), `limit_choices_to={'role': 'PM'}` on the
> `Project.assigned_pm` field for the Django admin, and `profile.role == 'PM'` in
> `create_opex_site`. The only path with no role filter is the Zoho webhook, which creates
> Residential projects only and is unreachable for a tender site.

Two immediate corollaries:

- **The blocker is the assignment step, not the authority model.** `user_can_manage_project()`
  is role-blind and returns True for him (Q4, proven). If the field were populated by any means,
  PM authority would work and PM tasks would route to him correctly (Q5, proven).
- **But populating it would not be sufficient.** 18 screens are gated on the role string before
  any assignment check runs (Q7), including the one that creates the tender's tasks.

---

# 3. THE BREAKAGE LIST

Ordered by how badly it misbehaves. Assumes the field were populated by some means (direct ORM
write, or a fix), since the UI refuses it outright.

### 1. The tender's site can never be activated → **no tasks are ever created** — SILENT, WORST
`opex_site_activate` is `@role_required(['PM', 'Project Coordinator'])`. **Admin is not in the
list.** It is the only path that attaches the OPEX task template. Praveen gets a 403 with no
message; the site stays `Draft` forever. Discovered weeks later as "the tender has no tasks" —
exactly the failure mode this audit was commissioned to find. **Requires a PM-role or
Coordinator-role account to unblock; not even Admin can do it.**

### 2. Holds the authority, never sees the tender — SILENT
`dashboard_pm` 403s (proven). `get_user_dashboard` lands him on `/dashboard/design/`, which
scopes on `assigned_design` OR holding a task — never on `assigned_pm`. Pre-activation he sees
nothing. Post-activation (if somebody else activates) his PM tasks would surface the site on the
*Design* dashboard as a designer's card, with no PM stat blocks and no management controls.

### 3. Cannot open the tender container at all — LOUD (403), but total
`program_list`, `program_detail`, `program_edit`, `program_create` are all `@role_required` lists
without `'Design'` (proven for two of them). He cannot create Dehradun, cannot be its
`created_by`, cannot open it, and cannot add a site to it (`opex_site_create` /
`opex_site_bulk_upload` are `['Admin','PM']`).

### 4. PM controls render, then 403 — LOUD but confusing
`project_overview` is `@login_required` only and gates its controls on
`is_assigned_pm = user_can_manage_project(...)`, which is **True** for him. So Edit, Activate,
Add Task, Add Milestone and the coordinator link all render — and every target (`project_edit`,
`project_activate`, `opex_site_activate`, `task_add`, `milestone_create`) 403s. A UI that offers
actions it will then refuse.

### 5. Creating a site silently yields `assigned_pm=None` — SILENT
`create_opex_site`: `site.assigned_pm = profile if (profile and profile.role == 'PM') else None`.
If Praveen ever reached the site-create screen (he cannot today — item 3), the site would be
created unowned with no warning. The same code path serves the bulk upload, so a 40-row file
would produce 40 unowned sites.

### 6. Projects nav hidden — COSMETIC but compounds items 2 and 3
`projects/templates/base.html`: `{% if user.profile.role == 'Admin' or ... 'PM' or ... 'CEO' %}`.
No navigation route to any project surface, even where one would work.

### 7. Design release → PM acceptance has no separation-of-duties check — SILENT, BY DESIGN
Q8. No comparison exists anywhere between a design actor and `project.assigned_pm`. He could
release the design as Head and act on it as PM. Pre-existing for every PM; made concrete here.
**Recorded, not built.**

### 8. Reassigning a tender's PM is N separate POSTs — SCALING, not a Dehradun problem
Q3-R. No bulk path. One site today; forty on the next tender, with no transaction and no screen
that shows a missed site.

### 9. EOD digest role grouping — LOW
Base metrics are assignment-driven and correct. Only the Coordinator content branch is
role-keyed, and he correctly does not get it. No PM-specific digest content exists to lose.

## What does NOT break — stated explicitly

- `user_can_manage_project()` / `manageable_projects_q()` — role-blind, correct (Q4, proven).
- PM task routing — reads `assigned_pm`, no role lookup anywhere (Q5, proven).
- Notification recipient resolution — `project_managers()`, assignment-driven (Q6).
- `_PROFILE_TO_TASK_ROLE` — one copy, doesn't touch `'PM'`, structurally pinned by tests (Q6).
- Mixed PMs across one Program's sites — handled correctly everywhere it is read (Q3c-R).
- The site-less-Program window — closed for the creator by `_can_access_program`'s `created_by`
  fallback (Q3b-R).
- The design two-gate review — still genuinely two-person; Praveen has `is_design_qc=False`.

---

# 4. OUT OF SCOPE

Appended to `SECONDARY_FINDINGS.md`. Headings:

- Zoho webhook seats any `UserProfile` as `assigned_pm` — no role filter, no `is_active` filter
- `opex_site_activate` excludes Admin, so no administrative unblock exists
- `subadmin_projects` PM assignment writes no `action_code`
- `LOGIN_REDIRECT_URL` points at `/dashboard/admin/` and is bypassed by `login_view`
- `project_create` and `create_opex_site` do not check `UserProfile.is_active`
- `_can_access_program`'s `created_by` fallback survives losing every site
- `program_create` and `ProgramForm` never validate that a tender will acquire an owner

None acted on.
