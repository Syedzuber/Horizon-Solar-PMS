# B-6 content mapping — ten real HRPPL checklists onto the real OPEX template

**Read-only audit. 5 Sep 2026.** No application code was written. This is the content
half of the question [`docs/CHECKLIST_GATE_DISCOVERY.md`](CHECKLIST_GATE_DISCOVERY.md)
left open — that audit found the mechanism complete and the content empty, and named
*"somebody must decide which of the 23 OPEX template tasks carry a checklist and what
lines are on each"* as the concrete gap. Real documents now exist. This maps them.

**The headline, before the detail:** the mapping is clean — every real document lands on
a real task and only one Installation task is left uncovered. The mechanism is what does
not fit. Three of the sizing questions below exist because the checklist model was built
for a different shape of document than the ones that turned up:

- **One checklist per task is a hard constraint at three layers.** Four of the nine
  Installation tasks need two source documents each. They must be merged into one
  `Checklist` — and nothing in `ChecklistItem` can then tell the reader which document a
  line came from.
- **A photo is mandatory, in the model comment, the view, the form and two tests.** These
  documents carry no evidence requirement at all. As built, an engineer cannot answer a
  line without photographing something.
- **There is no remarks field, and no way to record "No".** These documents are Yes/No +
  Remarks. `is_checked` is a boolean whose False means *not yet answered*.

---

## Counting note, stated first

The prompt says *"ten checklists"* and then lists **eleven** titles. Enumerated as given:

```
 1. Concrete Block (RCC/PCC)
 2. DCDB
 3. Structure (mounting, fixed & tracker)
 4. Inverters
 5. PV Modules (SPV)
 6. AC-DC Cables
 7. Earthing System
 8. ESE Type Lightning Arrestor
 9. LT Panel/ACDB
10. SCADA System
11. String Combiner Box/Array Junction Box
```

Eleven titles are mapped below. If one of them is not a real separate document — the most
likely candidate is **AC-DC Cables**, which reads as one title covering two cable types and
is the one document this mapping wants to split in two anyway (see task 2) — the count
resolves to ten. **Not guessed at:** all eleven are carried through.

---

## Task 1 — the nine Installation-phase task names

Not previously enumerated in any audit report. `docs/OPEX_task_template_spec.md:189`
lists them inline in prose; this is the first enumeration against the database.

Queried live, from the **active** OPEX template (`OPEX v1`), local Postgres
`solarpms_local`:

```
TEMPLATE: OPEX v1 (active) code= OPEX
 PHASE 1 Design -> 1
 PHASE 2 Approvals (Pre-Installation) -> 2
 PHASE 3 Procurement & Delivery -> 4
 PHASE 4 Installation -> 9
 PHASE 5 Testing & Commissioning -> 2
 PHASE 6 Approvals (Post-Installation) -> 1
 PHASE 7 Closeout -> 4
--- PHASE 4 ---
1 | Civil Work and MMS Installation | CIVIL_WORK_AND_MMS_INSTALLATION | Site Engineer | mirror= False
2 | Module Installation | MODULE_INSTALLATION | Site Engineer | mirror= False
3 | LA and Earthing Installation | LA_AND_EARTHING_INSTALLATION | Site Engineer | mirror= False
4 | DC Cable Laying with Conduit | DC_CABLE_LAYING_WITH_CONDUIT | Site Engineer | mirror= False
5 | DCDB and ACDB Installation | DCDB_AND_ACDB_INSTALLATION | Site Engineer | mirror= False
6 | Inverter Installation | INVERTER_INSTALLATION | Site Engineer | mirror= False
7 | AC Cable Laying | AC_CABLE_LAYING | Site Engineer | mirror= False
8 | RMS Installation | RMS_INSTALLATION | Site Engineer | mirror= False
9 | Solar Generation Meter Installation | SOLAR_GENERATION_METER_INSTALLATION | Site Engineer | mirror= False
```

Confirmed facts:

- **Nine tasks, all `is_mirror=False`.** Every one is an ordinary task with a real status
  and a real approver — all nine are gateable, unlike the COD row.
- **All nine are `Site Engineer`.** The checklist completer gate
  (`_user_can_complete_checklist_item`, [views.py:2642](../projects/views.py#L2642)) is
  role-match **or** PM/coordinator, so the assigned SE and the PM can both tick.
- **`code` is the stable identity.** `ChecklistTaskLink` resolves through
  `template_task__code` since 2.4, so the `CODE` column above — not the label — is what a
  link survives a template version on. Content authoring should be recorded against these.
- **v1 is the only OPEX template that has ever existed.** `0075_seed_opex_template_v1.py`
  is the sole seed; migrations run to `0080_punch_point` and none of `0076`–`0080` touches
  the template. The **OPEX v2 spec decisions are still unbuilt** — if v2 lands with the
  four delivery mirrors and dropped inspections, Phase 4 is *not* among the phases it
  changes, so this mapping survives it. Worth re-confirming at build time, not assumed.

---

## Task 2 — mapping the eleven documents onto the nine tasks

### The constraint that shapes the answer

**Confirmed still exactly as built in 2.4.** `unique_together = ('task_name',
'project_type')` was set when the model was created in `0041` and has **never been
altered since** — a grep over every migration for an `AlterUniqueTogether` on
`checklisttasklink` returns nothing:

```
$ grep -rn "checklisttasklink" projects/migrations/*.py | grep -i "unique|alter"
(no output)

$ grep -n "unique_together" projects/migrations/0041*.py
69:                'unique_together': {('task_name', 'project_type')},
88:                'unique_together': {('item', 'task')},
```

`0077` (prompt 2.4) **added** a second constraint beside it and removed nothing
([models.py:2843](../projects/models.py#L2843)):

```python
    class Meta:
        unique_together = ('task_name', 'project_type')
        ordering        = ['project_type', 'task_name']
        constraints = [
            models.UniqueConstraint(
                fields=['template_task'],
                condition=models.Q(template_task__isnull=False),
                name='uniq_checklist_task_link_template_task',
            ),
        ]
```

And the model comment states plainly why both are kept — *"UNIQUENESS, TWO CONSTRAINTS AND
NEITHER IS REDUNDANT."*

There is a **third** layer, in the authoring view
([views.py:12181](../projects/views.py#L12181)), which refuses a duplicate by `code`
before either DB constraint fires and is the only layer that can name the offender:

```python
    if existing is not None:
        if existing.checklist_id == checklist.pk:
            messages.error(request, f'"{task_name}" ({project_type}) is already assigned to this checklist.')
        else:
            messages.error(
                request,
                f'"{task_name}" ({project_type}) is already assigned to checklist '
                f'"{existing.checklist.name}". Unassign it there first.'
            )
```

**Therefore, and this is the load-bearing fact for the whole mapping:**

| Direction | Allowed? |
|---|---|
| One `Checklist` → **many** tasks | **Yes.** `related_name='task_links'`; one checklist may hold any number of links. |
| One task → **many** `Checklist`s | **No.** Refused at the view, at `unique_together`, and at the partial constraint. |

So "two documents on one task" is unbuildable as two Checklists. It is only buildable as
**one merged Checklist**, which is what makes task 3 a real question rather than a nicety.

### The mapping

| # | Task (`code`) | Source document(s) | Fit |
|---|---|---|---|
| 1 | `CIVIL_WORK_AND_MMS_INSTALLATION` | **Concrete Block (RCC/PCC)** + **Structure (mounting, fixed & tracker)** | **Merge of 2.** Foundation and the structure bolted to it; the task name literally names both halves ("Civil Work **and** MMS"). MMS = Module Mounting Structure. |
| 2 | `MODULE_INSTALLATION` | **PV Modules (SPV)** | **1:1, clean.** |
| 3 | `LA_AND_EARTHING_INSTALLATION` | **Earthing System** + **ESE Type Lightning Arrestor** | **Merge of 2.** Again the task name names both: LA = Lightning Arrestor. |
| 4 | `DC_CABLE_LAYING_WITH_CONDUIT` | **String Combiner Box / Array Junction Box** + the **DC half of AC-DC Cables** | **Merge of 2** (one of which is half a document — see the split below). |
| 5 | `DCDB_AND_ACDB_INSTALLATION` | **DCDB** + **LT Panel / ACDB** | **Merge of 2.** Third time the task name names both halves. |
| 6 | `INVERTER_INSTALLATION` | **Inverters** | **1:1, clean.** |
| 7 | `AC_CABLE_LAYING` | the **AC half of AC-DC Cables** | **Half a document** — see below. |
| 8 | `RMS_INSTALLATION` | **SCADA System** | **Needs confirmation.** See flag (d). |
| 9 | `SOLAR_GENERATION_METER_INSTALLATION` | *(none)* | **Uncovered task.** See flag (b). |

### Flagged explicitly

**(a) Every document maps to a task. None is orphaned.** All eleven titles find a home in
Phase 4. Nothing needed to be pushed into Phase 3, 5 or 6 to be placed.

**(b) One task has no matching document: `SOLAR_GENERATION_METER_INSTALLATION` (#9).**
The eleven documents cover civil, structure, modules, earthing, LA, DC cabling, combiner
boxes, DCDB, ACDB/LT panel, inverters and SCADA — and stop. Metering is not among them.
This is a content question for the Tenders/QA-QC team, not an engineering one: either a
twelfth document exists and was not supplied, or the meter install is genuinely not
checklisted. **It is not a blocker** — a task with no link simply renders "No checklist
items yet" exactly as today. But if B-6's gate is later written as *"every Installation
task must have a completed checklist"* rather than *"every task **with a linked
checklist**"*, this task deadlocks. The existing resolver already returns `None` for an
unlinked task ([views.py:4092](../projects/views.py#L4092)), so the safe predicate is the
second one, and B-6 should be written that way on purpose.

**(c) One document spans two tasks: AC-DC Cables → tasks #4 and #7.** Two ways to place
it, and the choice is content, not code:

- **Split it into two Checklists** — "DC Cables" linked to `DC_CABLE_LAYING_WITH_CONDUIT`,
  "AC Cables" linked to `AC_CABLE_LAYING`. **Recommended.** Nothing in the model resists
  it, and it is the only option under which the AC task does not display DC lines the
  engineer must ignore. It also resolves the eleven-vs-ten count.
- **Link one Checklist to both tasks.** *Legal* — one checklist may hold many links — but
  each task then shows the whole document, and under a completion gate the AC task cannot
  be closed until the DC lines are ticked on it too. Completion is per-`(item, task)`, so
  the same lines would have to be answered twice, once per task. **Not recommended.**

**(d) SCADA System → RMS Installation is the one inferential mapping.** RMS = Remote
Monitoring System; SCADA is the supervisory-control system it is the site end of. They are
plausibly the same equipment under two names, and no other Phase 4 task is a better fit.
**Confirm with the author of the documents before seeding.** If they are distinct systems,
`RMS_INSTALLATION` joins #9 as an uncovered task and SCADA becomes the orphan.

**(e) Four tasks need two documents each: #1, #3, #4, #5.** Under the constraint above,
each must become **one merged Checklist** carrying both documents' lines. That is task 3.

---

## Task 3 — sizing a section label

### What exists today

`ChecklistItem` has **three** fields, and none of them groups
([models.py:2764](../projects/models.py#L2764)):

```python
    checklist = models.ForeignKey(Checklist, on_delete=models.CASCADE, related_name='items')
    label     = models.TextField()
    order     = models.PositiveIntegerField(default=0)  # Ascending within checklist; swapped by move up/down

    class Meta:
        ordering = ['order', 'pk']  # pk tiebreak keeps ordering stable when two items share an order
```

The render is a **flat table**, one row per item, numbered by `forloop.counter`
([_checklist.html](../projects/templates/projects/partials/_checklist.html)):

```django
      {% for row in checklist_rows %}
      <tr>
        <td class="text-muted small">{{ forloop.counter }}</td>
        <td>{{ row.label }}</td>
```

The authoring form posts a single field
([checklist_edit.html:139-142](../projects/templates/projects/admin/checklist_edit.html#L139-L142)):

```django
    <form method="POST" action="{% url 'admin_checklist_item_add' checklist.pk %}" class="flex items-center gap-2 mt-4">
      <input type="text" name="label" required placeholder="New item…"
```

**Answer: there is no grouping or section label of any kind.** Items from two source
documents merged onto one task would sit in one undifferentiated numbered list. On task
#1 that is Concrete Block lines running straight into Structure lines with nothing
between them — an engineer reading row 14 cannot tell which document they are answering.

### Option A — no schema change: encode the section in the label

Author the merged checklist with the source named in each line, e.g.
`"[Concrete Block] Formwork aligned and braced"`, ordered so each document's lines are
contiguous. `order` is already manual, so contiguity is free.

- **Cost: zero code.** Content-only.
- **Lost:** no visual break; the prefix repeats on every row and eats the label column;
  `item_text_snapshot` (R-8) captures the prefix as part of the answered text, which is
  arguably correct but permanent; renaming a source document later means editing every
  line, which R-7 forbids on an active checklist — it forces a v2.

### Option B — add `section` to `ChecklistItem`

Minimal shape, and each piece is small:

| Change | File | Size |
|---|---|---|
| `section = models.CharField(max_length=100, blank=True, default='')` | `models.py` (~2775) | 1 line |
| Migration `0081` — one `AddField`, no backfill (0 items exist) | `migrations/` | ~15 lines, boilerplate |
| Accept `section` on add + edit | `admin_checklist_item_add` ([12055](../projects/views.py#L12055)), `admin_checklist_item_edit` ([12085](../projects/views.py#L12085)) | ~4 lines |
| One input beside `label` in the add form and the edit row | `checklist_edit.html` | ~6 lines |
| Group rows by section in the shared builder | `_checklist_context` ([4102](../projects/views.py#L4102)) | ~8 lines — the loop already builds `rows`; emit `[{section, rows}]` |
| Render a section header row | `_checklist.html` | ~5 lines |

**~40 lines across 5 files, one trivial migration, no data to migrate.** Immutability
comes free: `section` is content on `ChecklistItem`, whose `save()` already calls
`_require_draft_template`, so R-7 covers it without a further line.

**Two things the sizing must not hide:**

1. **`_checklist_context` has two other callers** — the full task-detail render
   ([views.py:9049](../projects/views.py#L9049)) and the HTMX swap
   ([4142](../projects/views.py#L4142)). Changing `checklist_rows` from a flat list to a
   grouped one changes what both render. Either keep `checklist_rows` flat and **add**
   `checklist_sections` beside it (safer, and `checklist_items` is already a parallel key
   for the count badge), or update both templates. The flat-plus-grouped shape is the
   cheaper of the two and is what the ~40 lines above assumes.
2. **R-8 does not cover `section`.** `item_text_snapshot` snapshots `label` only
   ([models.py:2895](../projects/models.py#L2895)). A completed row would render its
   snapshotted label under a **live** section heading. Either snapshot the section too
   (one more field, one more line in the atomic save) or accept that the section is
   presentational and never part of the answered record. **Recommend accepting it as
   presentational** and saying so in a comment — the section names a source document, not
   a question, and inventing a second snapshot column for a heading is the kind of
   parallel record this model has been removing.

**Recommendation: Option B**, and only if the merges of task 2 are actually seeded. If the
AC-DC split (flag (c)) is taken and the four merges are instead authored as documents whose
two halves are genuinely one procedure, Option A is honest and free.

---

## Task 4 — the three-signature gap

### What is captured today: exactly one completer

`ChecklistItemCompletion`, in full, on the "who" question
([models.py:2915](../projects/models.py#L2915)):

```python
    checked_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='checked_checklist_items',
    )
    checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
```

Written once, from the request user, in the atomic save
([views.py:9455](../projects/views.py#L9455)):

```python
    completion.checked_by          = request.user
    completion.checked_at          = timezone.now()
```

Rendered as one name and one timestamp:

```django
            <span class="badge bg-success">✓ Checked</span>
            <div class="text-muted small mt-1">
              {% if row.completion.checked_by %}{{ row.completion.checked_by.get_full_name|default:row.completion.checked_by.username }}{% endif %}
              {% if row.completion.checked_at %} · {{ row.completion.checked_at|date:"d M Y H:i" }}{% endif %}
            </div>
```

**Stated plainly: one signer. The model supports Checked By and nothing else.** There is
no witness field, no second or third actor, no free-text field a witness's name could go
in, and no related model pointing at a completion. It does not support the real pattern
(Checked By + two Witnessed By) in any form.

**One structural note before the options.** On the real documents the three signatures are
almost certainly **per-document**, signed once at the foot of the sheet — not per line.
`ChecklistItemCompletion` is keyed `(item, task)`, so anything added there is captured
**per line**: on a 30-line merged checklist that is 90 signatures where the paper form has
3. Neither option below fixes that by itself; a per-*checklist-per-task* signature record
is a third shape, and it is the one that matches the paper. It is called out here rather
than sized because it changes what B-6's gate reads (a signature row, not N ticks) and
that is a scoping decision, not a line count.

### Option 1 — extend the model to capture multiple signers

Two shapes:

- **1a — two nullable FK pairs on the completion:** `witness_1` / `witness_1_at`,
  `witness_2` / `witness_2_at`. ~4 fields, one migration, plus a UI for a second and third
  person to sign a row that the first person already closed — the current flow *hides the
  form* once `is_checked` is true, so witnessing needs a **new action, a new URL, a new
  permission rule** (who may witness? not the completer, presumably) and a new partial.
  The field pair is small; the flow around it is not.
- **1b — a `ChecklistItemWitness` child model** (`completion` FK, `user` FK,
  `witnessed_at`, optional role). Cleaner, uncapped at two, same UI cost. Adds a model, a
  migration, an inline in admin, and a query in `_checklist_context` (which would need a
  prefetch to avoid N+1 across 30 rows).

**Realistic size for either: a model/field change plus a witness action end-to-end —
new view, new URL, new permission predicate, new partial, tests.** Several hundred lines.
Not a field addition.

### Option 2 — one primary completer, witnesses as free text

Add **one** field to `ChecklistItemCompletion`:

```python
    remarks = models.TextField(blank=True, default='')
```

- Migration `0081`: one `AddField`, no backfill (0 completions exist — `completions 0`,
  queried above).
- `checklist_item_complete` reads `request.POST.get('remarks', '').strip()` and adds
  `'remarks'` to the existing `update_fields` list — **2 lines**, inside the save that is
  already atomic.
- One `<textarea>` in the completion form and one `<td>` in the render — **~6 lines**.
- Tests: none break. The photo tests
  ([tests_checklist_snapshot.py:469](../projects/tests_checklist_snapshot.py#L469)) and
  the snapshot tests are untouched by an additive optional field.

**~10 lines, one migration, one field. This is by a wide margin the cheaper option** —
and note that **task 5 requires this exact field anyway**, so as a witness mechanism it is
free.

**What is lost with Option 2, stated without softening:**

- **The witnesses are not users.** No FK, no identity, no login behind the name. A typed
  name is an *assertion by the completer*, not a signature by the witness. If the point of
  a witness is that a second person independently attests, free text does not deliver it —
  the completer types both names alone, in one action, in five seconds.
- **No per-witness timestamp.** All three "signatures" carry the completer's single
  `checked_at`.
- **Unqueryable.** "Which items did X witness", "how many witnessed sign-offs did this
  site collect" — impossible without parsing prose.
- **No structure to gate on.** A future rule of the form *"an item is not complete until
  two witnesses have signed"* has nothing to test. Option 1 could support it; Option 2
  cannot, ever, without a later migration that re-does the work.
- **Nothing enforces that anything is typed.** A blank remarks field is a valid completion.

**Recommendation: Option 2 now, and be explicit in the commit and the UI label that it
records witness *names*, not witness *signatures*.** The distinction matters if these
documents are ever produced as evidence of a QA process. If the client's requirement is
genuinely that three people attest, Option 2 does not meet it and no amount of labelling
makes it — that is the decision this section exists to put in front of someone, and it
should be answered before the field is named `witnesses`.

---

## Task 5 — evidence per item

**This is the largest mismatch in the audit, and the answer is: no, it does not match.**
Three separate gaps, each independently blocking.

### Gap 1 — a photo is MANDATORY, at four layers

The model comment states it as an invariant
([models.py:2883](../projects/models.py#L2883)):

> *"Completing an item requires **BOTH** a tick and a photo: is_checked is only ever set
> True together with the three photo_* fields in the same save, so a checked item can
> never lack a photo."*

The view refuses the POST outright ([views.py:9414](../projects/views.py#L9414)):

```python
    photo = request.FILES.get('photo')
    if not photo:
        return _checklist_error(request, project, task,
                                'A photo is required to check this item.')
```

The form input carries `required`, and the comment above it explains that the camera opens
by design:

```django
              <input type="file" name="photo" accept=".jpg,.jpeg,.png" capture="environment" required
                     class="form-control form-control-sm">
              <button type="submit" class="btn btn-success btn-sm">Check + Upload Photo</button>
```

And **two tests pin it**
([tests_checklist_snapshot.py:469-480](../projects/tests_checklist_snapshot.py#L469-L480)):

```python
    def test_a_photo_is_still_mandatory(self):
        _client_for(self.pm).post(
            reverse('checklist_item_complete',
                    args=[self.project.project_id, self.task.pk, self.item.pk]), {})
        self.assertEqual(ChecklistItemCompletion.objects.count(), 0)

    def test_a_checked_item_can_still_never_lack_a_photo(self):
        self._complete(self.item)
        for completion in ChecklistItemCompletion.objects.filter(is_checked=True):
            self.assertTrue(completion.photo_file_name)
            self.assertTrue(completion.photo_url)
            self.assertTrue(completion.photo_supabase_path)
```

Seeding these eleven documents as-is means an engineer must photograph something **once
per line** — on the merged task #1 that is potentially thirty photographs for a document
that asks for none. It is not a friction problem; it is a rule the product enforces that
the paper form does not have.

**Sizing the fix.** Make the photo **per-checklist optional** rather than globally
optional — a boolean on `Checklist` (`requires_photo`, default `True`, so every existing
behaviour and both tests above keep passing unchanged) consulted by the view and the
template. ~1 field, 1 migration, ~4 lines of view, ~2 lines of template. Then the HSE-style
document that *does* want evidence keeps it and these eleven do not. **A global switch to
optional would silently weaken the HSE draft's requirement and should not be taken** — the
two documents genuinely differ and the model should say which is which.

### Gap 2 — there is no Remarks field

`ChecklistItemCompletion` has `is_checked`, `item_text_snapshot`, three `photo_*` fields,
`checked_by`, `checked_at`, `created_at`. **No text field the engineer can write in.**
`item_text_snapshot` is the *question*, written by the system, not an answer.

This is the same field as task 4's Option 2, and adding it once serves both. ~10 lines.

### Gap 3 — "No" is unrepresentable

`is_checked = models.BooleanField(default=False)`. Every row starts False. False therefore
means **"not yet answered"** and is rendered as such:

```django
          {% else %}
            <span class="badge bg-secondary">Pending</span>
```

There is no third state. An engineer who inspects an item and finds it **not** compliant
has no way to record that: ticking it asserts the opposite, and leaving it untouched is
indistinguishable from not having looked. On a Yes/No form that is a real loss of
information — and under a completion gate it is worse than a loss, because the only way to
close the task is to answer Yes to everything.

**Sizing.** Replace `is_checked` with a three-value `answer` field (`yes` / `no` / unset)
— **or**, much cheaper and additive, keep `is_checked` as "answered" and add
`answer = models.CharField(choices=[('yes','Yes'),('no','No')], blank=True)` beside it.
The additive form is ~1 field, 1 migration, ~3 lines of view, ~8 lines of template, and
**breaks nothing**: every existing read of `is_checked` keeps meaning "this line has been
answered", which is exactly what a gate wants to count. The replacement form is cleaner
and touches `_checklist_context`, both templates, the admin, and the snapshot tests.

**Note for whoever scopes B-6 after this:** if "No" becomes recordable, the gate's
predicate is a product question — does a checklist with a recorded "No" block the task, or
is that precisely what a **punch point** is for? `PunchPoint` was built in 2.3a and wired
to `task_reject`. A "No" line and a punch point are plausibly the same fact recorded twice.
**Out of scope here; flagged so it is decided rather than discovered.**

### Summary of task 5

| Real document expects | Model supports today | Verdict |
|---|---|---|
| No evidence requirement | Photo mandatory at 4 layers, incl. 2 tests | **Blocked** — needs `Checklist.requires_photo` |
| Remarks column | No text field at all | **Blocked** — needs `ChecklistItemCompletion.remarks` |
| Yes / No | Boolean where False = unanswered | **Blocked** — needs a third state |

Nothing here is large. All three together are one migration, one model change to each of
two models, and under a hundred lines. But **none of the eleven documents can be seeded
faithfully until they are done**, and seeding them without would produce a checklist that
asks for photographs nobody agreed to take and cannot record a failure.

---

## Task 6 — the master QA/QC checklist is out of scope

**Confirmed plainly, and nothing in this document builds toward it.**

The master 20-section QA/QC checklist — the one ending in a single **Accepted /
Accepted-with-Punch-Points / Rejected** verdict — is a **site-level gate**, not per-task
content. It does not get seeded as a `Checklist` in this pass, and the eleven documents
above are not steps toward it.

The mechanical reason it cannot be one, not merely a scoping preference: a `Checklist`
resolves through `ChecklistTaskLink` to **one template task**
(`unique_together` + `uniq_checklist_task_link_template_task`, quoted in task 2), and its
completion state lives per `(item, task)`. A site-level document has no single task to
hang from, and its verdict is one value for the whole site, which `ChecklistItemCompletion`
has no shape for. Forcing it into this model would mean either inventing a carrier task or
storing a site-level verdict on a per-item row.

**Forward-looking note only — where it would eventually sit.** Phase 6, `Approvals
(Post-Installation)`, whose sole task is:

```
{'task_order': 1, 'task_name': 'Post-Installation Approvals', 'assigned_role': 'PM', 'task_type': 'Internal'},
```

That is where a site-level acceptance verdict belongs, and its three outcomes line up with
machinery that already exists: **Accepted** → approve; **Rejected** → `task_reject`;
**Accepted with Punch Points** → the `PunchPoint` model built in 2.3a, which is already
wired to `task_reject` and already has a PM-only waiver. The verdict would be the thing the
**COD gate** reads.

The COD gate remains deferred for the reason `docs/QAQC_PUNCH_POINT_DISCOVERY.md:750`
gives, unchanged by anything in this document:

> *"The COD `Task` row exists but is `is_mirror=True` with **no source object and no
> derivation hook**. It is permanently Not Started, refused to every human at rung 0. **A
> gate on it would be a no-op that passes its own tests** — the worst kind of shipped
> feature."*

Confirmed against the live template: Phase 7's `COD` is still `is_mirror=True` and still
has no source object. **Nothing here changes that, and nothing here should be read as
preparation for it.**

---

## What needs a decision before any build

| # | Decision | Recommendation |
|---|---|---|
| 1 | Is **AC-DC Cables** one document or two? (resolves eleven vs ten) | Split into two Checklists, one per cable task |
| 2 | Is **SCADA System** the same equipment as **RMS Installation**? | Confirm with the document author; it is the one inferential mapping |
| 3 | Is there a twelfth document for **Solar Generation Meter Installation**? | If not, B-6's gate must key on *linked* checklists, not on all tasks |
| 4 | Section labels — schema field, or prefixes in the label text? | Option B (`ChecklistItem.section`, ~40 lines) if the four merges are seeded |
| 5 | Witnesses — structured signers, or names in a remarks field? | Option 2 (free text, ~10 lines), **provided** it is labelled "names", not "signatures" |
| 6 | Photo requirement | Per-checklist `requires_photo`, defaulting True — never a global switch |
| 7 | Recording "No" | Additive `answer` field beside `is_checked`; then decide whether a "No" blocks or raises a punch point |

Items 5, 6 and 7 are one migration between them. **Items 1–3 are content questions and
nobody in this repository can answer them.**

---

## Appendix — current checklist inventory, queried live

```
--- CHECKLISTS ---
7 DEMOCHECKLIST DemoChecklist draft
--- LINKS ---
1 'SLD' Residential 10
items 0 completions 0
```

One draft demo checklist with no items, one Residential link, **zero OPEX links, zero
items, zero completions.** Seeding is greenfield: no migration below needs a backfill and
no existing completion can be invalidated by any of the model changes sized above.

*(Incidental correction to the record: `0077`'s header predicted the `SLD` link would be
left null — "EXPECTED HERE: exactly one row, 'SLD' (Residential), which predates the
current Residential template and has no label in it." On this database it resolved, to
`template_task` 10, which is `SLD` in the Design phase of `RESIDENTIAL v1`. Harmless, and
noted only so the next reader is not confused by the mismatch.)*
