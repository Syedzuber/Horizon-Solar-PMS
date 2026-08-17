# SESSION C AUDIT — Design Head access to the BOQ item master, plus a mandatory flag

**Mode:** investigate only. No application code was written, edited or deleted. No migration was
created. No `makemigrations` was run that writes. No database write was performed. The only file
created is this report.

**Date:** 2026-08-14
**Local HEAD:** `9fb3c59b9efa702efe63b1c259bfc3d94d0e9e5b`
**Deployed SHA (Railway `triumphant-forgiveness` / `Horizon-Solar-PMS` / `production`):**
`9fb3c59b9efa702efe63b1c259bfc3d94d0e9e5b` — deployment `6aa6c3d6`, SUCCESS, direct from the MCP
(not inferred).
**Migration head:** `0063_designassignment_qc_assigned_at_and_more.py`

---

## THE PREMISE, ANSWERED FIRST

**The prompt's premise is correct.** Item 6 is ~90% built. A full CRUD item master exists at
`/portal-admin/boq-items/` with list, add, edit and deactivate, a `project_type` filter, an
active filter and a linked-BOQ-row count. This session is not building a screen. It is about
**who may reach it**, plus one new column.

Two of the prompt's five hard stops were specifically checked and **neither fired**:

- **Hard stop 4 (the important one):** the OPEX picker **does** filter on `is_active`.
  Deactivation is not cosmetic. The feature as requested will work.
- **Hard stop 5:** the item master view **is** gated, `@role_required(['Admin'])`. There is no
  access hole on this screen.

Hard stop 3 (hard-delete) is a qualified no — see 1.4.

The one thing this audit corrects most sharply is **Part 3's framing**. The prompt suggests that
if a "this role sees these portal-admin screens only" pattern exists, it is the answer. A
precedent does exist, and it is the **opposite** of a filtered sidebar: the System Admin gets a
wholly parallel `sub-admin/` namespace with its own views and its own base template. See 2.4.

---

## SESSION OPENING CHECK

**1.** Repo `c:\SolarPMS\Horizon-Solar-PMS`. All git run there.

**2. `git status` — raw:**

```
On branch main
Your branch is up to date with 'origin/main'.

Untracked files:
  (use "git add <file>..." to include in what will be committed)
	SESSION_B_AUDIT.md

nothing added to commit but untracked files present (use "git add" to track)
```

No tracked file modified or staged. The one untracked file is a `.md` report. **Pass.**

> Note for the record: at the first attempt to open this session the tree carried six modified
> files and an untracked migration `0063`. That was an uncommitted Session B / B.1
> implementation. It was committed as `9fb3c59` and deployed before this audit began.

**3. `git log --oneline -5`:**

```
9fb3c59 Let the Design Head name a gate-1 QC reviewer per site
338ebde Let a designer leave a submission note on the BOQ
5501fc0 Let a designer leave a submission note on an Arka and a CAD
c85399e Add a Home control that resolves to the user's own dashboard
018c598 Add audit and status reports
```

**DEVIATION FROM THE PROMPT.** The prompt expects `[Session B]` and `[Session B.1]` to both be
present. They are not, in two ways: no commit in this repo's history uses a `[Session N]` tag
form at all, and B and B.1 landed as **one** commit (`9fb3c59`), not two. They could not be
split — B.1's changes sit inside B's functions and docstrings, so no ordering of the hunks
yields a first commit that both compiles and describes itself honestly. Both sessions are named
explicitly in that commit's body. **Treated as pass on substance.**

**4. Deployed SHA:** `9fb3c59b9efa702efe63b1c259bfc3d94d0e9e5b` == local HEAD. **Pass.**

**5. Migration head:** `0062_designattempt_boq_remarks.py`,
`0063_designassignment_qc_assigned_at_and_more.py`. Head is **0063**.

---

## PART 1 — WHAT ALREADY EXISTS

### 1.1 Location of the item master screen

| Piece | File | Lines |
|---|---|---|
| URL patterns | `projects/urls.py` | 328–332 |
| List view `admin_boq_items` | `projects/views.py` | 9023–9058 |
| Create view `admin_boq_item_create` | `projects/views.py` | 9061–9083 |
| Edit view `admin_boq_item_edit` | `projects/views.py` | 9086–9114 |
| Toggle view `admin_boq_item_toggle` | `projects/views.py` | 9117–9136 |
| List template | `projects/templates/projects/admin/boq_items.html` | 1–113 |
| Form template | `projects/templates/projects/admin/boq_item_form.html` | 1–71 |
| Form class `BOQItemMasterForm` | `projects/forms.py` | 479–526 |
| Section banner comment | `projects/views.py` | 9011–9021 |

`projects/urls.py:328-332`:

```python
    # BOQ Item Master — catalogue BOQ line items reference. Deactivate only, no delete. Admin only.
    path('portal-admin/boq-items/',                        views.admin_boq_items,       name='admin_boq_items'),
    path('portal-admin/boq-items/add/',                    views.admin_boq_item_create, name='admin_boq_item_create'),
    path('portal-admin/boq-items/<int:item_id>/edit/',     views.admin_boq_item_edit,   name='admin_boq_item_edit'),
    path('portal-admin/boq-items/<int:item_id>/toggle/',   views.admin_boq_item_toggle, name='admin_boq_item_toggle'),
```

The list view in full, `projects/views.py:9023-9058`:

```python
@login_required
@role_required(['Admin'])
def admin_boq_items(request):
    """List all BOQItemMaster entries in template order, with an optional active filter
    and the count of BOQ rows linked to each. Access: Admin only.

    PART 11 added the project_type filter and the explicit ordering. The catalogue holds
    two independent templates now (37 Residential, 207 OPEX), and `sort_order` restarts at
    1 for each — so the model's default ordering interleaves them here, and only here.
    Ordering by type first keeps each template contiguous and in its own order.
    """
    items = (BOQItemMaster.objects
             .annotate(linked_count=Count('boq_items'))
             .order_by('project_type', 'sort_order', 'code'))

    active_filter = request.GET.get('active', '')
    if active_filter == '1':
        items = items.filter(is_active=True)
    elif active_filter == '0':
        items = items.filter(is_active=False)

    type_filter = request.GET.get('type', '')
    if type_filter in dict(Project.PROJECT_TYPE_CHOICES):
        items = items.filter(project_type=type_filter)

    return render(request, 'projects/admin/boq_items.html', {
        'items':         items,
        'active_filter': active_filter,
        'type_filter':   type_filter,
        'type_choices':  Project.PROJECT_TYPE_CHOICES,
        'type_counts':   [(value, label,
                           BOQItemMaster.objects.filter(project_type=value).count())
                          for value, label in Project.PROJECT_TYPE_CHOICES],
        'active_count':  BOQItemMaster.objects.filter(is_active=True).count(),
        'total_count':   BOQItemMaster.objects.count(),
    })
```

### 1.2 The `BOQItemMaster` model in full

`projects/models.py:654-708`:

```python
class BOQItemMaster(models.Model):
    """
    Catalogue of BOQ line items. The single source of truth for what a standard
    BOQ line *is* — BOQItem rows reference an entry here so quantities can be
    summed across sites for grouped procurement.

    BOQItem.description stays a point-in-time snapshot: editing a catalogue row
    here never rewrites BOQ rows already created from it.

    Rows are deactivated via is_active, never deleted — BOQItem.item_master is
    SET_NULL, so a delete would silently break the aggregation join.

    PART 11 SCOPES THE CATALOGUE BY PROJECT TYPE. Residential and OPEX buy different
    things: the 37 Residential rows (ITM-001..ITM-037, Part 0.5) describe a rooftop
    house system, and the 207 OPEX rows (OPX-001..OPX-207) are the design team's tender
    catalogue. ONE TABLE, scoped by `project_type` — not two tables, because everything
    downstream of a BOQ row joins on `item_master`, and a second master table would mean
    a second join in aggregate_group_boq(), the admin catalogue screen and every future
    reader of a BOQ line.

    `category` STOPS BEING DECORATIVE HERE. On the Residential side it remains a display
    grouping. On the OPEX side it groups the picker and the saved sheet, and its ORDER
    matters — it must match the source spreadsheet, which it does because category order
    is derived from `sort_order` (first row of each category wins) rather than stored.
    """

    code        = models.CharField(max_length=32, unique=True)   # Short stable identifier, e.g. ITM-001 / OPX-001
    description = models.CharField(max_length=255)
    unit        = models.CharField(max_length=20)                # Required — quantities cannot be aggregated across sites without a consistent unit
    category    = models.CharField(max_length=64, blank=True)    # Residential: display grouping. OPEX: groups the picker and the sheet (Part 11)
    # Which kind of project this catalogue row serves. Defaults to Residential so the 37
    # pre-Part-11 rows are correctly scoped by the default alone; migration 0057 also sets
    # them explicitly rather than relying on it. Reuses Project.PROJECT_TYPE_CHOICES so the
    # vocabulary cannot drift from the value that is actually compared against it.
    project_type = models.CharField(
        max_length=20,
        choices=Project.PROJECT_TYPE_CHOICES,
        default='Residential',
        db_index=True,
    )
    is_active   = models.BooleanField(default=True)
    sort_order  = models.PositiveIntegerField(default=0)         # Also becomes BOQItem.serial_no on creation
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        # DELIBERATELY UNCHANGED by Part 11. `sort_order` now repeats across types (both
        # ITM and OPX start at 1), so the unfiltered default ordering interleaves them —
        # which is fine, because every functional query filters by project_type first and
        # the one screen that does not (the admin catalogue) orders explicitly. Adding
        # project_type here would silently reorder aggregate_group_boq()'s output.
        ordering = ['sort_order', 'code']

    def __str__(self):
        return f"{self.code} — {self.description}"
```

**`is_active` is a real `BooleanField`, default `True`** (line 694). There is **no `save()`
override and no property** on this model. `Meta` carries **only `ordering`** — no
`constraints`, no `unique_together`, no `indexes`. The only uniqueness anywhere is
`code = models.CharField(max_length=32, unique=True)`.

### 1.3 Add / edit / deactivate handlers

**Three separate views**, not branches in one. All three use a Django `ModelForm`
(`BOQItemMasterForm`) except the toggle, which reads no form input at all.

Create, `projects/views.py:9061-9083`:

```python
@login_required
@role_required(['Admin'])
def admin_boq_item_create(request):
    """Add one catalogue entry. Access: Admin only."""
    if request.method == 'POST':
        form = BOQItemMasterForm(request.POST)
        if form.is_valid():
            item = form.save()
            log_activity(None, request.user.profile,
                         f"Created BOQ catalogue item '{item.code} — {item.description}'",
                         entity_type='BOQItemMaster', entity_id=item.pk)
            messages.success(request, f'Catalogue item "{item.code}" added.')
            return redirect('admin_boq_items')
    else:
        # Suggest the next slot at the end of the template rather than 0
        next_order = (BOQItemMaster.objects.aggregate(m=Max('sort_order'))['m'] or 0) + 1
        form = BOQItemMasterForm(initial={'sort_order': next_order, 'is_active': True})

    return render(request, 'projects/admin/boq_item_form.html', {
        'form':  form,
        'title': 'Add BOQ Catalogue Item',
        'item':  None,
    })
```

Edit, `projects/views.py:9086-9114`:

```python
@login_required
@role_required(['Admin'])
def admin_boq_item_edit(request, item_id):
    """Edit one catalogue entry. `code` is create-only and is removed from the form here
    — reassigning it would break the identifier existing BOQ rows were grouped under.
    Existing BOQItem rows are never modified. Access: Admin only."""
    item = get_object_or_404(BOQItemMaster, pk=item_id)

    if request.method == 'POST':
        form = BOQItemMasterForm(request.POST, instance=item)
        del form.fields['code']
        if form.is_valid():
            form.save()
            log_activity(None, request.user.profile,
                         f"Updated BOQ catalogue item '{item.code}' "
                         f"(active={item.is_active})",
                         entity_type='BOQItemMaster', entity_id=item.pk)
            messages.success(request, f'Catalogue item "{item.code}" saved.')
            return redirect('admin_boq_items')
    else:
        form = BOQItemMasterForm(instance=item)
        del form.fields['code']

    return render(request, 'projects/admin/boq_item_form.html', {
        'form':         form,
        'title':        f'Edit BOQ Catalogue Item — {item.code}',
        'item':         item,
        'linked_count': item.boq_items.count(),
    })
```

Validation, `projects/forms.py:479-526`:

```python
class BOQItemMasterForm(forms.ModelForm):
    """Create / edit one catalogue entry. `code` is create-only — it is the stable
    identifier BOQ rows and grouped procurement refer to, so the edit view drops the
    field rather than letting it be reassigned (see admin_boq_item_edit)."""

    class Meta:
        model  = BOQItemMaster
        fields = ['code', 'project_type', 'description', 'unit', 'category',
                  'is_active', 'sort_order']
    ...
    def clean_code(self):
        value = (self.cleaned_data.get('code') or '').strip().upper()
        if not _ITEM_CODE_RE.fullmatch(value):
            raise forms.ValidationError(
                'Use uppercase letters, digits, hyphen or underscore only (e.g. ITM-038).'
            )
        return value

    def clean_description(self):
        return (self.cleaned_data.get('description') or '').strip()

    def clean_unit(self):
        value = (self.cleaned_data.get('unit') or '').strip()
        if not value:
            raise forms.ValidationError('Unit is required.')
        return value
```

with `projects/forms.py:476`:

```python
_ITEM_CODE_RE = re.compile(r'^[A-Z0-9][A-Z0-9\-_]*$')
```

Validation that runs: model-level `unique=True` on `code`; the regex on `code`; a non-empty
check on `unit`; whitespace strip on `description`. **There is no validation of `sort_order`
uniqueness, no check that `category` matches an existing OPEX category** (the help text asks
for it but nothing enforces it), and no cross-field validation.

### 1.4 What Deactivate actually does — and the delete question

`projects/views.py:9117-9136`:

```python
@login_required
@role_required(['Admin'])
def admin_boq_item_toggle(request, item_id):
    """Deactivate / reactivate one catalogue entry. Deactivated entries drop out of
    get_standard_boq_items(), so new BOQs stop including them; BOQ rows already created
    from the entry keep their description, quantity and item_master link untouched.
    Access: Admin only. POST only."""
    if request.method != 'POST':
        return redirect('admin_boq_items')

    item = get_object_or_404(BOQItemMaster, pk=item_id)
    item.is_active = not item.is_active
    item.save(update_fields=['is_active', 'updated_at'])

    state = 'activated' if item.is_active else 'deactivated'
    log_activity(None, request.user.profile,
                 f"{state.capitalize()} BOQ catalogue item '{item.code}'",
                 entity_type='BOQItemMaster', entity_id=item.pk)
    messages.success(request, f'Catalogue item "{item.code}" {state}.')
    return redirect('admin_boq_items')
```

**Confirmed: it flips a flag and deletes nothing.** It is POST-only and it is a toggle, so the
same button reactivates.

**Hard-delete paths — a qualified answer.**

- **No application code path can hard-delete a `BOQItemMaster`.** There is no delete view, no
  delete URL, and no `.delete()` call against the model anywhere in `views.py`,
  `design_views.py`, `models.py` or `forms.py`.
- **`BOQItemMaster` is NOT registered in the Django admin.** `projects/admin.py:3-12` imports
  `Project, Milestone, ProjectDocument, ProjectPhase, Task, UserProfile, NotificationLog,
  SystemSettings, Checklist, ChecklistItem, ChecklistTaskLink, ChecklistItemCompletion,
  Program, DesignAssignment, DueDateCommitment, DesignAttempt, ArkaSubmission, DesignFile,
  DesignChangeRequest` — the catalogue model is absent, so `/admin/`'s generic delete is not a
  route in.
- **Two reverse migrations do delete rows**, by design.
  `projects/migrations/0047_...py:89-93`:

  ```python
      BOQItemMaster = apps.get_model('projects', 'BOQItemMaster')
      ...
      deleted, _ = BOQItemMaster.objects.filter(code__in=codes).delete()
  ```

  and `projects/migrations/0057_...py:340-357`, which is careful about it:

  ```python
  def drop_import(apps, schema_editor):
      """Reverse: remove ONLY the 207 rows this migration created.

      Deletes by code prefix AND project_type, so an OPEX catalogue row somebody added
      afterwards through the admin screen survives. Touches no Residential row and no
      BOQItem: BOQItem.item_master is SET_NULL, so any BOQ line built from a deleted OPEX
      row keeps its description, quantity and serial number and simply loses the catalogue
      link — the same behaviour 0047's reverse has.
      """
  ```

**Assessment: hard stop 3 does NOT fire.** A migration rollback is a deliberate operator
action, not a live application path, and both reverses are scoped and documented. `SET_NULL`
means even that case degrades to an unlinked row rather than a cascade. **No stop.**

### 1.5 The BOQ ROWS column

`projects/views.py:9034-9036`:

```python
    items = (BOQItemMaster.objects
             .annotate(linked_count=Count('boq_items'))
             .order_by('project_type', 'sort_order', 'code'))
```

rendered at `projects/templates/projects/admin/boq_items.html:84`:

```html
      <td class="px-4 py-3 text-center text-gray-600">{{ item.linked_count }}</td>
```

**It is an annotation, not an N+1.** One query for the whole table.

**It counts ALL `BOQItem` rows** joined via the `boq_items` reverse relation
(`projects/models.py:882-887`) with **no filter whatsoever**:

```python
    item_master      = models.ForeignKey(
        'BOQItemMaster',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='boq_items',   # The join used to sum quantities across sites; null for ad-hoc / legacy rows
    )
```

So it includes rows on soft-deleted projects, rows with null or zero `boq_quantity`, and rows
on draft BOQs. It is a raw link count, not a "in use in a live BOQ" count. That matters for 4.5.

The edit screen computes the same number separately and **this one IS a per-row query**,
`projects/views.py:9113` — `'linked_count': item.boq_items.count()` — but it is one query on a
single-object page, so it is not an N+1.

### 1.6 Code generation — there is none

**Codes are TYPED BY THE USER, not generated.** `code` is a plain form field
(`projects/forms.py:486`), validated only by regex and DB uniqueness. The create view
pre-fills **only `sort_order`**, `projects/views.py:9075-9077`:

```python
        # Suggest the next slot at the end of the template rather than 0
        next_order = (BOQItemMaster.objects.aggregate(m=Max('sort_order'))['m'] or 0) + 1
        form = BOQItemMasterForm(initial={'sort_order': next_order, 'is_active': True})
```

**The `generate_project_id()` bug shape does not apply here** — there is no generator to be
resilient or non-resilient to deletions. The `ITM-nnn` / `OPX-nnn` codes were produced once, by
migration, and only by migration: `projects/migrations/0047_...py:51` —
`CODE_TEMPLATE = 'ITM-{:03d}'  # Deterministic: ITM-001 … ITM-037, by position in the list above`.

Two consequences worth noting rather than fixing:

- The `sort_order` suggestion uses `Max('sort_order')` **across the whole table, unscoped by
  `project_type`**. Adding a Residential item today suggests `208` (one past the OPEX maximum),
  not `38`. It is a suggestion and is editable, so it is cosmetic — but a Head adding items
  would hit it on every add.
- Nothing suggests or validates the code *prefix* against `project_type`. An admin can create
  an `OPX-` coded Residential row.

### 1.7 The trailing-comma summary line — REPORT ONLY, NOT FIXED

`projects/templates/projects/admin/boq_items.html:21-24`:

```html
    <p class="text-sm text-gray-500 mt-1">
      {{ active_count }} active of {{ total_count }} total —
      {% for value, label, count in type_counts %}{% if count %}{{ count }} {{ label }}{% if not forloop.last %}, {% endif %}{% endif %}{% endfor %}.
    </p>
```

Confirmed as the cause of the screenshot's `"244 active of 244 total — 37 Residential, 207 OPEX, ."`
The separator is decided by `forloop.last` over the **full** `type_counts` list, but the label is
suppressed by `{% if count %}`. `PROJECT_TYPE_CHOICES` orders Residential, OPEX, CAPEX; CAPEX has
0 rows so it renders nothing, while OPEX — not being last — still emits its trailing `", "`.
**Not fixed.**

---

## PART 2 — WHO MAY REACH IT TODAY

### 2.1 The permission gate on the item master and its write handlers

**All four views carry the identical gate.** `projects/views.py:9023-9024`, `9061-9062`,
`9086-9087`, `9117-9118`:

```python
@login_required
@role_required(['Admin'])
```

`role_required`, `projects/decorators.py:92-115`:

```python
def role_required(allowed_roles):
    """
    Restrict a view to users whose UserProfile.role is in allowed_roles.
    Redirects unauthenticated users to /login/.
    Redirects authenticated users without the right role back to their own dashboard.
    Falls back to 'Admin' role if the user has no UserProfile (avoids a hard crash).
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect('/login/')
            try:
                role = request.user.profile.role
            except Exception:
                # No UserProfile — treat as Admin so Django superusers can still navigate
                logger.warning("No UserProfile for user %s in role_required check", request.user.username)
                role = 'Admin'
            if role not in allowed_roles:
                messages.error(request, "You don't have access to this page")
                return redirect(get_user_dashboard(request.user))
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator
```

**Exactly who passes:** a user whose `UserProfile.role == 'Admin'`, **plus** any authenticated
user who has **no `UserProfile` at all** (the `except` branch treats them as Admin). That
fallback is a deliberate documented choice for Django superusers, not an oversight, but it is
the only non-Admin way in and it is worth knowing before this gate is widened.

Note `role_required` **redirects** rather than 403s, so a Design Head hitting the URL today
bounces to their own dashboard with a message.

### 2.2 The surrounding area — the sidebar and every target's gate

`projects/templates/projects/admin/admin_base.html:82-147`:

```html
      <nav class="py-4 flex flex-col gap-1">

        <p class="px-4 py-1 text-xs font-semibold text-gray-400 uppercase tracking-wider mt-2">Projects</p>

        <a href="/portal-admin/projects/"
           class="nav-item {% block nav_projects %}{% endblock %}">
          <i data-lucide="folder-open" class="w-4 h-4 shrink-0"></i>
          All Projects
        </a>

        <p class="px-4 py-1 text-xs font-semibold text-gray-400 uppercase tracking-wider mt-4">Settings</p>

        <a href="/portal-admin/settings/"
           class="nav-item {% block nav_settings %}{% endblock %}">
          <i data-lucide="settings" class="w-4 h-4 shrink-0"></i>
          Master switches
        </a>

        <a href="/portal-admin/notification-prefs/"
           class="nav-item {% block nav_notif %}{% endblock %}">
          <i data-lucide="bell" class="w-4 h-4 shrink-0"></i>
          Notification prefs
        </a>

        <a href="/portal-admin/task-durations/"
           class="nav-item {% block nav_task_durations %}{% endblock %}">
          <i data-lucide="clock" class="w-4 h-4 shrink-0"></i>
          Task Durations
        </a>

        <a href="/portal-admin/checklists/"
           class="nav-item {% block nav_checklists %}{% endblock %}">
          <i data-lucide="check-square" class="w-4 h-4 shrink-0"></i>
          Checklists
        </a>

        <a href="/portal-admin/boq-items/"
           class="nav-item {% block nav_boq_items %}{% endblock %}">
          <i data-lucide="package" class="w-4 h-4 shrink-0"></i>
          BOQ Item Master
        </a>

        <p class="px-4 py-1 text-xs font-semibold text-gray-400 uppercase tracking-wider mt-4">Reports</p>

        <a href="/portal-admin/departments/"
           class="nav-item {% block nav_dept %}{% endblock %}">
          <i data-lucide="users" class="w-4 h-4 shrink-0"></i>
          Users
        </a>

        <a href="/portal-admin/send-records/"
           class="nav-item {% block nav_send %}{% endblock %}">
          <i data-lucide="file-text" class="w-4 h-4 shrink-0"></i>
          Send records
        </a>

        <a href="/portal-admin/audit-log/"
           class="nav-item {% block nav_audit %}{% endblock %}">
          <i data-lucide="bar-chart-2" class="w-4 h-4 shrink-0"></i>
          Audit log
        </a>

        <div class="mt-auto px-4 pt-8 pb-4">
          <p class="text-xs text-gray-400">Phase 2: Create Department / Create Role</p>
        </div>
      </nav>
```

**THE SIDEBAR IS NINE HARDCODED `<a>` TAGS.** It is not a loop, it is not permission-aware, and
it reads no context variable. This is the single fact Part 3 turns on.

Gate on each target — **all nine are identical**:

| Sidebar entry | URL | View | Line | Gate |
|---|---|---|---|---|
| All Projects | `/portal-admin/projects/` | `admin_project_list` | 8537–8538 | `@login_required` + `@role_required(['Admin'])` |
| Master switches | `/portal-admin/settings/` | `admin_master_switches` | 7985–7986 | `@login_required` + `@role_required(['Admin'])` |
| Notification prefs | `/portal-admin/notification-prefs/` | `admin_notification_prefs` | 8137–8138 | `@login_required` + `@role_required(['Admin'])` |
| Task Durations | `/portal-admin/task-durations/` | `admin_task_durations` | 8635–8636 | `@login_required` + `@role_required(['Admin'])` |
| Checklists | `/portal-admin/checklists/` | `admin_checklists` | 8747–8748 | `@login_required` + `@role_required(['Admin'])` |
| BOQ Item Master | `/portal-admin/boq-items/` | `admin_boq_items` | 9023–9024 | `@login_required` + `@role_required(['Admin'])` |
| Users | `/portal-admin/departments/` | `admin_departments` | 8179–8180 | `@login_required` + `@role_required(['Admin'])` |
| Send records | `/portal-admin/send-records/` | `admin_send_records` | 8352–8353 | `@login_required` + `@role_required(['Admin'])` |
| Audit log | `/portal-admin/audit-log/` | `admin_audit_log` | 8457–8458 | `@login_required` + `@role_required(['Admin'])` |

**ANSWER TO THE CENTRAL QUESTION.** If the Design Head is granted access **to this one view
only**, he reaches nothing else — every other portal-admin view has its own independent
`@role_required(['Admin'])` and would still refuse him. The blast radius of a per-view grant is
exactly one view.

**But he would SEE all nine links**, because the sidebar is hardcoded into the shared base
template with no permission logic. He would get a nav full of entries that bounce him to his own
dashboard with "You don't have access to this page". That is a cosmetic problem, not a security
one — and it is the entire practical cost of option A.

### 2.3 Session A's `dashboard_admin` finding — CONFIRMED, still holds

`projects/views.py:279-282`, verbatim, unchanged:

```python
@login_required
def dashboard_admin(request):
    """Admin landing page. Access: any authenticated user (no role restriction here — Admin nav is in the template)."""
    return render(request, 'dashboard/admin.html')
```

**Confirmed.** Any authenticated user renders the admin landing page. The docstring
acknowledges it explicitly.

**The item master view does NOT share this weakness.** It carries `@role_required(['Admin'])`.
The two are unrelated views; `dashboard_admin` renders a template with nav markup, while every
screen that nav points at is independently gated. **Hard stop 5 does not fire.**

### 2.4 Precedent for a non-Admin reaching portal-admin — THE PROMPT'S ASSUMPTION IS INVERTED

**There is no precedent for a non-Admin role reaching a `portal-admin/` URL. Not one.** Every
`portal-admin/` route in `projects/urls.py:301-332` is `@role_required(['Admin'])`, under a
banner comment at line 301 that says so:

```python
    # Admin Panel — portal-admin/ prefix (Admin role only)
```

The precedent that **does** exist is the System Admin, and it is a **wholly parallel namespace**.
`projects/urls.py:343-344`:

```python
    path('sub-admin/task-durations/', views.subadmin_task_durations, name='subadmin_task_durations'),
    path('sub-admin/departments/',   views.subadmin_departments,    name='subadmin_departments'),
```

Its scoping is expressed three ways, none of them a filtered sidebar:

1. **A separate decorator**, `projects/decorators.py:118`:
   ```python
   system_admin_required = role_required(['System Admin'])
   ```
2. **Separate view functions** — `projects/views.py:9250` `subadmin_projects`, `:9307`, `:9534`
   `subadmin_task_durations`. The last one's docstring states the pattern outright,
   `projects/views.py:9535-9536`:
   ```python
   def subadmin_task_durations(request):
       """System Admin view for task duration templates — same data as admin version, own template."""
   ```
3. **A separate base template.** `projects/templates/projects/subadmin/` contains
   `subadmin_base.html`, `departments.html`, `projects.html`, `task_durations.html` — its own
   sidebar, entirely.

Data scoping is expressed as explicit constants, `projects/views.py:9210-9223`:

```python
# Roles that System Admin must never see, query, or be able to assign
_SA_EXCLUDED_ROLES = ['Admin', 'System Admin']

# Operational roles System Admin may create/edit — never includes Admin or System Admin
_SA_EDITABLE_ROLE_CHOICES = [
    ('PM',                  'PM'),
    ...
]
```

**CONFLICT WITH THE PROMPT.** The prompt says: *"If a pattern exists for 'this role sees these
portal-admin screens only', that pattern is the answer and should be reused rather than
reinvented."* No such pattern exists. The established pattern in this codebase is
**duplicate the screen into your own namespace with your own base template** — which is
**option B**, not option A. If precedent is the deciding criterion, precedent points at B.

### 2.5 The canonical Design Head authority helper

`projects/permissions.py:434-453`:

```python
def user_has_design_head_authority(user):
    """Return True if `user` may act with Design Head authority — the Head OR his
    named deputy.

    THIS IS THE FUNCTION VIEWS SHOULD CALL. It is the single gate for survey upload,
    allocation, due-date approval and rejection, Arka approve and reject, and QC start
    and verdict — everywhere Parts 2 and 3 used `is_design_head` directly.

    It deliberately does NOT confer portfolio-wide project or BOQ visibility. The Head
    gets that from his own branches in user_can_view_project() and
    user_can_view_project_boq(), which are out of scope for this part and are not
    touched; a deputy sees design surfaces through user_can_view_design() below and
    otherwise keeps whatever visibility their own role gives them. Widening a deputy's
    read access across the whole portfolio would be a much larger decision than
    "somebody is covering QC this week".

    NOT SUFFICIENT FOR QC ON ITS OWN — see user_can_qc_design(), which additionally
    refuses the assigned designer.
    """
    return user_is_design_head(user) or user_is_design_head_deputy(user)
```

**Any new gate must call `user_has_design_head_authority(user)`** — not `is_design_head`, not a
role-string comparison.

**One design tension to flag, not resolve.** This helper admits the Head **and his deputy**.
Whether "a deputy covering QC this week" should also be able to add and delete catalogue rows is
a real question the request does not answer. If the answer is Head-only, the correct call is
`user_is_design_head(user)`, and that would be the first place in the module to deliberately
prefer the narrow helper — worth stating explicitly wherever it lands so it does not read as
having missed the canonical one.

**Second tension:** `role_required` compares `UserProfile.role`, while Design Head authority is a
flag on the profile (`is_design_head`), and a Design Head's `role` is `'Design'`. The two gates
are different mechanisms. A combined gate cannot be written as
`@role_required(['Admin', 'Design'])` — that would admit **every** designer. It has to be an
explicit `if` inside the view, or a new decorator.

---

## PART 3 — THE THREE OPTIONS, WITH EVIDENCE (no choice made)

**The deciding fact, stated plainly:** the sidebar at
`projects/templates/projects/admin/admin_base.html:82-147` is **nine hardcoded `<a>` tags with
no permission logic and no context variable**. It is not data-driven.

### Option A — grant the Head access to the existing view, filter the sidebar

**Files touched:** `projects/views.py` (4 gates), `projects/templates/projects/admin/admin_base.html`
(sidebar rewritten to be permission-aware), and a context processor or per-view context to feed
it. Realistically 3 files.

**Changes what an Admin sees:** **Yes — unavoidably.** Making the sidebar conditional means
editing the markup every Admin screen renders. The nine links are shared by all nine templates
through `{% extends %}`, so any error lands on every admin screen at once. That is the risk, and
it is larger than the feature.

**Third role later:** poor. Each new role adds another condition to every one of nine hardcoded
links, or forces the data-driven rewrite that should have happened first.

**Cheapest only if** you accept the Head seeing eight links that bounce him — in which case the
sidebar is untouched and the change is 4 decorator lines in one file. That variant is genuinely
the smallest possible change, and its whole cost is the misleading nav.

### Option B — a second view and template in the design module namespace

**Files touched:** `projects/urls.py` (4 new routes), `projects/design_views.py` (4 new views, or
thin wrappers), one new template directory (list + form), reusing `BOQItemMasterForm` and
`BOQItemMaster` unchanged. Realistically 4 files, one of them new.

**Changes what an Admin sees:** **No. Nothing.** The existing screen, its gates and its sidebar
are untouched. This is the only option with a zero blast radius on the Admin surface.

**Third role later:** good if the second copy is written as a shared implementation with two thin
gated entry points; poor if it is a literal copy-paste, which is how the `subadmin_*` precedent
did it — three near-duplicate views and a duplicate base template.

**This is what the codebase already does** for the System Admin (2.4). Duplicated template is the
known, accepted cost.

### Option C — move the screen out of portal-admin into a shared location

**Files touched:** `projects/urls.py` (URL change — **breaks any bookmark and any external link**),
`projects/views.py` (gate rewritten to admit two role mechanisms), both templates (they
`{% extends "projects/admin/admin_base.html" %}` at line 1 of each, so they would need a new
base), the sidebar entry, and `{% url %}` reversals are safe since names would be kept.

**Changes what an Admin sees:** **Yes** — the screen moves out of their sidebar's Settings group,
or the sidebar keeps a link to a URL that is no longer under portal-admin. Either way the Admin's
mental model of "everything under portal-admin is mine" breaks, and that model is currently
enforced by a comment at `projects/urls.py:301`.

**Third role later:** best of the three — a genuinely shared location with one gate listing
permitted roles. But it is the largest change and the only one that alters an existing URL.

---

## PART 4 — DEACTIVATION SEMANTICS

### 4.1 The OPEX picker's catalogue queryset — IT DOES FILTER (hard stop 4 does NOT fire)

`projects/views.py:4627` inside `opex_boq_entry` (`projects/views.py:4591`):

```python
    catalogue      = get_opex_boq_catalogue()
```

`projects/models.py:751-770`:

```python
def get_opex_boq_catalogue():
    """The active OPEX catalogue, in spreadsheet order — what the Part 11 picker offers.

    Returns model instances, not dicts, because the picker needs the pk: a chosen row is
    recorded as BOQItem.item_master, which is the join Part 6 aggregation runs on. That is
    the whole reason the picker writes catalogue rows rather than free text.

    NO PRE-POPULATION. Unlike get_standard_boq_items(), nothing calls this to seed a BOQ.
    At 207 items a designer would scroll past ~160 irrelevant rows on every one of
    potentially 200 sites, so the sheet starts empty and they add what the site uses.

    Returns [] on an empty catalogue rather than raising, unlike its Residential
    counterpart: an empty picker renders as "nothing to add", which is a legible screen,
    whereas the Residential path would silently create a BOQ with no rows at all.
    """
    return list(
        BOQItemMaster.objects
        .filter(is_active=True, project_type='OPEX')
        .order_by('sort_order', 'code')
    )
```

**`filter(is_active=True, project_type='OPEX')`. Deactivation has a real, immediate effect on
the picker.**

Stronger still — it is enforced on **POST**, not merely on render.
`projects/views.py:4649-4654`:

```python
        # The chosen catalogue rows, in the order the sheet lists them. Anything that is
        # not an active OPEX master pk is dropped rather than trusted — this is a POST.
        chosen = []
        for raw in request.POST.getlist('item'):
            if raw.isdigit() and int(raw) in catalogue_by_id and int(raw) not in chosen:
                chosen.append(int(raw))
```

`catalogue_by_id` is built from the active catalogue at `projects/views.py:4629`, so a
hand-crafted POST naming a deactivated pk is silently dropped. A deactivated item cannot be
added by any route.

`opex_catalogue_category_order()` filters identically, `projects/models.py:783-786`.

### 4.2 The Residential pre-population path — also filters

`projects/models.py:729-734` inside `get_standard_boq_items()`:

```python
    rows = list(
        BOQItemMaster.objects
        .filter(is_active=True, project_type='Residential')
        .order_by('sort_order', 'code')
        .values('sort_order', 'category', 'description', 'unit')
    )
```

and the seeding caller independently re-filters, `projects/views.py:4328-4332`:

```python
            masters = {
                m.description: m
                for m in BOQItemMaster.objects.filter(is_active=True,
                                                      project_type='Residential')
            }
```

Both filter on `is_active=True`. A deactivated Residential item stops appearing on new BOQs.

Note the documented stability guarantee, `projects/models.py:715-717`: *"serial numbers must stay
stable — serial_no comes from the catalogue's sort_order, not from row position, so deactivating
an entry does not renumber the rest."*

### 4.3 A `BOQItem` whose master is deactivated after the fact

**Draft BOQ (OPEX):** the row survives and is shown, reclassified as off-catalogue.
`projects/models.py:792-811`:

```python
def split_opex_boq_rows(boq, catalogue_ids):
    """Split a BOQ's rows into (catalogue rows, off-catalogue rows).

    `catalogue_ids` is the set of ACTIVE OPEX master pks. A row whose master was
    deactivated after it was added counts as off-catalogue — it is still a real quantity on
    a real sheet, so it renders and can be removed, it just cannot be re-added.

    OFF-CATALOGUE ROWS ARE NOT AN ERROR CONDITION. OPEX sites created before Part 11 were
    seeded from the Residential template by the shared boq_detail, and an ad-hoc row has no
    `item_master` at all. Both are real data; both are shown wherever the sheet is shown.

    Lives here rather than in views.py because BOTH the entry screen and the Part 9 review
    screen need it, and design_views must not import views.
    """
    on, off = [], []
    if boq is None:
        return on, off
    for item in boq.items.select_related('item_master').order_by('serial_no', 'pk'):
        (on if item.item_master_id in catalogue_ids else off).append(item)
    return on, off
```

**This case is explicitly designed for and documented.** The row renders, keeps its quantity, and
can be removed — it simply cannot be re-added.

**Design-locked BOQ:** the same split runs on the reviewer's screen,
`projects/design_views.py:1608-1610`:

```python
    category_order = opex_catalogue_category_order()
    catalogue_ids  = {m.pk for m in get_opex_boq_catalogue()}
    on_rows, off_rows = split_opex_boq_rows(boq, catalogue_ids)
```

so the row moves into the reviewer's off-catalogue group. Nothing is lost; the sheet is read-only
in this state anyway.

**Group-locked BOQ:** quantities are frozen and no write path is open, so deactivation cannot
change the stored data at all.

**The Part 6 aggregation — `aggregate_group_boq()`, `projects/design_views.py:4146-4155`:**

```python
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
```

**IT DOES NOT FILTER ON `is_active`.** It filters on `item_master__isnull=False` and
`boq_quantity__gt=0` only. **This is correct and is the behaviour you want** — a deactivated
catalogue row still aggregates, so procurement totals do not silently drop when somebody retires
an item mid-tender. The docstring states the principle, `projects/design_views.py:4126-4128`:

```python
    THE JOIN IS `item_master`, WHICH IS WHY BOQItemMaster EXISTS (Part 0.5). Every item
    aggregates the same way — there is no per-item rule on the master and none is
    invented here.
```

That last sentence is a direct warning to Session D: **the aggregation deliberately has no
per-item rule.** A mandatory flag must not become one here.

### 4.4 `[PRODUCTION]` Inactive catalogue rows

Read via the Railway public proxy against host `acela.proxy.rlwy.net`, database `railway`.
Read-only SELECTs. Credentials were not printed.

```
{'project_type': 'OPEX', 'is_active': True, 'n': 207}
{'project_type': 'Residential', 'is_active': True, 'n': 37}
total: 244 inactive: 0
```

`[PRODUCTION]` **Zero inactive rows, of either type. 244 of 244 active.** The screenshot is
confirmed exactly. **The deactivate path has never been exercised in production.**

### 4.5 Guard against deactivating an item in use — THERE IS NONE

`admin_boq_item_toggle` (`projects/views.py:9117-9136`, quoted in 1.4) reads **no** count and
performs **no** check. It flips the flag unconditionally.

The `linked_count` annotation from 1.5 **is displayed and never consulted.** The edit screen
shows it as prose, `projects/templates/projects/admin/boq_item_form.html:16-17`:

```html
      {{ linked_count }} existing BOQ row{{ linked_count|pluralize }} link{{ linked_count|pluralize:"s," }} to this entry —
      saving here does not change any of them.
```

**Information available, purely advisory.** Given 4.3, that is defensible — deactivating an
in-use item is safe by construction, since existing rows keep rendering and keep aggregating.
There is also **no confirmation dialog**: the Deactivate button is a bare submit
(`boq_items.html:96-105`), one click, no `onsubmit` confirm.

---

## PART 5 — THE MANDATORY FLAG

### 5.1 Does anything like it already exist? — NO

**Plainly: no such field exists.** `BOQItemMaster` has exactly nine fields (1.2): `code`,
`description`, `unit`, `category`, `project_type`, `is_active`, `sort_order`, `created_at`,
`updated_at`. A search for `is_mandatory`, `mandatory`, `always_include`, `is_required` and
`required_item` across `models.py`, `forms.py`, `views.py` and `design_views.py` returns only
unrelated hits — an invoice document rule (`models.py:1487`, `views.py:5182`) and the design
error-category / reason rules (`models.py:1820`, `design_views.py:1032`, `1202`, `1462`, `1966`,
`2089`, `2769`, `2916`, `3273`). **Nothing near the BOQ catalogue.**

A migration is therefore **required**. It would be `0064`, one additive `AddField`, and it must
be `default=False` — no backfill, and every one of the 244 production rows lands on `False`.

### 5.2 Where the column and form field would go

**The Add/Edit form needs NO template change.**
`projects/templates/projects/admin/boq_item_form.html:31-44`:

```html
  {% for field in form %}
    <div class="mb-5">
      <label for="{{ field.id_for_label }}" class="block text-sm font-medium text-gray-700 mb-1">
        {{ field.label }}{% if field.field.required %} <span class="text-red-500">*</span>{% endif %}
      </label>
      {{ field }}
      {% if field.help_text %}
        <p class="text-xs text-gray-500 mt-1">{{ field.help_text }}</p>
      {% endif %}
      {% for error in field.errors %}
        <p class="text-xs text-red-600 mt-1">{{ error }}</p>
      {% endfor %}
    </div>
  {% endfor %}
```

A generic field loop. Adding the field name to `BOQItemMasterForm.Meta.fields`
(`projects/forms.py:486-487`) is sufficient — it renders, styles itself via the `__init__`
widget-class loop (`forms.py:491-497`, which already handles `CheckboxInput`), and picks up help
text the same way the other fields do.

**The list table DOES need a change**, and it has room. Header,
`projects/templates/projects/admin/boq_items.html:56-66`, currently nine columns:

```html
    <tr class="bg-gray-100 text-left text-xs font-semibold text-gray-600 uppercase tracking-wider">
      <th class="px-4 py-2 w-16 text-center">Sort</th>
      <th class="px-4 py-2 w-28">Code</th>
      <th class="px-4 py-2 w-28">Type</th>
      <th class="px-4 py-2">Description</th>
      <th class="px-4 py-2 w-20">Unit</th>
      <th class="px-4 py-2 w-36">Category</th>
      <th class="px-4 py-2 w-24 text-center">BOQ rows</th>
      <th class="px-4 py-2 w-24 text-center">Active</th>
      <th class="px-4 py-2 w-40"></th>
    </tr>
```

Every column except Description carries a fixed width; **Description is the only fluid column**
(no `w-` class), so a new `w-24` cell steals width from Description alone and nothing reflows.
The existing Active cell (lines 85–91) is a ready-made pattern to copy — a pill badge. **No
restructuring needed at the screenshot's widths.**

Two more edits go with it: the `{% empty %}` row's `colspan="9"` at line 109 becomes `10`, and a
mandatory filter — if wanted — mirrors the existing Show/Type filter blocks (lines 34–52).

### 5.3 What currently pre-populates a BOQ — the Residential path

`projects/views.py:4310-4336`, inside `boq_detail`:

```python
    if boq is None:
        # Seeding 53 catalogue rows is a WRITE, so it takes the write gate even though we
        # are on a GET. A reader with no authorship relationship to this project must not
        # be able to bring a BOQ into existence just by loading the page — nor may one
        # appear on a site whose group has already been locked.
        if user_can_edit_project_boq(request.user, project) and not boq_group_locked:
            boq = BOQ.objects.create(project=project)
            # description is copied as a point-in-time snapshot; item_master carries the
            # stable catalogue link that quantity aggregation across sites joins on.
            #
            # PART 11 — THE project_type TERM IS REQUIRED, NOT COSMETIC. This dict is keyed
            # by description, and three descriptions now exist in BOTH catalogues:
            # "PVC Elbow 25MM" (ITM-015 / OPX-131), "PVC Tee 25MM" (ITM-016 / OPX-132) and
            # "Silver Spray Paint" (ITM-024 Nos / OPX-193 Kg). Unscoped, the OPEX row wins
            # the key — it sorts later — and a Residential BOQ line would silently carry an
            # `item_master` pointing at an OPEX catalogue row, which is the join Part 6
            # aggregation runs on. It restores the pre-Part-11 result exactly: before the
            # 207 rows existed, `is_active=True` returned only Residential rows.
            masters = {
                m.description: m
                for m in BOQItemMaster.objects.filter(is_active=True,
                                                      project_type='Residential')
            }
            BOQItem.objects.bulk_create([
                BOQItem(boq=boq, item_master=masters.get(item_data['description']), **item_data)
                for item_data in get_standard_boq_items()
            ])
```

**The shape Session D should know.** Pre-population is a **one-shot `bulk_create` at BOQ
creation**, gated on the write permission, triggered lazily by a GET on a BOQ that does not yet
exist. It is **all-or-nothing** — every active Residential row, in `sort_order`. There is no
incremental "add the missing mandatory rows to an existing BOQ" mechanism anywhere, and the OPEX
side has no seeding call at all (`get_opex_boq_catalogue()`'s docstring: *"NO PRE-POPULATION.
Unlike get_standard_boq_items(), nothing calls this to seed a BOQ."*).

**Note the description-keyed dict.** It is keyed by `description`, not code, and already has a
documented three-way collision hazard. Any Session D work that reuses this shape inherits that.

### 5.4 OPEX-only or both types? — REPORTED, NOT DECIDED

**What the data model permits:** the flag would sit on the shared `BOQItemMaster` table, so it is
**structurally available to both types whether or not both use it.** `project_type` is a plain
`CharField` with no per-type schema, and there is no mechanism to make a field apply to one type
only. Scoping would have to be by convention plus a form-level or view-level rule.

**What existing Residential pre-population would do if Residential rows were flagged:** *nothing
at all.* Every active Residential row is **already** pre-populated onto every new Residential BOQ
(5.3). A mandatory flag on a Residential row would be **a no-op by construction** — the item is
already there, unconditionally. It could only become meaningful if it were also used to prevent
*removal* of a row, and Residential BOQ rows have no removal path that consults the catalogue.

**The asymmetry, stated plainly:** OPEX starts empty and the designer adds rows, so "mandatory"
has real work to do. Residential starts full, so "mandatory" describes a state that already
holds. The request's own words — *"In BOQ a few mandatory items must be there"* — describe the
OPEX problem.

**Not decided here.** The relevant risk if it is made global: 37 Residential rows get a column
that means nothing on their side of the table, which invites a later reader to assume it is
enforced there.

### 5.5 Sort order — per type, and not unique by constraint

**Per type by data, global by schema.** The model comment at `projects/models.py:700-704` is
explicit:

```python
        # DELIBERATELY UNCHANGED by Part 11. `sort_order` now repeats across types (both
        # ITM and OPX start at 1), so the unfiltered default ordering interleaves them —
        # which is fine, because every functional query filters by project_type first and
        # the one screen that does not (the admin catalogue) orders explicitly. Adding
        # project_type here would silently reorder aggregate_group_boq()'s output.
```

**Uniqueness — `[PRODUCTION]`:**

```
dupe (type,sort_order) pairs: 0 []
globally repeated sort_order values: 37
```

- **Within a `project_type`, `sort_order` is currently unique** — zero duplicate pairs across all
  244 rows.
- **Globally it is not** — 37 values (1..37) appear twice, once for Residential and once for OPEX.

**But that uniqueness is a property of the seeded data, not a guarantee.** `Meta` declares no
`constraints` and no `unique_together` (1.2), `sort_order` is a plain `PositiveIntegerField` with
`default=0`, and `BOQItemMasterForm` has no `clean_sort_order`. **Nothing prevents two rows of
the same type sharing a `sort_order`** — and the unscoped `Max()` suggestion in 1.6 means a Head
adding Residential items would be handed `208`, drifting the Residential block away from the
1..37 contiguity it currently has.

If Session D wants mandatory items first in the picker, ordering by `(-is_mandatory, sort_order,
code)` would work on today's data, with ties broken by `code`, which **is** unique.

---

## PART 6 — BLAST RADIUS

### 6.1 Every place `BOQItemMaster` is read, written or joined

**Model and catalogue helpers — `projects/models.py`**

| Line | Note |
|---|---|
| 654–708 | Model definition |
| 711–748 | `get_standard_boq_items()` — reads active Residential, raises on empty |
| 751–770 | `get_opex_boq_catalogue()` — reads active OPEX, returns `[]` on empty |
| 773–789 | `opex_catalogue_category_order()` — reads active OPEX, derives category order |
| 792–811 | `split_opex_boq_rows()` — takes active pks, classifies stored rows |
| 882–887 | `BOQItem.item_master` FK, `SET_NULL`, `related_name='boq_items'` |
| 2354 | Comment only — settled decision 7 |
| 2752 | Comment only — Part 6 aggregation note |

**Admin catalogue screen — `projects/views.py`**

| Line | Note |
|---|---|
| 26–28 | Imports of model + three helpers |
| 38 | Import of `BOQItemMasterForm` |
| 9034–9036 | List queryset + `linked_count` annotation |
| 9054 | Per-type count for the filter chips (one query per type) |
| 9056–9057 | Active / total counts |
| 9066–9071 | Create — form save + activity log |
| 9076–9077 | `Max('sort_order')` suggestion (unscoped by type) |
| 9092–9106 | Edit — `get_object_or_404`, `code` deleted from form |
| 9113 | `item.boq_items.count()` for the edit screen |
| 9127–9134 | Toggle — flips `is_active`, activity log |

**BOQ screens — `projects/views.py`**

| Line | Note |
|---|---|
| 4301–4302 | OPEX author redirect to the picker |
| 4328–4336 | Residential seeding — description-keyed master lookup + `bulk_create` |
| 4591–4780 | `opex_boq_entry` — picker; 4627–4629 build the active catalogue, 4649–4654 validate the POST against it, 4690 and 4736 split stored rows |

**Design module — `projects/design_views.py`**

| Line | Note |
|---|---|
| 73–74 | Imports of the four catalogue helpers |
| 1608–1610 | Part 9 reviewer's read-only sheet — same split as the picker |
| 4123–4184 | `aggregate_group_boq()` — joins `item_master`, **does not** filter `is_active` |

**Migrations**

| File | Note |
|---|---|
| `0047_boqitemmaster_boqitem_item_master.py` | Creates the model, seeds 37 Residential rows, backfills `BOQItem.item_master`; reverse deletes by code |
| `0057_boqitemmaster_project_type_opex_catalogue.py` | Adds `project_type`, seeds 207 OPEX rows; reverse deletes by prefix + type |

**Tests / commands**

| File | Note |
|---|---|
| `projects/tests_design_part11.py` | ~25 references; **suite is pre-existing-broken, see UNCERTAIN** |
| `projects/management/commands/seed_scm_handoff_data.py:123-155, 321-365` | Dev seeding; deliberately leaves one ad-hoc row with no `item_master` |

**Not referenced anywhere:** `projects/admin.py` (model not registered) and
`projects/design_analytics.py` (see 6.4).

### 6.2 Residential seeding, and whether a Head editing OPEX touches it

**The 37 rows come from migration `0047`.** `projects/migrations/0047_...py:5-8`:

```python
# Verbatim snapshot of the literal list that get_standard_boq_items() returned before
# this migration. The function now reads BOQItemMaster instead; this copy exists solely
# to seed the catalogue and must never be edited — later catalogue changes belong in the
# admin screen, not here.
```

with codes assigned deterministically, `projects/migrations/0047_...py:50-51`:

```python
DEFAULT_UNIT = 'Nos'          # Applied only to seed rows whose source dict carries no uom
CODE_TEMPLATE = 'ITM-{:03d}'  # Deterministic: ITM-001 … ITM-037, by position in the list above
```

**Would a Head editing OPEX rows touch anything Residential depends on? No — with one caveat.**

Every Residential read path carries `project_type='Residential'` (`models.py:731`,
`views.py:4330-4331`), and every OPEX read path carries `project_type='OPEX'`
(`models.py:768`, `784`). The two are disjoint by filter, so editing, adding or deactivating an
OPEX row cannot change what a Residential BOQ is seeded with.

**The caveat is `project_type` itself.** It is an editable form field
(`projects/forms.py:486`) with no guard. An item master editor who changes an existing OPEX row's
`project_type` to `Residential` **immediately adds it to every new Residential BOQ**. That is one
dropdown on the edit screen, and it is the single field on this form with cross-type blast
radius. Worth knowing before the screen is handed to a second role — reported, not fixed.

### 6.3 `[PRODUCTION]` The two null-`item_master` rows

```
36 item_master_id= None qty= None serial= 36 proj= HRP-RES-2026-003 type= Residential desc= Miscellaneous net metering transportation rubber mat fire ex
74 item_master_id= None qty= None serial= 36 proj= HRP-RES-2026-008 type= Residential desc= Miscellaneous net metering transportation rubber mat fire ex
null item_master rows total: 2
their pks: [36, 74]
```

`[PRODUCTION]` **Both still exist, and they are still the only two.** Confirmed against
`BLOCK0_VERIFICATION.md`: PKs 36 and 74, both Residential, both null quantity, both `serial_no`
36, both the "Miscellaneous" line.

The cause is visible in the seed data — `projects/migrations/0047_...py:44` stores the
description with hyphen and no spaces after commas:

```python
        {'serial_no': 36, 'category': 'BOS',           'description': 'Miscellaneous - (net metering,transportation,rubber mat,fire extinguishers,warning boards)',             'uom': 'Nos'},
```

while the two stored rows carry a differently-punctuated variant, so the description-keyed
lookup in `views.py:4328-4332` found no match and `masters.get(...)` returned `None`.

**Does an item master screen change interact with them? No.** They have no `item_master`, so no
catalogue edit, deactivation or new flag can reach them. They surface only in
`aggregate_group_boq()`'s `unlinked` list (`design_views.py:4171-4177`), and only if they ever
carry a quantity above zero — both are null, so today they do not appear even there. A mandatory
flag would not touch them. **Out of scope, unchanged, and correctly so.**

### 6.4 Does `design_analytics.py` or Part 6 assume an immutable catalogue?

**`design_analytics.py`: no coupling at all.** A search of all 1,261 lines for `BOQItemMaster`,
`item_master` and `is_active` returns **zero matches**. Its `catalogue_for_display()` (line 325)
is a catalogue of *metrics*, not of BOQ items. **Nothing there can be wrong if items are added or
deactivated.**

**Part 6 aggregation: it explicitly does NOT assume immutability**, and says so —
`projects/design_views.py:4126-4128`:

```python
    THE JOIN IS `item_master`, WHICH IS WHY BOQItemMaster EXISTS (Part 0.5). Every item
    aggregates the same way — there is no per-item rule on the master and none is
    invented here.
```

Because it filters on `item_master__isnull=False` rather than `is_active=True` (4.3), an item
deactivated mid-tender keeps aggregating and totals do not move.

**The one thing that WOULD be wrong if items changed mid-tender** is ordering, not totals.
`projects/design_views.py:4154`:

```python
        .order_by('item_master__sort_order', 'item_master__code')
```

`sort_order` is mutable from the edit screen with no constraint (5.5). Editing it mid-tender
**reorders an already-reviewed procurement sheet** — quantities stay correct, but a line moves.
Given that grouped procurement is the point of the join, a reviewer comparing two printouts of
the same locked group could see rows in different orders. Low severity, no data loss, but it is
the one real "catalogue is not immutable" consequence in Part 6. Reported, not fixed.

---

## UNCERTAIN

1. **Whether the Design Head should include his deputy.** `user_has_design_head_authority()`
   admits both (2.5). The request says "Design head" and "the item head". Which of the two
   helpers is correct is a product decision, not a code fact.

2. **Whether "the item head" in item 7 is the same person as "Design head" in item 6.** The
   request uses two different phrases. This audit assumed they are the same role because the two
   items were handed over together, but nothing in the code or the request establishes it. If
   "item head" is a distinct person, Part 3's "what if a third role needs access later" stops
   being hypothetical.

3. **Whether the Head should get add + deactivate but not edit**, or all three. The request says
   "add or delete items" — and there is no delete, only deactivate (1.4). Whether "delete" in the
   request means deactivate, or means the requester expects rows to vanish, is not established.
   **This is the most likely place the request and the system disagree.**

4. **`tests_design_part11.py` cannot be run to check for regressions.** Per prior sessions the
   suite errors 100% in `setUp` (ITM-001 duplicate against migration 0047). This audit did not
   run it — running it is read-only but would have produced no usable signal, and fixing it is
   out of scope. Any Session C/D implementation lands without that suite as a safety net.

5. **CAPEX.** `Project.PROJECT_TYPE_CHOICES` includes CAPEX and the catalogue has zero CAPEX rows
   `[PRODUCTION]`. Whether CAPEX ever gets a catalogue, and whether a mandatory flag should
   anticipate it, is not established.

6. **No local database comparison was made.** All database results here are `[PRODUCTION]`. The
   local dev database was not queried, because every question asked about production state.

---

## CONFLICTS — where this prompt's assumptions disagree with the code

1. **"If a pattern exists for 'this role sees these portal-admin screens only', that pattern is
   the answer"** (2.4). **No such pattern exists.** The only precedent is the System Admin's
   fully parallel `sub-admin/` namespace with its own views and its own base template — which is
   option B, not option A. The prompt's hint points the wrong way.

2. **"Cheapest if the sidebar is data-driven"** (Part 3). **It is not data-driven.** Nine
   hardcoded `<a>` tags, no permission logic, shared by all nine admin screens
   (`admin_base.html:82-147`).

3. **1.6 anticipates a code generator with a deletion-resilience bug.** **There is no generator.**
   Codes are typed by the user and validated by regex. The `generate_project_id()` bug shape does
   not apply. A different, smaller issue exists instead: the `sort_order` suggestion uses an
   unscoped `Max()`.

4. **Hard stop 4 was expected to be a live risk.** It is not. Both the OPEX picker and the
   Residential seeding filter on `is_active`, and the picker enforces it on POST as well as on
   render (4.1, 4.2).

5. **Hard stop 5 was expected to possibly fire.** It does not. All four item master views carry
   `@role_required(['Admin'])`. Session A's `dashboard_admin` finding is real and confirmed
   (2.3), but it is a different view and does not extend to this screen.

6. **The prompt calls the screen's actions "Edit and Deactivate"** — correct — but item 6 asks
   for "add or **delete**". No delete exists, by explicit design (1.4). The request and the
   system use different words for different things.

7. **The opening check expects `[Session B]` and `[Session B.1]` as separate commits.** They are
   one commit with no tag prefix, and no commit in this repo has ever used that form.

8. **The prompt describes the BOQ ROWS column as if it might be an N+1.** On the list screen it
   is a single annotation. (The edit screen does a separate `.count()`, but that is one query on
   a one-object page.)

---

## CLOSING TABLE

| Item | Question | Status |
|---|---|---|
| 1.1 | Locate screen: URL, view, template, lines | ANSWERED |
| 1.2 | `BOQItemMaster` in full; `is_active` real? | ANSWERED |
| 1.3 | Add / edit / deactivate handlers; form vs raw POST; validation | ANSWERED |
| 1.4 | What Deactivate does; any hard-delete path | ANSWERED |
| 1.5 | BOQ ROWS column: annotation or N+1; what it counts | ANSWERED |
| 1.6 | Code generation: generated or typed | ANSWERED |
| 1.7 | Trailing-comma summary line | ANSWERED (not fixed) |
| 2.1 | Gate on view and write handlers; who passes | ANSWERED |
| 2.2 | Sidebar block; gate on each of nine targets | ANSWERED |
| 2.3 | `dashboard_admin` finding still holds? | ANSWERED — confirmed |
| 2.4 | Precedent for non-Admin in portal-admin | ANSWERED — none; inverted precedent found |
| 2.5 | Canonical Design Head authority helper | ANSWERED |
| 3 | Options A / B / C: cost, risk, Admin impact, third role | ANSWERED (no choice made) |
| 4.1 | OPEX picker filters `is_active`? | ANSWERED — yes |
| 4.2 | Residential pre-population filters `is_active`? | ANSWERED — yes |
| 4.3 | Deactivated master on draft / design-locked / group-locked; aggregation | ANSWERED |
| 4.4 | `[PRODUCTION]` inactive rows by type | ANSWERED — zero |
| 4.5 | Guard against deactivating an in-use item | ANSWERED — none |
| 5.1 | Does a mandatory-like field exist? | ANSWERED — no |
| 5.2 | Where column and form field would go | ANSWERED |
| 5.3 | Residential pre-population code and shape | ANSWERED |
| 5.4 | OPEX-only or both types | ANSWERED (reported, not decided) |
| 5.5 | `sort_order` per-type or global; unique? | ANSWERED |
| 6.1 | Every read / write / join of `BOQItemMaster` | ANSWERED |
| 6.2 | Residential seeding source; cross-type risk | ANSWERED |
| 6.3 | `[PRODUCTION]` PKs 36 and 74 still present | ANSWERED — both present |
| 6.4 | `design_analytics` / Part 6 immutability assumptions | ANSWERED |

**No item is PARTIAL or NOT ESTABLISHED.** Open questions are product decisions, not unverified
code — they are listed under UNCERTAIN.

---

## SIZE ESTIMATE

**The premise holds.** Item 6 is largely built. State that first because it changes the shape of
the work: this is an access-control change plus one column, not a CRUD build.

### Item 6 — Design Head access

| Option | Files | Migration | Notes |
|---|---|---|---|
| A (minimal, no sidebar work) | 1 (`views.py`) | No | 4 gates. Head sees 8 links that bounce him. |
| A (full, sidebar filtered) | 3 | No | Touches markup every Admin screen renders. |
| B (parallel namespace) | 4, one new dir | No | Zero Admin blast radius. Matches the `subadmin_*` precedent. |
| C (move out of portal-admin) | 5+ | No | Changes an existing URL. Largest. |

**No migration under any option.**

### Item 7 — the mandatory flag

- **Migration required:** yes. One additive `AddField`, `default=False`, no backfill. Would be
  `0064`.
- **Files:** `models.py` (field), `forms.py` (one entry in `Meta.fields`, one help_text),
  `boq_items.html` (one `<th>`, one `<td>`, `colspan` 9→10). The form template needs **no**
  change (5.2).
- **Three files plus a migration.** Small and low-risk — the flag is inert until Session D reads
  it.

### One session or two?

**One session, provided the ordering is right.** Item 7 is genuinely small and touches files
item 6 barely touches. The two overlap in exactly one place — whichever template ends up being
the Head's list view gets the new column — and doing them together means writing that column
once rather than twice.

**The one thing that would force two sessions** is choosing option B or C for item 6. Under
those, item 7's column has to be added to a template that does not exist yet, so item 6 must land
first. Under option A they are trivially concurrent.

**Recommended sequencing regardless:** decide Part 3 before writing anything, because that
decision determines whether item 7 edits one template or two.

**Two things to resolve before either starts,** both from UNCERTAIN and both cheap to answer:
whether "delete" in the request means deactivate (it must — there is no delete, by design), and
whether the deputy is included.
