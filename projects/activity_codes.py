"""
ActivityLog action codes that readers branch on, gathered in one place.

MONEY_ACTIVITY_CODES (AF-1) — every code whose feed line carries a money amount: an
order total, a payment request, an approval, hold, rejection or payment, a contractor
bill's payment request (D-A53), a milestone amount. A user who fails
permissions.user_can_see_project_money() sees none of these rows, on the overview's
Recent activity card or on the project timeline, excluded in the query.

KEPT COMPLETE BY A TEST, NOT BY MEMORY. tests_activity_money scans every log_activity()
call in the codebase and fails if one whose description carries a money amount is
written under a code missing from this set, or under a code it cannot resolve; it also
fails if a code here is no longer written by any money call. A new money line is
therefore added here in the same change that writes it, or the suite goes red.

Constants only — no imports, so any module may import this without a cycle.
"""

MONEY_ACTIVITY_CODES = frozenset({
    'vendor_order_raised',        # order_views: an order recorded, with its total
    'payment_request_raised',     # order_views / purchases_views / approval_views (bill)
    'payment_request_approved',   # order_views: approved, possibly partially
    'payment_request_held',       # order_views
    'payment_request_rejected',   # order_views
    'payment_hold_answered',      # order_views: SCM's response to a hold
    'payment_request_paid',       # payments.mark_payment_paid
    'milestone_amounts_set',      # views: BD's set_milestone_amounts and the PM edit's amount
    'milestone_amount_received',  # views: amount received / variance reason (FIN-M-L)
    'milestone_invoice_amount',   # views: the invoice amount Finance records (FIN-M-B3)
    'milestone_amounts_imported', # milestone_amount_import: Finance's one-time sheet (FIN-M-AB)
    'milestone_legacy_marked',    # mark_legacy_received_milestones: names the old amount (FIN-M-B5-5a)
    # FIN-M-B5-2: every receipt line names its amount (receipts.py, the only writer).
    'receipt_recorded',           # record_receipt: BD / PM side / Finance records money received
    'receipt_queried',            # query_receipt: Finance queries it
    'receipt_query_answered',     # answer_receipt_query: the recorder answers
    'receipt_confirmed',          # confirm_receipt: Finance confirms it against the bank
    'receipt_rejected',           # reject_receipt: Finance rejects it
    'receipt_amount_completed',   # complete_receipt_amount: Finance types a legacy row's figures
    # FIN-M-B5-4a: both lines name the receipt's amount.
    'receipt_unconfirmed',        # unconfirm_receipt: Finance undoes a confirmation (FR35)
    'receipt_proof_opened',       # views_receipts.receipt_proof_open: an access record
})
# FIN-M-L's plain milestone code, 'milestone_updated' (description, dates, status,
# creation), is deliberately NOT here: those lines carry no amount and every viewer of
# the site sees them.

# NON_ACTIVITY_CODES (DASH-B1, decision D3) — codes whose rows are NOT something a person
# did on the site, so they never count as a site's "latest activity" or fill its feed on
# the common dashboard. Read through site_dashboard.is_human_activity(), which also drops
# every code ending in `_opened` (pure reads) and keeps BLANK codes: ~80 older call sites
# write no code, and their rows (activations, file uploads, coordinator changes) are
# human actions.
#
# EXCLUDED BY CODE, NOT BY ACTOR. A trigger run of the scheduler writes
# `due_dates_scheduled` WITH the triggering person as actor, so "actor is set" does not
# mean a person did this; the code is the only reliable test.
#
# The four scheduler codes are spelt out rather than imported from scheduling.py, to keep
# this module import-free (see the docstring); tests_site_dashboard pins them equal to
# scheduling's constants, so a renamed code fails the suite instead of silently counting.
#
# TODO: export_dashboard_prototype keeps its own IGNORED_CODES, which lacks the four
# scheduler codes; switch it to this set. Deferred: DASH-B1 may not edit the export.
NON_ACTIVITY_CODES = frozenset({
    'user_login',                       # signals.log_user_login (project=None)
    'user_logout',                      # signals.log_user_logout (project=None)
    'due_dates_scheduled',              # scheduling.SCHEDULED_ACTION_CODE
    'due_dates_forecast_shift',         # scheduling.FORECAST_ACTION_CODE, never an actor
    'baseline_reset',                   # scheduling.BASELINE_RESET_ACTION_CODE
    'auto_schedule_exclusion_changed',  # admin.ProjectAdmin: scheduling bookkeeping
    'notify_template_seeded',           # seed_notification_templates (project=None)
    'cad_pdf_extraction_failed',        # design_artifact_upload: a failure note after the upload's own row
    'bd_backfilled',                    # backfill_site_bd: a one-off data fill, not work on the site
    # FIN-M-AB: the one-time amount backfill from Finance's sheet. A data fill on ~37
    # sites at once; counted, it would show every one of them "Active today".
    'milestone_amounts_imported',       # import_milestone_amounts
    # FIN-M-B5-2 (ruling Q7): Finance's checking of a receipt is office work about money,
    # not work on the site. The recorder's two codes — 'receipt_recorded' and
    # 'receipt_query_answered' — are deliberately NOT here: a BD or PM taking or explaining
    # the client's money is a person's act on the site and counts as its latest activity.
    'receipt_queried',                  # receipts.query_receipt
    'receipt_confirmed',                # receipts.confirm_receipt
    'receipt_rejected',                 # receipts.reject_receipt
    'receipt_amount_completed',         # receipts.complete_receipt_amount
    # FIN-M-B5-4a (ruling Q9): undoing a confirmation is Finance's office work, as
    # confirming is. ('receipt_proof_opened' needs no entry: every *_opened code is
    # already dropped by site_dashboard.is_human_activity().)
    'receipt_unconfirmed',              # receipts.unconfirm_receipt
    # FIN-M-B5-5a (ruling Q6): the release-time marking of old Received milestones is a
    # data step on ~51 sites at once, not work on any of them.
    'milestone_legacy_marked',          # mark_legacy_received_milestones
})
# 'milestone_amounts_imported' is deliberately NOT 'milestone_amounts_set': the Finance
# Head check "Amount changed after Received" reads only the latter, so Finance's first
# fill of an amount that was blank does not list ~49 Received milestones as changed.
# A BD set from the overview's form writes 'bd_assigned' and is NOT here: like
# 'pm_assigned', it is a person's decision about the site and counts as its latest activity.


# BULK ACTIONS (RPT-B1, decision D8 and Zuber's ruling 2, 28 Nov 2026) — the Reports
# section counts one person doing one action across many sites at once as ONE action, and
# a site touched only by such a batch is not an active site. A batch is a run of
# ActivityLog rows (or unpaired StatusTransition rows) by the SAME actor with the SAME
# action code, each within BULK_BATCH_GAP_SECONDS of the previous one, touching at least
# BULK_MIN_SITES distinct sites. Read by report_activity only.
#
# WHY A TIMING RULE AND NOT A LIST OF CODES. No bulk path writes a code of its own:
# design_bulk_allocate writes the single-site 'design_allocated' / 'design_qc_assigned',
# hand_over_sites writes 'pm_assigned' through change_site_pm, opex_site_bulk_upload writes
# 'opex_site_created' through create_opex_site. So the code cannot tell bulk from single.
#
# WHY 2 SECONDS (the 27 Nov 2026 production evidence). A true bulk POST writes its rows
# within a second: design_bulk_allocate put 30 sites in at 12:52:41-42 on 25 Nov. Fast
# queue work is NOT bulk and counts in full: on 27 Nov the Design Head made 144 separate
# single-site submissions (78 survey links, 66 Head QC passes), 4-8 s apart (median 4.2 s
# and 8.6 s). Each is real work on that site. A 120 s gap would also have collapsed the
# QC engineers' reviews and a PM's 17 design approvals.
BULK_BATCH_GAP_SECONDS = 2
# Ten sites: below this a run is a person working quickly, not a bulk screen.
BULK_MIN_SITES = 10
# One bulk ACTION per screen use (B1 answer 1, 28 Nov 2026): batches by the same actor over
# the same set of sites whose first rows are under this many seconds apart are merged into
# one, showing every code. design_bulk_allocate writes design_allocated and
# design_qc_assigned for each site in the same POST (25 Nov: 12:52:41-42, 30 sites, both
# codes), which the per-code rule above would otherwise count twice.
BULK_MERGE_SECONDS = 1
