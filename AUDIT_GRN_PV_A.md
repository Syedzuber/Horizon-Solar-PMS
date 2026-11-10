# GRN-PV-A — Read-only audit: mandatory photo at SE receipt + optional unboxing video

Horizon Solar PMS · 10 Nov 2026 · at `dbbd31e` (main = origin/main). Read-only: no repo
file changed except this one; no migration, no install, no push.

Evidence marking used below:
- **[code]** quoted from the repo at `dbbd31e`.
- **[run]** observed in this session (local DB, test run, or a read-only call to the DEV
  Supabase project `tvsbedlxapovitpaekyx`).
- **[knowledge]** from general platform knowledge, not verified here. Each one needs a
  check on a real device or in the vendor docs before a build relies on it.

---

## 0. Safety

| Check | Value |
|---|---|
| Local DB | `localhost` / `solarpms_local` (the 9 Nov production restore) |
| `SystemSettings.email_enabled` | **False** |
| `SystemSettings.whatsapp_enabled` | **False** |
| `INTERAKT_API_KEY` | empty |
| `ZEPTOMAIL_API_KEY` | empty |
| `SUPABASE_URL` project | `tvsbedlxapovitpaekyx` (DEV, not production `wqpxtjelsfgwhhyyafus`) |

Nothing is on, so the audit went ahead. The only network calls were read-only and went to the
DEV project: `get_bucket`, `list`, one 60-second signed link with two `Range` GETs, and one CORS
`OPTIONS` preflight. Nothing was uploaded. The 60-second link was printed by httpx's INFO logger
and has since expired.

Railway variables were **not** read (they hold secrets). The brief's
`GUNICORN_CMD_ARGS` = 2 workers, `--timeout 60` is taken as given. Code alone would give
gunicorn's defaults: `Procfile` runs `gunicorn solarpms.wsgi --log-file -` with no flags, i.e. one
sync worker and a 30 s timeout.

---

## PART P — mandatory photo

### P1. confirm_grn's order today, and where the rule sits

`projects/views.py:14784` `confirm_grn` (Site Engineer only, POST only) runs in this order
**[code]**:

| # | Step | Line | On failure |
|---|---|---|---|
| 1 | `user_can_view_project` → 404; `user_works_on_site` → 403 | 14832–14835 | bare 404/403 |
| 2 | challan in project, else 404; challan already Received → 403 | 14838–14844 | bare 403 |
| 3 | `_read_grn_lines` — quantity parse errors | 14852 | `_refuse_grn`, draft kept |
| 4 | `changed_grn_lines` — keep changed lines only (WA) | 14860 | — |
| 5 | `plan_damage` — photo count per damaged line, description, **then each damage photo through `validate_damage_photo`** | 14864 | `_refuse_grn` |
| 6 | **G4** `if not grn_lines:` → `DOCUMENTS_NEED_A_QUANTITY` or `NOTHING_TO_RECORD` | 14874–14878 | `_refuse_grn` |
| 7 | `plan_receipt_documents` — count ≤ 5, type per file, no-prices tick, storage on, **then each file through `validate_dc_document`** | 14881 | `_refuse_grn` |
| 8 | `upload_planned_photos` (damage photos → Supabase) | 14887 | `_refuse_grn` |
| 9 | `upload_receipt_documents` (documents → Supabase) | 14895 | discard photos, `_refuse_grn` |
| 10 | `transaction.atomic()`: `lock_challan` → Received re-check → `reload_grn_lines` → `recheck_damage_plan` → `changed_grn_lines` again → line saves → `write_goods_receipt` → `record_damage` → `link_receipt_documents` → `recalculate_dc_status` → `log_activity` | 14905–14962 | discard both, `_refuse_grn` (refusal) or re-raise (failure) |

Quoted, steps 6–9:

```python
    if not grn_lines:
        empty = (DOCUMENTS_NEED_A_QUANTITY if receipt_documents_chosen(request)
                 else NOTHING_TO_RECORD)
        _refuse_grn(request, challan, GRN_DRAFT_CONFIRM, line_items, [empty], '')
        return back
    # NTF-DCG 4b (A2–A5): every document refusal — count, type, tick, storage, the files
    # themselves — before any upload, so a refusal stores nothing.
    receipt_documents, document_errors = plan_receipt_documents(request)
    if document_errors:
        _refuse_grn(request, challan, GRN_DRAFT_CONFIRM, line_items, document_errors,
                    'The GRN was not recorded.')
        return back
    try:
        stored_photos = upload_planned_photos(damage_plan, project)
```

**Where the new rule sits.** Put it between step 6 (G4) and step 8 (the first upload). Join it to
step 7's errors so they come back as **one** refusal. If it sat on its own before step 7, an SE
missing both the photo and the no-prices tick would be refused twice. Each refusal makes them
pick every file again, because a browser cannot re-fill a file input. The proposed shape, for the
build:

```python
receipt_documents, document_errors = plan_receipt_documents(request)
document_errors += receipt_photo_errors(request, grn_lines, damage_plan)   # GRN-P
if document_errors: _refuse_grn(...); return back
```

`receipt_photo_errors` needs only what the request already holds: `damage_plan.files`, which
`plan_damage` has already validated as images, and `_chosen_documents(request)` with the type
and extension of each file. It must not depend on whether `plan_receipt_documents` passed, or it
would hide behind the tick error. It writes nothing and touches no storage, so a refusal stores
nothing. That is the same guarantee every refusal above step 8 gives.

It does **not** need re-checking under the lock: the files cannot change between step 7 and
step 10. If the plan becomes "required only when new material arrived" (ruling P-c below), it
reads `grn_lines` before the lock. A concurrent GRN could then turn an increase into a no-change
under the lock. That makes the photo unnecessary, never missing, so no re-check is needed.

### P2. Which files can satisfy it, and what the code can tell apart

**What the server can tell apart [code]:**

- **Damage photos** (`damage_photos_<line pk>`): `validate_damage_photo` allows only
  `jpg/jpeg/png` by extension, then `validate_dc_document` checks the MIME claim and the leading
  bytes (`\xff\xd8\xff` / `\x89PNG…`). They are images by construction, and
  `DCLineDamagePhoto`'s CHECK `dc_damage_photo_mime_image` backs that up.
- **Receipt documents** (`receipt_doc_1..5`): `validate_dc_document` allows `pdf/jpg/jpeg/png`.
  It returns the canonical MIME (`application/pdf` / `image/jpeg` / `image/png`) from the
  extension, after checking that the leading bytes match it. **An image is any receipt document
  whose canonical MIME starts with `image/`.** That is reliable: a renamed PDF is refused because
  its leading bytes do not match. Each document also has a `document_type` chosen per slot
  (`signed_dc`, `lr`, `delivery_photo`, `other`).
- The code **cannot** tell what a photo shows. A phone photo of the signed DC and a photo of the
  material are both `delivery_photo` or `signed_dc` JPEGs, depending on what the SE picks in the
  select.

**Options for "at least one image" (ruling P-a):**

| Option | Satisfied by | Weakness |
|---|---|---|
| (i) any image | any image receipt document of any type, or a damage photo | a photo of the paper DC (`signed_dc`) passes, so it proves nothing about the material |
| **(ii) material photo** | an image receipt document typed `delivery_photo`, **or** a damage photo in the same submission | relies on the SE choosing the type honestly, as it already does |
| (iii) dedicated input | a new required "Photo of the material" input, separate from the 5 document slots | more UI, and a new count rule against the 5-file cap |

**Recommended: (ii).** It is the smallest change that still asks for a picture of the goods. A
damage photo counts because it shows the material.

**How an iPhone photo arrives:**

- Both GRN inputs name MIME types as well as extensions **[code]**:
  - damage: `accept="image/jpeg,image/png,.jpg,.jpeg,.png"` (`delivery_challan_detail.html:535`)
  - documents: `accept=".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png"` (`:568`)
- **[knowledge]** iOS Safari converts a HEIC photo from the photo library or camera to JPEG
  when the input's `accept` does not name `image/heic`. A `.heic` file picked from the **Files**
  app is not converted. It is either greyed out or uploaded as `.heic`.
- The server refuses `.heic` by extension in both validators **[code]**:
  `'{name}: must be a PDF, JPG or PNG.'` or `'a damage photo must be a JPG or PNG.'`.
- No test posts a HEIC file to a GRN. The only HEIC test is
  `tests_approval_views.py:1047` (proxy evidence, refused `photo.heic: unsupported type (.heic).`).
- `approval_forms.file_accept`'s docstring and SECONDARY_FINDINGS:283 both record that **the
  HEIC→JPEG conversion has never been walked on a real iPhone**.
- **[knowledge]** Android Chrome's camera writes JPEG. Samsung's optional "HEIF pictures"
  setting (off by default) can put `.heic` files in the gallery. Chrome does not convert them,
  so they would be refused by extension.

**Size matters more than format here [code]:**

- Damage photo inputs carry `data-qaqc-photo`, so `_qaqc_photo_script.html` downscales them in
  the browser to 1600 px long edge, JPEG q0.8, typically a few hundred KB.
- The 5 document slots deliberately do **not** carry it (4b ruling Q5, "documents upload at full
  size"). A full-size phone photo is 3–8 MB. See V1: at 1 Mbps, 5 MB takes about 40 s **inside a
  gunicorn worker** before Supabase is even called. Two full-size photos in one submission pass
  the 60 s timeout.
- Making a photo mandatory makes this the normal path, not the occasional one. Ruling P-d below.

### P3. Existing tests that post an SE GRN with no image

**Method [run].** A static grep would miss tests that post through shared helpers, or from a
class-level seed. So the suite was run under `solarpms.test_settings` with two wrappers patched
into `projects.views`:

- `_read_grn_lines`: every SE GRN POST.
- `plan_receipt_documents`: an SE POST that got past every refusal up to and including G4, i.e.
  one that a new rule at P1's position would see.

Each record held the test id, the posted files and the field types. The scripts are in the
scratchpad (`grn_probe.py`, `grn_probe2.py`); no repo file was touched.

Results:

- Full `projects` suite: **1 failure**, the known SQLite-only `tests_design_part46`. Nothing new.
- **155 tests** post an SE GRN. 140 reach the rule's position. **15** are refused earlier (403,
  404, quantity errors, G4) and are unaffected.

| Rule | Tests that would need an image added |
|---|---|
| (i) any image, every SE submission | **107** |
| (ii) "Delivery photo" image or damage photo, every SE submission | **107** |
| **(ii) + P-c: only when a received quantity goes up** (blank counts as 0) | **105** |

The two dropped by P-c are `tests_grn_damage.ChangedMeanwhileTests.test_a_photo_withdrawn_in_the_window_refuses_a_grn_relying_on_it`
and `tests_grn_notify.ContentTests.test_a_line_with_damage_before_or_after_shows_the_damaged_change`.
Their SE posts change damage only.

The 105 by module: access_isolation 1, dc_edit 1, dc_messages 1, dc_status_rule 8,
finance_dc_access 22, goods_receipts 16, grn_damage 8, grn_notify 12, grn_on_behalf 5,
number_input 1, receipt_attachments 23, residential_baseline 4, soft_delete 1,
status_transition 1, walkthrough_seed 1.

The walkthrough seed's class setup counts as 1. It runs `seed_walkthrough`, whose `grn()` posts
three SE GRNs: WALK-DC-0002 partial, 0003 full, and 0004 with 0 received. Under P-c the first two
need an image. So **the seed command itself changes**, not only its test.

Per class (38 classes; under (ii) + P-c):

| Module | Class | # | Tests |
|---|---|---|---|
| `tests_access_isolation` | `RoleGateScopingTests` | 1 | `test_the_projects_own_site_engineer_can_still_confirm_a_grn` |
| `tests_dc_edit` | `ConcurrencyTests` | 1 | `test_se_confirms_between_open_and_save` |
| `tests_dc_messages` | `DcDetailMessageTests` | 1 | `test_grn_confirmed_is_shown_once` |
| `tests_dc_status_rule` | `DcEditTests` | 1 | `test_removing_the_unreceived_line_closes_a_partially_received_challan` |
| `tests_dc_status_rule` | `SiteEngineerPathTests` | 2 | `test_a_blank_unarrived_line_keeps_the_form_open_until_its_total_is_entered`, `test_typing_zero_saves_zero` |
| `tests_dc_status_rule` | `ZeroDisplayTests` | 5 | `test_a_recorded_zero_renders_as_zero_on_the_scm_form`, `test_a_recorded_zero_renders_as_zero_on_the_se_form`, `test_an_unrecorded_line_still_renders_blank`, `test_the_scm_form_reposting_a_zero_is_not_a_change`, `test_the_se_form_reposting_a_zero_is_not_a_change` |
| `tests_finance_dc_access` | `FinanceOpensFilesTests` | 4 | `test_finance_opens_a_challan_document_and_the_open_is_logged`, `test_finance_opens_a_live_damage_photo_and_the_open_is_logged`, `test_finance_opens_a_receipt_document_and_the_open_is_logged`, `test_withdrawn_files_are_a_404_for_finance_and_log_nothing` |
| `tests_finance_dc_access` | `FinancePageTests` | 7 | `test_a_deactivated_finance_login_is_sent_to_sign_in`, `test_a_deactivated_finance_profile_gets_403`, `test_ceo_and_design_are_still_refused_the_page`, `test_finance_is_shown_no_write_control`, `test_finance_opens_the_page_and_sees_receipts_and_documents`, `test_the_raise_issue_modal_is_still_there_for_its_own_roles`, `test_withdrawn_files_are_not_listed_for_finance` |
| `tests_finance_dc_access` | `FinanceWritesRefusedTests` | 3 | `test_every_write_view_refuses_finance_and_writes_nothing`, `test_finance_cannot_open_the_dc_edit_form`, `test_no_write_gate_reads_the_read_audience` |
| `tests_finance_dc_access` | `HelpTextTests` | 2 | `test_both_grn_forms_name_finance`, `test_dc_create_and_edit_pages_name_finance` |
| `tests_finance_dc_access` | `ReadAudienceTests` | 6 | `test_a_deactivated_finance_profile_or_login_reads_nothing`, `test_finance_is_a_read_only_dc_role`, `test_finance_reads_every_project`, `test_open_document_is_exactly_the_read_audience_for_every_role`, `test_the_active_check_is_finance_only`, `test_who_reads_the_challan` |
| `tests_goods_receipts` | `BackfillTests` | 2 | `test_a_challan_fully_covered_by_live_receipts_is_skipped_and_listed`, `test_a_gap_challan_imports_the_before_values_of_its_first_live_receipt` |
| `tests_goods_receipts` | `ConfirmReceiptTests` | 4 | `test_a_notes_only_change_is_recorded`, `test_first_confirm_writes_one_receipt_with_lines_only_for_recorded_lines`, `test_identical_resubmit_is_refused_as_empty_and_keeps_the_draft`, `test_second_partial_confirm_records_before_and_after` |
| `tests_goods_receipts` | `ModelTests` | 1 | `test_the_challan_and_line_are_protected` |
| `tests_goods_receipts` | `NoReceiptOnFailureTests` | 3 | `test_a_grn_refused_as_changed_meanwhile_writes_no_receipt`, `test_a_rolled_back_grn_writes_no_receipt`, `test_the_same_values_committed_in_the_window_leave_nothing_and_refuse` |
| `tests_goods_receipts` | `OverrideReceiptTests` | 2 | `test_a_correction_is_a_correction_and_carries_no_unused_reason`, `test_an_empty_override_is_refused_and_keeps_the_reason` |
| `tests_goods_receipts` | `PageTests` | 2 | `test_more_receipts_cost_nothing_more`, `test_se_scm_and_pm_see_the_receipts` |
| `tests_goods_receipts` | `UnchangedLineNotSavedTests` | 2 | `test_an_unchanged_line_keeps_its_confirmer_and_date_when_another_se_confirms`, `test_an_unchanged_unconfirmed_line_asks_scm_for_no_reason` |
| `tests_grn_damage` | `AllOrNothingTests` | 1 | `test_a_grn_with_no_damage_is_unchanged` |
| `tests_grn_damage` | `ChangedMeanwhileTests` | 7 | `test_a_challan_received_in_the_window_refuses_the_se`, `test_a_header_edit_in_the_window_is_kept_and_the_grn_goes_through`, `test_a_line_added_in_the_window_is_left_alone`, `test_a_line_edit_in_the_window_is_not_reverted_and_refuses_the_grn`, `test_a_second_grn_in_the_window_the_later_one_writes_on_fresh_rows`, `test_damage_recorded_by_a_second_grn_in_the_window_refuses_the_later_one`, `test_no_race_confirm_then_override_write_as_before` |
| `tests_grn_notify` | `ContentTests` | 6 | `test_a_named_finance_recipient_can_open_the_link`, `test_no_money_is_declared_or_rendered`, `test_receipt_subject_header_and_table`, `test_table_cells_are_escaped`, `test_the_link_anchors_to_the_receipt`, `test_vendor_and_po_when_filled` |
| `tests_grn_notify` | `RecipientTests` | 6 | `test_a_receipt_sends_one_email_per_recipient_scm_all_and_the_pm`, `test_a_rolled_back_grn_sends_nothing`, `test_an_inactive_pm_is_not_sent_it`, `test_each_receipt_sends_once`, `test_named_recipients_are_added`, `test_paused_email_writes_skipped_rows_and_in_app_still_goes` |
| `tests_grn_on_behalf` | `BlankReasonRefusalTests` | 2 | `test_a_pure_correction_needs_no_reason`, `test_a_refused_mixed_submission_does_not_apply_the_correction_either` |
| `tests_grn_on_behalf` | `ChallanDetailDisplayTests` | 1 | `test_an_engineer_confirmed_line_names_the_engineer_and_nothing_more` |
| `tests_grn_on_behalf` | `CorrectionTests` | 1 | `test_the_confirmer_is_unchanged_and_the_flag_stays_false` |
| `tests_grn_on_behalf` | `MixedSubmissionTests` | 1 | `test_the_flag_lands_on_the_unconfirmed_line_only` |
| `tests_number_input` | `DeliveryQuantityTests` | 1 | `test_confirm_grn_saves_a_two_decimal_quantity_exactly` |
| `tests_receipt_attachments` | `ChallanDocumentsUnaffectedTests` | 3 | `test_only_receipt_files_still_reads_none_attached`, `test_receipt_files_never_appear_on_the_challan_documents_card`, `test_receipt_files_never_count_toward_the_cap_of_ten` |
| `tests_receipt_attachments` | `DiscardTests` | 1 | `test_a_rollback_discards_the_uploads` |
| `tests_receipt_attachments` | `DisplayTests` | 5 | `test_a_withdrawn_file_is_hidden_from_se_and_shown_to_scm`, `test_more_documents_cost_nothing_more`, `test_scm_withdraws_a_receipt_document_through_the_dc_b2_view`, `test_the_open_view_follows_the_dc_b2_rule`, `test_the_receipt_lists_its_documents_with_type_name_and_uploader` |
| `tests_receipt_attachments` | `FormTests` | 1 | `test_the_refusal_alert_names_photos_and_documents` |
| `tests_receipt_attachments` | `LinkTests` | 3 | `test_a_grn_without_documents_is_unchanged`, `test_a_type_chosen_with_no_file_records_nothing`, `test_override_with_a_file_links_it_to_the_correction` |
| `tests_receipt_attachments` | `RefusalTests` | 10 | `test_a_file_without_a_type_is_refused`, `test_a_wrong_file_type_is_refused_before_upload`, `test_an_oversize_file_is_refused_before_upload`, `test_an_unknown_type_is_refused`, `test_documents_alone_are_refused_in_their_own_words`, `test_documents_alone_win_over_their_own_refusals`, `test_no_prices_tick_missing_is_refused_with_the_draft_kept`, `test_six_files_are_refused`, `test_storage_off_is_refused_before_upload`, `test_two_files_in_one_slot_count_towards_the_five` |
| `tests_residential_baseline` | `DeliveryGRNWorkflowTests` | 3 | `test_a_shortfall_alone_rolls_up_to_partially_received`, `test_scm_overrides_a_grn_without_overwriting_the_original_engineer`, `test_the_site_engineer_on_this_project_confirms_a_full_grn` |
| `tests_residential_baseline` | `NotificationTests` | 1 | `test_grn_confirmation_notifies_nobody` |
| `tests_soft_delete` | `LiveProjectStillWorksTests` | 1 | `test_a_live_challan_still_takes_a_grn` |
| `tests_status_transition` | `InstrumentedSubjectTypeTests` | 1 | `test_delivery_challan_creation_and_grn` |
| `tests_walkthrough_seed` | `SeededWalkthroughTests` | 1 | class setup (via `seed_walkthrough.grn()`) |

**Edit count, not test count.** Most of the 105 post through a few shared helpers, so the edit
is far smaller than 105:

- `GrnDamageBase.confirm` (`tests_grn_damage`, also imported by `tests_goods_receipts`,
  `tests_finance_dc_access` and `tests_grn_notify`).
- `ReceiptDocumentBase.confirm_docs` (`tests_receipt_attachments`).
- `_confirm` (`tests_number_input`), `_confirm_as_se` (`tests_grn_on_behalf`), and the
  class-level poster in `tests_dc_status_rule:126`.
- `seed_walkthrough.grn()`.
- About ten direct posts.

Two caveats:

- Some "affected" tests assert a refusal, for example `RefusalTests`. Their **setup** GRN
  succeeds without an image, or their refused post reaches the document plan, so the joined
  photo error would show up in their messages. Each needs a look, not just an image added.
- Many 4b tests attach only a PDF (39 tests' image-less posts carry a PDF document). They need an
  image *beside* the PDF, not instead of it.

### P4. Receipts with and without an image

**Local [run]** (`solarpms_local` = 9 Nov production restore):

```
first receipt document: None
SE receipts (kind='receipt'): total 0
receipts by kind: {'imported': 15}
latest DCLineItem.grn_date: 2026-11-03; lines with grn_date >= 2026-11-04: 0
```

As of the 9 Nov restore, production had **no SE receipt at all since 4a went live**. The 15
receipts are `backfill_goods_receipts` imports, so there is no "with / without image" split to
report yet.

For scale, distinct (DC, date, confirmer) GRN submissions by month in the restore were: Jun 6,
Oct 10, Nov (to the 3rd) 3. That is about **10 a month**.

**Production command (read-only).** Run in the Railway Console of service Horizon-Solar-PMS,
after `source /app/.venv/bin/activate`, from `/app`. It sets `SET TRANSACTION READ ONLY` and
rolls back, and prints counts only, with no names or paths. It was tested locally against the
restore and printed the figures above.

```bash
python - <<'EOF'
import os, sys; sys.path.insert(0, os.getcwd()); os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'solarpms.settings')
import django; django.setup()
from collections import Counter
from django.db import connection, transaction
from django.db.models import Exists, OuterRef, Min, Max
from projects.models import GoodsReceipt as G, DeliveryChallanAttachment as A, DCLineDamagePhoto as P
with transaction.atomic():
    connection.cursor().execute('SET TRANSACTION READ ONLY')
    print('DB', connection.settings_dict['HOST'][:12], connection.settings_dict['NAME'])
    first = A.objects.filter(receipt__isnull=False).aggregate(t=Min('uploaded_at'))['t']
    print('first receipt document:', first)
    qs = G.objects.filter(kind='receipt').annotate(
        img=Exists(A.objects.filter(receipt=OuterRef('pk'), mime__startswith='image/')),
        doc=Exists(A.objects.filter(receipt=OuterRef('pk'))),
        dmg=Exists(P.objects.filter(receipt=OuterRef('pk'))))
    print('SE receipts first/last:', qs.aggregate(a=Min('created_at'), b=Max('created_at')))
    for label, q in (('all SE receipts', qs), ('since first 4b doc', qs.filter(created_at__gte=first) if first else qs.none())):
        n = q.count(); w = q.filter(img=True).count() + q.filter(img=False, dmg=True).count()
        print(label, '| total', n, '| with image', w, '| without image', n - w,
              '| pdf-only', q.filter(doc=True, img=False, dmg=False).count(),
              '| no file', q.filter(doc=False, dmg=False).count())
    print('receipt docs by type+mime:', dict(Counter(A.objects.filter(receipt__isnull=False).values_list('document_type', 'mime'))))
    print('largest receipt docs (bytes):', list(A.objects.filter(receipt__isnull=False).order_by('-size').values_list('size', flat=True)[:5]))
    print('receipts by kind:', dict(Counter(G.objects.values_list('kind', flat=True))))
    transaction.set_rollback(True)
EOF
```

How to read the output:

- "SE receipts" means `kind='receipt'`, which only `confirm_grn` writes. SCM overrides are
  `correction`, and the backfill writes `imported`.
- "with image" = an image receipt document (any type, withdrawn included) **or** a damage photo
  on that receipt.
- 4b's push date is not recorded in git. The commit is `d4333f6`, 5 Nov 07:58 IST. "Since first
  4b doc" uses the first receipt document as the date 4b was in use from. If no SE has attached
  one yet, that line prints 0, and "all SE receipts" (since 4a) is the figure to use.

Do **not** use `manage.py shell < file` or a `shell` heredoc: Django's InteractiveConsole
silently skipped an indented block here **[run]**. `python -` runs it as a script.

---

## PART V — unboxing video

### V1. Today every byte goes through the Django request

The uploader, `projects/delivery_storage.py:110` `upload_dc_document` (the same shape as
`upload_damage_photo`, `:217`) **[code]**:

```python
    mime = validate_dc_document(file_obj)
    path = build_design_path(project.project_id, path_kind, file_obj.name)
    try:
        file_obj.seek(0)
        payload = file_obj.read()
    ...
    try:
        _client().storage.from_(bucket).upload(
            path=path, file=payload, file_options={'content-type': mime})
```

The path is: browser multipart POST → gunicorn sync worker → Django parses `request.FILES` →
`file_obj.read()` into memory → one `POST /storage/v1/object/…` to Supabase (httpx,
`storage_client_timeout` 20 s from `storage3.constants.DEFAULT_TIMEOUT`) → the row is written.
Nothing is direct-to-storage.

`solarpms/settings.py:48-49` set `DATA_UPLOAD_MAX_MEMORY_SIZE` and
`FILE_UPLOAD_MAX_MEMORY_SIZE` to 100 MB **[code]**. So any upload under 100 MB is held **in RAM**
by the upload handler, and `.read()` makes a second in-memory copy.

**A 30 MB video on a 1 Mbps uplink, plainly:**

- 30 MB ≈ 252 Mbit, so the body takes **about 4 min 12 s** to arrive.
- A gunicorn **sync** worker reads the body itself, while it is handling that request. Neither
  `Procfile` nor the repo has a buffering proxy in front.
- **[knowledge]** Railway's edge proxy streams request bodies; it does not buffer them.
- The sync worker does not heartbeat mid-request. At **60 s** the arbiter logs
  `WORKER TIMEOUT` and kills it with SIGABRT.
- The SE's browser gets a **502**, or a reset connection, after a minute of "uploading".
- Nothing is recorded: no receipt, no rows. `_refuse_grn` never ran, so **the typed draft is
  lost**, not stashed.
- The killed worker was one of two, so for that minute the whole site runs on one worker. Two SEs
  doing this at once leave **nobody** served for up to 60 s.
- Retrying fails the same way every time. Nothing in the path can succeed: the timeout is on the
  worker, not on the client.

The same arithmetic holds **today** without any video. A 20 MB document (the current cap) takes
about 168 s at 1 Mbps and fails the same way. A full-size 5 MB phone photo in a document slot
takes about 42 s and only just fits.

So video **cannot** go through the request. It must go browser → Supabase directly.

### V2. Supabase: signed upload URLs, resumable uploads, size limit

**Installed [run]:** `supabase 2.31.0`, `storage3 2.31.0`, `httpx 0.28.1` (both `venv` and
`.venv`). `requirements.txt` pins `supabase==2.31.0`.

**Signed upload URL: supported [code].** `storage3/_sync/file_api.py`:

```python
    def create_signed_upload_url(self, path: str, options: Optional[CreateSignedUploadUrlOptions] = None) -> SignedUploadURL:
        ...
        response = self._request("POST", ["object", "upload", "sign", self.id, *path_parts], headers=headers)
        ...
        return {"signed_url": full_url.geturl(), "signedUrl": full_url.geturl(), "token": query_params["token"][0], "path": path}

    def upload_to_signed_url(self, path: str, token: str, file, file_options=None) -> UploadResponse:
        ...
        response = self._request("PUT", final_url, files=_file, headers=headers, data=_data, query_params=query_params)
```

How it would work:

- The server, holding the service key, mints a URL plus token **for one exact path** that it
  chooses (`{project}/grn-video/{uuid}.mp4`). The browser PUTs the file to that URL. The service
  key never reaches the browser, and the browser cannot choose the path.
- Python's `create_signed_upload_url` has **no expiry parameter**. **[knowledge]** Supabase
  documents signed upload URLs as valid for 2 hours.
- `x-upsert` defaults to `false`, so the token cannot overwrite an object that already exists.
- `info()` and `exists()` exist **[code]** (`file_api.py:376, 395`). After the upload, the server
  can confirm the object is there and read its size and MIME before writing a row.

**CORS [run]** (DEV project): an `OPTIONS` preflight for `PUT` on
`/storage/v1/object/upload/sign/…` with Origin `https://horizon-solar-pms.up.railway.app`
returned:

```
200
access-control-allow-origin = *
access-control-allow-methods = GET,HEAD,PUT,PATCH,POST,DELETE,OPTIONS,TRACE,CONNECT
access-control-allow-headers = content-type,x-upsert
```

The repo sets no CSP and no Permissions-Policy (grep of settings and middleware: none). A browser
PUT to `*.supabase.co`, and `getUserMedia` on our HTTPS origin, are therefore not blocked by our
own headers.

**Resumable (TUS): not in the Python client [code]** (no `resumable` / `tus` in `storage3` or
`supabase`).

- **[knowledge]** The Supabase Storage server has a TUS endpoint at
  `/storage/v1/upload/resumable`, with 6 MB chunks. The browser side is `tus-js-client` (or Uppy).
- It authenticates with a bearer JWT plus RLS policies on `storage.objects`. We have no Supabase
  Auth users and no RLS policies. Every app call uses the service key server-side.
- Newer storage servers also take a signed-upload token for TUS (`x-signature`). **Unverified:**
  check the current Supabase docs before relying on it.
- For a 5–15 MB file a single PUT is enough. TUS earns its keep only if field tests show uploads
  dying mid-way.

**Bucket size limit:**

- Not set or read anywhere in the repo **[code]** (no `file_size_limit`, `create_bucket` or
  `update_bucket` outside site-packages).
- **[run]** DEV bucket `Horizon-PMS-Delivery`: `public=False, file_size_limit=None,
  allowed_mime_types=None`. There is no per-bucket limit, so the **project-wide** upload limit
  applies.
- **Production is not visible from here.** Check in the Supabase dashboard → project
  `wqpxtjelsfgwhhyyafus`:
  - **Storage → Buckets →** (the delivery bucket) **→ ⋯ → Edit bucket →** "Restrict file upload
    size" and "Allowed MIME types".
  - **Project Settings → Storage →** "Upload file size limit" (global).
  - The plan is under **Organization → Billing**.
- **[knowledge]** Free plan: a global per-file limit of 50 MB that cannot be raised, 1 GB total
  storage, and limited egress. Pro: the limit is configurable and 100 GB of storage is included.
  Confirm on the pricing page.

**A note for the build:** with a direct upload, the **only** server-enforced size cap is the
bucket's `file_size_limit`, or the project's global limit. Django never sees the bytes. The bucket
limit should therefore be set (ruling V-f).

### V3. The "no XHR from Alpine" rule

The source **[code]** is `PROJECT_CONTEXT.md:1125-1128`, "Session Update — 2026-06-17, UX-First
Strategic Pivot":

```
**Tech stack change for all new frontend work:**
- FROM: Bootstrap 5
- TO: Tailwind CSS + Alpine.js (local UI state only — no fetch/backend calls
  from Alpine) + Lucide Icons — all via CDN, no build pipeline
```

It is repeated in template comments, for example `dashboard/ceo.html:946` ("Alpine holds local
view state and nothing else: no fetch, no XHR, no backend call") and
`views_site_dashboard.py:21` / `tenders_gantt.html:8` ("No fetch, no XHR, no polling").

**What it forbids:** an Alpine component calling our backend for data. Data is rendered
server-side, and Alpine only shows or hides it.

**Does a vanilla-JS direct upload conflict?** Not by the letter:

- The GRN form is Bootstrap plus plain `<script>`, not Alpine.
- The PUT goes to Supabase, not to our backend.
- Vanilla `fetch` already exists in `project_overview.html:2182` (a document upload to Django)
  and `vendors/vendor_list.html:145`.

**The one place it touches the spirit** is minting the signed URL. That is a backend call. Two
ways stay inside the house style:

1. **Mint at render.** Mint the URL when the SE's GRN form is rendered (the challan is not
   Received and the user is an SE who works on the site). It costs one Supabase API call per
   render of that form, and the URL lives 2 h.
2. **Mint on demand via HTMX.** "Add unboxing video" does an `hx-get` that swaps in a small
   fragment carrying the URL and path. That is an ordinary HTMX swap of server-rendered HTML,
   which the house already uses everywhere.

Recommended: (2), so no Supabase call is spent on page views that record no video. The upload
itself should use `XMLHttpRequest`, not `fetch`: only XHR gives upload progress
(`xhr.upload.onprogress`), and an SE on a slow uplink needs a progress bar. The rule restricts
Alpine, not XHR in plain JS, but because the word "XHR" appears in it, **ruling V-c** records
this as an explicit exception.

### V4. Browser recording options

The repo has no video capture of any kind (grep: no `MediaRecorder`, `getUserMedia` or
`accept="video`). Everything below is **[knowledge]**. The figures are typical, not measured on
SE phones.

| | (a) `<input type=file accept="video/*" capture>` | (b) `getUserMedia` + `MediaRecorder` | (c) client transcoding (WebCodecs / ffmpeg.wasm) |
|---|---|---|---|
| How | opens the phone's own camera app | our page shows a live preview and records with constraints `{width: 640/1280, height: 480/720}` and `videoBitsPerSecond` | (a) records, then the page re-encodes |
| Size control | **none**: the camera app's settings decide | **yes**, approximately: resolution and bitrate (Safari follows the bitrate loosely) | yes, to whatever we encode |
| ~MB/min at 480p | not selectable on most stock cameras | **~6–7 MB** (≈800 kbps video + 64 kbps audio) | ~6–7 MB |
| ~MB/min at 720p | Android ~60–75 MB (8–10 Mbps); iPhone HEVC ~40 MB, H.264 ~80 MB | **~11–15 MB** (1.5–2 Mbps) | ~11–15 MB |
| ~MB/min at 1080p (camera default) | Android ~120–150 MB; iPhone HEVC ~60 MB, H.264 ~130 MB | — | — |
| Output | Android: MP4 (H.264 or HEVC); iPhone: MOV (HEVC, or H.264 on "Most Compatible") | iOS Safari 14.5+: **MP4** (H.264/AAC). Android Chrome: **WebM** (VP8/VP9 + Opus) by default; Chrome ≥126 can also record `video/mp4`, so check `MediaRecorder.isTypeSupported('video/mp4;codecs=avc1')` and prefer it | MP4 (H.264) via WebCodecs + a muxer, or ffmpeg.wasm |
| Phone support | all phones | Android Chrome: yes. iOS Safari ≥14.5: yes. Needs HTTPS (we have it) and a camera permission prompt. **Stops if the screen locks or the SE switches app** | WebCodecs encode: Chrome Android 94+, iOS Safari 16.4+/17 partial. ffmpeg.wasm: about 30 MB download, very slow on phones, often out of memory on low-end Android |
| Playback of the result | iPhone HEVC MOV does **not** play in Chrome on Android or Windows | MP4: plays everywhere. WebM: Android, desktop Chrome and Edge yes; iPhone Safari is uncertain on older iOS. **Chrome's MediaRecorder WebM has no duration and no cues, so seeking is poor** until remuxed | MP4 plays everywhere |
| Effort | trivial, but the files are far too big (V1) | medium: about 200 lines of vanilla JS (preview, start/stop, timer, hard stop at N s, retake, upload with progress) | high, and fragile on low-end phones |

**Recommended: (b)** at **640×480, about 1 Mbps, hard stop at 90 s**. That is at most about
**10 MB** per video, about 80–90 s to upload on a 1 Mbps uplink, and well under any Supabase
limit. Prefer MP4 when `isTypeSupported` says yes, which covers all iPhones and current Android
Chrome; fall back to WebM.

Offer (a) only as a fallback when `MediaRecorder` is missing, and cap it client-side: refuse files
over the bucket limit before uploading. Do not offer (a) at all if the bucket limit stays at the
plan default.

**Prototype on real SE phones first** (the brief's plan), measuring:

1. Actual MB/min on 2–3 real phones.
2. Whether Chrome there gives MP4.
3. Upload time from a real site.
4. Playback on an iPhone **and** an Android of what each produced.

### V5. Server-side transcoding

- **ffmpeg is not present [code].** No reference in the repo; `requirements.txt` has no
  ffmpeg/moviepy/imageio; there is no `railpack.json` (only the dead `nixpacks.toml`).
- **[knowledge]** It is installable: `RAILPACK_DEPLOY_APT_PACKAGES=ffmpeg`, or a new
  `railpack.json` with `deploy.aptPackages: ["ffmpeg"]`, on the Debian 13 runtime image. That
  adds roughly 100–200 MB to the image.
- **Inside the request: not safe.** Transcoding a 1-minute 720p clip on a shared vCPU takes
  roughly 20–90 s, which on its own breaks the 60 s timeout. It also holds one of the two sync
  workers the whole time, and first needs the file in the request (V1's problem again).
- **In the deferred pool: not safe either.** `notify_deferred` is a
  `ThreadPoolExecutor(4)` inside each gunicorn worker process, not a durable queue
  (NTF-DCG S2):
  - The job dies with the worker: on a timeout kill, a deploy, or a restart.
  - Pending rows are never retried (S7).
  - The ffmpeg subprocess takes the CPU the web requests need.
  - The job would also have to download from Supabase, transcode, and re-upload.
- **Needed at all if (b) is chosen? No.** (b) records small files to begin with. The only
  remaining reasons would be WebM→MP4 for iPhone playback, and the missing WebM duration. Both go
  away if the recorder prefers `video/mp4` where the browser offers it. Recommend **no server
  transcoding**. Revisit only if the prototype shows Android Chrome on SE phones cannot record MP4
  **and** an iPhone user must watch those videos.

### V6. Playback

How an attachment is opened today **[code]**:

- `delivery_challan_attachment_open` (`views.py:14525`) checks
  `user_can_open_dc_document`. That is exactly `user_can_view_delivery_challan`: SE, PM,
  coordinator, SCM, Admin, and Finance since 5a.
- It then calls `dc_document_url` (`delivery_storage.py:164`), which signs for
  `DC_DOCUMENT_LINK_SECONDS = 300`, and `redirect(url)`s.
- A video opened that way plays in the browser's own player in that tab, if the browser can
  decode the format (V4).

**Range requests: supported [run].** On the DEV delivery bucket, a 60 s signed link to an
existing 45,370-byte PDF answered `Range: bytes=0-15` with **`206 Partial Content`** and
`content-range: bytes 0-15/45370`, and `bytes=5-9` with `206` too. Served via Cloudflare
(`cf-cache-status: MISS`). The response carried no `Accept-Ranges` header. Browsers send `Range`
anyway for media, so this does not matter. Seeking works.

**Catch: the 300 s expiry is per request, not per session.** **[knowledge]** Every range request
the player makes re-presents the token. Someone who opens the video, pauses for more than 5
minutes, then seeks or resumes will get a 400, and must reopen it from the DC page.

For a 90 s clip that is minor. Two fixes, either acceptable:

- A longer expiry for videos only (for example 900 s).
- An inline `<video>` on the DC page, with the link minted on click (HTMX swap), so reopening is
  one tap.

(Ruling V-g.)

### V7. The model: DeliveryChallanAttachment or its own?

**It can live on `DeliveryChallanAttachment`** **[code]** — a new `document_type`
`'unboxing_video'`. What that touches:

- **`document_type` CHECK.** `dc_attachment_receipt_document_type` lists
  `RECEIPT_DOCUMENT_TYPE_CHOICES`. Adding a choice changes the CHECK, so it needs a migration
  (RemoveConstraint + AddConstraint). That is additive and safe on existing rows.
- **`mime`.** `CharField(100)` with **no** CHECK on this model, unlike
  `DCLineDamagePhoto`'s `dc_damage_photo_mime_image`, so `video/mp4` and `video/webm` fit with no
  constraint change. The *validator* is the issue:
  - `validate_dc_document` reads bytes from a Django upload, and a direct upload never reaches
    Django.
  - The video needs its own check *after* the upload: `info()` for size and MIME, plus one
    `Range: bytes=0-15` read of the object to check the leading bytes (`ftyp` at offset 4 for
    MP4/MOV, `1A 45 DF A3` for WebM).
  - That is **its own allow-list for this type only**. `DC_DOCUMENT_TYPES` stays PDF/JPG/PNG.
- **20 MB cap.** `DC_DOCUMENT_MAX_BYTES` lives only in `validate_dc_document`, so the video
  never meets it.
  - The video cap is enforced by the **bucket's `file_size_limit`**, the only real server-side
    stop, plus a post-upload size check before the row is written.
  - If the video shares the delivery bucket, that bucket limit also applies to PDFs and photos.
    That is harmless if it is ≥ 20 MB, since Django already holds them to 20 MB.
  - `size` is a `PositiveIntegerField` (up to 2 GB), which is fine.
- **No-prices tick.** The CHECK `dc_attachment_no_prices_confirmed` holds for every row, so the
  video needs the tick too. This **matters more** for a video than for a photo: vendor invoices
  and packing slips with rates are often packed inside the box, and an unboxing film shows them.
  The help text must say so (ruling V-e).
- **Withdraw.** It works unchanged: a guarded update on the row, and the object is kept. A
  withdrawn video still uses storage.
- **The ≤5 rule.** `_chosen_documents` counts only `receipt_doc_N` keys in `request.FILES`. A
  directly uploaded video arrives as a posted **path**, not a file, so it is not counted. Proposal:
  **one video per submission, outside the 5** (ruling V-d).
- **Bucket.** `dc_document_url` and `discard_unrecorded_dc_document` both refuse any bucket other
  than `SUPABASE_DELIVERY_BUCKET`. **Same bucket, new folder `grn-video`** needs no change there.
  A separate bucket would mean widening both.
- **Readers that change by themselves:**
  - the DC page receipt list (`get_document_type_display`, `delivery_challan_detail.html:340,
    359`);
  - the GRN email's `grn_notify.documents_summary`, which iterates
    `RECEIPT_DOCUMENT_TYPE_CHOICES`, so it would print "Unboxing video: 1" with no code change.
  - Not affected: `dc_edit.live_document_count` and the Challan Documents card, which filter
    `receipt__isnull=True`.
- **G4 and documents only.** A video with no quantity change is refused by G4
  (`DOCUMENTS_NEED_A_QUANTITY`), exactly like a document. A video already uploaded must then be
  discarded, or kept for the draft (see below).

**Its own model is not needed.** Every behaviour it would need (private bucket, signed link,
withdraw, append-only, receipt FK, no-prices tick) is already on this row.

**New with a direct upload, whichever model is used:**

- **Orphans.** The video lands in the bucket *before* the GRN is submitted. An SE who records
  and then abandons the form leaves an object with no row.
  `discard_unrecorded_dc_document` handles refusals and rollbacks, but not abandonment. A sweep
  command is needed: `grn-video/` objects older than 24 h with no row, dry run by default.
- **Path forgery.** The form posts back a path. The server must accept only a path it minted for
  *this* user and *this* challan, under `{project}/grn-video/`. Sign it with
  `django.core.signing` (salt, user pk, challan pk, max_age 2 h), and refuse anything else.
- **Refusal keeps the video.** A refused GRN keeps a typed draft but loses every picked file.
  The video is already in storage, so the signed path can ride in the draft, and the SE need not
  record again. Discard happens on the sweep, not on refusal. (This differs from photos and
  documents, which are discarded on refusal.)

---

## Rulings proposed

| # | Question | Recommendation |
|---|---|---|
| **P-a** | What satisfies "≥1 image"? | (ii): an image receipt document typed **Delivery photo**, or a damage photo, in the same submission |
| **P-b** | Who is bound? | `confirm_grn` (SE) only. Not `override_grn`: SCM is not at site, and its on-behalf branch already asks for a reason |
| **P-c** | Which SE submissions? | Only when **some line's received quantity goes up** (new material arrived). A notes-only edit or a downward correction needs no new photo |
| **P-d** | Full-size photos on a slow uplink | Downscale image documents in the browser: add `data-qaqc-photo` to the 5 slots. PDFs pass through untouched (the script keeps any file it cannot load as an image). This relaxes 4b Q5; 1600 px keeps a photographed DC readable. If Q5 must stand, require the photo through a damage-style downscaled input instead (P-a iii) |
| **P-e** | Storage off (bucket unset) | Refuse with `RECEIPT_DOCUMENT_STORAGE_OFF`'s wording. Production has the bucket |
| **P-f** | Message | One sentence, joined to the document errors (P1): "Attach at least one photo of the material received — choose 'Delivery photo' for it." |
| **P-g** | Client-side | The help text names the rule. No JS block: the server is the gate, as everywhere |
| **V-a** | Recording | (b) `MediaRecorder` at 640×480, ~1 Mbps, hard stop 90 s, MP4 if supported else WebM. (a) only as a size-capped fallback. Not (c) |
| **V-b** | Upload path | Direct browser → Supabase via `create_signed_upload_url`. A single PUT with `XMLHttpRequest` for progress. TUS only if the prototype shows drops |
| **V-c** | House rule | Explicit exception: plain-JS XHR to Supabase only, never to our backend. The URL is minted by an HTMX `hx-get` fragment, not a fetch |
| **V-d** | Count | One video per submission, outside the 5-document cap. Optional. Ignored by P-a (a video is not "an image") |
| **V-e** | No-prices | Tick required, with the help text naming invoices packed in boxes |
| **V-f** | Size cap | 25 MB, set as the bucket's `file_size_limit` (real enforcement) and re-checked with `info()` before the row. With the bucket shared, the limit is ≥20 MB so PDFs are unaffected |
| **V-g** | Playback | Inline `<video>` on the DC page, link minted on click, 900 s for videos |
| **V-h** | Model | `DeliveryChallanAttachment`, `document_type='unboxing_video'`, same bucket, folder `grn-video` |
| **V-i** | Server transcoding | None |
| **V-j** | Orphans | A sweep command, dry run by default, `--apply` removes unrecorded `grn-video/` objects older than 24 h |

## Build split and files touched

**GRN-P (mandatory photo, small, can ship now):**

| File | Change |
|---|---|
| `projects/goods_receipts.py` | `receipt_photo_errors(request, grn_lines, damage_plan)` + message constant; module docstring order note |
| `projects/views.py` | `confirm_grn`: join its errors to `document_errors` (P1); docstring |
| `projects/templates/projects/delivery_challan_detail.html` | SE form help text names the rule; `data-qaqc-photo` on the 5 slots (P-d, SE form; override form only if ruled) |
| `projects/tests_receipt_photo_required.py` (new) | rule, P-a/P-c cases, single refusal with the tick error, no upload on refusal, override unaffected |
| existing tests listed in P3 (14 test modules) | add an image to the helpers and direct posts; review the refusal tests' messages |
| `projects/management/commands/seed_walkthrough.py` | `grn()` posts a delivery photo on the two SE GRNs that receive material (P3) |
| `docs/` / `SECONDARY_FINDINGS.md` | 4b Q5 relaxed; the HEIC walk is still owed |

No migration.

**GRN-V0 (prototype, not shipped):** a standalone HTML page (scratch, not in the app) that records
with `MediaRecorder` at the V-a settings and reports MB/min, the chosen container, and the upload
time to a DEV-bucket signed URL. It runs on 2–3 real SE phones, and the result plays back on an
iPhone and an Android. That answers V4's open points before any app code is written.

**GRN-V (build, after the prototype):**

| File | Change |
|---|---|
| `projects/models.py` | `RECEIPT_DOCUMENT_VIDEO = 'unboxing_video'` added to the choices (CHECK changes) |
| `projects/migrations/0143_receipt_video_type.py` (new) | Remove/AddConstraint `dc_attachment_receipt_document_type` |
| `projects/delivery_storage.py` | `RECEIPT_VIDEO_PATH_KIND`, video MIME allow-list and cap, `mint_video_upload()` (`create_signed_upload_url`), `verify_uploaded_video()` (`info()` + 16-byte range read), video link expiry |
| `projects/goods_receipts.py` | `plan_receipt_video()` (signed-path check, verify) and the link through `link_receipt_documents` |
| `projects/views.py` | the HTMX mint view; `confirm_grn` (and `override_grn` if ruled) plans and links the video; draft keeps the signed path |
| `projects/urls.py` | the mint route |
| `projects/permissions.py` | none expected: mint = `confirm_grn`'s own two gates; open = `user_can_open_dc_document` |
| `projects/templates/projects/delivery_challan_detail.html` | the recorder block, inline `<video>` in the receipt list |
| `projects/templates/projects/partials/_grn_video_recorder.html` (new) | the plain-JS recorder + XHR uploader |
| `projects/templates/projects/partials/_grn_video_slot.html` (new) | the HTMX fragment with the signed URL |
| `projects/management/commands/sweep_grn_video_orphans.py` (new) | V-j |
| `projects/grn_notify.py` | none: the summary picks the type up from the choices. Its registry/email tests may need the new label |
| `projects/tests_receipt_video.py` (new) | mint gates, forged/expired path, verify (size/MIME/leading bytes, storage mocked), G4 with a video, no-prices, withdraw, draft keeps the path, the sweep |
| Supabase dashboard (production) | set the bucket's `file_size_limit` (V-f). This is a manual step, not code |

## Facts only Zuber has

1. **SE phones:** mostly Android, or are there iPhones? This decides whether WebM from Android
   Chrome is acceptable (V4/V5), and whether the HEIC walk is urgent (P2).
2. **Supabase plan:** free or paid? Free means a 50 MB per-file ceiling **[knowledge]** that
   cannot be raised, and 1 GB of total storage shared with every other bucket. At about 10 GRNs a
   month × ~10 MB, video adds about 100 MB a month.
