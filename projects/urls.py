from django.urls import path
from django.views.generic import RedirectView
from . import views

urlpatterns = [
    # ---------------------------------------------------------------------------
    # Auth — no login required
    # ---------------------------------------------------------------------------
    path('login/',  views.login_view,  name='login'),
    path('logout/', views.logout_view, name='logout'),

    # ---------------------------------------------------------------------------
    # Dashboards — each restricted to its own role via @role_required
    # ---------------------------------------------------------------------------
    path('dashboard/admin/',         views.dashboard_admin,         name='dashboard_admin'),
    path('dashboard/pm/',            views.dashboard_pm,            name='dashboard_pm'),
    path('dashboard/site-engineer/', views.dashboard_site_engineer, name='dashboard_site_engineer'),
    path('dashboard/design/',        views.dashboard_design,        name='dashboard_design'),
    path('dashboard/finance/',       views.dashboard_finance,       name='dashboard_finance'),
    path('dashboard/scm/',           views.dashboard_scm,           name='dashboard_scm'),
    path('dashboard/ceo/',           views.dashboard_ceo,           name='dashboard_ceo'),
    path('dashboard/bd/',            views.dashboard_bd,            name='dashboard_bd'),

    # ---------------------------------------------------------------------------
    # User management — Admin only
    # ---------------------------------------------------------------------------
    path('users/',                    views.user_list,   name='user_list'),
    path('users/create/',             views.user_create, name='user_create'),
    path('users/<int:user_id>/edit/', views.user_edit,   name='user_edit'),

    # ---------------------------------------------------------------------------
    # Projects — PM (own projects only), Admin, CEO
    # ---------------------------------------------------------------------------
    path('projects/',                                          views.project_list,          name='project_list'),
    path('projects/create/',                                   views.project_create,        name='project_create'),       # PM only
    path('projects/<str:project_id>/',                         views.project_detail,        name='project_detail'),
    path('projects/<str:project_id>/delete/',                  views.project_delete,        name='project_delete'),        # Admin only, POST only
    path('projects/<str:project_id>/edit/',                    views.project_edit,          name='project_edit'),          # PM only, Draft status only
    path('projects/<str:project_id>/activate/',                views.project_activate,           name='project_activate'),           # PM only, POST only — creates phases/tasks/milestones
    path('projects/<str:project_id>/recalculate-dates/',       views.project_recalculate_dates,  name='project_recalculate_dates'),  # PM only, POST only
    path('projects/<str:project_id>/tasks/add/',               views.task_add,              name='task_add'),              # PM only
    path('projects/<str:project_id>/tasks/<int:task_id>/update/',   views.task_status_update, name='task_status_update'), # Assigned role or PM
    path('projects/<str:project_id>/tasks/<int:task_id>/assign/',   views.task_assign,        name='task_assign'),        # PM only
    path('projects/<str:project_id>/tasks/<int:task_id>/due-date/', views.task_set_due_date,         name='task_set_due_date'),         # PM + role-owners, triggers cascade recalculation for PM only
    path('projects/<str:project_id>/enable-cascade/',              views.enable_cascade_scheduling,  name='enable_cascade_scheduling'), # PM only, irreversible, POST only

    # ---------------------------------------------------------------------------
    # Vendors — SCM and Admin only
    # ---------------------------------------------------------------------------
    path('vendors/',                              views.vendor_list,          name='vendor_list'),
    path('vendors/add/',                          views.vendor_add,           name='vendor_add'),
    path('vendors/<int:vendor_id>/edit/',         views.vendor_edit,          name='vendor_edit'),
    path('vendors/<int:vendor_id>/toggle-status/', views.vendor_toggle_status, name='vendor_toggle_status'),  # Returns JSON {is_active: bool}

    # ---------------------------------------------------------------------------
    # Notifications — any logged-in user (their own notifications only)
    # ---------------------------------------------------------------------------
    path('notifications/', views.notifications_view, name='notifications'),

    # ---------------------------------------------------------------------------
    # BOQ (Bill of Quantities) — Design, SCM, PM, Admin
    # ---------------------------------------------------------------------------
    path('projects/<str:project_id>/boq/',                   views.boq_detail,           name='boq_detail'),
    path('projects/<str:project_id>/boq/submit/',            views.boq_submit,           name='boq_submit'),            # Design only
    path('projects/<str:project_id>/boq/acknowledge/',       views.boq_acknowledge,      name='boq_acknowledge'),       # SCM only
    path('projects/<str:project_id>/boq/request-revision/',  views.boq_request_revision, name='boq_request_revision'),  # PM only
    path('projects/<str:project_id>/boq/history/',           views.boq_history,          name='boq_history'),           # PM, Design, SCM, Admin

    # ---------------------------------------------------------------------------
    # Payment milestones — Finance (invoice/receive), PM (create), BD/PM (set amounts)
    # ---------------------------------------------------------------------------
    path('projects/<str:project_id>/milestone/<int:milestone_pk>/invoice/', views.milestone_invoice,     name='milestone_invoice'),     # Finance only
    path('projects/<str:project_id>/milestone/<int:milestone_pk>/receive/', views.milestone_receive,     name='milestone_receive'),     # Finance only
    path('projects/<str:project_id>/milestones/create/',                    views.milestone_create,      name='milestone_create'),      # PM only
    path('projects/<str:project_id>/milestones/set-amounts/',               views.set_milestone_amounts, name='set_milestone_amounts'), # BD + PM

    # ---------------------------------------------------------------------------
    # Project overview — all roles with access to the project
    # ---------------------------------------------------------------------------
    path('projects/<str:project_id>/overview/', views.project_overview, name='project_overview'),

    # ---------------------------------------------------------------------------
    # Payment Requests — SCM raises, Finance confirms, PM/SCM/Finance view
    # ---------------------------------------------------------------------------
    # Raise payment request — SCM role only
    path('projects/<str:project_id>/payment-requests/raise/',
         views.raise_payment_request, name='raise_payment_request'),
    # Confirm payment request — Finance role only
    path('projects/<str:project_id>/payment-requests/<int:request_id>/confirm/',
         views.confirm_payment_request, name='confirm_payment_request'),

    # ---------------------------------------------------------------------------
    # Webhooks — unauthenticated, no login required
    # ---------------------------------------------------------------------------
    path('webhooks/zoho/deal-closed/',        views.zoho_deal_closed_webhook, name='zoho_webhook'),
    path('webhooks/interakt/delivery/',        views.interakt_webhook,         name='interakt_webhook'),

    # ---------------------------------------------------------------------------
    # Task detail — all roles with access to the project
    # ---------------------------------------------------------------------------
    path('projects/<str:project_id>/tasks/<int:task_id>/',
         views.task_detail, name='task_detail'),

    # ---------------------------------------------------------------------------
    # File uploads — project documents (any role with project access)
    # ---------------------------------------------------------------------------
    path('projects/<str:project_id>/documents/upload/',
         views.upload_project_document, name='upload_project_document'),
    path('projects/<str:project_id>/documents/<int:doc_pk>/delete/',
         views.delete_project_document, name='delete_project_document'),   # Uploader or Admin only

    # ---------------------------------------------------------------------------
    # File uploads — task attachments (any role with project access)
    # ---------------------------------------------------------------------------
    path('projects/<str:project_id>/tasks/<int:task_id>/attachments/upload/',
         views.upload_task_attachment, name='upload_task_attachment'),
    path('projects/<str:project_id>/tasks/<int:task_id>/attachments/<int:attach_pk>/delete/',
         views.delete_task_attachment, name='delete_task_attachment'),     # Uploader or Admin only

    # ---------------------------------------------------------------------------
    # Issues — any role with project access can raise; PM closes
    # ---------------------------------------------------------------------------
    path('projects/<str:project_id>/issues/create/',
         views.create_project_issue, name='create_project_issue'),
    path('projects/<str:project_id>/tasks/<int:task_id>/issues/create/',
         views.create_task_issue, name='create_task_issue'),
    path('issues/<int:issue_id>/',
         views.issue_detail, name='issue_detail'),
    path('issues/<int:issue_id>/update-status/',
         views.update_issue_status, name='update_issue_status'),    # Open → In Progress; requires assignee
    path('issues/<int:issue_id>/resolve/',
         views.resolve_issue, name='resolve_issue'),                # Anyone → Resolved; notifies PM
    path('issues/<int:issue_id>/close/',
         views.close_issue, name='close_issue'),                    # PM only; Resolved → Closed
    path('issues/<int:issue_id>/reopen/',
         views.reopen_issue, name='reopen_issue'),                  # PM only; Resolved → Open
    path('issues/<int:issue_id>/assign/',
         views.assign_issue, name='assign_issue'),

    # ---------------------------------------------------------------------------
    # Comments — all roles; one level of replies only
    # ---------------------------------------------------------------------------
    # Task comment (all roles)
    path('projects/<str:project_id>/tasks/<int:task_id>/comments/create/',
         views.create_task_comment, name='create_task_comment'),
    # Issue comment (all roles)
    path('issues/<int:issue_id>/comments/create/',
         views.create_issue_comment, name='create_issue_comment'),
    # Comment soft delete — author or Admin only
    path('comments/<int:comment_id>/delete/',
         views.delete_comment, name='delete_comment'),

    # ---------------------------------------------------------------------------
    # Activity logs
    # ---------------------------------------------------------------------------
    # Project activity timeline — all roles, PM isolation applies
    path('projects/<str:project_id>/timeline/',
         views.project_timeline, name='project_timeline'),
    # Portal-wide activity log — Admin only
    # NOTE: /portal/ prefix used instead of /admin/ because Django admin
    # intercepts all /admin/* URLs before the projects URLconf can match them
    path('portal/whatsapp-log/',
         views.admin_whatsapp_log, name='admin_whatsapp_log'),

    # ---------------------------------------------------------------------------
    # Admin Panel — portal-admin/ prefix (Admin role only)
    # ---------------------------------------------------------------------------
    path('portal-admin/settings/',                      views.admin_master_switches,    name='admin_master_switches'),
    path('portal-admin/users/',                         views.admin_user_management,    name='admin_user_management'),
    path('portal-admin/notification-prefs/',            views.admin_notification_prefs, name='admin_notification_prefs'),
    path('portal-admin/departments/',                   views.admin_departments,         name='admin_departments'),
    path('portal-admin/departments/<int:user_id>/edit/', views.admin_user_edit,          name='admin_user_edit'),
    path('portal-admin/send-records/',                  views.admin_send_records,        name='admin_send_records'),
    path('portal-admin/audit-log/',                     views.admin_audit_log,           name='admin_audit_log'),
    # Redirect old activity-log URL to the new audit-log screen
    path('portal/activity-log/',
         RedirectView.as_view(pattern_name='admin_audit_log', permanent=False),
         name='portal_activity_log_redirect'),

    # ---------------------------------------------------------------------------
    # Task drill-down — due-date filters for PM, Design, SCM dashboards
    # ---------------------------------------------------------------------------
    # ---------------------------------------------------------------------------
    # My Documents — personal record-keeping sub-page
    # ---------------------------------------------------------------------------
    path('profile/documents/', views.my_documents, name='my_documents'),
    path('design-submissions/<int:pk>/', views.design_submission_detail, name='design_submission_detail'),
    path('projects/<str:project_id>/payment-requests/<int:request_id>/',
         views.payment_request_detail, name='payment_request_detail'),

    path('tasks/due-today/', views.tasks_drill_down, {'filter_type': 'due-today'}, name='tasks_due_today'),
    path('tasks/due-soon/',  views.tasks_drill_down, {'filter_type': 'due-soon'},  name='tasks_due_soon'),
    path('tasks/overdue/',   views.tasks_drill_down, {'filter_type': 'overdue'},   name='tasks_overdue'),

    # ---------------------------------------------------------------------------
    # Delivery Challans — SCM Delivery Tracker (Day 9)
    # ---------------------------------------------------------------------------
    # Create DC — SCM only
    path('projects/<str:project_id>/delivery-challans/create/',
         views.create_delivery_challan, name='create_delivery_challan'),
    # DC detail — SCM, PM, SE, Admin
    path('projects/<str:project_id>/delivery-challans/<int:dc_id>/',
         views.delivery_challan_detail, name='delivery_challan_detail'),
    # SE GRN confirmation — SE only
    path('projects/<str:project_id>/delivery-challans/<int:dc_id>/grn/',
         views.confirm_grn, name='confirm_grn'),
    # SCM GRN override — SCM only
    path('projects/<str:project_id>/delivery-challans/<int:dc_id>/grn/override/',
         views.override_grn, name='override_grn'),
    # Raise issue against a specific DC — all roles with project access
    path('projects/<str:project_id>/delivery-challans/<int:dc_id>/issues/create/',
         views.create_delivery_issue, name='create_delivery_issue'),
]
