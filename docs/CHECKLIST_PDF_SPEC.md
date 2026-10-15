# Checklist PDF Spec v1.0 — task checklist and QA/QC visit downloads

Horizon Solar PMS · 15 Oct 2026
Status: **rulings closed by Zuber** on the questions raised by the checklist-PDF data audit (15 Oct 2026).
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
| F-1 | Final vs draft (task). The PDF is “final” only when the task is Done, approved_at is set, and no answer was recorded after approved_at. Anything else prints “NOT FINAL” in the header with the generation time. The third condition covers the stale-approval defect (G63). |
| F-2 | Draft rule (visit). Only submitted visits can be downloaded. An open visit has no download button. |
| F-3 | Rows. The task PDF lists every item of the pinned version, with unanswered items marked “Not checked”. A checklist PDF that silently omits items reads as complete. The visit PDF lists only the points checked, plus a coverage line. |
| F-4 | Columns. Checking point · Status · Witness · Photo proof (Yes/No) · Date of checking · Remark. Remark is added because a No without its reason is useless to a client. |
| F-5 | Item wording comes from item_text_snapshot when answered and the pinned version’s label when not. |
| F-6 | Header. Site code, client, tender, checklist name and version, and the source form reference already stored on the checklist (e.g. HRPPL/ELE/I&C/DCDB R00), plus the generation time in IST. Paper-form fields the system doesn’t hold (Job No, Drawing, Spec No) are left out rather than printed blank. |
