# Secondary findings

Incidental issues noticed during audits. Logged, not fixed. Each entry: file/line + one sentence.

## From the survey-link audit (2026-08-05)

- `projects/design_views.py:463-465` — the paragraph "ALLOCATION ALSO STAMPS Project.assigned_design (Part 4.5, finding F1)." is duplicated verbatim on consecutive lines in the `_allocate_one` docstring.
- `projects/models.py:24` and `projects/models.py:57` — comments still describe the OPEX project ID as `{short_tender_code}-{site_code}`, which stopped being true in commit b43e401 (site_code is now the ID verbatim).
- `projects/design_metrics.py:357` — `unassigned_sites` is computed as `total_sites - len(assignments)` but is rendered as "not yet started (no survey uploaded)" at `projects/templates/projects/design/tender_dashboard.html:82`; it is a proxy for "no DesignAssignment row exists", which is only equivalent to "no survey" because the row is created lazily by the first upload.
- `projects/tests_design_part11.py` — pre-existing: every test errors in `setUp` on an ITM-001 duplicate against migration 0047; unrelated to any current change.
