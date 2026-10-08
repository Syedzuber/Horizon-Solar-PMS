# Closeout Spec v2 — COD, As-Built, HOTO, QA/QC verification

Horizon Solar PMS · OPEX/RESCO sites · 7 Oct 2026
Status: **decisions closed with the product owner, Nirankar Mishra and the PMs. Not built.**
Supersedes: §4 of `docs/OPEX_task_template_spec.md` (stale: it says no mirror derives; Design and the four deliveries do).
Evidence base: `docs/CLOSEOUT_MIRROR_AUDIT.md` (6 Oct 2026, HEAD `203ab65`). Every factual claim about the code below comes from that audit and must be re-verified by each build session before it is built on.

---

## 1. The idea

COD, As-Built Drawings and HOTO are mirror tasks (`is_mirror=True`) in OPEX template v1. A mirror is read-only to humans and written only by the record that owns the work, through `apply_mirror_status()`. Design and the four delivery mirrors work this way today.

COD, As-Built and HOTO read Not Started forever because **their owning records do not exist**. This spec defines those records, plus the QA/QC verification layer and the per-site QA/QC dossier the team asked for.

No mirror converts to an entered task. No template version change is required for anything in this spec.

---

## 2. Decisions (closed 7 Oct 2026)

| # | Decision |
|---|---|
| CL-1 | **Punch points close when their task is re-approved.** `task_approve` closes that task's open points. Waiver by PM or coordinator, with a reason, stays. |
| CL-2 | **Every open punch point on the site blocks COD and HOTO.** No severity, no "blocking" field. Scope: every task on the site. |
| CL-3 | **COD evidence: any one of** DISCOM letter · net-meter installation · HRPPL commissioning report — a PDF, plus a **mandatory PM note**. |
| CL-4 | **QMS-01 (Project-QMS-01_R00, 23 Aug 2026) is split across six homes** (§5). Agreed by Nirankar and the PMs. |
| CL-5 | **Checks are recorded twice**: (a) by the site engineer at the time of execution, and (b) independently by the site's QA/QC engineer on spot-check visits. Both feed a per-site QA/QC dossier used for COD, HOTO, the client and the O&M team. |
| CL-6 | **As-Built**: either upload drawings, or declare "same as released design" with a note. Approved by the PM or a coordinator. The declaration names the released design version automatically and warns (never blocks) when a design change request was accepted after release, or when an installation task carries a punch point or a "No" checklist answer. |
| CL-7 | **Client signature on Final Acceptance**: the client representative's name plus an uploaded signed sheet. No client login. |
| CL-8 | **HOTO override**: PM **or** coordinator may release HOTO without As-Built, with a mandatory reason. Recorded with who, role, when and why; counted on a report. The override skips As-Built only — never punch points, never the Final Acceptance verdict. As-Built stays open and owed afterwards. |
| CL-9 | **QA/QC engineer per site**: the PM assigns one from the active site engineers. Dated assignment; history kept. |
| CL-A | The QA/QC engineer **may not** be a site engineer holding a task on that same site. Refused, not warned. |
| CL-B | A **"No" on a spot check raises a punch point** on the task that checklist point belongs to. It then blocks COD and HOTO through CL-2. |
| CL-C | **At least one QA/QC visit** before HOTO. Coverage % shown; no coverage threshold. |
| CL-D | The assigned QA/QC engineer **approves tasks on their assigned sites only**. Authority comes from the assignment, not from a flag. `is_qaqc` is left unchanged until audited (Prompt Q-0). |
| CL-E | The assigned QA/QC engineer is automatically the **"QA/QC Engineer" signatory** on Final Acceptance. |

---

## 3. Build order

Order is by dependency. Every step is audit → build → commit, one concern per session.

| Step | What | Days | Needs |
|---|---|---|---|
| 1 | Safety fixes | ~2 | — |
| 2 | Punch points close on re-approval; project-level open-points query | ~1 | — |
| 3 | QA/QC engineer assignment | ~2 | Q-0 result |
| 4 | COD record | ~2 | 2 |
| 5 | T&C checklist (QMS-01 §14–16) | ~1 | — |
| 6 | As-Built record | ~1 | — |
| 7 | QA/QC verification visits | 3–4 | 2, 3, 5 |
| 8 | Final Acceptance + HOTO + override | 4–5 | 2, 3, 6, 7 |
| 9 | Per-site QA/QC dossier | 3–4 | all above |
| | **Total** | **19–22** | ±30% until each step's audit |

Steps 4, 5 and 6 are independent of each other and may be reordered.

---

## 4. Step detail

### Step 1 — Safety fixes

**1a. Checklist version pinning (must land before any checklist revision, QMS-01 R01 included).**
Today `_checklist_for_task` always serves the active version and `_checklist_context` reads only that version's completions. A task answered under R00 would show R01 blank once R01 activates, and could not be re-answered if Done.
Fix, zero schema: if a task already has completions, serve the checklist version those completions belong to; only tasks with no answers move to the new version. Test: a Done task crosses a version change and still shows its answers.

**1b. Mirror presentation.**
- Task-detail page: show the Derived chip and suppress the status select for mirrors.
- Chip tooltip names the actual source per mirror code ("Updates from the delivery challans", "Updates from the commissioning record", …).
- Assignment and due dates on mirrors stay allowed — production assigns delivery mirrors to people and the morning report relies on it.

**1c. Housekeeping.**
- `fix_fixtures.py` (repo root): an untracked, gitignored local script, never committed (1b pre-flight corrected the audit's "tracked"). Delete the local file; keep the `.gitignore` line. No third tracked caller of `sync_design_mirror` exists.
- Correct the stale comments: rung-0 comment ("five mirrors", "hooks unbuilt"), `Task.is_mirror` field comment ("refusal not built"), `_task_row.html` chip comment ("never change status"), `apply_mirror_status` docstring ("out of every overdue count").
- Correct the `is_qaqc` comments found by Q-0: `models.py` ("NOTHING READS THESE YET … no UI") and `seed_opex_test_data` ("no writer anywhere").
- Drop `derivation_source`; rows are found by `template_task__code`.

### Step 2 — Punch points

- `task_approve` sets the task's Open points to a closed state. Decide in the audit whether that is a third status (`Closed`) or a closed flag; the ledger must show who closed and when (the approver).
- One helper in a single place: `open_punch_points_for_project(project)`. Every COD/HOTO gate reads it.
- Waiver unchanged: PM/coordinator via `user_can_waive_punch_point`, with reason.

### Step 3 — QA/QC engineer assignment

- New dated assignment: site, engineer, assigned by, from, to. One active per site.
- PM-facing screen to assign and replace. Eligible: active site engineers not holding a task on the site (CL-A).
- Access: the assignment grants view of that site through a `permissions.py` helper. Update `tests_access_isolation` and the access matrix.
- Approval: `user_can_approve_task` admits the site's assigned QA/QC engineer (CL-D). Precedence written in one helper.
- **`task_has_independent_approver` must count the site's assigned QA/QC engineer** in its candidate pool (today it reads only `is_qaqc=True` holders). Without this, self-certification (`user_may_self_certify`, migration 0088) fires on sites that do have an independent approver.
- Retire the workaround of giving the QA/QC person one task per pilot site.
- Full audit first: this widens site visibility.

**Q-0 findings (7 Oct 2026) that shape this step:**
- `is_qaqc` holders: 0 locally; the code's own docstring records 0 in production on 20 Sep. No one relies on the flag today, so moving authority to the assignment breaks nobody.
- `is_qaqc` is always ANDed with `user_can_view_project`, which for a site engineer means holding a task on the site. Under CL-A the QA/QC engineer holds no task there, so **the flag cannot work for this design**; the assignment must grant view. This confirms CL-D.
- `is_qaqc` stays as a column, unchanged. No new writer (System Admin's edit path does not carry it; leave it so). Retire or repurpose after step 3 ships.
- Stale comments about `is_qaqc` ("NOTHING READS THESE YET" in `models.py`; "no writer anywhere" in `seed_opex_test_data`) are corrected in step 1c.

### Step 4 — COD record

- Fields: COD date · evidence type (DISCOM letter / net-meter installation / commissioning report) · PDF · note (NOT NULL) · recorded by · recorded at.
- Refused while `open_punch_points_for_project` is non-empty.
- Writer: `apply_mirror_status` on the `COD` mirror. Withdrawing the record moves COD back out of Done.
- PDF storage: private bucket with signed links, following the bill-PDF precedent (D-A40), not the public bucket.

### Step 5 — T&C checklist

- Seed QMS-01 §14 (DC, AC, earthing tests), §15 (inverter commissioning), §16 (performance & functional) as a checklist linked to `TESTING_COMMISSIONING`.
- Follows `seed_opex_installation_checklists` conventions: source citation, mechanical diff against the PDF.

### Step 6 — As-Built record

- Two modes: **upload** (PDF/DWG files) or **same as released** (references the released design version automatically; note required).
- Approved by PM or coordinator. Writer: `apply_mirror_status` on `AS_BUILT_DRAWINGS`.
- Warnings before a "same as released" declaration (CL-6): accepted design change request after release; punch point or "No" answer on any installation task.
- The dossier states the mode in words: "Declared same as released design v3 by <name>" vs "Uploaded by <name>".
- Opens once installation-phase tasks are Done.

### Step 7 — QA/QC verification visits

- A visit: site, engineer, date. Per point checked: Yes/No/NA, remark, photo, text snapshot.
- **Own table.** `ChecklistItemCompletion` allows one answer per (item, task), so an independent second answer cannot live there.
- Points come from the site's checklists (installation I&C, T&C). The engineer chooses; the system shows coverage.
- A "No" raises a punch point on that point's task (CL-B).
- PM dashboard: "Last QA/QC visit: N days ago" per site.

### Step 8 — Final Acceptance + HOTO

- One Final Acceptance record per site, covering QMS-01 §18–20:
  - §18 housekeeping and handover checks;
  - §19 records register: 22 rows, each with status and PDF upload;
  - §20 verdict: Accepted · Accepted with Punch Points · Re-inspection Required · Rejected;
  - signatories: QA/QC Engineer (the assigned engineer, CL-E), Construction Engineer (the site engineer), Client Representative (name + signed sheet, CL-7).
- "Re-inspection Required" reopens the record for a further inspection; history kept.
- HOTO turns Done when: verdict is Accepted, or Accepted with Punch Points with every point closed or waived; **and** As-Built is Done or overridden; **and** at least one QA/QC visit exists (CL-C).
- Override (CL-8): flag + reason + DB CHECK, modelled on `grn_on_behalf` / `grn_on_behalf_requires_reason`. First use of an override in the codebase.

### Step 9 — Per-site QA/QC dossier

- Screen plus PDF export, one HRPPL format.
- Per point: execution answer (site engineer) beside verification answer (QA/QC engineer); mismatches highlighted.
- Includes punch points and how each closed, COD record, As-Built record (mode in words), Final Acceptance verdict and signatories.
- Sections not yet in the system (QMS-01 §1–3, §2, §17) are shown as **"Not recorded in the system"**, never left blank.
- Readers: PM, coordinators, CEO; handed to the client and O&M as a PDF. No O&M login in this phase.

---

## 5. QMS-01 — where each section lives

| Sections | Content | Home | When |
|---|---|---|---|
| 1, 3 | Documentation, roof & structural readiness | Mobilisation task with HSE | Later (needs a template version and B-11) |
| 2 | Material receiving inspection | GRN | Later (inventory phase 1) |
| 4–13 | Installation checks | The 7 seeded installation checklists | Exists |
| 14–16 | Testing, inverter commissioning, performance | `TESTING_COMMISSIONING` checklist | Step 5 |
| 17 | Safety | HSE checklist | Later (B-11) |
| 18–20 | Handover, records register, final acceptance | Final Acceptance record | Step 8 |

---

## 6. Open, not blocking

- **Prompt Q-0**: done 7 Oct 2026; findings folded into step 3.
- **`is_design_qc` reads Residential BOQs** (Q-0): `boq_detail` and `boq_history` call `user_can_view_project_boq` with no OPEX check, so a design-QC holder can read any Residential BOQ. The branch is justified only by OPEX QC review. Separate single-purpose fix (R-12): add the OPEX check. Not closeout scope.
- **B-11**: HSE checklist content. Blocks the mobilisation task, §1–3 and §17.
- **O&M access** to the dossier beyond PDF: future role decision.
- **`is_qaqc` future**: retire, or keep for a company-wide QA/QC lead with cross-site approval. Decide after Q-0 and step 3.
- **Delivery mirrors reconcile at activation** (audit Q26): half-day fix, when real tender sites activate.

---

## 7. Premortem

- **~55% — QA/QC visits don't happen.** Site engineers have their own sites. Counters: one-visit minimum before HOTO (CL-C), visit age on the PM dashboard.
- **~55% — "same as released" becomes the default As-Built answer.** CL-6 warnings slow it; watch the declared-vs-uploaded ratio after ten sites.
- **~40% — not enough site engineers for CL-A** in a region. If so, relax to a warning with a reason — deliberately, by decision.
- **~40% — the dossier is handed to a client before §1–3 and §17 exist.** The "Not recorded" marker is the counter; PMs must say it out loud.
- **~30% — spot-check punch points swamp PMs early.** Useful signal; warn PMs before step 7 ships.
- **~30% — the PDF dossier grows** into client-specific formats. Hold to one HRPPL format for v1.
- **Access risk — step 3** widens site visibility. Full audit-then-build, access tests updated in the same session.
- **Odds:** steps 1–3 within two weeks of starting ~80%; full chain before the first tender site needs HOTO ~55%.
