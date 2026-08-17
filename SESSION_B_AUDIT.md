# SESSION B AUDIT — QC reviewer allocation per site

**Mode:** investigation only. No application code was written, edited or deleted. No
migration was created. `makemigrations` was run once as `--check --dry-run` (writes
nothing) during the pre-push check described below; it was not run during the audit.

**Databases.** Every code finding below is read from the working tree at HEAD. Every
figure in Part 3 is labelled `[PRODUCTION]` and was read from the Railway production
Postgres. **No result in this report is labelled `[LOCAL]`**: the local database was not
queried at all, because a local figure cannot answer a production question and reporting
one would invite exactly that substitution.

**How production was reached, and what was issued.** `DATABASE_URL` was overridden with
the production `DATABASE_PUBLIC_URL` for the lifetime of one script — the same method
`BLOCK0_VERIFICATION.md:89-97` documents, and for the same reason (the service's own
`DATABASE_URL` points at `postgres.railway.internal`, unresolvable from outside Railway).
The credential was read from a local file rather than passed on the command line, so it
never entered shell history. **Every statement issued was a `SELECT`** — Django ORM reads
plus one `current_database()` / `version()` connection proof. No `INSERT`, `UPDATE`,
`DELETE`, `CREATE`, `ALTER`, no management command with side effects, no `migrate`, no
`railway up`. Hard stop 5 did not fire: no question required a write.

Connection proof `[PRODUCTION]`:

```
HOST  : acela.proxy.rlwy.net
PORT  : 28397
NAME  : railway
SERVER: ('railway', '10.228.87.113', 'PostgreSQL 18.4 (Debian 18.4-1.pgdg13+1) on x86_64-pc-linux-gnu, compiled by gcc (Debian 14.2.0-19) 14.2.0, 64-bit')
```

**The production reads were blocked twice by the permission classifier before being run.**
The user resolved it by switching permission mode and approving the call. Nothing about the
script changed between the refusals and the run.

---

## SESSION OPENING CHECK

### 1. Repo

`c:\SolarPMS\Horizon-Solar-PMS`. All git commands were run there.

### 2. `git status` — first run, HARD STOP 1 FIRED

```
On branch main
Your branch is ahead of 'origin/main' by 1 commit.
  (use "git push" to publish your local commits)

Changes not staged for commit:
  (use "git add <file>..." to update what will be committed)
  (use "git restore <file>..." to discard changes in working directory)
	modified:   projects/design_views.py
	modified:   projects/models.py
	modified:   projects/templates/projects/design/_attempt_history.html
	modified:   projects/templates/projects/design/head_review.html
	modified:   projects/templates/projects/design/site_workspace.html
	modified:   projects/templates/projects/opex_boq_entry.html

Untracked files:
  (use "git add <file>..." to include in what will be committed)
	projects/migrations/0062_designattempt_boq_remarks.py
```

Six tracked files modified. The untracked file was a migration, not a `.md` report, so
the prompt's carve-out did not cover it. The audit stopped and reported.

**Resolution.** The user instructed: *"commit and push the in-flight work."* That work —
a designer's submission note on the BOQ, the companion to `5501fc0`, which had added the
same note to the Arka and the CAD — was committed as `338ebde` and pushed. Pre-push
checks run first, all passing: `manage.py check` (0 issues), `makemigrations --check
--dry-run` (`No changes detected`, so 0062 fully captured the model state), and a
template compile of all four changed templates.

**This is a deviation from the prompt's DO NOT list, made on the user's explicit
instruction after the hard stop was reported.** It was the unblock the user chose from
three offered. The audit itself wrote nothing.

### 2b. `git status` — after the unblock

Clean. No tracked file modified or staged, no untracked file.

### 3. `git log --oneline -5` and local HEAD

```
338ebde Let a designer leave a submission note on the BOQ
5501fc0 Let a designer leave a submission note on an Arka and a CAD
c85399e Add a Home control that resolves to the user's own dashboard
018c598 Add audit and status reports
2792f2c Fix survey-link comment rendering onto the Actions cell
```

Local HEAD: `338ebde7c3b2d20ff00e5a342f45b6067e30c163`

**Sessions A1, A2 and A3 — NOT ESTABLISHED.** The commit subjects carry no session
identifiers, and no mapping from these subjects to the labels "A1", "A2", "A3" exists in
the repo that this audit could verify. `018c598 Add audit and status reports` is the only
commit whose subject even gestures at session output. This audit does not assert that the
three sessions are present, and does not assert that they are absent.

### 4. Deployed SHA — observed, not inferred

The Railway MCP authenticated, so this is an observation. `triumphant-forgiveness` /
`Horizon-Solar-PMS` / `production`:

```
196a5c2d-e959-4251-9061-ef53f0660a9f | SUCCESS | 2026-08-14 13:36:54.597 UTC | 338ebde7c3b2d20ff00e5a342f45b6067e30c163
18d5225c-254f-47cc-b6f2-b4ccf14b7d09 | REMOVED | 2026-08-14 02:14:29.895 UTC | c85399e73c59f3cf630309123e63e171d4231ab2
```

Deployed SHA `338ebde…` **equals local HEAD**. Hard stop 2 does not fire.

At the first check, before the unblock, the deployed SHA was `c85399e` against a local
HEAD of `5501fc0` — hard stop 2 fired then, and was resolved by the same push.

### 5. Migration head

```
0058_part46_change_request_triage.py
0059_part10_design_analytics_preference.py
0060_designassignment_survey_folder_url_and_more.py
0061_arkasubmission_remarks_designfile_remarks.py
0062_designattempt_boq_remarks.py
```

Head is `0062_designattempt_boq_remarks`. It applied on production as part of the deploy
(`Procfile` runs `migrate --run-syncdb` on boot):

```
[2026-08-14T13:37:43.994294780Z]   Applying projects.0062_designattempt_boq_remarks... OK
```

**Disclosure against hard stop 5.** That `AddField` is a production schema write. It was
the deploy the user instructed, executed before the audit began, and no audit question
caused it. The audit issued no write of any kind.

---

## PART 1 — HOW GATE 1 WORKS TODAY

### 1.1 — The verdict path

**They are separate functions. There is no shared verdict view and no mode flag on any
view.** Gate 1 and gate 2 are eight distinct view functions across two artifacts, plus one
gate-1 start endpoint.

| Artifact | Gate | URL name | Function | File:lines |
|---|---|---|---|---|
| Arka | 1 | `design_arka_approve` | `design_arka_approve` | `projects/design_views.py:1701-1750` |
| Arka | 1 | `design_arka_reject` | `design_arka_reject` | `projects/design_views.py:1753-1817` |
| Arka | 2 | `design_arka_head_approve` | `design_arka_head_approve` | `projects/design_views.py:1820-1873` |
| Arka | 2 | `design_arka_head_reject` | `design_arka_head_reject` | `projects/design_views.py:1876-1946` |
| Package | 1 | `design_qc_start` | `design_qc_start` | `projects/design_views.py:2436-2481` |
| Package | 1 | `design_qc_pass` | `design_qc_pass` | `projects/design_views.py:2500-2553` |
| Package | 1 | `design_qc_fail` | `design_qc_fail` | `projects/design_views.py:2556-2640` |
| Package | 2 | `design_head_qc_pass` | `design_head_qc_pass` | `projects/design_views.py:2643-2700` |
| Package | 2 | `design_head_qc_fail` | `design_head_qc_fail` | `projects/design_views.py:2703-…` |

Routes, `projects/urls.py:114-123`:

```python
    path('design/qc/',                              design_views.design_qc_queue,        name='design_qc_queue'),
    path('design/<str:project_id>/qc/',             design_views.design_qc_review,       name='design_qc_review'),
    path('design/<str:project_id>/qc/start/',       design_views.design_qc_start,        name='design_qc_start'),
    # Gate 1 — Design QC. Same routes and names as Part 4; the fields they write are the
    # Design QC verdict, so they keep their URLs and change who may POST to them.
    path('design/<str:project_id>/qc/pass/',        design_views.design_qc_pass,         name='design_qc_pass'),
    path('design/<str:project_id>/qc/fail/',        design_views.design_qc_fail,         name='design_qc_fail'),
    # Gate 2 — the Design Head. Release now happens here, not at qc/pass/.
    path('design/<str:project_id>/qc/head/pass/',   design_views.design_head_qc_pass,    name='design_head_qc_pass'),
    path('design/<str:project_id>/qc/head/fail/',   design_views.design_head_qc_fail,    name='design_head_qc_fail'),
```

Arka routes, `projects/urls.py:78-81`:

```python
    path('design/<str:project_id>/arka/approve/',   design_views.design_arka_approve,    name='design_arka_approve'),
    path('design/<str:project_id>/arka/reject/',    design_views.design_arka_reject,     name='design_arka_reject'),
    path('design/<str:project_id>/arka/head/approve/', design_views.design_arka_head_approve, name='design_arka_head_approve'),
    path('design/<str:project_id>/arka/head/reject/',  design_views.design_arka_head_reject,  name='design_arka_head_reject'),
```

**The near-miss on hard stop 4, stated precisely.** The five package endpoints do share a
guard helper that takes a gate parameter — `_qc_guard(request, project, required_statuses,
gate='qc')`, `projects/design_views.py:2390-2423`:

```python
    `gate` selects the predicate: 'qc' -> user_can_qc_gate_design (the is_design_qc flag),
    'head' -> user_can_head_gate_design (Head authority or named deputy). Both refuse the
    assigned designer, so a designer reviewing their own package is refused identically at
    all five endpoints regardless of what flags they hold.
```

```python
    allowed = (user_can_head_gate_design(request.user, assignment) if gate == 'head'
               else user_can_qc_gate_design(request.user, assignment))
    if not allowed:
        return None, None, None
```

That is a shared **guard**, not a shared view, and the flag selects a **different
predicate per gate**. Narrowing `user_can_qc_gate_design` would therefore scope gate 1
alone and leave `user_can_head_gate_design` untouched. **Hard stop 4 does not fire** —
scoping one would not scope both. The four Arka endpoints do not use `_qc_guard` at all;
each calls its predicate inline (e.g. `projects/design_views.py:1711`).

### 1.2 — The permission gate

The exact check, `projects/permissions.py:513-527`:

```python
def user_can_qc_gate_design(user, assignment):
    """Return True if `user` may record the DESIGN QC (first-gate) verdict on `assignment`.

        the is_design_qc flag
        AND NOT the designer this site is allocated to

    The Design Head does NOT satisfy this by virtue of being the Head — the two flags are
    independent, and a Head who has not been given `is_design_qc` reviews at gate 2 only.
    That is the whole point of a second gate: two people, not one person twice.
    """
    if assignment is None:
        return False
    if not user_is_design_qc(user):
        return False
    return not user_is_assigned_designer(user, assignment)
```

Underlying flag read, `projects/permissions.py:497-510`:

```python
def user_is_design_qc(user):
    ...
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    return bool(profile.is_design_qc)
```

**Does it test `is_design_qc` alone, or something more?** Two conditions: the flag, AND
not the assigned designer. Nothing else. It does not consult role, tender, program,
status, or any per-site field.

**Is `user_can_manage_project()` involved anywhere in this path?** No. It appears in the
design module only for the PM change-request path, `projects/permissions.py:555-567`:

```python
def user_can_request_design_change(user, project):
    ...
    if project is None:
        return False
    return user_can_manage_project(user, project)
```

That function is not reachable from any gate-1 verdict endpoint.

**Is there any per-site scoping at all today?** **None.** The only site-specific term in
the predicate is the *negative* one — `not user_is_assigned_designer(user, assignment)`.
There is no positive per-site term. A QC-flag holder is authorised to record the gate-1
verdict on **every site in the system** except those they are the allocated designer of.
The module's own comment states the design intent, `projects/permissions.py:492-494`:

```python
# THE DEPUTY IS GATE 2 ONLY (settled decision 9). A Design Head's deputy acts for the
# Head; there is no deputy for Design QC in this session, so user_can_qc_gate_design()
# consults `is_design_qc` and nothing else.
```

### 1.3 — The designer-cannot-review-own-site rule

**It is enforced server-side, in a helper, reached by every gate-1 and gate-2 endpoint.
It is not template-only. Hard stop 3 does not fire.**

The rule, `projects/permissions.py:570-582`:

```python
def user_is_assigned_designer(user, assignment):
    """Return True if `user` is the designer this assignment is allocated to.

    Deliberately strict: it is an identity check against `assignment.assigned_to` and
    nothing else. The Design Head does NOT satisfy it — proposing a due date is the
    designer's act, and the Head approving his own proposal would collapse the
    two-sided handshake into one side."""
    if assignment is None:
        return False
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    return assignment.assigned_to_id == profile.pk
```

It is applied as the final term of **both** gate predicates — `user_can_qc_gate_design`
(quoted at 1.2, line 527) and `user_can_qc_design`, `projects/permissions.py:466-470`:

```python
    if assignment is None:
        return False
    if not user_has_design_head_authority(user):
        return False
    return not user_is_assigned_designer(user, assignment)
```

with the stated rationale at `projects/permissions.py:455-459`:

```python
    THE SECOND CONDITION IS THE POINT. Nobody QCs their own package — not a designer,
    not a deputy who happens to be the allocated designer, and not the Head himself if
    he has taken a site on personally (settled decision 1). QC is a second pair of eyes
    or it is nothing, and a self-review that passes is indistinguishable in the data
    from one that was never done.
```

**Enforced in more than one place?** It is defined once and *applied* at every gate-1 and
gate-2 endpoint — inline for the four Arka views (`design_views.py:1711`, `1768`, `1831`,
`1891`) and through `_qc_guard` for the five package views. There is additionally a
template-visible signal, but it is display-only and not the enforcement:
`projects/design_views.py:3291` puts `'is_self_qc': user_is_assigned_designer(request.user,
assignment)` in the context, rendered at `projects/templates/projects/design/qc_review.html:53`.

### 1.4 — The same-person-cannot-record-both-verdicts rule

`projects/design_views.py:1230-1249`:

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

    A null reviewer (no verdict recorded at that gate yet, or a historical row backfilled
    by migration 0055 with head_reviewed_by left null) compares False and admits the
    actor — correct in both cases, because no person is being asked to agree with
    themselves.
    """
    if profile is None or other_gate_reviewer_id is None:
        return False
    return other_gate_reviewer_id == profile.pk
```

**What it compares:** the acting user's `profile.pk` against the `*_reviewed_by_id`
already stored **by the opposite gate on that exact row**. It is per artifact-row, not per
user and not per site. Null on the other side admits the actor.

Applied from both sides. Gate 1 checks the Head's column —
`projects/design_views.py:2529`:

```python
    if _other_gate_actor_conflict(profile, attempt.head_reviewed_by_id):
```

Gate 2 checks QC's column — `projects/design_views.py:2672`:

```python
    if _other_gate_actor_conflict(profile, attempt.qc_reviewed_by_id):
```

### 1.5 — The deputy

**What it is: an FK, not a flag and not a group.** `projects/models.py:557-566`:

```python
    # Names a deputy who may act for the Design Head during absence. STORAGE ONLY in this
    # ... the deputy is itself a UserProfile; SET_NULL so deactivating the deputy's profile
    design_head_deputy      = models.ForeignKey(
        ...
        related_name='deputy_for',
    )
```

**Where it is checked:** `projects/permissions.py:406-422`:

```python
def user_is_design_head_deputy(user):
    """Return True if `user` is the named deputy of somebody who is a Design Head.

    THE PRESENCE OF THE FK IS THE WHOLE RULE (settled decision 6). There is no absence
    schedule, no date range and no on/off switch — if the Head has named you, you may
    act, and when he clears the field you may not. Anything richer is a scheduling
    feature nobody asked for.

    `is_design_head=True` is re-checked on the NAMING profile, not assumed: a deputy is
    only a deputy of an actual Head, so clearing someone's Head flag silently revokes
    the authority of anyone they had deputised. ...
    """
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    return profile.deputy_for.filter(is_design_head=True).exists()
```

**What authority it confers**, `projects/permissions.py:425-444`:

```python
def user_has_design_head_authority(user):
    """Return True if `user` may act with Design Head authority — the Head OR his
    named deputy.
    ...
    NOT SUFFICIENT FOR QC ON ITS OWN — see user_can_qc_design(), which additionally
    refuses the assigned designer.
    """
    return user_is_design_head(user) or user_is_design_head_deputy(user)
```

and gate 2 delegates to it, `projects/permissions.py:530-539`:

```python
def user_can_head_gate_design(user, assignment):
    """Return True if `user` may record the DESIGN HEAD (second-gate) verdict.
    ...
    """
    return user_can_qc_design(user, assignment)
```

**Direct answer: a deputy can record a gate-2 verdict only — not gate 1, and not both.**
`user_can_qc_gate_design` (1.2) reads `is_design_qc` and the designer exclusion, and never
consults deputy status. A deputy reaches gate 1 if and only if they separately hold
`is_design_qc`, in which case they reach it *as a QC-flag holder*, not as a deputy. The
module states this itself at `projects/permissions.py:492-494` (quoted at 1.2).

The deputy does additionally get **read** access to design surfaces
(`user_can_view_design`, `projects/permissions.py:585-610`) and is admitted to the QC
dashboard and queue by `user_can_view_design_qc_dashboard`,
`projects/permissions.py:542-552`:

```python
def user_can_view_design_qc_dashboard(user):
    ...
    Read only. It confers no authority to record any verdict; both gates are decided
    per site by the two predicates above.
    """
    return user_is_design_qc(user) or user_has_design_head_authority(user)
```

### 1.6 — Where QC users are listed today

**There is no screen that lists QC holders, and no application queryset anywhere that
fetches `is_design_qc=True` users.** This is the single most consequential finding in
Part 1 for sizing the request.

Every occurrence of the flag being *selected on* rather than *read from the requesting
user*:

| File:line | What it does |
|---|---|
| `projects/admin.py:155` | `list_display` — shows the column in Django admin's UserProfile changelist |
| `projects/admin.py:156` | `list_filter` — lets a Django-admin user filter by it; the only "list QC holders" surface that exists, and it is the admin site, not the product |
| `projects/tests_design_part9.py:74,76`, `part10.py:86-87`, `part11.py:113`, `part46.py:66`, `part9.py:276` | test fixtures constructing QC users |

Grep for `is_design_qc=True` / `filter(...is_design_qc...)` across all `*.py` returns
**only test files**. No view, helper, dashboard or form builds a list of QC users.

Every other occurrence reads the flag off **the requesting user**, never a list:

| File:line | What it does |
|---|---|
| `projects/permissions.py:510` | `user_is_design_qc()` — reads the flag off one user |
| `projects/permissions.py:525` | gate-1 predicate |
| `projects/permissions.py:269` | BOQ read access branch |
| `projects/permissions.py:552` | QC dashboard read gate |
| `projects/permissions.py:610` | design view access |
| `projects/design_views.py:1550` | `ctx['is_design_qc'] = can_qc_gate` on the Arka screen |
| `projects/design_views.py:3246` | `'is_design_qc': user_is_design_qc(request.user)` on the queue |
| `projects/design_views.py:3507` | `if not user_is_design_qc(user): return None` — dashboard counts |
| `projects/models.py:556` | the field itself |
| `projects/forms.py:158` | `is_design_qc = forms.BooleanField(...)` — the admin user form |
| `projects/views.py:8310, 8341, 9383` | writing the flag from the two user-edit paths |
| `projects/views.py:1042` | dashboard comment about dual-flag holders |
| `projects/templates/projects/admin/user_edit.html:102-105` | the checkbox |
| `projects/templates/projects/subadmin/departments.html:128, 299, 303` | the checkbox in the department editor |
| `projects/templates/projects/design/qc_queue.html:35` | `{% if is_design_qc %}` section header |

**Contrast with the designer control, which does have its list**,
`projects/design_views.py:300-302`:

```python
    designers = (UserProfile.objects.select_related('user')
                 .filter(role='Design', is_active=True)
                 .order_by('user__first_name', 'user__username'))
```

An "assign QC" selector needs an equivalent queryset that **does not exist today** and
would have to be written, along with a decision about what it filters on — `is_design_qc`
alone, or `is_design_qc` minus the site's own designer.

---

## PART 2 — WHERE ASSIGNMENT WOULD LIVE

### 2.1 — How designer allocation works

**The field**, `projects/models.py:2136-2145`:

```python
    # ── Allocation ──────────────────────────────────────────────────────────
    assigned_to = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='design_assignments',
    )
    assigned_by = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='made_design_assignments',
    )
    assigned_at = models.DateTimeField(null=True, blank=True)
```

- name: `assigned_to`
- type: `ForeignKey` to `'UserProfile'`
- `null=True, blank=True`
- `related_name='design_assignments'`
- `on_delete=models.SET_NULL`

Note the shape: **three** fields, not one — who, who by, and when. A QC assignment
mirroring this pattern would be three fields, not one.

**The view**, `projects/design_views.py:681-712`:

```python
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
```

**Its permission gate:** `user_has_design_head_authority(request.user)` — Head **or named
deputy**. Note this is the *wide* helper: a deputy can allocate designers today.

**Who may be selected**, `projects/design_views.py:670-678`:

```python
def _resolve_designer(raw_id):
    """A designer must hold role='Design' and be active. Raises ValueError otherwise."""
    if not (raw_id or '').strip():
        raise ValueError('Please choose a designer.')
    try:
        return UserProfile.objects.select_related('user').get(
            pk=raw_id, role='Design', is_active=True)
    except (UserProfile.DoesNotExist, ValueError):
        raise ValueError('Selected user is not an active Design user.')
```

**The template control**, `projects/templates/projects/design/head_sites.html:249-268`:

```html
          {% if row.allocatable %}
          <tr class="collapse" id="al{{ row.site.pk }}">
            <td colspan="7" class="bg-light">
              <form method="post" action="{% url 'design_allocate' project_id=row.site.project_id %}"
                    class="d-flex flex-wrap align-items-center gap-2 py-2">
                {% csrf_token %}
                <select name="designer_id" class="form-select form-select-sm" style="max-width:16rem;" required>
                  <option value="">Choose a designer…</option>
                  {% for d in designers %}
                    <option value="{{ d.pk }}"
                      {% if row.assignment.assigned_to_id == d.pk %}selected{% endif %}>
                      {{ d.user.get_full_name|default:d.user.username }}
                    </option>
                  {% endfor %}
                </select>
                <button class="btn btn-sm btn-primary">Allocate</button>
              </form>
            </td>
          </tr>
          {% endif %}
```

**What else happens at allocation** — `_allocate_one`, `projects/design_views.py:583-667`.
Preconditions first:

```python
    if not assignment.survey_ready:
        raise ValueError('cannot be allocated before its survey is uploaded')
    if assignment.status == DESIGN_SURVEY_RETURNED:
        raise ValueError('is on Design Hold over an inadequate survey — '
                         'upload a replacement first')
    if assignment.status not in REALLOCATABLE_STATUSES:
        # Reallocation after work has started is out of scope for Part 2.
        raise ValueError(
            f'has already started design work (status {assignment.status}); '
            f'reallocation at this stage is not supported yet')
```

Then five distinct effects:

```python
    now = timezone.now()
    allocated_on = allocated_on or timezone.localdate(now)
    due = design_due_date(allocated_on)

    previous = assignment.assigned_to
    assignment.assigned_to = designer
    assignment.assigned_by = actor
    assignment.assigned_at = now
    assignment.status = DESIGN_IN_DESIGN
    assignment.save()

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

    if previous and previous.pk != designer.pk:
        detail = (f'Site reallocated from {previous.user.get_full_name() or previous.user.username} '
                  f'to {designer.user.get_full_name() or designer.user.username}')
        code = 'design_reallocated'
    else:
        detail = f'Site allocated to {designer.user.get_full_name() or designer.user.username}'
        code = 'design_allocated'
    log_activity(assignment.project, actor, f'{detail}; due {due}',
                 entity_type='DesignAssignment', entity_id=assignment.pk, action_code=code)
    return due
```

So: **due date calculation** — yes, computed and auto-approved as a `DueDateCommitment`.
**Status transition** — yes, `→ DESIGN_IN_DESIGN`. **Activity log** — yes, with
`action_code` `design_allocated` or `design_reallocated`. **Notification** — **no**. There
is none, at allocation or anywhere in the module (see 4.3).

**Most of this does not transfer to QC.** A QC assignment has no due date to compute, no
status transition to make (the site's status is driven by the designer's progress, not by
who will review it), and — unlike designer allocation — must not stamp
`Project.assigned_design`. What transfers is the FK triple, the log line, and the Head-only
gate.

### 2.2 — Is there room on `DesignAssignment`?

Full field list, `projects/models.py:2068-2164`. Survey (file):

```python
    project = models.OneToOneField(
        Project, on_delete=models.CASCADE, related_name='design_assignment',
    )

    # ── Survey (file only) ──────────────────────────────────────────────────
    survey_file_bucket = models.CharField(max_length=100, blank=True, default='')
    survey_file_path   = models.CharField(max_length=500, blank=True, default='')
    survey_uploaded_by = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='uploaded_design_surveys',
    )
    survey_uploaded_at = models.DateTimeField(null=True, blank=True)
    # Designer returns an inadequate survey; reason is free text.
    survey_returned_at   = models.DateTimeField(null=True, blank=True)
    survey_returned_by   = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='returned_design_surveys',
    )
    survey_return_reason = models.TextField(blank=True, default='')
```

Survey (link):

```python
    survey_folder_url    = models.URLField(max_length=1000, blank=True, default='')
    survey_link_added_by = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='linked_design_surveys',
    )
    survey_link_added_at = models.DateTimeField(null=True, blank=True)
```

Allocation (quoted in full at 2.1), then:

```python
    # 0 until the first attempt is opened; mirrors the highest DesignAttempt.attempt_number.
    # Maintained by the transition logic in a later part, NOT by this model.
    current_attempt_number = models.PositiveIntegerField(default=0)

    status = models.CharField(
        max_length=30, choices=DESIGN_ASSIGNMENT_STATUS_CHOICES,
        default=DESIGN_AWAITING_SURVEY,
    )

    # ── Release ─────────────────────────────────────────────────────────────
    released_at = models.DateTimeField(null=True, blank=True)
    released_by = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='released_design_assignments',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
```

`Meta` in full, `projects/models.py:2166-2170`:

```python
    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Design — {self.project.project_id} ({self.status})"
```

**`Meta` contains `ordering` and nothing else. There are no constraints, no indexes, no
`unique_together` on this model.**

**Would a nullable FK to `UserProfile` collide with anything?** **No.** Five nullable
`UserProfile` FKs already coexist on this model with the identical signature
(`null=True, blank=True, on_delete=models.SET_NULL`). The only uniqueness requirement is
`related_name`, and the six already taken are `uploaded_design_surveys`,
`returned_design_surveys`, `linked_design_surveys`, `design_assignments`,
`made_design_assignments`, `released_design_assignments` — a new name such as
`qc_design_assignments` is free. There is no `CheckConstraint` on this model for a new
field to contradict, in contrast with `DesignAttempt`, which does carry them
(`projects/models.py:2374-2375`).

### 2.3 — The Head's screen

**CONFLICT with the prompt.** The prompt names `tender_dashboard.html`. Designer
allocation is **not** offered there. It is offered on
`projects/templates/projects/design/head_sites.html`, rendered by `design_head_sites`
(`projects/design_views.py:263-308`, route `programs/<int:pk>/design/`, name
`design_head_sites`). `design_tender_dashboard` is a separate Part 4/5 metrics screen at
`programs/<int:pk>/design/dashboard/`.

The per-site control is quoted in full at 2.1. Its structure for the purposes of this
question:

```html
          {% if row.allocatable %}
          <tr class="collapse" id="al{{ row.site.pk }}">
            <td colspan="7" class="bg-light">
              <form method="post" action="{% url 'design_allocate' project_id=row.site.project_id %}"
                    class="d-flex flex-wrap align-items-center gap-2 py-2">
```

**Is there room for a second selector?** Yes, without restructuring. The control is a
**collapsed full-width row** (`colspan="7"`) containing a `d-flex flex-wrap
align-items-center gap-2` form. A second `<select style="max-width:16rem;">` drops into
that flex container and wraps on narrow viewports on its own. No table column is added and
no cell layout changes.

**But the gating condition is the real constraint, and it is a finding, not a detail.**
The block renders only `{% if row.allocatable %}`, and `allocatable` is computed at
`projects/design_views.py:296-297`:

```python
            'allocatable':   bool(assignment and assignment.survey_ready
                                  and assignment.status in REALLOCATABLE_STATUSES),
```

`REALLOCATABLE_STATUSES` is what `_allocate_one` also enforces server-side
(`projects/design_views.py:625-629`), and it excludes every status from `in_design`
onward — the comment says so: *"Reallocation after work has started is out of scope for
Part 2."*

**Consequence for this feature: a QC selector placed inside this existing control would be
available only before design work starts, and would disappear from the screen for the
entire period the site is actually in or approaching QC.** Assigning QC at the same moment
as the designer is possible; assigning or changing it once the site reaches
`artifacts_uploaded` or `in_qc` is not, through this control. That is a layout question
only in appearance; underneath it is question 3.2.

### 2.4 — Bulk allocate

`design_bulk_allocate`, `projects/design_views.py:715-768`, route
`programs/<int:pk>/design/allocate/` (`projects/urls.py:54`).

```python
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
```

The validation, and exactly what it refuses on:

```python
    try:
        designer = _resolve_designer(request.POST.get('designer_id', ''))
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect('design_head_sites', pk=program.pk)

    project_ids = request.POST.getlist('site_ids')
    if not project_ids:
        messages.error(request, 'Select at least one site to allocate.')
        return redirect('design_head_sites', pk=program.pk)
```

```python
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
```

**What its current validation refuses on**, in order:

1. designer id absent → `'Please choose a designer.'`
2. designer not `role='Design'` **and** `is_active=True` → `'Selected user is not an active Design user.'`
3. no `site_ids` posted → `'Select at least one site to allocate.'`
4. any site not in this program / deleted / not OPEX → 404 (aborts the batch)
5. any site with no `DesignAssignment`, or `survey_ready` false → `ValueError`, batch aborts
6. inside `_allocate_one`: on Design Hold (`DESIGN_SURVEY_RETURNED`), or status not in `REALLOCATABLE_STATUSES` → `ValueError`, batch aborts

All-or-nothing is a single `transaction.atomic()` wrapping the loop, with any `ValueError`
rolling the whole thing back.

**Could QC assignment ride along?** Mechanically yes — the form control is
`projects/templates/projects/design/head_sites.html:47-66`, a detached `<form
id="bulkAllocateForm">` with inputs bound by `form="bulkAllocateForm"`:

```html
<form method="post" action="{% url 'design_bulk_allocate' pk=program.pk %}" id="bulkAllocateForm">
  {% csrf_token %}
</form>
```

```html
    <select name="designer_id" form="bulkAllocateForm"
            class="form-select form-select-sm" style="max-width:16rem;">
      <option value="">Choose a designer…</option>
      {% for d in designers %}
        <option value="{{ d.pk }}">{{ d.user.get_full_name|default:d.user.username }}</option>
      {% endfor %}
    </select>
```

A second `<select name="qc_id" form="bulkAllocateForm">` needs no restructuring. **The
substantive question it raises is one bulk assignment applied to many sites.** One
designer across a batch is coherent. One QC reviewer across a batch is coherent *only*
where no site in the batch is allocated to that same person as designer — and since the
bulk path allocates a single designer to every site in the batch, picking that same person
as QC would fail on every row at once, or on none. With a QC pool of two and an overlap of
one, the batch is legal only when the QC picked is not the designer picked. Whether that
should abort the batch (consistent with all-or-nothing) or be refused at selection time is
a design decision this audit does not make.

---

## PART 3 — THE FOUR QUESTIONS THAT DECIDE THE DESIGN

**Two of this prompt's stated premises are contradicted by production.** Both are recorded
under CONFLICTS and both change the shape of the problem. Read 3.3 and 3.4 with that in
mind.

### 3.1 — Must "unassigned" remain valid?

**`DesignAssignment` rows by status `[PRODUCTION]`** — 87 rows total:

```
  arka_submitted           1
  artifacts_uploaded       2
  awaiting_allocation     82
  in_design                1
  in_qc                    1
```

**Rows at or past `in_qc` `[PRODUCTION]`** — counting `in_qc`, `awaiting_head_qc` and
`released`:

```
count: 1
```

**That single row is `MB0141`, and it is exactly what the prompt expected `[PRODUCTION]`:**

```
  project_id     : MB0141
  status         : in_qc
  assigned_to    : anilgupta
  current_attempt: 1
  released_at    : None
    attempt 1: qc_verdict=pending qc_started_at=2026-08-06 12:15:09.573628+00:00 qc_reviewed_by=None head_verdict=pending closed_at=None
```

**No gate-1 verdict has ever been recorded on production `[PRODUCTION]`:**

```
total attempts: 4
  qc_verdict=pending    4
attempts with qc_reviewed_by set: 0
```

So of the prompt's phrase "a live gate-1 decision pending or recorded": **one pending, zero
recorded, ever.**

**The number that actually sizes the blast radius is 4, not 1.** "At or past `in_qc`" is
the wrong cut for this feature, because gate 1 also owes a verdict on Arkas and on
completed packages that have not been picked up yet. The statuses where a gate-1 verdict
is currently owed are `arka_submitted` (1) + `artifacts_uploaded` (2) + `in_qc` (1) =
**4 live sites**. The other 83 rows are at `awaiting_allocation` (82) or `in_design` (1)
and have no gate-1 decision in front of them.

**Code side — what would happen if the field were null at verdict time, under each
reading.**

The single point of decision is `user_can_qc_gate_design`
(`projects/permissions.py:513-527`, quoted at 1.2). Today it has two terms. Adding
per-site scoping means adding a third, and the null case is decided entirely by how that
third term is written.

*Open pool falls back* — the null value means "nobody assigned, so anybody flagged may
review". The added term is satisfied when the field is null:

```
    assignment.qc_assigned_to_id is None or assignment.qc_assigned_to_id == profile.pk
```

Under this reading every existing row — whatever its status, whatever its stage —
continues to behave exactly as it does today, and no backfill is needed. The field is
nullable, the migration is a pure `AddField`, and sites already at or past `in_qc` are
untouched. `_qc_guard` and the four Arka endpoints all inherit the change with no edit,
because they all route through the predicate.

*Verdict refused* — the null value means "no reviewer assigned, so no verdict may be
recorded". The added term:

```
    assignment.qc_assigned_to_id == profile.pk
```

Under this reading **every `DesignAssignment` row that currently exists becomes
unreviewable at gate 1 the moment the code deploys**, because every row's new field is
null. Any site already sitting at `artifacts_uploaded`, `in_qc`, `arka_submitted` or
`awaiting_head_arka` stalls until somebody assigns a QC user to it.

**Measured, that stall is 4 sites `[PRODUCTION]`** — the three at `arka_submitted` /
`artifacts_uploaded` plus `MB0141` at `in_qc`. That is small enough to clear by hand, which
materially weakens the case against the strict reading: the migration-day cost is four
assignments, not eighty-seven. Two things qualify it. First, this reading interacts with
2.3 — the existing allocation control is hidden for exactly those statuses, so the Head
would have **no UI route** to unstick them without new UI built for the purpose. Second,
`MB0141` is mid-review with `qc_started_at` already stamped (3.2), so clearing it by
assignment means naming a reviewer for a review that is already under way.

There is a third position the prompt does not name but the code makes available: refuse
the verdict when the field is set and does not match, fall back to the open pool when it
is null. That is the "open pool falls back" branch above; it differs only in framing.

### 3.2 — Can assignment change mid-review?

**Yes, there is a partially-recorded state, the model already names it, and one live
production site is sitting in it right now.**

`[PRODUCTION]` — attempts with `qc_started_at` set, `qc_verdict` still pending and not
closed:

```
count: 1
  MB0141 attempt 1 qc_started_at=2026-08-06 12:15:09.573628+00:00 status=in_qc
```

That review has been open since **6 August 2026** — eight days at the time of this audit —
with no verdict and, per 3.1, no `qc_reviewed_by` recorded. This is not a hypothetical
state to design around; it is the state of the only site currently in QC.

`qc_started_at` exists and is stamped on the **attempt**, not the assignment.
`projects/design_views.py:2437-2481`:

```python
def design_qc_start(request, project_id):
    """GATE 1 — a DESIGN QC reviewer takes a completed package into review.

    `artifacts_uploaded` -> `in_qc`, and `qc_started_at` is stamped on the attempt.

    STAMPING qc_started_at IS WHAT OPENS THE PM CHANGE REQUEST WINDOW (settled
    decision 3). Before this moment a PM asking for a change is a conversation; after
    it, it is a system action that suspends this review and goes to the Design Head, who
    decides whether it opens a new attempt (Part 4.6).
```

```python
    profile = request.user.profile
    with transaction.atomic():
        attempt.qc_started_at = timezone.now()
        attempt.save(update_fields=['qc_started_at'])
        assignment.status = DESIGN_IN_QC
        assignment.save(update_fields=['status', 'updated_at'])
```

**What makes a reassignment ambiguous, from the code:**

1. **`qc_started_at` is set but `qc_reviewed_by` is not.** That is the in-progress state,
   and it is a real, persisted, observable condition — `status == in_qc`, `qc_verdict ==
   pending`. A reassignment during it means the person who started the review is not the
   person recorded as having done it. Nothing in the model records "who started".
   `design_qc_start` stamps a **timestamp only** — it writes no actor. So after a
   reassignment there would be no way to reconstruct who had been holding the review.
2. **`qc_started_at` opened the PM change-request window**, and that window's state is
   keyed off the timestamp, not off any reviewer identity — `projects/design_views.py:3702`:

   ```python
       window_open = bool(attempt and attempt.qc_started_at
                          and assignment.status in allowed_statuses
                          and not group_locked)
   ```

   Reassignment does not disturb this, which is a point in its favour.
3. **The Arka gate has no start step at all.** There is no `arka_qc_started_at`. Gate 1 on
   an Arka goes straight from `arka_submitted` to a verdict, so "mid-review" for an Arka
   is not a state the system can see. A reassignment between Arka submission and Arka
   verdict is invisible and unambiguous — there is nothing partial to be ambiguous about.
4. **A gate-1 failure closes the attempt and opens N+1.** So "mid-review" has a hard
   upper boundary: it ends at the verdict, one way or the other. There is no long-lived
   partially-decided row.

`assigned_at` (`projects/models.py:2145`) is the precedent for stamping when an assignment
was made; there is no equivalent "reassigned from" field — reallocation of a designer is
captured only in the activity log, via the `design_reallocated` action code
(`projects/design_views.py:658-666`, quoted at 2.1).

### 3.3 — The mahwar problem

**Every `is_design_qc` holder `[PRODUCTION]`, with role, head flag, and designer load by
status:**

```
  username='mahwar'   pk=5 role='Design' is_design_head=False is_active=True deputy_of=[]
      as designer on 2 DesignAssignment row(s):
        arka_submitted           1
        in_design                1
      sites: [('MB0005', 'arka_submitted'), ('TESTTENDER26-MB010', 'in_design')]
  username='priyanka' pk=6 role='Design' is_design_head=False is_active=True deputy_of=[]
      as designer on 0 DesignAssignment row(s):
      sites: []
```

Exactly two holders, as the prompt says. `mahwar` holds two sites as designer, as the
prompt says. Neither holds `is_design_head`; neither is anyone's deputy.

**The prompt's conclusion from those facts does not survive contact with the data.** It
states: *"priyanka is currently the only legal QC for mahwar's sites"* — true, and
unremarkable, since those are `MB0005` and `TESTTENDER26-MB010`. But the site the prompt
is worried about, `MB0141`, **is not one of mahwar's sites**. Its designer is `anilgupta`
(3.1), who holds neither flag `[PRODUCTION]`:

```
  'anilgupta' pk=31 qc=False head=False
```

So **both** QC holders are legal reviewers on `MB0141`, the only site in QC. The
designer-exclusion collision the prompt frames as the live constraint is not live on the
site it names. It is live on mahwar's own two sites, neither of which has reached gate 1
yet — `MB0005` is at `arka_submitted` (gate 1 owes an Arka verdict, so it *is* live there)
and `TESTTENDER26-MB010` is still `in_design`.

Full active Design roster `[PRODUCTION]`, for pool arithmetic:

```
  'shyam' pk=19 qc=False head=False
  'sudhanshu' pk=30 qc=False head=False
  'mahwar' pk=5 qc=True head=False
  'suvajit' pk=29 qc=False head=False
  'praveen' pk=17 qc=False head=True
  'anilgupta' pk=31 qc=False head=False
  'aman' pk=51 qc=False head=False
  'priyanka' pk=6 qc=True head=False
  'praveenkumar' pk=43 qc=False head=False
```

Nine active Design users; two hold QC; one holds Head; **no user holds both**.

**Code side — if the Head assigned mahwar as QC on her own site, would anything stop him
today?**

**Nothing would stop the assignment, and the code contains no check that could.** The
reason is categorical: **there is no QC assignment field, no QC assignment view, and no QC
assignment form.** There is nothing to run a check in. The question can only be answered
about what exists, and what exists is the runtime predicate — which produces a specific
and important failure mode:

`user_can_qc_gate_design` (`projects/permissions.py:513-527`) would still refuse her at
verdict time, because its third line is unchanged and unconditional:

```python
    return not user_is_assigned_designer(user, assignment)
```

So under any of the Part 3.1 readings, assigning the site's own designer as its QC
reviewer produces a site that is **assigned to a reviewer who is permanently barred from
reviewing it**. Under "open pool falls back" the site remains reviewable by the other QC
holder, and the assignment is merely wrong. Under "verdict refused" the site becomes
**unreviewable by anybody** — the assigned reviewer is refused by the designer exclusion,
and everyone else is refused by the assignment mismatch. That is a deadlock reachable in
one click, with no code path to escape it except reassignment.

The nearest existing precedent for validating an assignment target is `_resolve_designer`
(`projects/design_views.py:670-678`, quoted at 2.1), and it checks **only** `role='Design'`
and `is_active=True`. It performs no self-review check, because for designer allocation
there is no self to exclude. A QC equivalent would be the first assignment-time validator
in this module that needs to consult the assignment it is being attached to.

### 3.4 — Deputy overlap

**`[PRODUCTION]` — there is no deputy. The scenario this question asks about is currently
unreachable, for want of the person it depends on.**

Every `is_design_head` holder, with their named deputy:

```
  head='praveen' pk=17 role='Design' is_design_qc=False is_active=True
      deputy=None
```

One Head. `design_head_deputy` is **null**. He does **not** hold `is_design_qc`, so he is a
gate-2-only reviewer. And per 3.3, neither QC holder is anyone's deputy (`deputy_of=[]` on
both).

So the specific overlap this question posits — *"if the deputy can record a gate-2 verdict
and is also assignable as gate-1 QC"* — has no subject on production today. It becomes
reachable the moment `praveen` names anyone, and the code places no restriction on who
that may be.

**Code side — is the state reachable in principle?** Yes, and the code makes it reachable
in more than one way.

The premise the prompt states is correct as far as it goes: a deputy can record a gate-2
verdict (1.5), and a deputy is a plain FK to *any* `UserProfile`
(`projects/models.py:561-566`) — there is no restriction on who may be named. The Part 10
comment says this explicitly, `projects/design_views.py:4318-4320`:

```python
# for named designers, and an overturn rate for named QC reviewers. The deputy field is a
# plain FK to any UserProfile — a Head can and often would name a senior DESIGNER as
# deputy so review queues keep moving during an absence. That is the right person to clear
```

The reachable barred states, from the predicates as written:

1. **Designer exclusion, both gates.** `user_is_assigned_designer` is the last term of
   *both* `user_can_qc_gate_design` and `user_can_qc_design`. A person who is the site's
   designer is barred at gate 1 **and** gate 2, today, with no assignment field needed.
   If that person is also the only deputy and one of two QC holders, the site needs
   someone else at both gates.
2. **One-person-two-verdicts, per artifact.** `_other_gate_actor_conflict` (1.4) bars the
   gate-1 actor from gate 2 **on that same row**. With a QC pool of two where one is the
   designer, gate 1 must fall to the other; if that other person is also the Head or the
   deputy, they are then barred from gate 2 on that row and the site needs a third person
   who may not exist.
3. **Adding gate-1 assignment adds a fourth constraint on top of those three.** Under the
   "verdict refused" reading of 3.1, the assigned QC user is the *only* person who may
   record gate 1 — so if that person is barred by (1) or becomes barred by (2), the site
   has no legal reviewer at all until someone reassigns it.

**With how many people? Measured `[PRODUCTION]`: the deadlock is one deputy-naming away,
and on two named sites.**

The barred state is reached when the set of `is_design_qc` holders who are not the site's
designer, minus those who already ruled on the other gate of that artifact, is empty.
Working it through on the live data:

| Site | Designer | Legal gate-1 QC today | Gate-2 (Head) |
|---|---|---|---|
| `MB0141` (`in_qc`) | anilgupta | mahwar **and** priyanka | praveen |
| `MB0005` (`arka_submitted`) | mahwar | priyanka only | praveen |
| `TESTTENDER26-MB010` (`in_design`) | mahwar | priyanka only | praveen |

No site is deadlocked right now, and `_other_gate_actor_conflict` cannot bite anywhere,
because zero gate-1 verdicts have ever been recorded (3.1) and no person holds both flags
(3.3).

**The reachable deadlock, precisely.** If `praveen` names `priyanka` as deputy — an
entirely natural choice, she is a QC holder with no designer load — then on `MB0005` and
`TESTTENDER26-MB010`: mahwar is barred at gate 1 as the designer, leaving priyanka as the
only legal gate-1 reviewer; priyanka records gate 1; `_other_gate_actor_conflict` then bars
her from gate 2 on that same artifact; and gate 2 falls to praveen, who is the only other
person with head authority. That works — but it works only because praveen is available. It
fails the moment he is the one absent, which is the situation deputies exist for. **Naming
the QC holder as deputy is the one choice that makes the deputy useless on exactly the
sites where the pool is already thinnest.**

Adding per-site gate-1 assignment under the strict reading of 3.1 adds a fourth
constraint on top: the assigned reviewer becomes the only legal one, so if they are barred
by the designer exclusion or by having ruled at the other gate, the site has no legal
reviewer at all until someone reassigns it.

---

## PART 4 — BLAST RADIUS

### 4.1 — Everything that reads `is_design_qc`

Complete list, application code and templates. Test files are listed separately at the
end because they are not blast radius in the same sense — they are the safety net that
would catch a regression.

| File:line | What it does |
|---|---|
| `projects/models.py:556` | `is_design_qc = models.BooleanField(default=False)` — the field |
| `projects/migrations/0055_part9_design_qc_gate.py:160` | the `AddField` that created it |
| `projects/permissions.py:497-510` | `user_is_design_qc()` — the single reader of the flag |
| `projects/permissions.py:525` | gate-1 predicate `user_can_qc_gate_design()` — **the one that would gain a per-site term** |
| `projects/permissions.py:269` | BOQ read branch — a QC reviewer may read a BOQ they must review |
| `projects/permissions.py:552` | `user_can_view_design_qc_dashboard()` — read gate for queue + dashboard |
| `projects/permissions.py:610` | `user_can_view_design()` — lets a QC reviewer open the CAD/Arka they are reviewing |
| `projects/design_views.py:81` | import |
| `projects/design_views.py:1550` | `ctx['is_design_qc'] = can_qc_gate` — Arka screen context |
| `projects/design_views.py:2398` | docstring naming the predicate `_qc_guard` selects |
| `projects/design_views.py:3246` | `'is_design_qc': user_is_design_qc(request.user)` — QC queue context |
| `projects/design_views.py:3501-3507` | `design_qc_dashboard_counts()` returns `None` for a non-holder |
| `projects/forms.py:158` | `is_design_qc = forms.BooleanField(...)` — admin user form |
| `projects/views.py:1042` | comment on dual-flag holders in dashboard assembly |
| `projects/views.py:8310` | `profile.is_design_qc = cd['is_design_qc']` — write path 1 |
| `projects/views.py:8341` | `'is_design_qc': profile.is_design_qc` — form initial |
| `projects/views.py:9383` | `target_user.profile.is_design_qc = request.POST.get('is_design_qc') == 'on'` — write path 2 |
| `projects/admin.py:155` | Django admin `list_display` |
| `projects/admin.py:156` | Django admin `list_filter` |
| `projects/templates/projects/admin/user_edit.html:102-105` | the checkbox |
| `projects/templates/projects/subadmin/departments.html:128` | passes the flag into the Alpine edit modal |
| `projects/templates/projects/subadmin/departments.html:299, 303` | the checkbox and its unchecked-posts-nothing note |
| `projects/templates/projects/design/qc_queue.html:35` | `{% if is_design_qc %}` — section visibility |
| `projects/templates/dashboard/design.html:238` | comment explaining the counts are holder-only |

Tests that construct QC users: `projects/tests_design_part9.py:52-76, 276`,
`tests_design_part10.py:57-87`, `tests_design_part11.py:81-113`,
`tests_design_part46.py:48-66`.

**What an assignment field would have to be reconciled against, from that list.** Only
`projects/permissions.py:525` decides gate-1 authority; every verdict endpoint routes
through it, so the authority change is one function. The **read** gates
(`permissions.py:552, 610, 269`) are a separate decision the prompt does not raise: today a
QC holder can open every site's package because the read gate is flag-based. If gate-1
authority becomes per-site but read access stays flag-wide, an unassigned QC holder keeps
full visibility of work they can no longer act on. That is defensible — reviewers benefit
from seeing the queue — but it is a decision, and leaving it unmade means it gets made by
default.

### 4.2 — Part 10 analytics

**QC identity does appear, and it is attributed to the actor who recorded the verdict, not
to any assignee.** `projects/design_analytics.py:973-1028`:

```python
def m_overturn_rate(data):
    """Head verdict differing from Design QC's, per QC REVIEWER and overall.

    `head_overturned_qc` is a stored boolean on both DesignAttempt and ArkaSubmission,
    written at verdict time precisely so this is countable rather than reconstructed. ...
    """
    per = {}
    team_num = team_den = 0
    for t in data['attempts']:
        if t.head_verdict == QC_PENDING:
            continue
        row = _bucket(per, t.qc_reviewed_by,
                      {'overturned': 0, 'reviewed': 0,
                       'pkg_overturned': 0, 'pkg_reviewed': 0,
                       'arka_overturned': 0, 'arka_reviewed': 0})
```

```python
    for k in data['arkas']:
        if k.head_verdict == ARKA_PENDING:
            continue
        row = _bucket(per, k.reviewed_by,
```

The grouping keys are `t.qc_reviewed_by` (`DesignAttempt`) and `k.reviewed_by`
(`ArkaSubmission`) — both written at verdict time by the endpoints quoted in Part 1. The
data is loaded with those relations selected, `projects/design_analytics.py:368, 375`:

```python
        .select_related('qc_reviewed_by__user', 'head_reviewed_by__user')
```

```python
        .select_related('reviewed_by__user')
```

**Would an assignment field change what these numbers mean?** Not on its own. The metric
counts who *did* the review; an assignment field would record who was *meant to*. The two
diverge exactly when a site is reviewed by someone other than its assignee — which is
possible under the "open pool falls back" reading of 3.1 and impossible under "verdict
refused". So the metric stays correct in both cases; what changes is that under the
fallback reading a new question becomes askable and unanswered ("how often did the
assignee not do it"), and answering it would need the assignment field added to the
analytics data load, which it is not today.

One adjacent note, `projects/design_views.py:4315-4327`: the analytics screen is gated on
`user_is_design_head()` — the **narrow** helper — deliberately excluding the deputy and
Design QC. So no QC holder sees these figures today, assigned or otherwise.

### 4.3 — Notifications

**Nothing notifies QC users. Nothing in the design module notifies anybody.**

`projects/notifications.py` grepped case-insensitively for `design|arka|qc`:

```
No matches found
```

`projects/design_views.py` imports (lines 1-100) contain no notifications import:

```
from .decorators import login_required
from .design_analytics import (
from .design_metrics import (
from .utils import design_due_date
from .design_storage import (
from .models import (
from .permissions import (
```

There is no `NotificationLog` write, no email and no WhatsApp send anywhere in the design
module. Every state change is recorded by `log_activity(...)` with an `action_code` and
nothing else — e.g. `projects/design_views.py:2544-2548`:

```python
        log_activity(project, profile,
                     f'Design QC passed attempt {attempt.attempt_number} — awaiting '
                     f'Design Head review',
                     entity_type='DesignAttempt', entity_id=attempt.pk,
                     action_code='design_qc_passed')
```

**So gate-1 work is discovered entirely by polling.** The two surfaces are the queue
(4.4) and the dashboard count strip, `projects/design_views.py:3493-3523`:

```python
def design_qc_dashboard_counts(user):
    """The two queue sizes a DESIGN QC reviewer can see for free — one COUNT each.

    THE GATE-1 COUNTERPART OF design_head_dashboard_counts(), and it exists for the reason
    Part 4.5 gives for that one: a screen that is URL-reachable only is unusable, because
    nobody types a URL. ...
    """
    if not user_is_design_qc(user):
        return None
```

**Why this matters before the feature is built, stated plainly.** With an open pool and no
notifications, two reviewers share one visible queue and whoever looks first takes the
work — polling is adequate because the queue is common property. Per-site assignment
changes that: work becomes *owed by a named person*, and a named person who does not
happen to open the dashboard has no way to learn that a site is waiting on them
specifically. Assignment without notification converts a self-balancing pool into a set of
private queues nobody is told about. That is not an argument against the feature; it is a
dependency on Part 7 that the "small" sizing does not appear to include.

### 4.4 — The QC dashboard

**Yes — `design_qc_queue`, `projects/design_views.py:3138-3248`, route `design/qc/`. It is
NOT scoped per user. It shows the whole pool.**

```python
def design_qc_queue(request):
    """The review worklist for BOTH artifacts and BOTH gates.
    ...
    PART 9 — ONE QUEUE, TWO AUDIENCES, TWO ARTIFACTS.

    Design QC and the Head share this screen and see the same rows, because knowing what is
    stacked up at the other gate is exactly the information a reviewer needs. What differs
    is which row each can ACT on, and that is computed PER ROW because both the self-review
    exclusion and the one-person-two-verdicts rule are per site.
```

The two querysets. Arkas, `projects/design_views.py:3169-3174`:

```python
    arka_assignments = (DesignAssignment.objects
                        .filter(status__in=(DESIGN_ARKA_SUBMITTED,
                                            DESIGN_AWAITING_HEAD_ARKA),
                                project__is_deleted=False)
                        .select_related('project', 'project__program', 'assigned_to__user')
                        .order_by('status', 'project__project_id'))
```

Packages, `projects/design_views.py:3208-3213`:

```python
    assignments = (DesignAssignment.objects
                   .filter(status__in=(DESIGN_ARTIFACTS_UPLOADED, DESIGN_IN_QC,
                                       DESIGN_AWAITING_HEAD_QC),
                           project__is_deleted=False)
                   .select_related('project', 'project__program', 'assigned_to__user')
                   .order_by('status', 'project__project_id'))
```

**Neither queryset filters by the requesting user.** The only user-dependent terms are
computed **per row, after fetching**, and they decide button visibility, not row
visibility — `projects/design_views.py:3216-3240`:

```python
    for assignment in assignments:
        attempt = _current_attempt(assignment)
        awaiting_head = assignment.status == DESIGN_AWAITING_HEAD_QC
        can_qc_gate   = user_can_qc_gate_design(request.user, assignment)
        can_head_gate = user_can_head_gate_design(request.user, assignment)
        rows.append({
            ...
            'can_qc':      can_qc_gate and not awaiting_head,
```

The dashboard counts (4.3, `projects/design_views.py:3509-3516`) are equally global:

```python
    base = DesignAssignment.objects.filter(project__is_deleted=False)
    return {
        'awaiting_arka': base.filter(status=DESIGN_ARKA_SUBMITTED,
                                     ...
        'awaiting_qc':   base.filter(status=DESIGN_ARTIFACTS_UPLOADED).count(),
        'in_qc':         base.filter(status=DESIGN_IN_QC).count(),
```

So today every QC holder sees every reviewable site and a count of all of them. If
assignment lands and these surfaces are not touched, a reviewer's dashboard would report a
number that includes sites assigned to their colleague — the count and the actionable work
would stop agreeing. The queue's per-row `can_qc` would go false for unassigned rows under
the "verdict refused" reading, which degrades gracefully; the **counts** have no per-row
equivalent and would simply be wrong.

---

## UNCERTAIN

1. **Whether Sessions A1, A2 and A3 are present in the log.** No commit subject carries a
   session identifier and no mapping exists in the repo that this audit could check.
   Neither asserted nor denied.
2. **Whether read access should follow write access.** Noted at 4.1 as an unmade decision,
   not a finding — nothing in the code decides it because the question does not arise until
   assignment exists.
3. **Who is actually reviewing `MB0141`.** The row records `qc_started_at` but no actor
   (3.2) — `design_qc_start` stamps a timestamp and writes no `qc_started_by`. So which of
   the two QC holders took that review on 6 August cannot be recovered from the database.
   It is knowable only by asking them. This is the concrete instance of the gap 3.2
   describes, and it is the reason a QC assignment field would have retrospective value
   beyond routing.
4. **Why `MB0141` has been open eight days.** The audit establishes the state, not the
   cause. Whether it is waiting on a person, a decision, or has simply been forgotten is
   not a database question — but it is the single best piece of evidence for or against the
   claim that an open pool self-balances.
5. **Whether the 82 `awaiting_allocation` rows are live work or test residue.** One site is
   named `TESTTENDER26-MB010`, which suggests at least some test data is mixed into the
   87. This does not affect any Part 3 answer — all four gate-1-relevant sites were
   inspected individually — but it means the raw total should not be quoted as a measure of
   real workload.

---

## CONFLICTS

Places where this prompt's assumptions disagree with the code.

1. **"Quote the relevant block of `tender_dashboard.html` (or wherever designer allocation
   is offered)"** — item 2.3. Designer allocation is **not** offered on a template of that
   name. It is on `projects/templates/projects/design/head_sites.html`, rendered by
   `design_head_sites` at route `programs/<int:pk>/design/`. `design_tender_dashboard` is a
   different screen (`programs/<int:pk>/design/dashboard/`) and carries no allocation
   control. The prompt's parenthetical anticipated this, and it was needed.

2. **"Today `is_design_qc` is a boolean flag on `UserProfile` and gate 1 is served by an
   open pool: any QC-flagged user can record the verdict on any site."** — nearly right,
   and the exception is load-bearing for this whole feature. Any QC-flagged user can
   record the verdict on any site **except one they are the allocated designer of**
   (`projects/permissions.py:527`). The prompt states the exception correctly further down
   ("a designer can never review their own site"), so this is an internal inconsistency in
   the prompt's own framing rather than a misreading of the code — but the pool is not
   fully open, and the exception is exactly what makes the two-person pool fragile.

3. **Hard stop 4 — "Gate-1 and gate-2 verdicts turn out to share a single view with a mode
   flag, such that scoping one would scope both."** Neither half is quite the situation.
   The verdict **views** are eight separate functions (1.1). But a shared helper with a
   mode flag does exist — `_qc_guard(..., gate='qc'|'head')`,
   `projects/design_views.py:2390-2423` — used by the five package endpoints. The stop's
   *substance* ("scoping one would scope both") is false, because the flag selects a
   different predicate per gate and the gate-1 predicate can be narrowed alone. Reported
   rather than treated as a stop, since the condition that matters does not hold.

4. **Item 2.1, "The field on `DesignAssignment` that holds the designer"** — singular. It
   is three fields (`assigned_to`, `assigned_by`, `assigned_at`,
   `projects/models.py:2137-2145`). A mirroring QC assignment is three fields, not one,
   which matters for the size estimate.

5. **Item 3.1, "every existing `DesignAssignment` row needs a value or a nullable
   field"** — the disjunction is right but understates the third option the code makes
   natural: a nullable field whose null is *given a meaning* by the predicate. That is the
   "open pool falls back" reading, and it needs neither a backfill nor a non-null field.
   Reported at 3.1 without choosing between it and the alternative.

6. **Item 4.3, "Part 7 is unwritten, but report whether anything currently notifies QC
   users at all."** Correct, and stronger than implied: nothing in the design module
   notifies **anyone** — not QC, not designers, not the Head. There is no partial
   notification surface to extend.

7. **The framing that this mirrors "assigning design task as of now"** (the original
   request, quoted in the prompt). Designer allocation carries a due-date computation, a
   status transition, a `Project.assigned_design` stamp and a survey precondition (2.1),
   none of which apply to a reviewer. The parts that transfer are the FK triple, the log
   line and the Head-only gate. The request's analogy is sound as a UI analogy and
   misleading as an implementation estimate.

8. **"`mahwar` is also an active designer holding at least two sites. Since a designer can
   never review their own site, priyanka is currently the only legal QC for mahwar's
   sites."** Every clause is true, and the inference drawn from it in the prompt's framing
   is not. The prompt presents this as *"the live constraint that makes this urgent and
   fragile"* immediately after naming `MB0141` as the site in QC. But `MB0141`'s designer
   is **`anilgupta`**, not mahwar, and anilgupta holds neither flag `[PRODUCTION]` — so
   **both** QC holders are legal on the one site currently in QC (3.3). The overlap is real
   on `MB0005` and `TESTTENDER26-MB010`, mahwar's own two sites. The urgency and the site
   named are not the same case.

9. **"Part 4 introduced a deputy concept… If the deputy can record a gate-2 verdict and is
   also assignable as gate-1 QC…"** — item 3.4 presupposes a deputy exists. **There is no
   deputy on production.** `praveen` is the only `is_design_head` holder and his
   `design_head_deputy` is null; neither QC holder is anyone's deputy (3.4). The question
   is answerable in principle and the code makes the state reachable, but nothing is in
   that state today, and the fourth-constraint scenario the prompt sketches needs a
   deputy-naming to occur first.

10. **"How many are at or past `in_qc` — these have a live gate-1 decision pending or
    recorded"** — item 3.1. The cut is too narrow for what it is trying to measure. A
    gate-1 verdict is also owed at `arka_submitted` and `artifacts_uploaded`, neither of
    which is "at or past `in_qc`". The prompt's cut yields **1**; the set of sites where
    gate 1 actually owes a verdict is **4** (3.1). The second number is the one that sizes
    the strict reading's migration-day cost.

11. **"these have a live gate-1 decision pending or recorded"** — the "or recorded" half is
    empty. **Zero gate-1 verdicts have ever been recorded on production**: 4 attempts
    exist, all at `qc_verdict='pending'`, and `qc_reviewed_by` is null on every one (3.1).
    The feature would be assigning reviewers to a gate that has not yet produced a single
    decision.

---

## CLOSING TABLE

| Item | Status |
|---|---|
| 1.1 The verdict path | **ANSWERED** — 8 separate view functions across 2 artifacts + `design_qc_start`; no shared verdict view |
| 1.2 The permission gate | **ANSWERED** — `user_can_qc_gate_design`, flag + designer exclusion, no `user_can_manage_project`, no per-site scoping |
| 1.3 Designer-cannot-review-own-site | **ANSWERED** — server-side in `user_is_assigned_designer`, applied at all 9 endpoints; hard stop 3 does not fire |
| 1.4 Same-person-both-verdicts | **ANSWERED** — `_other_gate_actor_conflict`, compares actor `profile.pk` to the opposite gate's `*_reviewed_by_id` on that row |
| 1.5 The deputy | **ANSWERED** — self-FK `design_head_deputy`; gate 2 only, never gate 1 |
| 1.6 Where QC users are listed | **ANSWERED** — nowhere; no application queryset selects `is_design_qc=True` |
| 2.1 How designer allocation works | **ANSWERED** — field triple, view, gate, template, and all five allocation effects quoted |
| 2.2 Room on `DesignAssignment` | **ANSWERED** — `Meta` has `ordering` only, no constraints; nullable FK collides with nothing |
| 2.3 The Head's screen | **ANSWERED** — `head_sites.html:249-268`; room exists, but the control is hidden once work starts |
| 2.4 Bulk allocate | **ANSWERED** — located, validation enumerated, ride-along mechanically trivial and semantically not |
| 3.1 Must "unassigned" remain valid | **ANSWERED** — 87 rows, 1 at/past `in_qc` (`MB0141`), 4 where gate 1 owes a verdict, 0 verdicts ever recorded; both code readings traced |
| 3.2 Can assignment change mid-review | **ANSWERED** — `qc_started_at` exists and stamps no actor; four ambiguity sources; 1 live site in that state for 8 days |
| 3.3 The mahwar problem | **ANSWERED** — both holders listed with load; nothing in code could stop the assignment; `MB0141` is anilgupta's, not mahwar's |
| 3.4 Deputy overlap | **ANSWERED** — no deputy exists on production; reachability and the exact one-naming deadlock traced |
| 4.1 Everything that reads `is_design_qc` | **ANSWERED** — 24 application/template sites + tests, tabulated |
| 4.2 Part 10 analytics | **ANSWERED** — groups by verdict **actor**, not assignee; quoted |
| 4.3 Notifications | **ANSWERED** — none exist anywhere in the design module |
| 4.4 The QC dashboard | **ANSWERED** — `design_qc_queue`, both querysets global, not per-user |

---

## SIZE ESTIMATE

The request was sized as small by the person who raised it. That sizing is an assumption,
and this audit was asked to test it rather than accept it.

**Migration: yes, one, and it is trivial.** A pure `AddField` of nullable columns on
`DesignAssignment`. No constraint, no backfill under the fallback reading, no data
migration. `Meta` carries no constraints to reconcile (2.2). This is the cheapest part.

**Files. Minimum viable, under the "open pool falls back" reading:**

| File | Change |
|---|---|
| `projects/models.py` | 3 fields on `DesignAssignment` (`qc_assigned_to`, `qc_assigned_by`, `qc_assigned_at`) |
| `projects/migrations/0063_*.py` | generated `AddField` |
| `projects/permissions.py` | one added term in `user_can_qc_gate_design` |
| `projects/design_views.py` | new `design_assign_qc` view, a `_resolve_qc_reviewer` validator, the QC-holder queryset in `design_head_sites`, one context key |
| `projects/urls.py` | one route |
| `projects/templates/projects/design/head_sites.html` | second selector in the allocation block |

Six files plus the migration. That much is genuinely small, and it is where the "small"
sizing comes from.

**What the audit found that the sizing does not cover:**

1. **The control is hidden exactly when it is needed** (2.3). `row.allocatable` requires
   `status in REALLOCATABLE_STATUSES`, so a QC selector inside the existing block vanishes
   from `in_design` onward — the site is never assignable at or near the moment it reaches
   gate 1. Either QC is assigned at designer-allocation time and never changed, or a
   second control with its own visibility rule is needed. That is a design decision with a
   UI cost, and it is question 3.2 wearing a layout disguise.
2. **The dashboard counts go wrong silently** (4.4). `design_qc_dashboard_counts` is
   global and has no per-row equivalent to degrade through. Leaving it unchanged means a
   reviewer is shown a number that counts their colleague's work.
3. **No queryset of QC users exists** (1.6). It has to be written from nothing, and what
   it filters on — flag alone, or flag minus this site's designer — is the mahwar question
   in its constructive form.
4. **The self-assignment deadlock** (3.3). Nothing today can prevent assigning a site's
   designer as its QC reviewer, and under the strict reading that produces a site no one
   can review. A validator has to be written, and it is the first in this module that must
   consult the row it is attached to.
5. **Assignment without notification** (4.3). Nothing in the design module notifies anyone.
   Per-site assignment turns a shared queue into private queues that nobody is told about.
   Part 7 is not a nice-to-have alongside this; it is what makes assignment mean anything
   operationally.
6. **The read/write split** (4.1). Three read gates key off the flag portfolio-wide. If
   they stay flag-based, unassigned holders keep full visibility — defensible, but it must
   be decided rather than defaulted into.

**What the production data settles, and what it does not.**

The migration-day objection is **smaller than expected**. Under the strict reading, only
**4 sites** stall (3.1), not 87 — clearable by hand, if there is a control to clear them
with, which per 2.3 there is not. The null-semantics decision is therefore no longer
gated on scale; it is a straight design choice, and both readings are viable.

The **mahwar collision is not on the site the prompt named** (3.3, CONFLICTS 8), and **no
deputy exists** (3.4, CONFLICTS 9). Two of the three pressures presented as making this
urgent are not currently exerting force. The one that is real: gate 1 has produced **zero
verdicts, ever** (3.1, CONFLICTS 11), and the only site in QC has been open **eight days
with no recorded reviewer** (3.2, UNCERTAIN 3).

That last pair is the strongest argument in the audit *for* the feature, and it is not the
argument the request made. An open pool of two, on four live sites, has produced no
decisions and one eight-day-old stalled review that nobody's name is on. Whether
assignment fixes that or merely records it is a judgement about the team, not the code —
but the failure the data shows is *nobody picking work up*, which assignment addresses only
if item 5 (notification) ships with it.

**One session or more.** The six-file core is one session's work, and the two open
decisions are now decidable in a conversation rather than needing another audit:

1. **Null semantics** — fallback or strict. Both are safe at this data volume. Strict needs
   a control that works at `in_qc`, which is decision 2.
2. **Where the control lives** — inside the existing allocation block, which disappears
   once work starts (2.3), or a second control with its own visibility rule. `MB0141` is
   the worked example: it is in the state where the existing control cannot reach it.

**Realistic shape: settle those two decisions, then one session to build the six-file core
and its migration. That much is genuinely small, and the original sizing is defensible for
it.** The parts the sizing does not cover are the dashboard counts (4.4), the QC-user
queryset that does not exist (1.6), the self-assignment validator (3.3), and above all
notification (4.3) — which is a separate piece of work of its own size, and without which
per-site assignment converts a shared queue into private queues nobody is told about. Given
that the measured failure mode is already *work not being picked up*, shipping assignment
without notification risks making the observed problem worse rather than better.
