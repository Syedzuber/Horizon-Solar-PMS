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
})
