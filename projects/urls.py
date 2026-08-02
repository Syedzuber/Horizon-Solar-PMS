from django.urls import path
from django.views.generic import RedirectView
from . import views
from . import design_views   # OPEX design workflow (Part 2) — separate module, no existing view changed

urlpatterns = [
    # ---------------------------------------------------------------------------
    # Auth — no login required
    # ---------------------------------------------------------------------------
    path('login/',  views.login_view,  name='login'),
    path('logout/', views.logout_view, name='logout'),

    # Context chooser (EPC Residential / Tenders) — CEO, Finance and SCM land
    # here after login. Display filter only; grants no access of any kind.
    path('landing/', views.landing, name='landing'),

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
    # Programs — OPEX tender / multi-site CAPEX contract parent (Admin, PM, CEO)
    # ---------------------------------------------------------------------------
    path('programs/',                 views.program_list,   name='program_list'),
    path('programs/create/',          views.program_create, name='program_create'),   # Admin/PM
    path('programs/<int:pk>/',        views.program_detail, name='program_detail'),
    path('programs/<int:pk>/edit/',   views.program_edit,   name='program_edit'),      # Admin/PM
    path('programs/<int:pk>/delete/', views.program_delete, name='program_delete'),    # Admin only, POST only
    path('programs/<int:pk>/sites/add/', views.opex_site_create, name='opex_site_create'),  # Admin/PM — OPEX programs only
    path("programs/<int:pk>/sites/bulk/", views.opex_site_bulk_upload, name='opex_site_bulk_upload'),
    path("programs/<int:pk>/sites/bulk/template/", views.opex_site_bulk_template, name='opex_site_bulk_template'),

    # ---------------------------------------------------------------------------
    # OPEX design workflow (Part 2) — survey, allocation, due-date handshake.
    # Design Head authority is UserProfile.is_design_head, not the role string.
    # All POST-only except the two screens and the signed-URL redirect.
    # ---------------------------------------------------------------------------
    path('programs/<int:pk>/design/',        design_views.design_head_sites,   name='design_head_sites'),
    path('programs/<int:pk>/design/allocate/', design_views.design_bulk_allocate, name='design_bulk_allocate'),
    path('design/my-sites/',                 design_views.design_my_sites,     name='design_my_sites'),
    path('design/<str:project_id>/survey/upload/',   design_views.design_survey_upload,   name='design_survey_upload'),
    path('design/<str:project_id>/survey/',          design_views.design_survey_download, name='design_survey_download'),
    path('design/<str:project_id>/allocate/',        design_views.design_allocate,        name='design_allocate'),
    path('design/<str:project_id>/due-date/propose/', design_views.design_due_date_propose, name='design_due_date_propose'),
    path('design/<str:project_id>/due-date/approve/', design_views.design_due_date_approve, name='design_due_date_approve'),
    path('design/<str:project_id>/due-date/reject/',  design_views.design_due_date_reject,  name='design_due_date_reject'),
    path('design/<str:project_id>/due-date/change/',  design_views.design_due_date_change,  name='design_due_date_change'),
    path('design/<str:project_id>/blocked/',          design_views.design_mark_blocked,     name='design_mark_blocked'),

    # ---------------------------------------------------------------------------
    # OPEX design workflow (Part 3) — Arka, CAD, BOQ and versioning.
    # NO NAVIGATION LINKS: these screens are URL-reachable only, exactly like the
    # Part 2 pair above. Wiring them into the dashboards is Part 4.5.
    # ---------------------------------------------------------------------------
    path('design/<str:project_id>/work/',           design_views.design_site_workspace,  name='design_site_workspace'),
    path('design/<str:project_id>/review/',         design_views.design_head_review,     name='design_head_review'),
    path('design/<str:project_id>/arka/submit/',    design_views.design_arka_submit,     name='design_arka_submit'),
    # PART 9 — the Arka now passes two gates. The two ORIGINAL routes are unchanged in
    # name and path and are now GATE 1 (Design QC), because the fields they write are the
    # Design QC verdict; the two head/ routes are GATE 2. Keeping the old names means
    # every existing link and reverse() still resolves, and what changed is who may POST.
    path('design/<str:project_id>/arka/approve/',   design_views.design_arka_approve,    name='design_arka_approve'),
    path('design/<str:project_id>/arka/reject/',    design_views.design_arka_reject,     name='design_arka_reject'),
    path('design/<str:project_id>/arka/head/approve/', design_views.design_arka_head_approve, name='design_arka_head_approve'),
    path('design/<str:project_id>/arka/head/reject/',  design_views.design_arka_head_reject,  name='design_arka_head_reject'),
    path('design/<str:project_id>/artifact/upload/', design_views.design_artifact_upload, name='design_artifact_upload'),
    path('design/<str:project_id>/artifact/<int:pk>/', design_views.design_file_download, name='design_file_download'),
    path('design/<str:project_id>/boq/complete/',   design_views.design_boq_complete,    name='design_boq_complete'),

    # ---------------------------------------------------------------------------
    # OPEX design workflow (Part 4) — QC review, attempt lifecycle, deputy,
    # PM change requests, release.
    # QC authority is Head OR named deputy, minus the site's own designer:
    # permissions.user_can_qc_design(). Change requests route through the untouched
    # user_can_manage_project(). Still no navigation links — Part 4.5 integrates.
    # ---------------------------------------------------------------------------
    # Part 5 — the Design Head's read-only tender dashboard. Landing view for a tender;
    # the Part 2 site list at /programs/<pk>/design/ is now the drill-down target.
    path('programs/<int:pk>/design/dashboard/', design_views.design_tender_dashboard, name='design_tender_dashboard'),

    # Part 9 — the Design QC dashboard: a strict subset of the Head's, above.
    path('programs/<int:pk>/design/qc-dashboard/', design_views.design_qc_dashboard, name='design_qc_dashboard'),

    # ---------------------------------------------------------------------------
    # Part 10 — quality analytics. DESIGN HEAD ONLY (the flag itself, deputy excluded —
    # see the note above design_quality_analytics). Two routes onto ONE view: the same
    # screen scoped to one tender or to every OPEX tender combined, so there is no
    # second code path for the combined view to drift down.
    #
    # The two POST endpoints are the only writes in Part 10 and they write one
    # DesignAnalyticsPreference row. Neither touches a workflow model.
    # ---------------------------------------------------------------------------
    path('design/analytics/',                       design_views.design_quality_analytics, name='design_quality_analytics'),
    path('programs/<int:pk>/design/analytics/',     design_views.design_quality_analytics, name='design_quality_analytics_tender'),
    path('design/analytics/configure/',            design_views.design_analytics_configure, name='design_analytics_configure'),
    path('design/analytics/reset/',                design_views.design_analytics_reset,     name='design_analytics_reset'),

    path('design/qc/',                              design_views.design_qc_queue,        name='design_qc_queue'),
    path('design/<str:project_id>/qc/',             design_views.design_qc_review,       name='design_qc_review'),
    path('design/<str:project_id>/qc/start/',       design_views.design_qc_start,        name='design_qc_start'),
    # Gate 1 — Design QC. Same routes and names as Part 4; the fields they write are the
    # Design QC verdict, so they keep their URLs and change who may POST to them.
    path('design/<str:project_id>/qc/pass/',        design_views.design_qc_pass,         name='design_qc_pass'),
    path('design/<str:project_id>/qc/fail/',        design_views.design_qc_fail,         name='design_qc_fail'),
    # Gate 2 — the Design Head. Release now happens here, not at qc/pass/.
    path('design/<str:project_id>/qc/head/pass/',   design_views.design_head_qc_pass,    name='design_head_qc_pass'),
    path('design/<str:project_id>/qc/head/fail/',   design_views.design_head_qc_fail,    name='design_head_qc_fail'),
    path('design/<str:project_id>/change-request/', design_views.design_change_request_form, name='design_change_request_form'),
    path('design/<str:project_id>/change-request/raise/', design_views.design_change_request, name='design_change_request'),

    # Part 4.6 — the Design Head triages what the PM raised. Keyed by the request's own
    # pk, not by the site: the row is the thing being decided, and a site can carry more
    # than one over its life.
    path('design/change-request/<int:pk>/accept/', design_views.design_change_request_accept, name='design_change_request_accept'),
    path('design/change-request/<int:pk>/reject/', design_views.design_change_request_reject, name='design_change_request_reject'),

    # ---------------------------------------------------------------------------
    # OPEX site groups (Part 6) — grouped procurement, aggregated BOQ, BOQ lock.
    # WRITE is role='SCM' alone (permissions.user_can_manage_site_groups); READ adds
    # Admin and Design Head authority (user_can_view_site_groups). The group lock is
    # enforced caller-side in views.boq_detail / boq_submit via
    # permissions.project_boq_is_group_locked() — no Part 0.6 helper is modified.
    # ---------------------------------------------------------------------------
    path('programs/<int:pk>/site-groups/',        design_views.site_group_list,   name='site_group_list'),
    path('programs/<int:pk>/site-groups/create/', design_views.site_group_create, name='site_group_create'),
    path('site-groups/<int:pk>/',                 design_views.site_group_detail,      name='site_group_detail'),
    path('site-groups/<int:pk>/add-sites/',       design_views.site_group_add_sites,   name='site_group_add_sites'),
    path('site-groups/<int:pk>/remove-site/',     design_views.site_group_remove_site, name='site_group_remove_site'),
    path('site-groups/<int:pk>/lock/',            design_views.site_group_lock,        name='site_group_lock'),

    # ---------------------------------------------------------------------------
    # Projects — PM (own projects only), Admin, CEO
    # ---------------------------------------------------------------------------
    path('projects/',                                          views.project_list,          name='project_list'),
    path('projects/create/',                                   views.project_create,        name='project_create'),       # PM only
    path('projects/<str:project_id>/',                         views.project_detail,        name='project_detail'),
    path('projects/<str:project_id>/delete/',                  views.project_delete,        name='project_delete'),        # Admin only, POST only
    path('projects/<str:project_id>/edit/',                    views.project_edit,          name='project_edit'),          # PM only, Draft status only
    path('projects/<str:project_id>/fields/edit/',             views.project_field_edit,    name='project_field_edit'),    # PM/Coordinator, non-Draft only — capacity/contract/target date
    path('projects/<str:project_id>/activate/',                views.project_activate,           name='project_activate'),           # PM only, POST only — creates phases/tasks/milestones
    path('projects/<str:project_id>/recalculate-dates/',       views.project_recalculate_dates,  name='project_recalculate_dates'),  # PM only, POST only
    path('projects/<str:project_id>/tasks/add/',               views.task_add,              name='task_add'),              # PM only
    path('projects/<str:project_id>/tasks/<int:task_id>/update/',        views.task_status_update,        name='task_status_update'),        # Assigned role or PM
    path('projects/<str:project_id>/tasks/<int:task_id>/detail-status/', views.task_detail_status_update, name='task_detail_status_update'), # Assigned user only
    path('projects/<str:project_id>/tasks/<int:task_id>/assign/',   views.task_assign,        name='task_assign'),        # PM only
    path('projects/<str:project_id>/tasks/<int:task_id>/assign-design/', views.task_assign_design_head, name='task_assign_design_head'),  # Design Head only (is_design_head flag)
    path('projects/<str:project_id>/tasks/<int:task_id>/due-date/', views.task_set_due_date,         name='task_set_due_date'),         # PM + role-owners, triggers cascade recalculation for PM only
    path('projects/<str:project_id>/enable-cascade/',              views.enable_cascade_scheduling,  name='enable_cascade_scheduling'), # PM only, irreversible, POST only
    path('projects/<str:project_id>/coordinators/',                views.assign_coordinators,        name='assign_coordinators'),        # PM or coordinator; manages coordinators M2M

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
    # PART 11 — OPEX BOQ entry. A separate screen, not a mode of boq_detail: the OPEX
    # catalogue is 207 items and is searched and added to, where the 37-item Residential
    # one is pre-populated. 404s on a Residential site; boq_detail redirects the OPEX
    # designer here, and everyone else on an OPEX site still reads the sheet on boq_detail.
    path('projects/<str:project_id>/boq/entry/',             views.opex_boq_entry,       name='opex_boq_entry'),        # OPEX Design only
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
    # Checklist completion — on the task detail page (role-match or PM/coordinator).
    # Item authoring/assignment lives under portal-admin/ (see below).
    # ---------------------------------------------------------------------------
    path('projects/<str:project_id>/tasks/<int:task_id>/checklist/<int:item_id>/complete/',
         views.checklist_item_complete, name='checklist_item_complete'),              # Role-match or PM/coordinator

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
    path('portal-admin/projects/',                      views.admin_project_list,        name='admin_project_list'),
    path('portal-admin/projects/<str:project_id>/assign-pm/', views.admin_assign_pm,      name='admin_assign_pm'),
    path('portal-admin/task-durations/',                views.admin_task_durations,      name='admin_task_durations'),
    path('portal-admin/users/<int:user_id>/reset-password/', views.admin_reset_password, name='admin_reset_password'),

    # Reusable Checklists — author name + items, assign to (task_name, project_type). Admin only.
    path('portal-admin/checklists/',                    views.admin_checklists,          name='admin_checklists'),
    path('portal-admin/checklists/create/',             views.admin_checklist_create,    name='admin_checklist_create'),
    path('portal-admin/checklists/<int:checklist_id>/',        views.admin_checklist_edit,   name='admin_checklist_edit'),
    path('portal-admin/checklists/<int:checklist_id>/update/', views.admin_checklist_update, name='admin_checklist_update'),
    path('portal-admin/checklists/<int:checklist_id>/delete/', views.admin_checklist_delete, name='admin_checklist_delete'),
    path('portal-admin/checklists/<int:checklist_id>/items/add/',                    views.admin_checklist_item_add,    name='admin_checklist_item_add'),
    path('portal-admin/checklists/<int:checklist_id>/items/<int:item_id>/edit/',     views.admin_checklist_item_edit,   name='admin_checklist_item_edit'),
    path('portal-admin/checklists/<int:checklist_id>/items/<int:item_id>/delete/',   views.admin_checklist_item_delete, name='admin_checklist_item_delete'),
    path('portal-admin/checklists/<int:checklist_id>/items/<int:item_id>/move/',     views.admin_checklist_item_move,   name='admin_checklist_item_move'),
    path('portal-admin/checklists/<int:checklist_id>/links/add/',                    views.admin_checklist_link_add,    name='admin_checklist_link_add'),
    path('portal-admin/checklists/<int:checklist_id>/links/<int:link_id>/delete/',   views.admin_checklist_link_delete, name='admin_checklist_link_delete'),

    # BOQ Item Master — catalogue BOQ line items reference. Deactivate only, no delete. Admin only.
    path('portal-admin/boq-items/',                        views.admin_boq_items,       name='admin_boq_items'),
    path('portal-admin/boq-items/add/',                    views.admin_boq_item_create, name='admin_boq_item_create'),
    path('portal-admin/boq-items/<int:item_id>/edit/',     views.admin_boq_item_edit,   name='admin_boq_item_edit'),
    path('portal-admin/boq-items/<int:item_id>/toggle/',   views.admin_boq_item_toggle, name='admin_boq_item_toggle'),

    # Redirect old activity-log URL to the new audit-log screen
    path('portal/activity-log/',
         RedirectView.as_view(pattern_name='admin_audit_log', permanent=False),
         name='portal_activity_log_redirect'),

    # ---------------------------------------------------------------------------
    # Sub Admin Panel — sub-admin/ prefix (Sub Admin role only)
    # ---------------------------------------------------------------------------
    path('sub-admin/projects/',      views.subadmin_projects,      name='subadmin_projects'),
    path('sub-admin/task-durations/', views.subadmin_task_durations, name='subadmin_task_durations'),
    path('sub-admin/departments/',   views.subadmin_departments,    name='subadmin_departments'),

    # ---------------------------------------------------------------------------
    # Task drill-down — due-date filters for PM, Design, SCM dashboards
    # ---------------------------------------------------------------------------
    # ---------------------------------------------------------------------------
    # My Documents — personal record-keeping sub-page
    # ---------------------------------------------------------------------------
    path('profile/documents/',    views.my_documents,    name='my_documents'),
    path('profile/change-password/', views.change_password, name='change_password'),
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
