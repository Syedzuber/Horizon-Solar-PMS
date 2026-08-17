# SESSION D AUDIT — Enforcing mandatory items in the OPEX BOQ picker

**Mode:** investigate only. No application code written, no migration created, no database write.
The only file created is this report.

**Opening check**

| Check | Result |
|---|---|
| Repo | `c:\SolarPMS\Horizon-Solar-PMS` |
| `git status` | clean tracked tree; untracked = `SESSION_B_AUDIT.md`, `SESSION_C_AUDIT.md` (both `.md`) — **pass** |
| Local HEAD | `92e1a50d3787e8953452557df4351d5e336259ae` `[Session C.1]` |
| `[Session C]` / `[Session C.1]` present | yes (`ee8c752`, `92e1a50`) |
| Deployed SHA | `92e1a50d3787e8953452557df4351d5e336259ae`, deployment `4f99560f`, SUCCESS, 2026-08-15 02:16:07 UTC — **equals local HEAD** |
| `origin/main` | `92e1a50` — in step |
| Migration head | `0064_boqitemmaster_is_mandatory.py` |

The session was hard-stopped once on the deployed-SHA check (C.1 was committed locally but never
pushed or deployed). It was deployed between that stop and this run; the check now passes and the
audit proceeded.

**Hard-stop status at close**

| # | Condition | Triggered? |
|---|---|---|
| 1 | Dirty tree / non-`.md` untracked | No |
| 2 | Deployed SHA ≠ HEAD | No (resolved before this run) |
| 3 | Design lock template-only | **No** — enforced on POST, `views.py:4649-4653` |
| 4 | POST deletes and recreates all rows | **No** — it is a diff, see 1.4 |
| 5 | Group-locked OPEX BOQ reachable by injection | **No** — zero `SiteGroup` rows exist on production |
| 6 | A question needing a database write | No |

Database access: production was read over the Railway TCP proxy using the commented `DATABASE_URL`
on line 3 of `.env`, through a `psycopg2` connection opened with `set_session(readonly=True)` and
closed with `rollback()`. `.env` was not modified.

---

# PART 1 — THE PICKER AS IT STANDS

## 1.1 `opex_boq_entry` in full, and the GET/POST boundary

[views.py:4599-4795](projects/views.py#L4599-L4795). The view is one function; the boundary is the
`if request.method == 'POST':` at line 4639 and the `# ---- GET ----` marker at line 4743. Lines
4614-4637 are **shared preamble** — they run on both paths — which matters for this session,
because that is where the catalogue is loaded and where the lock predicates are computed.

Preamble (4614-4637):

```python
    project = get_object_or_404(Project, project_id=project_id)

    if project.project_type != 'OPEX':
        raise Http404('The BOQ picker is for OPEX sites. Residential BOQs are entered on '
                      'the standard BOQ screen.')

    # Read gate first and separately from the write gate, exactly as boq_detail does: they
    # are different questions and one must not stand in for the other.
    if not user_can_view_project_boq(request.user, project):
        return HttpResponseForbidden()

    boq_group_locked  = project_boq_is_group_locked(project)
    boq_design_locked = project_boq_is_design_locked(project)
    can_author        = user_can_edit_project_boq(request.user, project)
    can_edit          = can_author and not boq_group_locked and not boq_design_locked

    try:
        boq = project.boq
    except BOQ.DoesNotExist:
        boq = None

    catalogue      = get_opex_boq_catalogue()
    category_order = opex_catalogue_category_order()
    catalogue_by_id = {m.pk: m for m in catalogue}
```

POST path (4639-4741) — quoted in full in 1.4 below.

GET path (4743-4795):

```python
    # ---- GET ----
    added_on, added_off = split_opex_boq_rows(boq, set(catalogue_by_id))

    # The catalogue, as the picker's JS consumes it. Items already on the sheet are still
    # sent — the JS filters them out of the results and needs them to render the sheet
    # after an add or a remove without a round trip.
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

    # Which categories the designer has never put anything in (settled decision 7). The
    # picker shows this per category so "I have not looked at Earthing yet" is visible at
    # a glance rather than something they have to remember.
    used_categories = {row.category for row in added_on}
    empty_categories = [c for c in category_order if c not in used_categories]

    # Why the screen is read-only, when it is. Most specific first: the procurement lock is
    # final, the design lock is not, and "you are not the designer" is neither.
    lock_reason = ''
    if boq_group_locked:
        lock_reason = ('This site is in a locked procurement group. Its BOQ quantities are '
                       'final — a correction now needs a variance against the order, raised '
                       'with SCM.')
    elif boq_design_locked:
        lock_reason = ('This BOQ is marked complete and is with design review. It reopens, '
                       'with the full catalogue, if a reviewer sends it back or a change '
                       'request opens a new attempt.')
    elif not can_author:
        lock_reason = ('Only the designer named in this site\'s assigned_design may enter '
                       'its BOQ.')

    return render(request, 'projects/opex_boq_entry.html', {
        'project':          project,
        'boq':              boq,
        'catalogue_count':  len(catalogue),
        'catalogue_json':   catalogue_json,
        'added_json':       added_json,
        'added_off':        group_boq_rows_by_category(added_off, category_order),
        'added_off_count':  len(added_off),
        'added_count':      len(added_on) + len(added_off),
        'category_order':   category_order,
        'empty_categories': empty_categories,
        'can_edit':         can_edit,
        'lock_reason':      lock_reason,
        'result_cap':       OPEX_PICKER_RESULT_CAP,
    })
```

The docstring states the save contract explicitly ([views.py:4601-4613](projects/views.py#L4601-L4613)):

```python
    """OPEX BOQ entry — search the catalogue, add what this site uses, enter quantities.

    GET renders the two-panel screen. POST takes one of two actions, both of which save
    the sheet first:

        save_draft    — save and come back here
        mark_complete — save, then hand off to design_boq_complete()

    THE SAVE IS A FULL RECONCILIATION of the posted sheet against the stored one, not an
    append. Rows the designer removed are gone from the POST, so they are deleted here;
    rows they added arrive as catalogue pks and are created. Doing it any other way would
    make "remove" a second round trip that could half-apply.
    """
```

Module constant, [views.py:4593-4596](projects/views.py#L4593-L4596):

```python
#: How many catalogue matches the picker renders before it stops and says how many more
#: there are. The cap is on the RENDERED list only — the count is always the true total,
#: so "N more" never lies about how much is hidden.
OPEX_PICKER_RESULT_CAP = 60
```

## 1.2 Every helper in the path

### `get_opex_boq_catalogue()` — [models.py:762-781](projects/models.py#L762-L781)

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

Returns a list of `BOQItemMaster` **instances**. **Filters `is_active=True`.** This is the single
place a mandatory pre-load would naturally read from, and its docstring is the sentence Session D
is being asked to falsify ("NO PRE-POPULATION").

### `opex_catalogue_category_order()` — [models.py:784-800](projects/models.py#L784-L800)

```python
def opex_catalogue_category_order():
    """OPEX category names in CATALOGUE order — the order of first appearance by
    sort_order, which is spreadsheet order.

    DERIVED, NOT STORED (settled decision 5). A stored per-category rank would be a second
    thing to keep in step with sort_order, and the two would eventually disagree. Used to
    group both halves of the picker and the read-only review panel, so the catalogue, the
    saved sheet and the reviewer's copy all list categories the same way.
    """
    seen = []
    for category in (BOQItemMaster.objects
                     .filter(is_active=True, project_type='OPEX')
                     .order_by('sort_order', 'code')
                     .values_list('category', flat=True)):
        if category not in seen:
            seen.append(category)
    return seen
```

Returns a list of category-name strings. **Filters `is_active=True`.**

### `split_opex_boq_rows(boq, catalogue_ids)` — [models.py:803-822](projects/models.py#L803-L822)

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

Returns `(on, off)` lists of `BOQItem`. It does not filter `is_active` itself — it inherits the
filter through the `catalogue_ids` set the caller passes, which is built from
`get_opex_boq_catalogue()`. **This is the mechanism by which a deactivated master's row silently
becomes "off-catalogue"**, and it is directly load-bearing for 3.5.

### `group_boq_rows_by_category(rows, category_order)` — [models.py:825-837](projects/models.py#L825-L837)

```python
def group_boq_rows_by_category(rows, category_order):
    """Group BOQ rows by category in CATALOGUE order — [(category, [rows]), ...].

    Categories the sheet does not use do not appear. A category the sheet uses but the
    catalogue no longer lists is appended at the end rather than dropped: the row exists,
    so it has to be somewhere.
    """
    buckets = {}
    for row in rows:
        buckets.setdefault(row.category, []).append(row)
    ordered = [(c, buckets.pop(c)) for c in category_order if c in buckets]
    ordered.extend(sorted(buckets.items()))
    return ordered
```

Returns `[(category, [rows])]`. No `is_active` filter — it works on rows already given to it. Used
on the GET path for `added_off` only.

### Lock and authority helpers

`project_boq_is_group_locked(project)` — [permissions.py:781-808](projects/permissions.py#L781-L808):

```python
    if project is None:
        return False
    return project.group_memberships.filter(
        removed_at__isnull=True, group__status='locked',
    ).exists()
```

`project_boq_is_design_locked(project)` — [permissions.py:811+](projects/permissions.py#L811). Its
docstring states the condition is one field:

```
    THE CONDITION IS ONE FIELD: the CURRENT attempt's `boq_submitted_at`. That single test
    produces every row of the Part 11 lock progression, because the Part 9 rework loop
    already maintains that stamp exactly as the progression describes:

        designer saving drafts        stamp null      -> editable
        marks BOQ complete            stamp set       -> frozen
        Design QC rejects             new attempt     -> reopens (see below)
        Design QC approves            same attempt    -> stays frozen
        Design Head rejects           new attempt     -> reopens
        Design Head approves          same attempt    -> DESIGN LOCK
        PM change request             new attempt     -> reopens
```

`user_can_view_project_boq` / `user_can_edit_project_boq` are the unchanged Part 0.6 helpers; the
picker ANDs them with the two lock predicates rather than folding the locks into them
([permissions.py:785-793](projects/permissions.py#L785-L793) explains why).

## 1.3 When a `BOQ` row first exists for an OPEX project — **lazily, on first save**

This is one of the two most consequential findings in the audit.

[views.py:4692-4696](projects/views.py#L4692-L4696), inside the POST path's `transaction.atomic()`:

```python
        with transaction.atomic():
            if boq is None:
                # Created on first save, never on GET — a page load must not bring a BOQ
                # row into existence on a site nobody has entered anything for.
                boq = BOQ.objects.create(project=project)
```

There is **no eager creation at project creation**. `_designer_boq()` in `design_views.py` is
explicitly read-only ([design_views.py:1589-1595](projects/design_views.py#L1589-L1595)):

```python
def _designer_boq(project):
    """The project's BOQ, or None. Read-only — this module never creates, seeds or
    writes a BOQ row (settled decision 4); `boq_detail` owns all of that."""
    try:
        return project.boq
    except BOQ.DoesNotExist:
        return None
```

And the OPEX branch of `boq_detail` redirects to the picker *before* its own seeding block, so the
Residential creation path is unreachable for OPEX
([views.py:4305-4310](projects/views.py#L4305-L4310)):

```python
    # PART 11: send the OPEX author to the OPEX screen. Only the author — SCM, PM, Admin
    # and the two QC reviewers have no picker to use and read the sheet right here. The
    # locks are deliberately NOT consulted: a locked OPEX BOQ still belongs on the picker,
    # which renders it read-only and says which lock is holding it.
    if project.project_type == 'OPEX' and user_can_edit_project_boq(request.user, project):
        return redirect('opex_boq_entry', project_id=project_id)
```

**Consequence for option 3.1(a): there is no "BOQ creation moment" for OPEX to hang a pre-load
on.** The only place a BOQ comes into existence for an OPEX site is line 4696, which is already
*inside the save*. Option (a) as the prompt frames it ("rows written when the `BOQ` is created —
mirrors Residential") collapses into option (b) for OPEX, unless the build first *adds* an eager
creation moment that does not exist today.

`[PRODUCTION]` confirms this empirically: **93 of 97 OPEX projects have no `BOQ` row at all.**

## 1.4 The POST path in detail

[views.py:4639-4741](projects/views.py#L4639-L4741), quoted in full:

```python
    if request.method == 'POST':
        action = request.POST.get('action', '')

        if not can_author:
            return HttpResponseForbidden()
        if boq_group_locked:
            messages.error(request, 'This site is in a locked procurement group — its BOQ '
                                    'quantities are final and can no longer be changed. A '
                                    'correction now needs a variance against the order.')
            return redirect('opex_boq_entry', project_id=project_id)
        if boq_design_locked:
            messages.error(request, 'This BOQ has been marked complete and is with design '
                                    'review — it cannot be changed until a reviewer sends '
                                    'it back or a change request opens a new attempt.')
            return redirect('opex_boq_entry', project_id=project_id)
        if action not in ('save_draft', 'mark_complete'):
            return redirect('opex_boq_entry', project_id=project_id)

        # The chosen catalogue rows, in the order the sheet lists them. Anything that is
        # not an active OPEX master pk is dropped rather than trusted — this is a POST.
        chosen = []
        for raw in request.POST.getlist('item'):
            if raw.isdigit() and int(raw) in catalogue_by_id and int(raw) not in chosen:
                chosen.append(int(raw))
        chosen_set = set(chosen)

        # Off-catalogue rows are identified by BOQItem pk, not master pk — an ad-hoc row
        # has no master. Only rows on THIS BOQ are addressable.
        keep_off = {int(raw) for raw in request.POST.getlist('keep_row') if raw.isdigit()}

        def _quantity(field, current=None):
            """The posted quantity for one row.

            AN ABSENT FIELD MEANS UNCHANGED; an EMPTY one means cleared. The browser always
            sends the input, so clearing a box still clears the value — but a partial or
            hand-built POST that names a row without naming its quantity must not silently
            wipe a number the designer entered on a previous save.

            A malformed or negative value is treated as no quantity rather than rejected:
            the same forgiving read boq_detail applies, and the "at least one quantity"
            guard on marking complete is what actually stops an unusable BOQ.
            """
            if field not in request.POST:
                return current
            raw = (request.POST.get(field) or '').strip()
            if not raw:
                return None
            try:
                value = Decimal(raw)
            except InvalidOperation:
                return None
            return value if value >= 0 else None

        with transaction.atomic():
            if boq is None:
                # Created on first save, never on GET — a page load must not bring a BOQ
                # row into existence on a site nobody has entered anything for.
                boq = BOQ.objects.create(project=project)

            existing_on, existing_off = split_opex_boq_rows(boq, set(catalogue_by_id))
            by_master = {row.item_master_id: row for row in existing_on}

            # Removed catalogue rows, and removed off-catalogue rows.
            for master_id, row in by_master.items():
                if master_id not in chosen_set:
                    row.delete()
            for row in existing_off:
                if row.pk not in keep_off:
                    row.delete()

            # Added and updated catalogue rows. serial_no comes from the catalogue's
            # sort_order, the same rule the Residential template uses, so a row's number is
            # stable regardless of the order it was added in.
            for master_id in chosen:
                master = catalogue_by_id[master_id]
                row    = by_master.get(master_id)
                if row is None:
                    BOQItem.objects.create(
                        boq=boq, item_master=master, serial_no=master.sort_order,
                        category=master.category, description=master.description,
                        uom=master.unit, boq_quantity=_quantity(f'qty_{master_id}'),
                        is_standard_item=True,
                    )
                else:
                    row.boq_quantity = _quantity(f'qty_{master_id}', row.boq_quantity)
                    row.save(update_fields=['boq_quantity'])

            # Surviving off-catalogue rows keep their quantity editable.
            for row in existing_off:
                if row.pk in keep_off:
                    row.boq_quantity = _quantity(f'qty_row_{row.pk}', row.boq_quantity)
                    row.save(update_fields=['boq_quantity'])

        if action == 'mark_complete':
            # design_boq_complete owns the attempt stamp and every precondition on it —
            # the assignment status, an approved Arka, "not already stamped", and at least
            # one quantity. Called rather than duplicated so the two cannot disagree. It
            # redirects to the site workspace and messages for itself.
            from .design_views import design_boq_complete
            return design_boq_complete(request, project_id)

        messages.success(request, 'BOQ saved.')
        return redirect('opex_boq_entry', project_id=project_id)
```

**Full replace or diff?** — **A diff, in the sense that matters.** It is a *reconciliation*, not a
delete-all-recreate. A row already on the sheet whose master is still in `chosen_set` is neither
deleted nor recreated; it is updated in place with
`row.save(update_fields=['boq_quantity'])` (line 4724), preserving its pk, `serial_no`,
`make_preference`, `ordered_vendor`, `ordered_quantity` and every other SCM-side field. **Hard
stop 4 does not trigger.**

**How unknown or inactive pks are dropped** — [views.py:4660-4662](projects/views.py#L4660-L4662):

```python
        for raw in request.POST.getlist('item'):
            if raw.isdigit() and int(raw) in catalogue_by_id and int(raw) not in chosen:
                chosen.append(int(raw))
```

`catalogue_by_id` is built at line 4637 from `get_opex_boq_catalogue()`, which filters
`is_active=True, project_type='OPEX'`. So membership in that dict is simultaneously the
"is a real pk", "is OPEX", "is active" and "is not a duplicate" test. A pk that fails any of them
is **silently skipped, not rejected** — the POST still succeeds.

**Write mechanism** — three different ones, deliberately:
- New catalogue rows: `BOQItem.objects.create(...)` individually (line 4716). **Not** `bulk_create`.
- Existing catalogue rows: `row.save(update_fields=['boq_quantity'])` (line 4724).
- Off-catalogue rows kept: `row.save(update_fields=['boq_quantity'])` (line 4730).
- No `update_or_create` anywhere on this path.

**A removed row is hard-deleted, not flagged** — lines 4702-4707:

```python
            for master_id, row in by_master.items():
                if master_id not in chosen_set:
                    row.delete()
            for row in existing_off:
                if row.pk not in keep_off:
                    row.delete()
```

There is no soft-delete field on `BOQItem`. A removed row and its quantity are gone.

**A risk worth recording even though hard stop 4 does not fire.** Removal is expressed by
*absence* from the POST, for both halves. A truncated or hand-built POST that omits `item` and
`keep_row` entirely — and passes the three lock gates — deletes **every** row on the sheet, and
`action` defaults to nothing but `save_draft` is the form's default hidden value. The design
intends this ("Rows the designer removed are gone from the POST, so they are deleted here"), and
the browser always sends the full set, so this is not a live bug. It is, however, exactly the
property that makes enforcement shape 3.4(a) attractive and 3.4(b) awkward — see 3.4.

## 1.5 Save draft versus Mark BOQ complete

**How the server distinguishes them:** a single hidden field, set by an `onclick` on each button.

[opex_boq_entry.html:80](projects/templates/projects/opex_boq_entry.html#L80):

```html
  <input type="hidden" name="action" id="boqAction" value="save_draft">
```

[opex_boq_entry.html:195-212](projects/templates/projects/opex_boq_entry.html#L195-L212):

```html
        <div class="boq-bar d-flex align-items-center gap-3 mt-2 rounded px-3 py-2 border">
          <span class="small"><strong id="boqTotal">0</strong>
            <span class="text-muted">items on this BOQ</span></span>
          <button type="submit" class="btn btn-sm text-white ms-auto"
                  style="background-color:#1a7a4a;" onclick="boqSetAction('save_draft')">
            Save draft
          </button>
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
        </div>
```

[opex_boq_entry.html:412-415](projects/templates/projects/opex_boq_entry.html#L412-L415):

```javascript
  window.boqSetAction = function (action) {
    document.getElementById('boqAction').value = action;
    dirty = false;   // we are submitting; the unload guard must not fire
  };
```

**They share the persistence path completely.** Both values fall through the same
`if action not in ('save_draft', 'mark_complete')` guard at line 4654 and run the identical
`transaction.atomic()` block at 4692-4730. The *only* difference is what happens after the
transaction commits ([views.py:4732-4741](projects/views.py#L4732-L4741)):

```python
        if action == 'mark_complete':
            # design_boq_complete owns the attempt stamp and every precondition on it —
            # the assignment status, an approved Arka, "not already stamped", and at least
            # one quantity. Called rather than duplicated so the two cannot disagree. It
            # redirects to the site workspace and messages for itself.
            from .design_views import design_boq_complete
            return design_boq_complete(request, project_id)

        messages.success(request, 'BOQ saved.')
        return redirect('opex_boq_entry', project_id=project_id)
```

`design_boq_complete` is **called as a function with the same `request` object**, not redirected
to — which is how it reads `boq_remarks` off the same POST
([design_views.py:2343-2348](projects/design_views.py#L2343-L2348)).

**This is good news for the build:** one persistence block means a mandatory rule added inside
`transaction.atomic()` covers both actions automatically, with no risk of the two disagreeing.

## 1.6 The design lock — **enforced on POST, not only in the template**

**Hard stop 3 does not trigger.**

Server-side, [views.py:4644-4653](projects/views.py#L4644-L4653), before any parsing and before
the transaction:

```python
        if boq_group_locked:
            messages.error(request, 'This site is in a locked procurement group — its BOQ '
                                    'quantities are final and can no longer be changed. A '
                                    'correction now needs a variance against the order.')
            return redirect('opex_boq_entry', project_id=project_id)
        if boq_design_locked:
            messages.error(request, 'This BOQ has been marked complete and is with design '
                                    'review — it cannot be changed until a reviewer sends '
                                    'it back or a change request opens a new attempt.')
            return redirect('opex_boq_entry', project_id=project_id)
```

Preceded by the authority gate at 4642-4643:

```python
        if not can_author:
            return HttpResponseForbidden()
```

Note the shape: **authority failure is a 403; lock failure is a redirect with a message.** They
are different because they are different conditions — being the wrong person is an error, hitting
a lock is a state.

Template-side, `can_edit` (computed at line 4628 as `can_author and not boq_group_locked and not
boq_design_locked`) gates **every** control:

| Control | Line | Gate |
|---|---|---|
| Search box, category select, results pane | 92-96 | `{% if can_edit %}` … `{% else %}` read-only notice |
| Empty-category panel | 104-118 | `{% if can_edit %}` |
| Off-catalogue quantity input + `keep_row` hidden | 154-158 | `{% if can_edit %}` |
| Off-catalogue remove button | 164-168 | `{% if can_edit %}` |
| Remarks textarea | 179-194 | `{% if can_edit %}` |
| Both submit buttons + total bar | 195-212 | `{% if can_edit %}` |
| Sheet quantity input, `item` hidden, `qty_` hidden, delete button | JS 347-359 | `CAN_EDIT` |

`CAN_EDIT` reaches the JS at [opex_boq_entry.html:224](projects/templates/projects/opex_boq_entry.html#L224):

```javascript
  var CAN_EDIT  = {% if can_edit %}true{% else %}false{% endif %};
```

The template lock is a **usability** layer over a real server gate, which is the correct
arrangement.

## 1.7 The `|safe` JSON payloads

**CONFLICT with the prompt:** the prompt says audit A found them "around `opex_boq_entry.html:203-204`".
They are at **lines 218-219**. Lines 203-204 are inside a `{% comment %}` block about the Mark
BOQ complete button. The file is 461 lines.

[opex_boq_entry.html:218-219](projects/templates/projects/opex_boq_entry.html#L218-L219):

```html
<script id="boqCatalogueData" type="application/json">{{ catalogue_json|safe }}</script>
<script id="boqAddedData" type="application/json">{{ added_json|safe }}</script>
```

**What they carry.** From [views.py:4749-4758](projects/views.py#L4749-L4758):

- `catalogue_json` — one object per **active OPEX master**, all 207 of them:
  `{'id': m.pk, 'code': m.code, 'cat': m.category, 'desc': m.description, 'unit': m.unit}`.
  Note it does **not** carry `sort_order`; the JS derives category order from array position.
- `added_json` — one object per **catalogue row already on this sheet**:
  `{'id': row.item_master_id, 'qty': ...}` with the quantity normalised to a plain string.

Consumed at [opex_boq_entry.html:223](projects/templates/projects/opex_boq_entry.html#L223) and
[240-243](projects/templates/projects/opex_boq_entry.html#L240-L243):

```javascript
  var CATALOGUE = JSON.parse(document.getElementById('boqCatalogueData').textContent);
```
```javascript
  JSON.parse(document.getElementById('boqAddedData').textContent).forEach(function (row) {
    chosen[row.id] = row.qty;
    order.push(row.id);
  });
```

**Would mandatory status go here?** Yes — if the client needs to know which rows are mandatory (to
grey out or hide the `×` button, per the "designer cannot remove them" half of the request), the
natural place is a sixth key on `catalogue_json`, e.g. `'mand': m.is_mandatory`. That would put
`is_mandatory` into a `|safe` payload. It is a boolean, so it carries no escaping risk of its own —
but it would be a sixth field on a payload that already ships 207 rows of admin-authored
free text (`code`, `cat`, `desc`, `unit`) through `|safe`.

**The `|json_script` alternative.** Django's builtin would replace both lines with, in the view,
passing the Python lists rather than pre-serialised strings:

```html
{{ catalogue_data|json_script:"boqCatalogueData" }}
{{ added_data|json_script:"boqAddedData" }}
```

`json_script` emits `<script id="boqCatalogueData" type="application/json">` itself and escapes
`<`, `>` and `&` as `\u003C`, `\u003E`, `\u0026` inside the JSON, which is precisely the
`</script>`-breakout case that `json.dumps` + `|safe` does not cover. The JS consuming side
(`JSON.parse(document.getElementById(...).textContent)`) is **already written in exactly the form
`json_script` expects** and would not change at all. The view change is removing the two
`json.dumps(...)` calls and renaming the context keys.

**Assessment, offered as evidence not as a recommendation:** the current construction is not
presently exploitable in an obvious way — the payload is admin-authored catalogue text, and the
`type="application/json"` wrapper means the browser does not execute it. But a `desc` containing
the literal `</script>` would break the page, and that string is reachable by anyone who can edit
the catalogue (Admin, and as of Session C the Design Head). Switching to `json_script` is a
strictly-smaller-risk two-line change. **It is out of scope for this session** and belongs in its
own decision, not bundled into mandatory-item enforcement.

## 1.8 Client-side removal — **pure client state until save**

Two removal paths, both local.

**Catalogue rows** — [opex_boq_entry.html:391-397](projects/templates/projects/opex_boq_entry.html#L391-L397):

```javascript
  function remove(id) {
    delete chosen[id];
    order = order.filter(function (x) { return x !== id; });
    dirty = true;
    renderResults();
    renderSheet();
  }
```

Bound at [432-435](projects/templates/projects/opex_boq_entry.html#L432-L435):

```javascript
    elSheet.addEventListener('click', function (event) {
      var target = event.target.closest('[data-del]');
      if (target) { remove(parseInt(target.getAttribute('data-del'), 10)); }
    });
```

`renderSheet()` rebuilds the sheet's innerHTML from `chosen`/`order`
([opex_boq_entry.html:335-364](projects/templates/projects/opex_boq_entry.html#L335-L364)), so the
row's `<input type="hidden" name="item">` (line 351) simply ceases to exist in the form. Its
absence from the next POST is what deletes it server-side.

**Off-catalogue rows** — [opex_boq_entry.html:399-410](projects/templates/projects/opex_boq_entry.html#L399-L410):

```javascript
  // A removed off-catalogue row drops its keep_row marker, which is what tells the server
  // to delete it. Hidden rather than detached so an accidental click is undoable by
  // reloading without saving.
  window.boqDropOffRow = function (pk) {
    var row = document.getElementById('offRow' + pk);
    if (!row) { return; }
    row.querySelectorAll('input').forEach(function (input) { input.disabled = true; });
    row.style.display = 'none';
    offRows -= 1;
    dirty = true;
    renderSheet();
  };
```

**Neither hits the server.** No fetch, no XHR, no HTMX on this screen. Removal is reversible by
reloading without saving, which the unload guard makes deliberate
([opex_boq_entry.html:449-453](projects/templates/projects/opex_boq_entry.html#L449-L453)):

```javascript
    window.addEventListener('beforeunload', function (event) {
      if (!dirty) { return; }
      event.preventDefault();
      event.returnValue = '';
    });
```

**Implication for enforcement:** blocking removal client-side is a one-line change in
`renderSheet()` (suppress the `data-del` button when the item is mandatory). But because removal
is expressed as *absence from the POST*, a client-side-only block is **not** enforcement — it is a
hint. The server must also act, which is what 3.4 is about.

---

# PART 2 — THE RESIDENTIAL PRECEDENT

## 2.1 The Residential pre-population `bulk_create`

[views.py:4318-4344](projects/views.py#L4318-L4344), inside `boq_detail`:

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
        else:
            return render(request, 'projects/boq_detail.html', {
                'project': project,
                'boq':     None,
                'role':    role,
            })
```

- **When it fires:** on a **GET** of `boq_detail`, when `project.boq` does not exist, and only if
  the viewer passes `user_can_edit_project_boq()` **and** the site is not group-locked. Note the
  design lock is *not* a term here — deliberately, since a design-locked project already has a BOQ.
- **What it filters on:** `get_standard_boq_items()` supplies the rows, filtering
  `is_active=True, project_type='Residential'`
  ([models.py:740-745](projects/models.py#L740-L745)); the `masters` dict is separately filtered
  the same way, keyed by `description`.
- **Idempotent?** Only by virtue of the `if boq is None:` guard. The `bulk_create` itself has no
  uniqueness protection — there is no `unique_together` on `(boq, item_master)` or
  `(boq, serial_no)`.
- **If it ran twice:** it would create a **duplicate set** of 37 rows on the same BOQ. Nothing
  raises. `BOQ.project` is a `OneToOneField`, so the *second* `BOQ.objects.create(project=project)`
  would raise `IntegrityError` first — which is what actually prevents the double-seed, not the
  guard. But a hypothetical seeding call that skipped the create and only ran the `bulk_create`
  would silently double the sheet.

**This matters for 3.1.** Any mandatory pre-load written the Residential way inherits the same
absence of a uniqueness constraint. If the OPEX pre-load is written at save time (3.1b) rather
than at a one-shot creation moment, **idempotency must be explicit** — the existing precedent does
not supply it.

**Note also that the Residential precedent writes on a GET.** See 3.1(c).

## 2.2 Can a Residential designer remove a pre-populated row? — **No. There is a real precedent.**

[views.py:4492-4501](projects/views.py#L4492-L4501):

```python
        elif action == 'delete_item' and _can_edit and boq.status in _DESIGN_EDITABLE:
            item_id = request.POST.get('item_id', '')
            if item_id.isdigit():
                # Object consistency: the item must belong to THIS project's BOQ. An id from
                # another project's BOQ is a 404, not a silent no-op reported as success.
                item = get_object_or_404(BOQItem, pk=int(item_id), boq=boq)
                if not item.is_standard_item:     # standard rows are undeletable, as before
                    item.delete()
                    messages.success(request, 'Row deleted.')
            return redirect('boq_detail', project_id=project_id)
```

The field, [models.py:916](projects/models.py#L916):

```python
    is_standard_item = models.BooleanField(default=True)  # False for ad-hoc rows added by Design; only non-standard items can be deleted
```

**So "unremovable pre-populated row" already exists in this codebase, and the mechanism is
`BOQItem.is_standard_item` checked server-side in the delete branch.** The request is therefore
*not* wholly new behaviour — it is a port of an existing pattern to a screen whose removal
semantics are shaped differently.

**But there is a sharp catch, and it is the second most consequential finding in this audit.**

The OPEX picker already writes **every** catalogue row with `is_standard_item=True`
([views.py:4716-4721](projects/views.py#L4716-L4721)):

```python
                    BOQItem.objects.create(
                        boq=boq, item_master=master, serial_no=master.sort_order,
                        category=master.category, description=master.description,
                        uom=master.unit, boq_quantity=_quantity(f'qty_{master_id}'),
                        is_standard_item=True,
                    )
```

…and its own removal path at line 4702-4704 **does not consult `is_standard_item` at all**:

```python
            for master_id, row in by_master.items():
                if master_id not in chosen_set:
                    row.delete()
```

`[PRODUCTION]` confirms the field is set on every live OPEX row: MB0141 52/52 standard, MB0164
43/43, MB0191 53/53, TESTTENDER26-MB010 37/37.

Two consequences:

1. **`is_standard_item` cannot be reused as the mandatory marker on the OPEX side.** It is already
   `True` on all 185 live OPEX rows, so a rule "refuse to delete `is_standard_item` rows" applied
   to the picker would freeze every existing sheet solid. The marker must be derived from
   `BOQItemMaster.is_mandatory` via the `item_master` join, not from a per-row boolean.
2. The two screens now mean different things by the same field, which is worth stating plainly
   even though nothing is presently broken by it.

---

# PART 3 — THE FIVE UNRESOLVED QUESTIONS

*Evidence only. No option is chosen.*

## 3.1 Persistence timing

### (a) Rows written when the `BOQ` is created

**Blocked as stated by 1.3.** There is no OPEX BOQ-creation moment outside the save. The only
`BOQ.objects.create(project=project)` reachable for an OPEX site is
[views.py:4696](projects/views.py#L4696), *inside* the POST transaction; `boq_detail`'s creation
block is unreachable for OPEX because of the redirect at line 4309. `[PRODUCTION]`: **93 of 97
OPEX projects have no BOQ row.**

To make (a) real, the build would first have to *introduce* an eager creation point — at project
creation (`create_opex_site`, both the single-add and bulk-upload paths) or at design allocation.
That is a larger change than it sounds: it would give 93 currently-BOQ-less sites a BOQ, and
`aggregate_group_boq()` and every "has a BOQ" check would start seeing them.

- **Code touched:** OPEX site creation core, or design allocation; plus a data migration for the 93.
- **Designer sees on first open:** the mandatory rows, already there, on a fresh site.

### (b) Rows written at first save; render composes persisted + missing-mandatory

- **Code touched:** the POST transaction ([views.py:4692-4730](projects/views.py#L4692-L4730)) to
  force the mandatory set into `chosen`, plus the GET path
  ([views.py:4744-4758](projects/views.py#L4744-L4758)) to union the mandatory masters into
  `added_json` so they render before anything is saved, plus `renderSheet()` in the template so
  the composed rows are not offered in the left-hand results pane.
- **Designer sees on first open:** the mandatory rows on the sheet, but **not yet in the
  database** — a distinction that surfaces the moment anything else reads the BOQ before the first
  save (the Part 9 review panel via `_boq_review_panel`, and `aggregate_group_boq`). Those readers
  would see an empty or short sheet where the picker showed rows.
- This is the shape with the **largest divergence between what the screen shows and what is
  stored**, and that divergence is its whole cost.

### (c) Rows written on GET

**The codebase does write on GET, in exactly this domain.** [views.py:4318-4344](projects/views.py#L4318-L4344)
creates a `BOQ` and `bulk_create`s 37 rows on a GET of `boq_detail`, and the comment at 4319-4322
shows the author knew and gated it accordingly:

```python
        # Seeding 53 catalogue rows is a WRITE, so it takes the write gate even though we
        # are on a GET. A reader with no authorship relationship to this project must not
        # be able to bring a BOQ into existence just by loading the page — nor may one
        # appear on a site whose group has already been locked.
```

So (c) is **not** without precedent. However, the OPEX picker's own author explicitly rejected it
for this screen ([views.py:4694-4695](projects/views.py#L4694-L4695)):

```python
                # Created on first save, never on GET — a page load must not bring a BOQ
                # row into existence on a site nobody has entered anything for.
```

- **Code touched:** the GET path only, plus a write gate mirroring lines 4323's three terms.
- **Designer sees on first open:** mandatory rows present and persisted — the most coherent screen
  of the three.
- **Cost:** it reverses a documented decision made 4 lines from where the change would go, and it
  means a QC reviewer or PM opening the picker read-only must not trigger the write (they can
  reach it: the redirect at 4309 only fires for `user_can_edit_project_boq`, but the picker URL is
  directly reachable by anyone passing `user_can_view_project_boq`).

**Note the comment at [models.py:695-704](projects/models.py#L695-L704) already anticipates a
choice here** and does not make it:

```python
    # OPEX-ONLY IN MEANING, and enforced as such on the form rather than here. A later
    # session pre-loads flagged items onto every new OPEX BOQ and stops the designer
    # removing them; the OPEX sheet starts empty and is picked from, so "mandatory" has
    # real work to do there.
```

"every **new** OPEX BOQ" is the phrase — it says nothing about the 4 that already exist. That is
3.3.

## 3.2 Quantity at completion

**What `Mark BOQ complete` validates today** —
[design_views.py:2328-2341](projects/design_views.py#L2328-L2341):

```python
    attempt = _current_attempt(assignment)
    try:
        _require_approved_arka(attempt)
    except ValueError as exc:
        return _back(f'{project.project_id}: {exc}.')

    if attempt.boq_submitted_at is not None:
        return _back(f'{project.project_id}: the BOQ for attempt '
                     f'{attempt.attempt_number} is already marked complete.')

    boq = _designer_boq(project)
    if boq is None or not boq.items.filter(boq_quantity__gt=0).exists():
        return _back(f'{project.project_id}: enter a quantity for at least one BOQ item '
                     f'before marking the BOQ complete.')
```

Preceded by the authority gate at [design_views.py:2317-2320](projects/design_views.py#L2317-L2320).

**Does it require a quantity on any row at all?** It requires **at least one row anywhere on the
BOQ with `boq_quantity > 0`**. That is the entire quantity rule. It is not per-row, not per-category,
and not per-item. The docstring is explicit that this mirrors `boq_detail`'s own submit branch
([design_views.py:2311-2314](projects/design_views.py#L2311-L2314)):

```
    The "at least one quantity" guard reads the existing BOQ and mirrors the check
    `boq_detail`'s own submit branch applies (views.py — `boq_quantity__gt=0`), so this
    stamp cannot be set on an empty BOQ. It is a READ of BOQ rows; nothing here writes
    one.
```

**Can a partially-filled sheet currently complete? — Yes, and one already has.**

`[PRODUCTION]`: **MB0191 has 53 rows, 51 with a quantity > 0, and `boq_submitted_at` stamped at
2026-08-06 13:28:00 UTC.** Two rows on a completed, design-locked BOQ carry no quantity. This is
not hypothetical — it is the live state of one of the three real MPUVNL BOQs.

**What it would take to require a quantity on mandatory rows specifically:** a new term in the
guard at line 2339, joining `BOQItem` → `BOQItemMaster` and asserting no mandatory-linked row has
a null or zero quantity. Roughly:

- one added query in `design_boq_complete`, and
- a decision about the *existing* three BOQs, because that guard runs on **re-submission after a
  rework loop too** — a stricter rule would refuse an attempt that a designer could previously
  complete.

Note the interaction with 3.5: if the guard reads `is_mandatory` unscoped by `is_active`, a
deactivated-but-mandatory master would make a BOQ permanently uncompletable.

## 3.3 Existing BOQs `[PRODUCTION]`

**Every OPEX BOQ on production** (4 of them; 97 OPEX projects exist, 93 have no BOQ row):

| Project | BOQ id | `BOQ.status` | Rows | On active OPEX catalogue | On Residential catalogue | Unlinked | Rows with qty > 0 | `is_standard_item` |
|---|---|---|---|---|---|---|---|---|
| MB0141 | 7 | Draft | 52 | 52 | 0 | 0 | 52 | 52 |
| MB0164 | 9 | Draft | 43 | 43 | 0 | 0 | 43 | 43 |
| MB0191 | 8 | Draft | 53 | 53 | 0 | 0 | **51** | 53 |
| TESTTENDER26-MB010 | 6 | Draft | 37 | **0** | **37** | 0 | **0** | 37 |

**Attempt stamps** `[PRODUCTION]`:

| Project | Assignment | Attempt | `boq_submitted_at` | `closed_at` | `qc_verdict` |
|---|---|---|---|---|---|
| MB0005 | 4 | 1 | *(null)* | *(null)* | pending |
| MB0141 | 42 | 1 | 2026-08-06 12:10:35 UTC | *(null)* | pending |
| MB0164 | 52 | 1 | 2026-08-06 13:13:57 UTC | *(null)* | pending |
| MB0191 | 63 | 1 | 2026-08-06 13:28:00 UTC | *(null)* | pending |

**Group locks** `[PRODUCTION]`: **zero `SiteGroupMembership` rows for any OPEX project, and zero
`SiteGroup` rows of any status in the entire database.** No OPEX site is group-locked, and none
can be until groups start being created.

**Classification:**

| Class | Members |
|---|---|
| **Design-locked** (stamp set → `project_boq_is_design_locked` True) | MB0141, MB0164, MB0191 — the three real MPUVNL BOQs |
| **Draft / editable** | TESTTENDER26-MB010 (has a BOQ, no assignment stamp); MB0005 (assignment, attempt 1 unstamped, **no BOQ row at all**) |
| **Group-locked** | **None** |
| **No BOQ row** | 93 OPEX projects, incl. MB0005 |

**Hard stop 5 does not trigger** — there is no group-locked OPEX BOQ for injection to reach.

**What would happen to each class if mandatory rows were injected**, read off the code:

- **The three design-locked BOQs (MB0141/164/191).** Under shapes 3.1(a) and 3.1(b), injection
  happens on the POST path, which is unreachable for them: the guard at
  [views.py:4649-4653](projects/views.py#L4649-L4653) returns before the transaction while
  `boq_submitted_at` is set. So they would be **untouched until a QC rejection, a Head rejection or
  a PM change request opens a new attempt** — at which point `project_boq_is_design_locked` goes
  False, the picker reopens, and the *next save* injects. Under shape 3.1(c) (write on GET), the
  write would have to carry the same three-term gate `boq_detail`'s seeding block carries, or a
  reviewer merely opening the read-only picker on a locked BOQ would mutate it.
- **TESTTENDER26-MB010.** This one is worth naming. Its 37 rows are all **Residential** masters —
  a pre-Part-11 seeding artifact. `split_opex_boq_rows` classifies all 37 as **off-catalogue**
  (their pks are not in the active-OPEX `catalogue_ids` set), so they render in the "Not in the
  OPEX catalogue" panel and survive only via `keep_row`. Injection would add mandatory rows
  *alongside* them. All 37 have `boq_quantity` null, so this BOQ cannot currently be marked
  complete under the 3.2 guard.
- **MB0005 and the other 92 BOQ-less sites.** Nothing to inject into today. Under 3.1(b) they get
  rows on first save; under 3.1(c) on first authorised GET; under 3.1(a) only if the build also
  creates their BOQs, which would be 93 new `BOQ` rows.
- **Group-locked BOQs.** Unreachable on both paths — [views.py:4644-4648](projects/views.py#L4644-L4648)
  returns before the transaction, and none exist anyway. If groups are ever used, a GET-write shape
  (3.1c) would need this term too; the Residential precedent at line 4323 includes it
  (`and not boq_group_locked`) precisely for this reason.

## 3.4 Enforcement mechanism

Both reported; **neither chosen.**

### (a) Server recomputes the mandatory set on every save and silently re-adds what is missing

**Composes naturally with the POST path's shape.** The reconciliation at
[views.py:4657-4663](projects/views.py#L4657-L4663) builds `chosen` from the POST and then treats
that list as the truth. Re-adding is a **three-line insertion** at the end of that block — union
the mandatory master pks into `chosen`/`chosen_set` before the transaction opens, and everything
downstream works unchanged:

- the delete loop at 4702-4704 will not delete them, because they are now in `chosen_set`;
- the create/update loop at 4712-4724 will create them if absent and leave existing ones alone;
- `_quantity(f'qty_{master_id}')` returns `None` for a re-added row whose field was never posted
  (line 4681-4682: `if field not in request.POST: return current`), so a silently re-added row
  arrives with a **null quantity** and, on a subsequent save, keeps whatever quantity it had.

That last point is the one to weigh: silent re-add produces null-quantity rows, which is exactly
the state 4.1 and 3.2 are about.

### (b) Server rejects a POST that omits a mandatory item

**Awkward against this POST path's shape, for a specific reason.** Removal here is expressed as
**absence**, not as an explicit delete instruction (see 1.4 and 1.8). The server cannot distinguish
"the designer deliberately removed the mandatory row" from "this POST is truncated / the JS did not
run / a mandatory item was added to the catalogue after this page was rendered". All three arrive
as the same absent `item` field.

The third case is not hypothetical: a designer opens the picker, the Head marks OPX-042 mandatory,
the designer saves. Their page never carried OPX-042, so their save is rejected with an error about
an item they have never seen, and the only fix is to reload and lose unsaved work. Rejection also
has to render *somewhere* — the POST path currently has no validation-error render, only
`messages.error` + redirect, which would discard the entire posted sheet.

There is also no CSRF-adjacent or transactional problem with either shape; both sit inside the
existing `transaction.atomic()`.

**Summary of the composition question the prompt asks:** the path is a **diff**, and the diff is
driven by a set (`chosen_set`) that is computed in one place immediately before the transaction.
Mutating that set is a small, local, well-contained change; rejecting on the basis of it requires
new error-rendering machinery that this view does not currently have.

## 3.5 Mandatory plus inactive

**Nothing stops the combination today.** `BOQItemMasterForm.clean()`
([forms.py:516-552](projects/forms.py#L516-L552)) validates `is_mandatory` against **`project_type`
only**:

```python
        cleaned = super().clean()
        if not cleaned.get('is_mandatory'):
            return cleaned
        if 'project_type' not in self.fields:
            return cleaned
        if cleaned.get('project_type') != 'OPEX':
            raise forms.ValidationError({
                'is_mandatory': 'Only OPEX items can be marked mandatory. Every active '
                                'Residential item is already added to every new '
                                'Residential BOQ, so the flag would do nothing there.',
            })
        return cleaned
```

`is_active` is not mentioned. And the deactivate handler is a separate view that does not run this
form at all — [views.py:9126-9131](projects/views.py#L9126-L9131):

```python
@role_required(['Admin'])
def admin_boq_item_toggle(request, item_id):
    """Deactivate / reactivate one catalogue entry. Deactivated entries drop out of
    get_standard_boq_items(), so new BOQs stop including them; BOQ rows already created
    from the entry keep their description, quantity and item_master link untouched.
    Access: Admin only. POST only."""
```

**What happens today** (i.e. with C.1 deployed and nothing reading the flag): nothing. The flag is
inert; deactivation removes the item from `get_opex_boq_catalogue()` and the picker stops offering
it. `[PRODUCTION]`: **zero inactive rows of either type** — 207 OPEX and 37 Residential, all active
— so the combination does not currently exist in data.

**Under each enforcement shape:**

| Shape | Behaviour when an item is mandatory **and** inactive |
|---|---|
| 3.1(a)/(b)/(c) pre-load reading `get_opex_boq_catalogue()` | The item is **absent from the catalogue**, so it is never pre-loaded. The mandatory flag becomes silently inert — no error, no warning. Existing rows created from it become off-catalogue via `split_opex_boq_rows` and remain freely removable. |
| Pre-load reading `is_mandatory=True` **without** `is_active` | The row is injected but its pk is **not** in `catalogue_by_id`, so `chosen` at line 4661 drops it on the very next save and the delete loop at 4702 removes it. The row would flicker in and out on alternate saves — a genuine oscillation bug. |
| 3.4(a) silent re-add | Same as above: whether it re-adds depends entirely on which queryset the mandatory set is computed from. If computed from the active catalogue, deactivation quietly disables the rule. |
| 3.4(b) reject on omission | If the rejection set includes inactive masters, **every save on every OPEX BOQ fails permanently** with an error naming an item the picker does not offer and the designer cannot add. This is the worst failure mode of the four. |
| 3.2 stricter completion guard | If it reads `is_mandatory` unscoped, a deactivated mandatory item makes affected BOQs **permanently uncompletable**. |

**Where the contradiction would surface — three candidate locations, all real:**

1. **Form validation** (`BOQItemMasterForm.clean`, [forms.py:541](projects/forms.py#L541)) — add
   an `is_active` term alongside the existing `project_type` term. Catches the edit path. **Does
   not catch `admin_boq_item_toggle`**, which is the more likely route to the state.
2. **The deactivate handler** (`admin_boq_item_toggle`, [views.py:9127](projects/views.py#L9127),
   and whatever equivalent Session C gave the Design Head) — refuse to deactivate a mandatory item,
   or clear the flag as part of deactivating. This is the location that actually closes the hole.
3. **The pre-load filter** — make it structurally impossible by always computing the mandatory set
   as `is_active=True, project_type='OPEX', is_mandatory=True`, accepting that deactivation
   silently disables the rule.

Locations 1 and 2 are enforcement; location 3 is containment. They are not exclusive.

---

# PART 4 — BLAST RADIUS

## 4.1 `aggregate_group_boq()`

**CONFLICT with the prompt:** the prompt cites `design_views.py:~4126-4155`. The function is at
[design_views.py:4129-4190](projects/design_views.py#L4129), with the docstring at 4130-4151 and
the first query at 4152-4161.

```python
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
```

**Confirmed: injecting mandatory rows upstream does not require touching it.** The docstring's
claim holds — the function selects on `item_master__isnull=False` and `boq_quantity__gt=0` and
reads no per-item flag. A mandatory row is an ordinary `BOQItem` with an ordinary `item_master`.

**What it does with a mandatory row carrying null or zero quantity: it excludes it entirely.**
`boq_quantity__gt=0` is applied in all three queries — `lines` (4154), `contributions` (4168) and
`unlinked` (4179). A null-quantity mandatory row therefore:

- contributes nothing to `total_quantity` (correct),
- does **not** increment `site_count` for that line (correct, and deliberate per the docstring),
- does **not** appear in `unlinked` (it has an `item_master`, so it is not an unlinked row),
- and, if *every* site's mandatory row is null, **the line does not appear in the aggregate at
  all.**

That last case is the one to note. Under enforcement shape 3.4(a), silent re-add produces exactly
null-quantity rows (see 3.4a). A mandatory item nobody filled in would be present on every sheet
and **invisible on the procurement aggregate** — which is arguably the correct behaviour (nothing
to order) but is also indistinguishable from "the item is not mandatory". Whether SCM should see a
zero line is a chat decision, not one this audit makes.

## 4.2 `design_analytics.py`

**It does not count BOQ rows or attribute anything per item.** A search of the whole module for
`BOQItem`, `boq_quantity`, `boq__items` and `items__` returns **no matches**. The only occurrence
of "BOQ" in the file is in a comment at [design_analytics.py:662](projects/design_analytics.py#L662):

```
    the failure was in the CAD or the BOQ and the layout was never touched.
```

— which is about QC error categories, not BOQ contents.

**No metric would be distorted by auto-added rows.** Design analytics measures the attempt/QC
lifecycle, not sheet composition. The concern the prompt raises (auto-added rows were not the
designer's work) has no metric to corrupt in this module.

The one place row counts *are* surfaced is the Part 9 review panel, `_boq_review_panel(boq)`
([design_views.py:1598+](projects/design_views.py#L1598)), whose docstring says "ONLY WHAT THE
DESIGNER ADDED". If mandatory rows are injected, that sentence becomes inaccurate — the reviewer
would see rows the designer did not choose. That is a wording/UX question, not a metric one, and
it is worth flagging to whoever writes the build.

## 4.3 `[PRODUCTION]` — mandatory rows today

```sql
SELECT id, code, description, unit, category, project_type, is_active, is_mandatory
FROM projects_boqitemmaster WHERE is_mandatory = true ORDER BY sort_order, code
```

**`[PRODUCTION]` result: 0 rows.**

Catalogue totals `[PRODUCTION]`:

| project_type | is_active | count |
|---|---|---|
| OPEX | True | 207 |
| Residential | True | 37 |

**244 rows, all active, none mandatory.**

**This directly contradicts the prompt.** The prompt states "The Head has been marking items;
report the real set rather than assuming it is small." The real set is **empty**. C.1 was deployed
at 2026-08-15 02:16 UTC — a few hours before this audit — so there has been essentially no window
in which the Head could have used the screen. See CONFLICTS.

**Consequence, and it is a favourable one:** every enforcement shape in Part 3 currently has a
**no-op data footprint**. Whatever is built, nothing changes on any existing BOQ until the Head
marks his first item. That converts the "what happens to the three MPUVNL BOQs" question (3.3)
from a migration problem into a sequencing choice — the build can ship inert and be activated by
the Head's first flag.

## 4.4 The Residential path cannot be reached

**Confirmed, by four independent gates.**

**1. The type gate on the picker itself** — [views.py:4616-4618](projects/views.py#L4616-L4618),
the third statement in the function, before any authority check:

```python
    if project.project_type != 'OPEX':
        raise Http404('The BOQ picker is for OPEX sites. Residential BOQs are entered on '
                      'the standard BOQ screen.')
```

**2. Every catalogue read on the path is OPEX-scoped.** `get_opex_boq_catalogue()`
([models.py:777-781](projects/models.py#L777-L781)) and `opex_catalogue_category_order()`
([models.py:794-797](projects/models.py#L794-L797)) both filter `project_type='OPEX'`. A
Residential master cannot enter `catalogue_by_id`, therefore cannot enter `chosen`, therefore
cannot be created or preserved by this view.

**3. The form refuses the flag on Residential** —
[forms.py:546-551](projects/forms.py#L546-L551), quoted in 3.5. A Residential master cannot be
marked mandatory through the UI at all.

**4. `[PRODUCTION]` confirms the data:** 0 mandatory rows, so 0 Residential mandatory rows.

The one direction that *does* cross is inbound and pre-existing: TESTTENDER26-MB010 carries 37
Residential-linked rows on an OPEX BOQ (see 3.3). Those are read by `split_opex_boq_rows` as
off-catalogue. Nothing in the proposed change writes to them; they are affected only in that
mandatory rows would appear beside them.

## 4.5 The test suite — still 100% errors in `setUp`

**Confirmed.** `python manage.py test projects.tests_design_part11`:

```
FAILED (errors=32)
Destroying test database for alias 'default'...
Found 32 test(s).
```

The failure, identical for all 32:

```
ERROR: test_01_counts_by_project_type (projects.tests_design_part11.CatalogueTests.test_01_counts_by_project_type)
VERIFICATION 1 — 207 OPEX, and the Residential rows untouched beside them.
----------------------------------------------------------------------
Traceback (most recent call last):
  File "...\django\db\backends\utils.py", line 105, in _execute
    return self.cursor.execute(sql, params)
psycopg2.errors.UniqueViolation: duplicate key value violates unique constraint "projects_boqitemmaster_code_key"
DETAIL:  Key (code)=(ITM-001) already exists.

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "C:\SolarPMS\Horizon-Solar-PMS\projects\tests_design_part11.py", line 98, in setUp
    BOQItemMaster.objects.bulk_create([
        BOQItemMaster(code=f'ITM-{i:03d}', description=description, unit=unit,
    ...<2 lines>...
        for i, (category, description, unit) in enumerate(RESIDENTIAL_SEED, start=1)
    ])
```

The cause, [tests_design_part11.py:93-109](projects/tests_design_part11.py#L93-L109):

```python
class Part11Base(TestCase):
    """The real catalogue — 207 OPEX rows from the migration literal, plus a Residential
    template — and one OPEX site with a designer, a QC reviewer and a Head."""

    def setUp(self):
        BOQItemMaster.objects.bulk_create([
            BOQItemMaster(code=f'ITM-{i:03d}', description=description, unit=unit,
                          category=category, project_type='Residential',
                          is_active=True, sort_order=i)
            for i, (category, description, unit) in enumerate(RESIDENTIAL_SEED, start=1)
        ])
        BOQItemMaster.objects.bulk_create([
            BOQItemMaster(code=f'OPX-{i:03d}', description=description, unit=unit,
                          category=category, project_type='OPEX',
                          is_active=True, sort_order=i)
            for i, (category, description, unit) in enumerate(OPEX_BOQ_ITEMS, start=1)
        ])
```

Migrations **0047** and **0057** run when the test database is built and already seed
`ITM-001..ITM-037` and `OPX-001..OPX-207` — the test-run log shows them doing it:

```
[0047] BOQItemMaster seeded: 37 rows (ITM-001..ITM-037)
[0057] Created 207 OPEX catalogue row(s), by category:
[0057] Final catalogue count by project type:
         Residential          37
         OPEX                207
```

`setUp` then bulk-creates the same codes into the same unique column. All six test classes
(`CatalogueTests`, `ResidentialUnaffectedTests`, `PickerTests`, `DesignLockTests`,
`AggregationAndBlastRadiusTests`, `OffCatalogueRowTests`) inherit `Part11Base`, so **one broken
`setUp` accounts for all 32 errors.** *(Not fixed — reported only, per the prompt.)*

**What it would take.** Mechanically, this is small: **one file, one `setUp`, most likely a single
statement** clearing the migration-seeded catalogue before the fixture is created (the test DB has
no `BOQItem` rows at that point, so nothing cascades). The fixture is deliberate — `RESIDENTIAL_SEED`
is a stand-in whose last three entries are the colliding descriptions the tests exist to catch
([tests_design_part11.py:59-63](projects/tests_design_part11.py#L59-L63)) — so the fix is to make
room for the fixture, not to delete it.

**But the estimate must carry an honest caveat: these 32 tests have never executed a single
assertion.** Clearing `setUp` reveals them for the first time. Anywhere between 0 and 32 may then
fail on real assertions, and some of those failures would be genuine drift between the tests and
seven sessions of subsequent change (Parts 9/9.1, Sessions A1–A3, B, B.1, C, C.1). The *unblocking*
is a one-liner; the *greening* is unknown and cannot be estimated without doing it. Planning on
"one line" would be planning on the optimistic half.

---

# UNCERTAIN

1. **How many of the 32 tests pass once `setUp` is unblocked.** Not established, and not
   establishable without editing the file, which this session is forbidden to do. This is the
   single largest unknown in the size estimate.
2. **Whether the Design Head's Session C catalogue screen has its own deactivate handler**
   distinct from `admin_boq_item_toggle`. I confirmed the Admin one at
   [views.py:9127](projects/views.py#L9127) and confirmed Session C's views delete the
   `project_type` field and force `'OPEX'` after validation (per the `clean()` docstring at
   [forms.py:525-537](projects/forms.py#L525-L537)), but I did not enumerate the Head's full view
   set. 3.5's "location 2" may therefore be **two** handlers, not one.
3. **Whether `_boq_review_panel()` needs changing.** I read its docstring's first two lines
   ([design_views.py:1598-1601](projects/design_views.py#L1598-L1601)) but not its body. Its claim
   "ONLY WHAT THE DESIGNER ADDED" becomes inaccurate under any injection shape; whether that is a
   comment fix or a behaviour fix is not established.
4. **Whether OPEX site creation is a viable pre-load hook for shape 3.1(a).** 1.3 establishes there
   is no BOQ-creation moment today; I did not read `create_opex_site` to judge how hard adding one
   would be. Memory records that path as having zero test coverage, which is relevant if it becomes
   the injection point.
5. **Local database state.** Every database result in this report is `[PRODUCTION]`. No `[LOCAL]`
   comparison was made — the local Postgres was used only to build the ephemeral test database.
6. **Row-ordering behaviour of injected mandatory rows.** `serial_no` comes from
   `master.sort_order` ([views.py:4717](projects/views.py#L4717)), so injected rows interleave by
   catalogue position rather than appearing first. Whether the Head expects mandatory items grouped
   at the top of the sheet is a product question nobody has asked.

---

# CONFLICTS

Places where this prompt's stated assumptions disagree with the code or the data.

1. **"The Head has been marking items; report the real set rather than assuming it is small."**
   `[PRODUCTION]`: **the set is empty — 0 rows with `is_mandatory=True`, out of 244 catalogue
   rows, all active.** C.1 reached production at 2026-08-15 02:16 UTC, hours before this audit,
   so there has been no meaningful window. This is favourable — see 4.3 — but the prompt's framing
   of 4.3 and of the 3.3 migration question is built on a premise that does not hold.

2. **"`|safe` JSON payloads (audit A found them around `opex_boq_entry.html:203-204`)."** They are
   at **lines 218-219**. Lines 203-204 are inside a `{% comment %}` block. Line numbers have moved
   since audit A.

3. **"`aggregate_group_boq()` (`design_views.py:~4126-4155`)."** The function begins at line
   **4129** and runs to ~4190. The cited range covers the docstring and first query only. Minor,
   and the prompt hedged with `~`.

4. **"Session C.1 made `BOQItemMaster.is_mandatory` settable and visible; nothing reads it."**
   Accurate, and the model comment says so
   ([models.py:704](projects/models.py#L704)): *"INERT AS OF THIS COMMIT. Nothing reads it but the
   two catalogue list templates."* Confirmed — the only reads are
   [admin/boq_items.html:102](projects/templates/projects/admin/boq_items.html#L102) and
   [design/boq_catalogue.html:99](projects/templates/projects/design/boq_catalogue.html#L99).
   No conflict; recorded because the prompt asked the audit to establish it.

5. **"(a) Rows written when the `BOQ` is created — mirrors Residential, but needs 1.3 to establish
   there is a creation moment for OPEX."** 1.3 establishes there is **not** one, outside the save
   itself. Option (a) is therefore not a variant of (b) — it is (b) *plus* a new creation moment
   *plus* a decision about 93 BOQ-less sites. The prompt anticipated this possibility; it is
   confirmed.

6. **The prompt's framing of the request as possibly "genuinely new behaviour rather than a port"
   (2.2).** It is a **port** — `is_standard_item` already makes Residential pre-populated rows
   undeletable at [views.py:4498](projects/views.py#L4498). But the field cannot be reused on the
   OPEX side, because the picker already writes `is_standard_item=True` on **every** row
   ([views.py:4720](projects/views.py#L4720)) and `[PRODUCTION]` shows all 185 live OPEX rows
   carry it. The pattern ports; the field does not.

7. **"the highest-risk surface remaining in the module."** Supported in the sense that the tests
   have never run, but two of the four hard stops written to catch picker danger did not fire: the
   design lock **is** enforced on POST (1.6), and the save **is** a diff that preserves quantities,
   pks and off-catalogue rows (1.4). The POST path is in better shape than the prompt's framing
   expects.

---

# ANSWERED / PARTIAL / NOT ESTABLISHED

| Item | Question | Status |
|---|---|---|
| 1.1 | `opex_boq_entry` in full, GET/POST boundary | ANSWERED — boundary at 4639/4743, shared preamble 4614-4637 |
| 1.2 | Every helper, return shape, `is_active` filtering | ANSWERED — 5 helpers + 4 permission helpers |
| 1.3 | When a `BOQ` first exists for OPEX | ANSWERED — lazily, inside the POST transaction, [views.py:4696](projects/views.py#L4696) |
| 1.4 | POST path: replace vs diff, pk filtering, write mechanism, removal | ANSWERED — diff; `catalogue_by_id` membership; `create` + `save(update_fields)`; hard delete |
| 1.5 | Save draft vs Mark complete | ANSWERED — one hidden field, shared persistence, divergence only after commit |
| 1.6 | Design lock enforcement | ANSWERED — enforced on POST at 4649-4653; hard stop 3 not triggered |
| 1.7 | `|safe` payloads and the `json_script` alternative | ANSWERED — lines 218-219, not 203-204 |
| 1.8 | Client-side removal | ANSWERED — pure client state, both paths, no server round trip |
| 2.1 | Residential pre-population | ANSWERED — GET-time `bulk_create`, idempotent only via the OneToOne constraint |
| 2.2 | Can Residential remove a pre-populated row | ANSWERED — no; `is_standard_item` at [views.py:4498](projects/views.py#L4498); field unusable on OPEX |
| 3.1 | Persistence timing, three shapes | ANSWERED — (a) blocked by 1.3; (c) has precedent but contradicts a documented local decision |
| 3.2 | Quantity at completion | ANSWERED — "at least one row anywhere"; MB0191 completed with 51/53 |
| 3.3 | Existing BOQs `[PRODUCTION]` | ANSWERED — 4 BOQs, 3 design-locked, 0 group-locked, 93 sites with none |
| 3.4 | Enforcement mechanism | ANSWERED — both reported, neither chosen; (a) composes with the diff, (b) needs new error rendering |
| 3.5 | Mandatory + inactive | ANSWERED — unguarded; three candidate surfaces named |
| 4.1 | `aggregate_group_boq()` | ANSWERED — no change needed; null-qty rows excluded by `boq_quantity__gt=0` |
| 4.2 | `design_analytics.py` | ANSWERED — no BOQ row references at all; no metric distorted |
| 4.3 | `[PRODUCTION]` mandatory rows | ANSWERED — **zero**, contradicting the prompt |
| 4.4 | Residential unreachable | ANSWERED — four independent gates |
| 4.5 | Test suite | ANSWERED for the failure; **PARTIAL** for the estimate — pass rate after unblocking is unknowable without editing |

---

# SIZE ESTIMATE

**Files touched** — 3 to 5, depending on the Part 3 decisions:

| File | Why | Conditional on |
|---|---|---|
| [projects/views.py](projects/views.py) | `opex_boq_entry` — mandatory set into `chosen` (POST) and/or into `added_json` (GET) | always |
| [projects/models.py](projects/models.py) | a `get_opex_mandatory_items()` helper beside the other four, so the picker and any completion guard read one definition | always (strongly implied by the existing file's structure) |
| [projects/templates/projects/opex_boq_entry.html](projects/templates/projects/opex_boq_entry.html) | suppress the `data-del` button on mandatory rows in `renderSheet()`; carry the flag in `catalogue_json` | always |
| [projects/design_views.py](projects/design_views.py) | stricter completion guard | only if 3.2 says mandatory rows need quantities |
| [projects/forms.py](projects/forms.py) + a deactivate handler | close the mandatory+inactive contradiction | only if 3.5 is closed at the source rather than contained |

**Migration needed — no.** `is_mandatory` already exists (0064, deployed). Every shape in Part 3
reads it; none adds a field. And because `[PRODUCTION]` shows **zero** flagged rows, **no data
migration is needed either** — there is no existing mandatory row to back-fill onto the three
MPUVNL BOQs. The build can ship inert and activate when the Head marks his first item. This is the
single biggest cost reduction the audit found, and it is a direct consequence of C.1 having only
just deployed.

**Should the test fix precede the build — yes, and it should be its own session.**

Not because unblocking is hard — it is plausibly one statement in one `setUp` (4.5). Because of
what unblocking *reveals*. Thirty-two tests have never executed an assertion, and six of them are
`PickerTests` — tests written specifically against the POST path this session proposes to change.
Building first means changing that path with its test suite dark, then discovering afterwards
whether a failure is the change's fault or seven sessions of pre-existing drift. Fixing first means
the picker tests either go green (and become a real safety net for the build) or produce a known
failure list (and the build knows what it is standing on). The asymmetry is large and the
sequencing is cheap.

The honest framing for the session boundary: **the unblock is small, the greening is unknown.**
Scope that session as "unblock `setUp` and report the resulting pass/fail list", not as "make 32
tests pass" — the second is not estimable from here.

**One session or more — more. Three, in this order:**

1. **Test unblock** (small, unknown tail). Own session, per above.
2. **The Part 3 decisions.** Five questions, and 3.1 is not the two-way choice the prompt frames:
   1.3 collapsed (a) into "(b) plus a new BOQ-creation moment plus 93 back-filled sites". These are
   chat decisions and need the evidence in Parts 1 and 3 in front of whoever makes them.
3. **The build.** Once 1 and 2 are done, the build itself is genuinely small — under shape 3.4(a)
   the server half is a handful of lines mutating `chosen_set` before the transaction, and the
   client half is a conditional on one button.

**Is enforcement larger than the "one screen, one rule" it looks like from outside? — Yes,
moderately, and for reasons that are structural rather than incidental.**

Three of them, in descending order of weight:

- **"Mandatory" is two rules, not one.** *Appears on every BOQ* and *cannot be removed* have
  different homes: the first is a persistence-timing question with three genuinely different
  answers (3.1), the second is a POST-shape question with two (3.4). They can be decided
  independently and one can ship without the other.
- **Removal is expressed as absence.** The picker has no "delete this row" instruction — a row is
  deleted by not being in the POST (1.4, 1.8). That single design fact is what makes 3.4(b)
  awkward and 3.4(a) natural, and it is not visible from outside the screen.
- **"Every OPEX BOQ" is ambiguous across four existing sheets and 93 absent ones.** Three of the
  four are design-locked and unreachable by the POST path; 93 sites have no BOQ at all. The word
  "every" has to be resolved into "every new one", "every one that saves again", or "every one
  including a back-fill" before anything is built (3.3).

Offsetting all three: the empty mandatory set on production (4.3) means whatever is decided has no
retroactive data cost, and `aggregate_group_boq()` (4.1) and `design_analytics.py` (4.2) both need
no changes at all. **The blast radius is smaller than expected; the decision surface is larger.**
