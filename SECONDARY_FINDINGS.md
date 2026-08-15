# Secondary findings

Incidental issues noticed during audits. Logged, not fixed. Each entry: file/line + one sentence.

## From the survey-link audit (2026-08-05)

- `projects/design_views.py:463-465` — the paragraph "ALLOCATION ALSO STAMPS Project.assigned_design (Part 4.5, finding F1)." is duplicated verbatim on consecutive lines in the `_allocate_one` docstring.
- `projects/models.py:24` and `projects/models.py:57` — comments still describe the OPEX project ID as `{short_tender_code}-{site_code}`, which stopped being true in commit b43e401 (site_code is now the ID verbatim).
- `projects/design_metrics.py:357` — `unassigned_sites` is computed as `total_sites - len(assignments)` but is rendered as "not yet started (no survey uploaded)" at `projects/templates/projects/design/tender_dashboard.html:82`; it is a proxy for "no DesignAssignment row exists", which is only equivalent to "no survey" because the row is created lazily by the first upload.
- `projects/tests_design_part11.py` — pre-existing: every test errors in `setUp` on an ITM-001 duplicate against migration 0047; unrelated to any current change.

## From the survey-link build (2026-08-05)

- `projects/templates/projects/design/tender_dashboard.html:82` — the `unassigned_sites` figure is still captioned "not yet started (no survey uploaded)", which now under-describes it: a site can also be started by a survey folder link, and the count itself (missing `DesignAssignment` row) stays correct either way.
- `projects/design_views.py` — `design_survey_link_set` mirrors the Arka precedent by calling bare `URLValidator()`, whose default schemes include `ftp`/`ftps`, so `ftp://drive.google.com/…` passes both the validator and the host allowlist; harmless (the host is still allowlisted) but not a folder link anyone can open.
- `projects/design_views.py` — `design_survey_upload` and `design_survey_link_set` now duplicate the replace-lock expression and the status/hold-clearing block in near-identical form; deliberate for this change (the upload path was not to be touched), but the two will need to stay in step by hand.

## From the CEO dashboard card-enrichment audit (2026-08-15)

- `projects/views.py:6444-6448` — `_MILESTONE_TO_FINANCE_TASK` maps `'M2': 'Finance Confirmation'`, but that task no longer exists in the Residential template; `build_residential_phases()` (utils.py:571) names it `'Pre Dispatch Payment Confirmation'`. The forward map at views.py:3326-3330 was updated and this reverse map was not, so a Finance edit setting M2 to Received never syncs the task to Done. Exactly the failure mode the name-matching pattern invites.
- `projects/views.py:1539` and `projects/views.py:1828` — both docstrings claim the CEO dashboard renders "in exactly 3 DB queries"; the measured figure is 8 (the `QUERY 4` finance block and `QUERY 5` assignee block were added later and the docstrings were never updated). Total page load is 12 including auth/session/notification chrome.
- `projects/models.py:1567-1606` — `DesignSubmission` is a dead model: read at views.py:8731 and views.py:8764, but no `create` path exists anywhere in the repo and production holds 0 rows. Either wire it or drop it; it currently reads as a working feature.
- Production data — 24 `PaymentMilestone` rows on active projects are marked `Received` while carrying `amount = NULL` and `amount_received = NULL`. ₹1.21 cr of active contract value has ₹10,000 of recorded receipts. Caused by the optional `amount_received` POST field at views.py:3334; the `Pending → Received` auto-transition never requires an amount.
- Production data — `HRP-CAP-2026-001` has **six** `PaymentMilestone` rows (a duplicate M1/M2/M3 set). `projects_paymentmilestone` has no uniqueness constraint on `(project, milestone_name)`, and both creation sites (views.py:2235 and views.py:5953) can run against the same project.
- Production data — `HRP-RES-2026-039` has all three milestones marked `Received` with zero money recorded on any of them.

## From the CEO card-fields build (2026-08-16)

- `projects/views.py` — `RESIDENTIAL_DESIGN_PHASE_ORDER = 3` / `RESIDENTIAL_DESIGN_TASK_ORDER = 1` is a POSITIONAL lookup for the Residential design task, and it is positional only because `Task` has no stable key (no `milestone_key`, no template FK, no slug). It survives a rename and breaks on a reorder or an insertion into phase 3 of `build_residential_phases()`; the name-match alternative breaks on the opposite. This is the primary debt introduced by the card-fields session — if a stable key is ever added to `Task`, that constant pair is the call site to retire. Both subqueries carry their own `project_type` term so the position can never be read against an OPEX/CAPEX project.
- **Django's `{# ... #}` comment cannot span multiple lines** — a multi-line one is not a comment at all, and any `{{ }}` inside it is parsed and executed. A multi-line hash comment in `dashboard/ceo.html` warning *against* per-card relation lookups contained `{{ p.design_assignment.status }}` and `{{ p.phases.all }}` as examples, which Django executed once per card: 16 extra queries on an 8-project page, 52 on production's 26. Fixed by switching to `{% comment %}`. Worth grepping the other templates for multi-line hash comments — this failure is silent, costs only queries, and nothing in the rendered page reveals it.
- Template drift across project vintages — active Residential projects carry 50, 51 or 52 tasks depending on when they were activated, and older ones still have a task literally named `Finance Confirmation` (e.g. `HRP-RES-2026-028`) where newer ones have `Pre Dispatch Payment Confirmation`. `(phase_order=3, task_order=1)` happens to be the `Design` task on all 26 today, but the drift shows positions are not guaranteed stable across vintages either. Relevant to the `_MILESTONE_TO_FINANCE_TASK` staleness noted above: on those older projects the stale name is the *correct* one, so fixing that map means handling both names, not swapping one for the other.
- Production data — `HRP-RES-2026-039` has all 51 tasks `Done` and design complete but is still `status='Active'` rather than `Commissioned`, so it occupies a CEO card as an active project with nothing left to do.
