# Session A — Audit: Home/Dashboard button + remarks on Arka / CAD / BOQ

Investigate-only session, 14 August 2026. No application code written, no migration created,
no `makemigrations` run, no file modified except this report.

The two requests are reported separately throughout. They are **not** designed as one change.

---

## SESSION OPENING CHECK

### 1. Repo root

The repository is `c:\SolarPMS\Horizon-Solar-PMS`. Every git command below was run there.

### 2. `git status` — raw output

```
On branch main
Your branch is up to date with 'origin/main'.

Untracked files:
  (use "git add <file>..." to include in what will be committed)
	BLOCK0_VERIFICATION.md
	PARTS_11_4.6_10_STATUS.md

nothing added to commit but untracked files present (use "git add" to track)
```

**No tracked file is modified or staged.** Hard stop 1 does not fire.

```
$ git diff --stat          -> empty
$ git diff --cached --stat -> empty
```

Two untracked files, both `.md` reports from prior audit sessions, named as the prompt
requires: **`BLOCK0_VERIFICATION.md`** and **`PARTS_11_4.6_10_STATUS.md`**. Both are still
uncommitted.

### 3. `git log --oneline -5` and HEAD

```
2792f2c Fix survey-link comment rendering onto the Actions cell
3ce77ef Survey folder link as a second route to the allocation gate
c7c583d Consolidate the survey gate behind two DesignAssignment properties
273ff94 Add SECONDARY_FINDINGS.md — incidental issues log
b43e401 OPEX sites: site_code IS the project ID; contact fields optional
```

**Local HEAD: `2792f2c8c96d5fa635de4b756797273d68a084c7`**
`origin/main`: `2792f2c8c96d5fa635de4b756797273d68a084c7` — identical.

### 4. Railway deployment record

The Railway MCP connection returned `Unauthorized. Please run 'railway login' again`, so the
deployment SHA could not be re-read directly this session. The CLI is still authenticated and
reports:

```
$ railway status
Project:         triumphant-forgiveness      (8f2553d2-eddb-4801-8c82-150948e2426e)
Environment:     production                  (aed8bb83-f0ad-426c-9adc-3e7c002b35a3)
Horizon-Solar-PMS
    status:        ● Online
    deployment ID: 2b4cc0e6-607f-428e-bbaf-7832fd739a8c

$ railway deployment list
Recent Deployments
  2b4cc0e6-607f-428e-bbaf-7832fd739a8c | SUCCESS | 2026-08-06 07:39:50 +05:30
  ec0759f3-8b5f-49b7-8395-997c6c7277e2 | REMOVED | 2026-08-05 18:38:53 +05:30
  ...
```

The current SUCCESS deployment is `2b4cc0e6-607f-428e-bbaf-7832fd739a8c`. That deployment ID
was read directly yesterday (13 Aug, `BLOCK0_VERIFICATION.md` Part A) and reported its commit
as `2792f2c8c96d5fa635de4b756797273d68a084c7`. The ID is unchanged, no newer deployment
exists, and a Railway deployment ID is immutably bound to one commit.

**Deployed SHA `2792f2c8...` equals local HEAD `2792f2c8...`. Hard stop 2 does not fire.**
The SHA is established by an unchanged deployment ID rather than by a fresh read this session
— see UNCERTAIN item 1.

### 5. `showmigrations projects | tail -15`

```
 [X] 0046_alter_project_customer_name
 [X] 0047_boqitemmaster_boqitem_item_master
 [X] 0048_userprofile_design_head_deputy_and_more
 [X] 0049_arkasubmission_rejection_reason_required_when_rejected_and_more
 [X] 0050_alter_designassignment_status
 [X] 0051_backfill_opex_assigned_design
 [X] 0052_sitegroup_sitegroupmembership
 [X] 0053_alter_userprofile_role
 [X] 0054_part8_cad_zip_and_design_hold
 [X] 0055_part9_design_qc_gate
 [X] 0056_part9_1_scoped_rework
 [X] 0057_boqitemmaster_project_type_opex_catalogue
 [X] 0058_part46_change_request_triage
 [X] 0059_part10_design_analytics_preference
 [X] 0060_designassignment_survey_folder_url_and_more
```

**Current migration head: `0060_designassignment_survey_folder_url_and_more`.** A new field
in either request would be `0061`.

This is `[LOCAL]` — see CONFLICTS item 1 on why a local read was used.

---

# PART 1 — HOME / DASHBOARD BUTTON

## 1.1 — How many base templates are there?

**Three.** Counted by every `{% extends %}` target across all 111 templates under
`projects/templates/`:

```
$ grep -rho "{% *extends *['\"][^'\"]*['\"]" --include=*.html projects/ | sort | uniq -c | sort -rn
     53 base.html
     14 projects/admin/admin_base.html
      3 projects/subadmin/subadmin_base.html
```

70 templates extend a base; the remaining 41 are partials, fragments and standalone pages.
None of the three bases extends another — each is a complete `<!DOCTYPE html>` document.

### `projects/templates/base.html` — **Bootstrap 5.3.3**, 53 templates, 342 lines

```html
projects/templates/base.html:8-9
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">

projects/templates/base.html:140-141
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script src="https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js"></script>
```

Bootstrap + Bootstrap Icons + htmx. It also calls `window.lucide.createIcons()` after htmx
swaps (`base.html:152-156`), so individual pages that load Lucide themselves get their icons
re-initialised.

### `projects/templates/projects/admin/admin_base.html` — **Tailwind + Alpine + Lucide**, 14 templates, 178 lines

```html
projects/templates/projects/admin/admin_base.html:7-9
  <script src="https://cdn.tailwindcss.com"></script>
  <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
  <script src="https://unpkg.com/lucide@latest"></script>
```

### `projects/templates/projects/subadmin/subadmin_base.html` — **Tailwind + Alpine + Lucide**, 3 templates, 100 lines

```html
projects/templates/projects/subadmin/subadmin_base.html:7-9
  <script src="https://cdn.tailwindcss.com"></script>
  <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
  <script src="https://unpkg.com/lucide@latest"></script>
```

So: **one Bootstrap base carrying 76% of the templates, two Tailwind bases carrying the
portal-admin and system-admin panels.** The prompt's description of the split is accurate.

## 1.2 — Which base do the design-module screens extend?

**All of them extend `base.html`. Every single one, including the picker.**

```
change_request.html       {% extends "base.html" %}
head_review.html          {% extends "base.html" %}
head_sites.html           {% extends "base.html" %}
my_sites.html             {% extends "base.html" %}
qc_dashboard.html         {% extends "base.html" %}
qc_queue.html             {% extends "base.html" %}
qc_review.html            {% extends "base.html" %}
quality_analytics.html    {% extends "base.html" %}
site_group_detail.html    {% extends "base.html" %}
site_groups.html          {% extends "base.html" %}
site_workspace.html       {% extends "base.html" %}
tender_dashboard.html     {% extends "base.html" %}
opex_boq_entry.html       {% extends "base.html" %}
```

The other twelve files in `projects/templates/projects/design/` are partials with no
`{% extends %}` (`_attempt_history.html`, `_design_artifacts.html`, `_qa_figure.html` …).

**Hard stop 3 does not fire.** The design-module screens extend exactly one base, so the
insertion point for a Home button is single: `base.html`. This is one edit for the whole
design module, not several.

## 1.3 — Where is each role's landing screen, and is there already a resolver?

**A reusable resolver already exists.** It is not inline in the login view.

`projects/decorators.py:10-24` — the mapping, a module-level dict:

```python
# Maps every role to its landing dashboard URL.
# Used by login_view and role_required to redirect users after auth.
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

`projects/decorators.py:37-59` — the resolver:

```python
def get_user_dashboard(user, context=None):
    """
    Return the correct dashboard URL for the given user based on their role.
    Falls back to /dashboard/admin/ if the user has no UserProfile — prevents
    a crash for superusers created via manage.py createsuperuser.
    ...
    """
    try:
        role = user.profile.role
        url = ROLE_DASHBOARD.get(role, '/dashboard/admin/')
    except Exception:
        logger.warning("No UserProfile found for user %s — falling back to admin dashboard", user.username)
        return '/dashboard/admin/'

    if context and role in LANDING_ROLES:
        return f'{url}?context={context}'
    return url
```

`projects/decorators.py:62-79` — the post-login variant, which layers the context chooser on
top for three roles:

```python
def get_post_login_url(user):
    """
    Where to send a user immediately after authenticating.

    CEO / Finance / SCM land on the context chooser; every other role keeps the
    exact previous behaviour (straight to their role dashboard). Used only by
    login_view — role_required's denial redirect deliberately still uses
    get_user_dashboard() so a wrong-role bounce is unchanged.
    """
    try:
        role = user.profile.role
    except Exception:
        logger.warning("No UserProfile found for user %s — falling back to admin dashboard", user.username)
        return '/dashboard/admin/'

    if role in LANDING_ROLES:
        return LANDING_URL
    return get_user_dashboard(user)
```

with `LANDING_ROLES = ('CEO', 'Finance', 'SCM')` and `LANDING_URL = '/landing/'`
(`decorators.py:32-34`).

Both are used by the login view (`projects/views.py:216-240`):

```python
def login_view(request):
    if request.user.is_authenticated:
        return redirect(get_post_login_url(request.user))
    ...
        if user is not None:
            login(request, user)
            return redirect(get_post_login_url(user))
```

**Direct answer to the question asked: a single reusable function already exists —
`get_user_dashboard(user)`. The mapping is NOT inline in the login view.** The consolidation
work the prompt anticipated has already been done. What is missing is only the **template
exposure**: there is no template tag and no context-processor key that surfaces it.

```
$ ls projects/templatetags/
NO templatetags PACKAGE
```

`projects/context_processors.py` contains one function, `notifications`, and
`solarpms/settings.py:86-92` registers exactly one project context processor:

```python
'context_processors': [
    'django.template.context_processors.debug',
    'django.template.context_processors.request',
    'django.contrib.auth.context_processors.auth',
    'django.contrib.messages.context_processors.messages',
    'projects.context_processors.notifications',  # Injects unread_count into every template
],
```

So the pattern for injecting a value into every template already exists and has one
occupant. Adding a second key is a two-line change plus one settings line.

### Two findings on the mapping itself

**(a) `is_design_head` and `is_design_qc` are not consulted anywhere in the resolution.**
`ROLE_DASHBOARD` keys on `profile.role` only. A Design Head, a Design QC reviewer and an
ordinary designer all carry `role='Design'` and all three resolve to `/dashboard/design/`.
Whether that is correct is a product question, not a defect I am asserting — but a "take me
home" button will send the Design Head to the designer dashboard, not to his tender dashboard
or the Part 10 analytics screen.

**(b) `LOGIN_REDIRECT_URL` in settings is a decoy.** `solarpms/settings.py:171` sets
`LOGIN_REDIRECT_URL = '/dashboard/admin/'`, but the app uses its own `login_view` rather than
Django's `LoginView`, so that setting is not what actually routes anyone. `dashboard_admin`
itself carries no role restriction (`projects/views.py:279-282`):

```python
@login_required
def dashboard_admin(request):
    """Admin landing page. Access: any authenticated user (no role restriction here — Admin nav is in the template)."""
    return render(request, 'dashboard/admin.html')
```

## 1.4 — What is in the existing header/nav?

### `base.html` — **the logo is NOT a link**

`projects/templates/base.html:43-81`, the entire nav:

```html
{% if user.is_authenticated %}
<nav class="navbar navbar-dark horizon-nav px-4">
  <div class="d-flex align-items-center gap-2">
    <img src="{% static 'images/horizon-logo.png' %}" alt="Horizon Renewable Power"
         style="height:28px; width:auto; filter:brightness(0) invert(1); opacity:.9;">
    <span class="navbar-brand mb-0">Horizon Solar — PMS</span>
  </div>
  <div class="d-flex align-items-center gap-3">
    {% if user.profile.role == 'Admin' or user.profile.role == 'PM' or user.profile.role == 'CEO' %}
      <a href="{% url 'program_list' %}" class="nav-link text-white small" title="Programs">
        <i class="bi bi-diagram-3 me-1"></i>Programs
      </a>
    {% endif %}
    <div class="position-relative" id="user-hover-menu">
      <span class="text-white-50 small" style="cursor:default;">
        {{ user.get_full_name|default:user.username }}
      </span>
      <div id="user-hover-dropdown">
        <a href="{% url 'my_documents' %}" class="dropdown-item small rounded">
          <i class="bi bi-folder2-open me-2"></i>My Documents
        </a>
        <a href="{% url 'change_password' %}" class="dropdown-item small rounded">
          <i class="bi bi-key me-2"></i>Change Password
        </a>
      </div>
    </div>
    <a class="nav-link position-relative me-1 text-white" href="{% url 'notifications' %}"
       title="Notifications">
      <i class="bi bi-bell fs-5"></i>
      {% if unread_notification_count %}
      <span class="position-absolute top-0 start-100 translate-middle
                   badge rounded-pill bg-danger">
        {{ unread_notification_count }}
      </span>
      {% endif %}
    </a>
    <a href="{% url 'logout' %}" class="btn btn-sm btn-outline-light">Logout</a>
  </div>
</nav>
{% endif %}
```

**The brand block (lines 45-49) is a plain `<div>` containing an `<img>` and a `<span>`. There
is no `<a href>`.** Clicking the Horizon logo on any of the 53 Bootstrap screens does nothing.
This is the direct answer to the prompt's concern about duplicating an existing affordance:
**there is nothing to duplicate.**

**Mobile behaviour: there is no collapse and no hamburger.** The `<nav>` carries
`navbar navbar-dark horizon-nav px-4` and **no `navbar-expand-*` class**, no
`navbar-toggler`, no `.collapse`, and no Alpine state — Alpine is not loaded in `base.html`
at all. The nav is two flex `<div>`s that simply wrap on a narrow viewport. Adding one more
control is layout-safe; it will wrap with the rest.

There is one conditional link to `program_list`, gated to Admin / PM / CEO by role-string
comparison (line 51).

### `base.html` — a second, conditional home-ish control

`projects/templates/base.html:95-102` renders an "All views" link back to the chooser, but
only when `context_nav` is in the template context:

```html
{% if context_nav %}
<div class="border-bottom bg-white">
  <div class="container py-2 d-flex flex-wrap align-items-center gap-2">
    <a href="{% url 'landing' %}"
       class="text-decoration-none small fw-semibold d-inline-flex align-items-center"
       style="color:#1a7a4a; min-height:36px;">
      <i class="bi bi-grid-1x2 me-1"></i>All views
    </a>
```

Per the comment at `base.html:84-94`, only the CEO, Finance and SCM dashboards supply
`context_nav`, so this strip appears on three screens and nowhere else.

### `admin_base.html` — brand IS a link, plus a separate Dashboard link, both hardcoded

`projects/templates/projects/admin/admin_base.html:63-77`:

```html
  <header class="bg-white border-b border-gray-200 h-14 flex items-center px-6 gap-4 fixed top-0 left-0 right-0 z-30">
    <a href="/dashboard/admin/" class="flex items-center gap-2">
      <span class="text-lg font-bold" style="color:#1a7a4a;">Horizon Solar</span>
      <span class="text-gray-300">/</span>
      <span class="text-sm text-gray-500 font-medium">Admin Panel</span>
    </a>
    <div class="ml-auto flex items-center gap-4">
      <a href="/dashboard/admin/" class="text-sm text-gray-500 hover:text-gray-800 flex items-center gap-1">
        <i data-lucide="layout-dashboard" class="w-4 h-4"></i> Dashboard
      </a>
      <a href="/logout/" class="text-sm text-gray-500 hover:text-red-600 flex items-center gap-1">
        <i data-lucide="log-out" class="w-4 h-4"></i> Logout
      </a>
    </div>
  </header>
```

**These 14 screens already have two home controls, and both are hardcoded to
`/dashboard/admin/` rather than resolved from the user's role.** For any non-Admin user who
reaches a portal-admin screen, both send them to the wrong dashboard — which
`dashboard_admin` will happily render, because it has no role gate (see 1.3(b)). Reported,
not fixed.

### `subadmin_base.html` — brand is a link, no Dashboard control

`projects/templates/projects/subadmin/subadmin_base.html:35-47`:

```html
  <header class="bg-white border-b border-gray-200 h-14 flex items-center px-6 gap-4 fixed top-0 left-0 right-0 z-30">
    <a href="{% url 'subadmin_projects' %}" class="flex items-center gap-2">
      <span class="text-lg font-bold" style="color:#1a7a4a;">Horizon Solar</span>
      <span class="text-gray-300">/</span>
      <span class="text-sm text-gray-500 font-medium">System Admin</span>
    </a>
    <div class="ml-auto flex items-center gap-4">
      <span class="text-xs px-2 py-1 rounded-full font-medium bg-blue-50 text-blue-700">System Admin</span>
      <a href="/logout/" class="text-sm text-gray-500 hover:text-red-600 flex items-center gap-1">
        <i data-lucide="log-out" class="w-4 h-4"></i> Logout
      </a>
    </div>
  </header>
```

This one is correct for its audience: `subadmin_projects` **is** the System Admin's landing
screen per `ROLE_DASHBOARD['System Admin'] = '/sub-admin/projects/'`.

## 1.5 — Does any screen deliberately have no way back?

No template extending `base.html` can suppress the nav: there is no `{% block %}` around it,
only `{% if user.is_authenticated %}` (`base.html:43`). So the question reduces to standalone
full-page templates.

```
$ standalone templates (DOCTYPE present, no {% extends %}):
  projects/templates/base.html                        <- a base
  projects/templates/projects/admin/admin_base.html   <- a base
  projects/templates/projects/subadmin/subadmin_base.html <- a base
  projects/templates/landing.html
  projects/templates/registration/login.html
  projects/templates/projects/email/eod_digest.html
```

Three of the six are the bases themselves. Of the remaining three:

- **`projects/templates/landing.html` — this is the one.** 122 lines, and a grep for
  `<a `, `<nav`, `href` and `logout` returns exactly **one** match:

  ```
  projects/templates/landing.html:77:          <a href="{{ card.url }}"
  ```

  The CEO / Finance / SCM context chooser has **no nav, no logo, no home control and no
  logout link**. The only way off it is to pick one of the two context cards. It is
  standalone Tailwind while `base.html` is Bootstrap, per the note at `base.html:90-91`.
  Given that CEO, Finance and SCM are exactly the three roles sent here on every login
  (`get_post_login_url`, `decorators.py:77-78`), this is a plausible origin of the request.

- **`registration/login.html`** — pre-authentication. Correctly has no nav.
- **`projects/email/eod_digest.html`** — an email body, not a screen. Not applicable.

No print views and no modal-only full pages were found.

## Shape of the work — Part 1 (reported separately, per the prompt)

**Small, and smaller than expected**, because `get_user_dashboard()` already exists and the
design module has a single base.

Minimum: **3 files** — `projects/context_processors.py` (expose the URL),
`solarpms/settings.py` (register it, if a new processor rather than an extra key on
`notifications`), `projects/templates/base.html` (the control). No migration, no model, no
view change.

Optional extras the audit surfaced, each independently decidable: `admin_base.html` and
`subadmin_base.html` currently hardcode their destination (**+2 files**), and `landing.html`
has no way off it at all (**+1 file**). Upper bound **6 files**.

The one design question the code does not answer: whether "home" for a Design Head means
`/dashboard/design/` (what `ROLE_DASHBOARD` returns today) or his tender dashboard.

---

# PART 2 — REMARKS ON ARKA / CAD / BOQ SUBMISSION

## 2.1 — The three submission paths

| | **Arka** | **CAD** | **BOQ** |
|---|---|---|---|
| URL name | `design_arka_submit` | `design_artifact_upload` | `design_boq_complete` |
| Path | `design/<str:project_id>/arka/submit/` | `design/<str:project_id>/artifact/upload/` | `design/<str:project_id>/boq/complete/` |
| `urls.py` line | `projects/urls.py:73` | `projects/urls.py:82` | `projects/urls.py:84` |
| View | `design_views.design_arka_submit` | `design_views.design_artifact_upload` | `design_views.design_boq_complete` |
| View lines | `design_views.py:1559-1634` | `design_views.py:1948-2044` | `design_views.py:2076-2135` |
| Row created / stamped | **`ArkaSubmission` row created** (`design_views.py:1620-1624`) | **`DesignFile` row created** (`design_views.py:2016-2026`) | **`DesignAttempt` stamped** — no row (`design_views.py:2121-2124`) |
| Submitting control | `site_workspace.html:95` | `site_workspace.html:161` | `site_workspace.html:229` **and** `opex_boq_entry.html:193-196` |

All three views are POST-only and gated on `user_is_assigned_designer`.

**The BOQ has two submitting controls, not one.** Besides the standalone form on the
workspace, the picker's own toolbar posts the whole sheet with `action=mark_complete`
(`opex_boq_entry.html:186-196`):

```html
          {% comment %}
          Mark BOQ complete posts THIS form, so unsaved quantities are saved on the way
          through. The view saves and then delegates to design_boq_complete(), which owns
          the attempt stamp and refuses if the Arka is not approved at both gates or no
          quantity has been entered.
          {% endcomment %}
          <button type="submit" class="btn btn-sm btn-outline-secondary"
                  onclick="boqSetAction('mark_complete')">
            Mark BOQ complete
          </button>
```

and `views.opex_boq_entry` delegates (`projects/views.py:4725-4730`):

```python
            # design_boq_complete owns the attempt stamp and every precondition on it —
            # the assignment status, an approved Arka, "not already stamped", and at least
            # one quantity. Called rather than duplicated so the two cannot disagree. It
            # redirects to the site workspace and messages for itself.
            from .design_views import design_boq_complete
            return design_boq_complete(request, project_id)
```

A BOQ remark must therefore be captured on **both** controls, and the picker path carries it
through `views.opex_boq_entry`'s POST handler in `views.py` — a second file, in a different
module from the other two remarks.

## 2.2 — Where would a remark hang, per artifact?

### Arka — a new row per submission. A remark is a field on that row.

`projects/design_views.py:1620-1624`:

```python
        arka = ArkaSubmission.objects.create(
            attempt=attempt, version=next_version,
            capacity_kw=capacity, arka_link=arka_link,
            submitted_by=profile, verdict=ARKA_PENDING, is_current=True,
        )
```

The model (`projects/models.py:2399-2412`) confirms the row is per-submission and versioned:

```python
    attempt = models.ForeignKey(
        DesignAttempt, on_delete=models.CASCADE, related_name='arka_submissions',
    )
    version     = models.PositiveIntegerField()
    capacity_kw = models.DecimalField(max_digits=10, decimal_places=2)
    arka_link   = models.URLField(max_length=1000)

    submitted_by = models.ForeignKey(
        'UserProfile', on_delete=models.PROTECT, related_name='arka_submissions',
    )
    submitted_at = models.DateTimeField(auto_now_add=True)
```

with `unique_together = ('attempt', 'version')` (`models.py:2474`). **Unambiguous: one row per
submission, so a `TextField` on `ArkaSubmission` belongs to exactly one submission.**

### CAD — a new row per upload. Re-upload does not mutate.

`projects/design_views.py:2016-2030`:

```python
        design_file = DesignFile.objects.create(
            attempt=attempt, kind=kind, version=next_version,
            bucket=bucket, path=stored_path,
            original_filename=(upload.name or '')[:255],
            size_bytes=getattr(upload, 'size', None),
            content_type=(getattr(upload, 'content_type', '') or '')[:100],
            archive_listing=listing,
            derived_from_arka=arka,
            uploaded_by=profile, is_current=True,
        )
        if previous is not None:
            previous.is_current    = False
            previous.superseded_by = design_file
            previous.save(update_fields=['is_current', 'superseded_by'])
```

**A re-upload creates a new row and flips the predecessor.** The only mutation of the old row
is `is_current` and `superseded_by`; nothing else on it is touched. `DesignFile.Meta` carries
`unique_together = ('attempt', 'kind', 'version')` (`models.py:2568-2570`). **Unambiguous.**

### BOQ — the asymmetric one. It is a **timestamp**, not a row.

`projects/design_views.py:2121-2124`:

```python
    with transaction.atomic():
        attempt.boq_submitted_at = timezone.now()
        attempt.boq_submitted_by = profile
        attempt.save(update_fields=['boq_submitted_at', 'boq_submitted_by'])
```

The docstring states it as a settled decision (`design_views.py:2077-2084`):

> THE BOQ ITSELF IS NOT DUPLICATED (settled decision 4). Quantities live in the existing BOQ /
> BOQItem rows and are entered through the existing `boq_detail` screen under the Part 0.6
> permission helpers, neither of which this module touches. All that happens here is that the
> ATTEMPT records boq_submitted_at / boq_submitted_by — the design workflow's own note that
> this step is done.

**No per-submission row is created.** The three options, with the cost the code actually
imposes on each — **reported, not chosen:**

**(a) A field on `DesignAttempt`.** Cheapest by a clear margin.
- The stamp already lives here, beside `boq_submitted_by` (`models.py:2330-2333`), so the
  remark sits with the two fields it describes.
- **It is unambiguous**, because the view refuses a second stamp
  (`design_views.py:2111-2113`): `if attempt.boq_submitted_at is not None: return _back(...'is already marked complete.')`.
  One BOQ completion per attempt, therefore one remark per attempt.
- `DesignAttempt` already carries two free-text fields of exactly this kind (`qc_remarks`,
  `head_remarks`) so the pattern is in place — see 2.6.
- **Cost:** one field, one migration, plus **explicit handling in `_carry_forward_artifacts()`**
  (see 2.3).
- **Limitation:** a designer who marks complete, is failed, and marks complete again on
  attempt N+1 writes a *separate* remark on the new attempt. Whether that is right or wrong is
  a product question; mechanically it is consistent with how `boq_submitted_at` already
  behaves.

**(b) A field on `BOQ`.** Cheapest in migration terms — **a suitable field already exists.**
`projects/models.py` (BOQ model) already has:

```python
    notes        = models.TextField(null=True, blank=True)
```

and it is already wired, though to a different purpose — `views.py:4391`
`boq.notes = request.POST.get('notes', '').strip() or None`, rendered at
`boq_detail.html:105-109` with placeholder `"Optional notes for SCM…"`.
- **But `BOQ` is `OneToOneField(Project)`** (one BOQ per project, forever), so a field here is
  **per project, not per submission**. Across a rework loop the remark for attempt 2 would
  overwrite the remark for attempt 1 with no history. This is the ambiguity the prompt's hard
  stop 4 is aimed at, arriving via the storage location rather than via the write.
- Reusing `notes` itself would additionally collide with the existing SCM-notes feature.

**(c) A new per-submission row.** Most expensive: a new model, a new migration, a new
lifecycle (who deletes it, does it carry forward, what is `is_current`), and it would
introduce the first BOQ-side row this module owns — cutting directly against settled decision
4 quoted above. It would, however, be the only option that records a remark per *act of
marking complete* rather than per attempt or per project.

**Hard stop 4 does not fire.** The prompt's condition is "mutates an existing row rather than
create or stamp one, *in a way that makes a remark ambiguous*". It **stamps**, which the hard
stop explicitly allows, and the once-per-attempt guard at `design_views.py:2111-2113` makes
option (a) unambiguous. Option (b) *would* be ambiguous — but that is a property of the choice,
not of the existing code, and the choice is not mine to make here.

## 2.3 — Attempt carry-forward

`_carry_forward_artifacts()`, `projects/design_views.py:2180-2245`. **Every copy is an
explicit, field-by-field enumeration. Nothing is copied generically.**

Arka (`design_views.py:2198-2214`):

```python
    if REDO_ARKA not in redo and old_arka is not None:
        arka_for_new = ArkaSubmission.objects.create(
            attempt=new_attempt, version=1,
            capacity_kw=old_arka.capacity_kw, arka_link=old_arka.arka_link,
            submitted_by=old_arka.submitted_by,
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
```

CAD (`design_views.py:2221-2233`):

```python
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
                carried_forward_from=old_file, is_current=True,
            )
```

BOQ (`design_views.py:2238-2243`):

```python
    if REDO_BOQ not in redo and old_attempt.boq_submitted_at is not None:
        new_attempt.boq_submitted_at = old_attempt.boq_submitted_at
        new_attempt.boq_submitted_by = old_attempt.boq_submitted_by
        new_attempt.save(update_fields=['boq_submitted_at', 'boq_submitted_by'])
        carried.append('BOQ completion')
```

**Direct answer: a remark would NOT carry forward automatically. All three paths need explicit
handling.**

- **Arka remark** — must be added as a kwarg to the `ArkaSubmission.objects.create(...)` call
  at `design_views.py:2199-2214`. Omit it and a carried-forward Arka silently loses its remark
  while keeping both verdicts.
- **CAD remark** — must be added to the `DesignFile.objects.create(...)` call at
  `design_views.py:2223-2233`. Same failure mode.
- **BOQ remark, under option (a)** — must be added to both the assignment lines and the
  `update_fields` list at `design_views.py:2240-2242`. `update_fields` is an explicit
  allow-list: a field assigned but not listed is silently **not saved**. This is the most
  error-prone of the three.
- **BOQ remark, under option (b)** — carries "automatically" only because `BOQ` is not
  per-attempt at all, which is the ambiguity in 2.2(b), not a benefit.

There is a semantic question the code cannot answer, flagged rather than resolved: the
existing carry-forward deliberately preserves *verdicts* and marks them honest with
`carried_forward_from` (`models.py:2456-2468`). A carried-forward *designer's remark* would
likewise be attributed to a submission the designer did not make on this attempt. The same
`carried_forward_from` field is available to render it honestly.

## 2.4 — Where would a remark be read?

**The key structural finding: there is one shared partial, `_design_artifacts.html`, included
by three screens.** A remark rendered there appears on all three at once.

```
projects/templates/projects/design/head_review.html:202   {% include "projects/design/_design_artifacts.html" %}
projects/templates/projects/design/qc_review.html:397     {% include "projects/design/_design_artifacts.html" %}
projects/templates/projects/design/site_workspace.html:265 {% include "projects/design/_design_artifacts.html" %}
```

### (i) `_design_artifacts.html` — CAD row, lines 93-98

```html
            <td class="small">
              {{ f.uploaded_at|date:"d M Y H:i" }}
              <div class="text-muted" style="font-size:.7rem;">
                {{ f.uploaded_by.user.get_full_name|default:f.uploaded_by.user.username }}
              </div>
            </td>
```

**Room: yes, no restructuring.** This is a `<td>` in a table row that already stacks a
timestamp over a name in a nested `<div>`. A third `<div>` for the remark drops in. If the
remark is long, the better placement is a full-width row beneath — the template already uses
that device for `archive_listing` via a `<details>` block at lines 88-91.

### (ii) `_design_artifacts.html` — Arka version block, lines 122-137

```html
      {% for a in arka_history %}
      <li class="list-group-item py-2">
        <div class="d-flex flex-wrap justify-content-between align-items-start gap-2">
          <div>
            <span class="ver-tag">Arka v{{ a.version }}</span>
            ...
            <div class="small">
              <a href="{{ a.arka_link }}" target="_blank" rel="noopener">{{ a.arka_link|truncatechars:70 }}</a>
            </div>
            <div class="text-muted" style="font-size:.7rem;">
              submitted {{ a.submitted_at|date:"d M Y H:i" }}
              by {{ a.submitted_by.user.get_full_name|default:a.submitted_by.user.username }}
            </div>
          </div>
```

**Room: yes, and there is an exact layout precedent inside the same `<li>`** — the rejection
reasons at lines 193-208 (quoted in 2.6) render as full-width alert blocks after the flex row.
A submitter's remark would mirror that shape with a neutral rather than danger style.

### (iii) `qc_review.html` — BOQ panel header, lines 136-142

```html
      {% if boq_complete %}
        <span class="small text-success ms-1">
          BOQ marked complete {{ attempt.boq_submitted_at|date:"d M Y H:i" }}
        </span>
      {% else %}
        <span class="small text-muted ms-1">BOQ not marked complete.</span>
      {% endif %}
```

**Room: constrained.** This is an inline `<span>` on a single header line that already
carries a title, a count and an "Open full BOQ" button pushed right with `ms-auto`
(`qc_review.html:117-127`). A remark of any length does not belong inline here; it needs a new
line inside the same `card-header`, or a row at the top of the `card-body`. **Minor
restructuring.**

### (iv) `site_workspace.html` — BOQ complete block, lines 223-228

```html
      {% if boq_complete %}
        <span class="small text-success">
          Marked complete {{ attempt.boq_submitted_at|date:"d M Y H:i" }}
          by {{ attempt.boq_submitted_by.user.get_full_name|default:attempt.boq_submitted_by.user.username }}
        </span>
```

**Room: yes.** Same shape as the CAD cell — a `<span>` already carrying timestamp and name.

### (v) `_attempt_history.html` — BOQ line, lines 198-203

```html
        {% if a.boq_submitted_at %}
          <div class="text-muted" style="font-size:.7rem;">
            BOQ marked complete {{ a.boq_submitted_at|date:"d M Y H:i" }}
            by {{ a.boq_submitted_by.user.get_full_name|default:a.boq_submitted_by.user.username }}
          </div>
        {% endif %}
```

**Room: yes**, a sibling `<div>`. Note this partial is itself included by three screens
(`qc_review.html:398`, `change_request.html:172`, `site_workspace.html:273`), so it is the
second one-edit-three-screens surface. It is also the historical view — it iterates *all*
attempts, so a remark here is where carry-forward honesty (2.3) becomes visible.

Other templates matching Arka/CAD/BOQ metadata but **not** rendering per-submission detail,
so out of scope for a remark: `_dashboard_design_chips.html`, `_design_status_chips.html`,
`_redo_scope.html`, `qc_dashboard.html`, `qc_queue.html`, `tender_dashboard.html`,
`quality_analytics.html`.

## 2.5 — Escaping risk

**Yes. There is a real and specific risk, and it is in `opex_boq_entry.html` — the one
template that carries the BOQ submitting control.**

Script-block inventory across the 2.4 templates:

```
qc_review.html          : 1 <script> block  (line 407)
_attempt_history.html   : 0
site_workspace.html     : 0
_design_artifacts.html  : 0
opex_boq_entry.html     : 3 <script> blocks (lines 203, 204, 206)
```

`projects/templates/projects/opex_boq_entry.html:203-204`:

```html
<script id="boqCatalogueData" type="application/json">{{ catalogue_json|safe }}</script>
<script id="boqAddedData" type="application/json">{{ added_json|safe }}</script>
```

Both payloads are built with a raw `json.dumps()` and rendered through **`|safe`**, which
disables Django's autoescaping entirely. `projects/views.py:4741-4749`:

```python
    catalogue_json = json.dumps([
        {'id': m.pk, 'code': m.code, 'cat': m.category,
         'desc': m.description, 'unit': m.unit}
        for m in catalogue
    ])
    added_json = json.dumps([
        {'id': row.item_master_id, 'qty': ('' if row.boq_quantity is None
                                           else f'{row.boq_quantity.normalize():f}')}
        for row in added_on
    ])
```

**Today this is tolerable but only narrowly**: every value in both payloads comes from
`BOQItemMaster` (admin-authored catalogue rows) or from a `Decimal`. No user-entered free text
reaches either block.

**A designer-authored remark placed into either payload would turn `|safe` into an injection
hole.** `json.dumps` escapes `"` and `\` but does **not** escape `<`, `>` or `/`, so a remark
containing `</script><script>…</script>` closes the block and executes. This is the reverse of
the failure mode the prompt describes (over-escaping breaking JS); here the template has
already traded that away for `|safe`, and the remaining exposure is under-escaping.

**The correct pattern is already in this codebase, in the design module, one file away.**
`projects/templates/projects/design/qc_review.html:406`:

```django
{{ redo_defaults_json|json_script:"redoDefaults" }}
```

Django's `json_script` filter emits `<script type="application/json">` itself and escapes
`<`, `>` and `&` as `\u003C`-style sequences, so it is safe by construction. The consuming JS
reads it identically (`qc_review.html:419-422`):

```js
  var defaults;
  try {
    defaults = JSON.parse(document.getElementById('redoDefaults').textContent);
  } catch (e) { return; }
```

**Required constraint for the implementation prompt** (naming it as the prompt asks):

> A designer remark must never be interpolated into the `{{ catalogue_json|safe }}` or
> `{{ added_json|safe }}` blocks at `opex_boq_entry.html:203-204`, nor into any new `|safe`
> block. If a remark must reach JavaScript at all, it goes through `|json_script`, matching
> `qc_review.html:406`. Rendering a remark as ordinary template text (`{{ ... }}`) in the five
> surfaces listed in 2.4 is safe and needs no special handling — all five are outside any
> `<script>` block.

Whether the BOQ remark needs to reach JS at all is an open design point: the picker's
"Mark BOQ complete" posts the whole form, so a `<textarea name="boq_remark">` placed in that
form is submitted with it and never needs to enter a JS payload. That is the cheap and safe
route, but it is a choice for the implementation prompt, not a finding.

## 2.6 — Existing precedent

**Yes — and the closest precedent is on `DesignAttempt` and is literally named "remarks".**

**Field definition** — `projects/models.py:2270-2271` and `2289-2290`:

```python
    # Required when qc_verdict is 'failed' — see the conditional-requirement note above.
    qc_remarks = models.TextField(blank=True, default='')
...
    # Required when head_verdict is 'failed' — CHECK constraint below, mirroring 0049.
    head_remarks = models.TextField(blank=True, default='')
```

**`TextField(blank=True, default='')`. No `max_length`, no `null=True`** — the module's
convention is empty-string-not-null for optional text. `ArkaSubmission.rejection_reason`
(`models.py:2419`), `ArkaSubmission.head_rejection_reason` (`models.py:2436`) and
`DesignChangeRequest.rejection_reason` (`models.py:2650`) are all declared identically. The
one exception in the wider codebase is `BOQ.notes = models.TextField(null=True, blank=True)`,
which is older and uses the opposite convention.

**Form widget: there is no Django `Form` class.** A grep of `projects/forms.py` for
`qc_remarks`, `rejection_reason` and `remark` returns nothing. The module uses a raw
`<textarea>` in the template and a raw `request.POST.get()` in the view.

Template — `projects/templates/projects/design/qc_review.html:297-299`:

```html
                <textarea name="qc_remarks" id="qc_pkg_remarks" rows="2" required
                          class="form-control form-control-sm"
                          placeholder="What is wrong with the package?"></textarea>
```

View — `projects/design_views.py:2571` and `2590-2596`:

```python
    remarks = (request.POST.get('qc_remarks') or '').strip()
...
        attempt.qc_remarks          = remarks
...
        attempt.save(update_fields=['qc_verdict', 'qc_remarks', 'qc_failure_category',
```

**Rendering** — `projects/templates/projects/design/_design_artifacts.html:193-208`, the
layout precedent a remark should mirror:

```html
        {% if a.rejection_reason %}
          <div class="alert alert-danger py-2 px-2 my-2 small mb-0">
            <strong>Design QC rejection:</strong> {{ a.rejection_reason }}
            {% if a.qc_failure_category %}
              <div style="font-size:.7rem;">Category: {{ a.get_qc_failure_category_display }}</div>
            {% endif %}
          </div>
        {% endif %}
        {% if a.head_rejection_reason %}
          <div class="alert alert-danger py-2 px-2 my-2 small mb-0">
            <strong>Design Head rejection:</strong> {{ a.head_rejection_reason }}
            {% if a.head_rejection_reason %}
              <div style="font-size:.7rem;">Category: {{ a.get_head_failure_category_display }}</div>
            {% endif %}
          </div>
        {% endif %}
```

Rendered as plain `{{ }}` — autoescaped, outside any `<script>`, wrapped in `{% if %}` so an
empty value renders nothing at all. A third precedent with a *required* free text is
`change_request.html:64-65`:

```html
        <textarea name="reason" class="form-control form-control-sm mb-2" rows="3" required
                  placeholder="Describe the change (required). The designer sees this verbatim."></textarea>
```

**One difference the implementation prompt must be aware of: every existing free-text field in
this module is _conditionally required_ and is written by a _reviewer_ at a verdict. A remark
on submission would be the module's first _optional_ free-text field, and its first written by
the _designer_.** The storage and rendering pattern transfers directly; the `required`
attribute and the CHECK constraint do not.

## 2.7 — Constraints in the way

Every constraint currently declared on the four models, read from `models.py`:

### `ArkaSubmission` — `models.py:2472-2494`

```python
    class Meta:
        ordering        = ['attempt', 'version']
        unique_together = ('attempt', 'version')
        constraints = [
            # Exactly one live Arka version per attempt; superseded rows coexist freely.
            models.UniqueConstraint(
                fields=['attempt'],
                condition=models.Q(is_current=True),
                name='uniq_current_arka_per_attempt',
            ),
            # A rejected Arka must say why. Same shape as the QC constraint above:
            # expressed as the valid case, and self-contained to this row.
            models.CheckConstraint(
                condition=~models.Q(verdict=ARKA_REJECTED) | ~models.Q(rejection_reason=''),
                name='rejection_reason_required_when_rejected',
            ),
            # PART 9 — the same rule at the Head gate.
            models.CheckConstraint(
                condition=(~models.Q(head_verdict=ARKA_REJECTED)
                           | ~models.Q(head_rejection_reason='')),
                name='head_rejection_reason_required_when_head_rejected',
            ),
        ]
```

2 CHECK + 1 partial UNIQUE + 1 `unique_together`. **All three CHECKs are conditioned on a
`verdict` column and a `*_reason` column. None references a submission-side field**, so an
optional `TextField(blank=True, default='')` added here sits entirely outside them and cannot
violate any.

### `DesignFile` — `models.py:2568-2570`

```python
    class Meta:
        ordering        = ['attempt', 'kind', 'version']
        unique_together = ('attempt', 'kind', 'version')
```

**No `constraints` list at all.** Clean slate — a new nullable/blank text field sits alongside
nothing.

### `DesignAttempt` — `models.py:2349-2366`

```python
    class Meta:
        ordering        = ['assignment', 'attempt_number']
        unique_together = ('assignment', 'attempt_number')
        constraints = [
            # A failed QC must say why. Expressed as the VALID case: either the verdict
            # is not 'failed', or remarks are non-empty. Self-contained (reads only this
            # row's own columns), which is what makes it expressible as a CHECK.
            models.CheckConstraint(
                condition=~models.Q(qc_verdict=QC_FAILED) | ~models.Q(qc_remarks=''),
                name='qc_remarks_required_when_qc_failed',
            ),
            # PART 9 — the same rule at the Head gate. Identical shape to the one above,
            # deliberately: a failure with no remarks is not a review at either gate.
            models.CheckConstraint(
                condition=~models.Q(head_verdict=QC_FAILED) | ~models.Q(head_remarks=''),
                name='head_remarks_required_when_head_failed',
            ),
        ]
```

2 CHECK + 1 `unique_together`. **Both CHECKs pair a `*_verdict` with a `*_remarks` column.** A
new `boq_remark` field would be a third `*_remarks`-shaped column on this model that is
deliberately *not* constrained — worth an explicit code comment when built, because the two
neighbours establish an expectation that every remarks field here has a matching CHECK.

### `BOQ` — no `Meta.constraints`

The BOQ model declares `project` as `OneToOneField(Project, on_delete=models.CASCADE,
related_name='boq')`, `status`, `version`, `submitted_by`, `submitted_at` and
`notes = models.TextField(null=True, blank=True)`. There is no `constraints` list.

### Summary for the implementation prompt

**No existing constraint blocks a new optional text field on any of the four models.** Every
CHECK in the design module is of the form "verdict X requires reason Y", and a submission
remark participates in none of them. The migration would be pure `AddField` with no
`AddConstraint` and no `RunPython`, on top of head `0060`.

## Shape of the work — Part 2 (reported separately, per the prompt)

**This is not small. Reporting that directly, as the prompt instructs.**

The Arka and CAD halves are genuinely small — a field, a textarea, a POST read, a display
block, and one line each in `_carry_forward_artifacts()`. The BOQ half is what makes it large,
for four independent reasons, each established above:

1. **BOQ has no per-submission row** (2.2), so the storage location is an open decision with
   three materially different answers.
2. **BOQ has two submitting controls in two modules** (2.1) — `site_workspace.html` posting to
   `design_views.design_boq_complete`, and `opex_boq_entry.html` posting to
   `views.opex_boq_entry` which delegates.
3. **Carry-forward for the BOQ stamp uses `update_fields`** (2.3), an explicit allow-list where
   an omission fails silently.
4. **The BOQ control lives in the one template with `|safe` JSON payloads** (2.5).

Files touched, if all three artifacts are done together:

| File | Why |
|---|---|
| `projects/models.py` | up to 3 new fields |
| `projects/migrations/0061_*.py` | new — pure `AddField` |
| `projects/design_views.py` | 3 submission views + `_carry_forward_artifacts()` (3 sites) |
| `projects/views.py` | `opex_boq_entry` POST path for the BOQ remark |
| `projects/templates/projects/design/site_workspace.html` | 3 submitting controls |
| `projects/templates/projects/opex_boq_entry.html` | the picker's mark-complete control |
| `projects/templates/projects/design/_design_artifacts.html` | 2 display blocks (Arka + CAD) |
| `projects/templates/projects/design/_attempt_history.html` | 1 display block |
| `projects/templates/projects/design/qc_review.html` | BOQ header block — minor restructure |

**9 files plus 1 migration.** Splitting Arka+CAD from BOQ would give roughly 6 files plus a
migration for the first, and 5 files plus a migration for the second — and would let the BOQ
storage decision (2.2) be taken on its own.

---

# PART 3 — ONE FORWARD-LOOKING READ

**Where is a new CAD version created today, and what does the creating code set?**

A new CAD version is created in exactly one place: `design_artifact_upload()`,
`projects/design_views.py:1948-2044`, with the row written at
**`projects/design_views.py:2016-2026`**. The file is pushed to storage first
(`upload_design_file(upload, path)`, line 2006), and then inside a single
`transaction.atomic()` block the view reads the current row for this `(attempt, kind)` pair
into `previous`, computes `next_version` as `max(version) + 1` over the same pair, and calls
`DesignFile.objects.create(...)` setting exactly these fields: **`attempt`** (the current
attempt), **`kind`**, **`version`** (`next_version`), **`bucket`** and **`path`** (both
returned by the storage upload), **`original_filename`** (truncated to 255),
**`size_bytes`**, **`content_type`** (truncated to 100), **`archive_listing`** (the zip
listing computed earlier in the view), **`derived_from_arka`** (the approved current Arka
returned by `_require_approved_arka()`, never inferred), **`uploaded_by`** (the acting
profile) and **`is_current=True`**. It then mutates the predecessor and only the predecessor,
setting `previous.is_current = False` and `previous.superseded_by = design_file` and saving
with `update_fields=['is_current', 'superseded_by']`. Not set on the new row, and therefore
left at their model defaults: `superseded_by` (null) and `carried_forward_from` (null — that
field is written only by `_carry_forward_artifacts()`). `uploaded_at` is `auto_now_add`.
Finally the view logs `action_code='design_artifact_uploaded'` and calls
`_maybe_advance_to_artifacts_uploaded()`.

No design work is proposed here.

---

# UNCERTAIN

1. **The deployed SHA was not re-read this session.** The Railway MCP returned
   `Unauthorized. Please run 'railway login' again`, and the CLI's `deployment list` prints
   IDs, status and timestamps but not commit SHAs. The equality of deployed SHA and local HEAD
   rests on the current SUCCESS deployment ID (`2b4cc0e6-…`) being byte-identical to the one
   read directly on 13 August, when it was reported as `2792f2c8…`, plus the absence of any
   newer deployment. That chain is sound but it is one inference, not a direct observation.

2. **`showmigrations` required a database connection** (see CONFLICTS 1). It was run against
   the **local** database. Production migration state was established directly yesterday
   (`BLOCK0_VERIFICATION.md` Part A: head `0060`, applied 2026-08-06 02:10:26 UTC) and is not
   re-established here.

3. **Whether a Design Head's "home" should be `/dashboard/design/`.** `ROLE_DASHBOARD` keys on
   role alone and returns that for every Design user. Whether the Head, the QC reviewers and
   the designers should share a landing screen is a product decision the code does not record
   an intent for.

4. **Whether the request for a Home button originates from `landing.html`.** That screen has
   no way off it except a context card (1.5), and the three roles that see it are sent there
   on every login — which makes it a plausible origin. Nobody said so; it is an inference from
   the code and is flagged as such.

5. **The rendered width available in the `qc_review.html` BOQ header** (2.4 item iii). I judged
   it "constrained" from the markup — an inline `<span>` on a line that already carries a
   title, a count and an `ms-auto` button. I did not render the page, so whether a short remark
   fits inline is not established.

6. **Whether a carried-forward remark should be shown as the designer's words for the new
   attempt.** 2.3 establishes mechanically that it would need explicit copying. Which
   behaviour is *correct* is a product question, and the honest-attribution device
   (`carried_forward_from`) exists either way.

# CONFLICTS

1. **The prompt's opening check requires a command that the prompt's own DO NOT list
   forbids.** Item 5 asks for `python manage.py showmigrations projects | tail -15`; the DO NOT
   list says "Do not touch any database, local or production — this audit is a code read", and
   hard stop 5 fires if "any question in this prompt cannot be answered without running code".
   `showmigrations` reads `django_migrations`. I ran it against the **local** database as a
   read-only query, judging the explicit instruction in the opening check to be the more
   specific of the two. Flagging rather than silently resolving. Nothing else in this session
   touched a database.

2. **The escaping defect class is described backwards for the template that matters.** The
   prompt describes "server-rendered content injected into a `<script>` block is HTML-escaped
   by Django and silently breaks the whole block". In `opex_boq_entry.html` the author already
   avoided that by using `|safe` (lines 203-204), so the live exposure is the **opposite**
   failure — under-escaping, i.e. injection — not over-escaping. Both are real defect classes;
   the one actually present in the template the prompt names is the second. See 2.5.

3. **The consolidation the prompt anticipated is already done.** Item 1.3 says "If it is
   inline, that consolidation is the actual work and the button is trivial on top of it." The
   mapping is not inline: `ROLE_DASHBOARD` and `get_user_dashboard()` have existed in
   `projects/decorators.py` since before this work. The remaining gap is only template
   exposure, which is smaller than the prompt allows for.

4. **`is_design_head` / `is_design_qc` do not participate in landing resolution at all.** Item
   1.3 asks for "the mapping from role (and from the `is_design_head` / `is_design_qc` flags)
   to landing URL name", presupposing the flags are consulted. They are not — `ROLE_DASHBOARD`
   reads `profile.role` only, and there is no flag-aware branch anywhere in the resolution
   path.

5. **`LOGIN_REDIRECT_URL` is set but inert.** `settings.py:171` reads
   `LOGIN_REDIRECT_URL = '/dashboard/admin/'`, which would suggest everyone lands on the admin
   dashboard. The app does not use Django's `LoginView`, so the setting never fires. Anyone
   reading settings alone would draw the wrong conclusion about the landing behaviour.

6. **The prompt's premise that the two requests "may share a template surface" is correct, and
   the overlap is narrower than it sounds.** Both touch `base.html`'s descendants, but the Home
   button touches `base.html` itself while every remark change is in design-module templates
   and partials. There is no file in common between the two file lists. They can be sequenced
   in either order with no interaction.

7. **`BOQ.notes` already exists and is already used.** Option 2.2(b) is not a greenfield field
   — `notes` is live, bound to `views.py:4391` and rendered at `boq_detail.html:105-109` with
   the placeholder "Optional notes for SCM…". Any proposal to put a designer remark on `BOQ`
   must either add a second field or collide with this one.

---

# CLOSING TABLE

| Item | Question | Result |
|---|---|---|
| Open 1 | Repo root | **ANSWERED** — `c:\SolarPMS\Horizon-Solar-PMS` |
| Open 2 | `git status`, tracked files clean | **ANSWERED** — clean; 2 untracked `.md` reports named |
| Open 3 | `git log -5`, HEAD | **ANSWERED** — `2792f2c8…` |
| Open 4 | Deployed SHA == HEAD | **ANSWERED (inferred)** — via unchanged deployment ID; MCP unauthorized |
| Open 5 | Migration head | **ANSWERED** — `0060`, `[LOCAL]` |
| 1.1 | How many base templates, which framework | **ANSWERED** — 3: `base.html` Bootstrap 5.3.3 (53), `admin_base.html` Tailwind (14), `subadmin_base.html` Tailwind (3) |
| 1.2 | Which base do design screens extend | **ANSWERED** — all extend `base.html`; hard stop 3 does not fire |
| 1.3 | Role→landing mapping; does a resolver exist | **ANSWERED** — `get_user_dashboard()` at `decorators.py:37`; exists, not inline; no template exposure |
| 1.4 | Existing header/nav, logo link, mobile | **ANSWERED** — `base.html` logo is **not** a link; no collapse/hamburger; `admin_base` has two hardcoded home links |
| 1.5 | Screens with no way back | **ANSWERED** — `landing.html`, one link total, no nav or logout |
| 2.1 | Locate the three submission paths | **ANSWERED** — URLs, views, lines, rows, controls tabulated; BOQ has **two** controls |
| 2.2 | Where would a remark hang | **ANSWERED** — Arka row, CAD row, BOQ **stamp**; three options costed, none chosen; hard stop 4 does not fire |
| 2.3 | Attempt carry-forward | **ANSWERED** — explicit field-by-field; a remark would **not** carry automatically; `update_fields` is the risk |
| 2.4 | Where would a remark be read | **ANSWERED** — 5 blocks quoted; 4 have room, `qc_review.html` header needs minor restructuring |
| 2.5 | Escaping risk | **ANSWERED** — real, at `opex_boq_entry.html:203-204` (`\|safe`); constraint named; `\|json_script` precedent at `qc_review.html:406` |
| 2.6 | Existing precedent | **ANSWERED** — `qc_remarks` / `head_remarks`, `TextField(blank=True, default='')`, raw textarea + `request.POST.get`, rendered in an alert div |
| 2.7 | Constraints in the way | **ANSWERED** — Arka 2 CHECK + 1 partial UNIQUE; DesignFile none; DesignAttempt 2 CHECK; BOQ none. None blocks a new optional text field |
| 3 | Where a new CAD version is created | **ANSWERED** — `design_views.py:2016-2026`, full field set listed |
| — | Part 1 size | **ANSWERED — small**, 3 files minimum, 6 upper bound, no migration |
| — | Part 2 size | **ANSWERED — NOT small**, 9 files + 1 migration; splitting Arka+CAD from BOQ is viable |
