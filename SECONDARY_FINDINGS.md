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
