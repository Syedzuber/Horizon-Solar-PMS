# Checklist PDF Spec v1.3 — task checklist and QA/QC visit downloads

Horizon Solar PMS · 15 Oct 2026
Status: **rulings closed by Zuber** on the questions raised by the checklist-PDF data audit (15 Oct 2026). v1.1, 16 Oct 2026: rulings on part 1's questions — F-1 and F-6 amended, F-7 to F-9 added (part 1b). v1.2, 16 Oct 2026: F-8 amended with the exact filename format (part 1c). v1.3, 16 Oct 2026: §3 added, rulings V-PDF-1 to V-PDF-8 for the QA/QC visit PDF (part 2).
Build: part 1 — the task checklist PDF and the shared renderer; part 2 — the QA/QC visit PDF, a separate session that reuses the renderer.

---

## 1. Rulings on the audit's questions

| # | Question | Ruling |
|---|---|---|
| 1 | Sign-off block | Both, in one grid. Four columns: Checked/Tested By · Witnessed By · Witnessed By · Approved By (PM/Coordinator). Rows: Signature, Name, Date, Company. Names are pre-filled only where the system recorded them (submitter; approver). Signature, Date and Company are left blank for wet signing. Witness names stay in each item’s row, not in the grid. |
| 2 | Witness caveat | Yes. One footnote: “Witness names are as entered by the person who recorded the check; they are not signatures.” Not repeated on every line. |
| 3 | Section headings | Print as they are now; no caveat in the PDF. Active versions can’t be edited, so renames are rare. Record “section not snapshotted” in DEFERRED. |
| 4 | Approval capacity | Print “Approved by <name> on <date>“, plus “self-certified” when that flag is set. Do not infer QA/QC capacity from assignment time spans. A guessed capacity on a client document is worse than no capacity. |
| 5 | Submission history | Current round only. Rejected rounds belong to the dossier (step 9), where punch points show them. |
| 6 | Access | Follows page access exactly: user_can_view_project for task checklists, user_can_read_visit for visits. The QA/QC engineer downloading any checklist on their site is correct; it’s a view, not a work gate. |
| 7 | QA/QC answerer / verdict | The answerer is visit.engineer; print that. The visit PDF has no verdict, only counts (points checked, No count, punch points raised). Final Acceptance (QMS-01 §20) is a separate document, step 8. |
| 8 | Library | xhtml2pdf, pinned. No WeasyPrint (it needs system libraries on nixpacks). The session must check the full dependency tree installs with no system packages; see the premortem. |
| 9 | Photos | Not embedded. A Yes/No column only. No server-side photo fetching in v1. |

## 2. Rules not asked about but needed

| # | Rule |
|---|---|
| F-1 | Final vs draft (task). The PDF is “final” only when the task is Done, approved_at is set, and no answer was recorded after approved_at. Anything else prints “NOT FINAL” in the header with the generation time. The third condition covers the stale-approval defect (G63). **Amended v1.1:** FINAL/NOT FINAL rule unchanged. The header ALSO shows "N of M points checked" beside the marker, same size and weight. |
| F-2 | Draft rule (visit). Only submitted visits can be downloaded. An open visit has no download button. |
| F-3 | Rows. The task PDF lists every item of the pinned version, with unanswered items marked “Not checked”. A checklist PDF that silently omits items reads as complete. The visit PDF lists only the points checked, plus a coverage line. |
| F-4 | Columns. Checking point · Status · Witness · Photo proof (Yes/No) · Date of checking · Remark. Remark is added because a No without its reason is useless to a client. |
| F-5 | Item wording comes from item_text_snapshot when answered and the pinned version’s label when not. |
| F-6 | Header. Site code, client, tender, checklist name and version, and the source form reference already stored on the checklist (e.g. HRPPL/ELE/I&C/DCDB R00), plus the generation time in IST. Paper-form fields the system doesn’t hold (Job No, Drawing, Spec No) are left out rather than printed blank. **Amended v1.1:** source form reference dropped until stored (G73; revisit at step 5). |
| F-7 | (v1.1) Unsupported characters print as a visible "□", never blank. |
| F-8 | (v1.1) Task name appears in the header and the filename. **Amended v1.2:** the filename is `<site code>_<task name slug>_t<task id>_<checklist code>_v<version>_<YYYYMMDD>.pdf`, dated by the IST generation day. The task name slug is lowercase ASCII letters, digits and hyphens (Django `slugify`), capped at 60 characters, and reads `task` when nothing of the name survives slugifying. `t<task id>` always follows the slug, so two tasks whose names slugify alike never share a filename. |
| F-9 | (v1.1) A render failure redirects to the task page with an error message; never a 500, never a partial file. |

## 3. The QA/QC visit PDF (v1.3, part 2)

| # | Rule |
|---|---|
| V-PDF-1 | Only SUBMITTED visits can be downloaded (F-2). Open and lapsed visits have no button, and the view refuses them (404). |
| V-PDF-2 | Access is exactly `qaqc_visit_detail`'s: `_visit_site` + `user_can_read_visit`. No new permission logic. |
| V-PDF-3 | Rows: only the points answered on this visit (F-3, visit variant), grouped by task, then checklist section, in checklist order. |
| V-PDF-4 | Columns as part 1: Checking point · Status (Yes/No/Not Applicable) · Witness · Photo proof · Date of checking · Remark. Witness: visit answers record none; print "—" and add the footnote "QA/QC visit answers do not record witnesses." Photo proof: Yes, always (a photo is required on every answer). Date of checking: `answered_at`, IST date. Remark: the remark; for a No, append "Punch point raised" and its current status (Open / Rectified / Closed / Waived). Wording: `item_text_snapshot` (always present on visit answers). |
| V-PDF-5 | Header: site code, client, tender, "QA/QC verification visit", visit date, engineer, submitted at (IST), counts ("N points checked, M No, M punch points raised"), the site coverage line as on the visit page, and the generation time. Marker: "SUBMITTED <date>" in the place part 1 prints FINAL. |
| V-PDF-6 | Sign-off grid, the same partial: QA/QC Engineer (Name pre-filled with `visit.engineer`) · Witnessed By · Witnessed By · Reviewed By (PM/Coordinator, blank). Signature, Date and Company blank. |
| V-PDF-7 | Filename: `<site code>_QAQC-visit_<YYYYMMDD>_<visit id>.pdf`. |
| V-PDF-8 | The same "□" replacement for undrawable characters (F-7) and the same render-failure behaviour (F-9): a redirect to the visit page with the error message, never a 500. |
