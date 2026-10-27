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
    'milestone_amounts_set',      # views.set_milestone_amounts (Residential)
})
