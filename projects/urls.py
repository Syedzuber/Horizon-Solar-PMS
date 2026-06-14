from django.urls import path
from . import views

urlpatterns = [
    # Auth
    path('login/',  views.login_view,  name='login'),
    path('logout/', views.logout_view, name='logout'),

    # Dashboards
    path('dashboard/admin/',         views.dashboard_admin,         name='dashboard_admin'),
    path('dashboard/pm/',            views.dashboard_pm,            name='dashboard_pm'),
    path('dashboard/site-engineer/', views.dashboard_site_engineer, name='dashboard_site_engineer'),
    path('dashboard/design/',        views.dashboard_design,        name='dashboard_design'),
    path('dashboard/finance/',       views.dashboard_finance,       name='dashboard_finance'),
    path('dashboard/scm/',           views.dashboard_scm,           name='dashboard_scm'),
    path('dashboard/ceo/',           views.dashboard_ceo,           name='dashboard_ceo'),
    path('dashboard/bd/',            views.dashboard_bd,            name='dashboard_bd'),

    # User management
    path('users/',                    views.user_list,   name='user_list'),
    path('users/create/',             views.user_create, name='user_create'),
    path('users/<int:user_id>/edit/', views.user_edit,   name='user_edit'),

    # Projects
    path('projects/',                                          views.project_list,          name='project_list'),
    path('projects/create/',                                   views.project_create,        name='project_create'),
    path('projects/<str:project_id>/',                         views.project_detail,        name='project_detail'),
    path('projects/<str:project_id>/edit/',                    views.project_edit,          name='project_edit'),
    path('projects/<str:project_id>/activate/',                views.project_activate,           name='project_activate'),
    path('projects/<str:project_id>/recalculate-dates/',       views.project_recalculate_dates,  name='project_recalculate_dates'),
    path('projects/<str:project_id>/tasks/add/',               views.task_add,              name='task_add'),
    path('projects/<str:project_id>/tasks/<int:task_id>/update/',   views.task_status_update, name='task_status_update'),
    path('projects/<str:project_id>/tasks/<int:task_id>/assign/',   views.task_assign,        name='task_assign'),
    path('projects/<str:project_id>/tasks/<int:task_id>/due-date/', views.task_set_due_date,  name='task_set_due_date'),

    # Vendors
    path('vendors/',                              views.vendor_list,          name='vendor_list'),
    path('vendors/add/',                          views.vendor_add,           name='vendor_add'),
    path('vendors/<int:vendor_id>/edit/',         views.vendor_edit,          name='vendor_edit'),
    path('vendors/<int:vendor_id>/toggle-status/', views.vendor_toggle_status, name='vendor_toggle_status'),

    # Notifications
    path('notifications/', views.notifications_view, name='notifications'),

    # BOQ
    path('projects/<str:project_id>/boq/',                   views.boq_detail,           name='boq_detail'),
    path('projects/<str:project_id>/boq/submit/',            views.boq_submit,           name='boq_submit'),
    path('projects/<str:project_id>/boq/acknowledge/',       views.boq_acknowledge,      name='boq_acknowledge'),
    path('projects/<str:project_id>/boq/request-revision/',  views.boq_request_revision, name='boq_request_revision'),
    path('projects/<str:project_id>/boq/history/',           views.boq_history,          name='boq_history'),

    # Payment milestones
    path('projects/<str:project_id>/milestone/<int:milestone_pk>/invoice/', views.milestone_invoice, name='milestone_invoice'),
    path('projects/<str:project_id>/milestone/<int:milestone_pk>/receive/', views.milestone_receive, name='milestone_receive'),
    path('projects/<str:project_id>/milestones/create/',                    views.milestone_create,  name='milestone_create'),

    # Project overview
    path('projects/<str:project_id>/overview/', views.project_overview, name='project_overview'),

    # Webhooks
    path('webhooks/zoho/deal-closed/', views.zoho_deal_closed_webhook, name='zoho_webhook'),

    # Task detail
    path('projects/<str:project_id>/tasks/<int:task_id>/',
         views.task_detail, name='task_detail'),

    # File uploads — project documents
    path('projects/<str:project_id>/documents/upload/',
         views.upload_project_document, name='upload_project_document'),
    path('projects/<str:project_id>/documents/<int:doc_pk>/delete/',
         views.delete_project_document, name='delete_project_document'),

    # File uploads — task attachments
    path('projects/<str:project_id>/tasks/<int:task_id>/attachments/upload/',
         views.upload_task_attachment, name='upload_task_attachment'),
    path('projects/<str:project_id>/tasks/<int:task_id>/attachments/<int:attach_pk>/delete/',
         views.delete_task_attachment, name='delete_task_attachment'),

    # Issues
    path('projects/<str:project_id>/issues/create/',
         views.create_project_issue, name='create_project_issue'),
    path('projects/<str:project_id>/tasks/<int:task_id>/issues/create/',
         views.create_task_issue, name='create_task_issue'),
    path('issues/<int:issue_id>/',
         views.issue_detail, name='issue_detail'),
    path('issues/<int:issue_id>/update-status/',
         views.update_issue_status, name='update_issue_status'),
    path('issues/<int:issue_id>/resolve/',
         views.resolve_issue, name='resolve_issue'),
    path('issues/<int:issue_id>/close/',
         views.close_issue, name='close_issue'),
    path('issues/<int:issue_id>/reopen/',
         views.reopen_issue, name='reopen_issue'),
    path('issues/<int:issue_id>/assign/',
         views.assign_issue, name='assign_issue'),
]
