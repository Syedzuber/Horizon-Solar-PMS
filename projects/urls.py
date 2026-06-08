from django.urls import path
from . import views

urlpatterns = [
    # Auth
    path('login/',   views.login_view,  name='login'),
    path('logout/',  views.logout_view, name='logout'),

    # Dashboards
    path('dashboard/admin/',          views.dashboard_admin,          name='dashboard_admin'),
    path('dashboard/pm/',             views.dashboard_pm,             name='dashboard_pm'),
    path('dashboard/site-engineer/',  views.dashboard_site_engineer,  name='dashboard_site_engineer'),
    path('dashboard/design/',         views.dashboard_design,         name='dashboard_design'),
    path('dashboard/finance/',        views.dashboard_finance,        name='dashboard_finance'),
    path('dashboard/scm/',            views.dashboard_scm,            name='dashboard_scm'),
    path('dashboard/ceo/',            views.dashboard_ceo,            name='dashboard_ceo'),

    # User management
    path('users/',                views.user_list,   name='user_list'),
    path('users/create/',         views.user_create, name='user_create'),
    path('users/<int:user_id>/edit/', views.user_edit, name='user_edit'),
]
