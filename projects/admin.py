from django.contrib import admin
from .models import Project, Milestone, ProjectDocument, ProjectPhase, Task, UserProfile


class MilestoneInline(admin.TabularInline):
    model = Milestone
    extra = 1
    fields = ['title', 'due_date', 'status', 'assigned_to']


class DocumentInline(admin.TabularInline):
    model = ProjectDocument
    extra = 1
    fields = ['doc_type', 'title', 'file']


class PhaseInline(admin.TabularInline):
    model = ProjectPhase
    extra = 0
    fields = ['phase_order', 'phase_name']
    readonly_fields = ['created_at']
    show_change_link = True


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display    = ['project_id', 'customer_name', 'project_type', 'status',
                       'city', 'capacity_kw', 'contract_value', 'assigned_pm', 'assigned_site_engineer']
    list_filter     = ['project_type', 'status', 'city', 'state']
    search_fields   = ['project_id', 'customer_name', 'customer_phone', 'zoho_crm_id']
    readonly_fields = ['project_id', 'created_at', 'activated_at']
    inlines         = [PhaseInline, MilestoneInline, DocumentInline]

    fieldsets = (
        ('Project Info', {
            'fields': ('project_id', 'project_type', 'status', 'assigned_pm', 'assigned_site_engineer', 'created_by')
        }),
        ('Customer Details', {
            'fields': ('customer_name', 'customer_phone', 'customer_email',
                       'site_address', 'city', 'state')
        }),
        ('Technical & Financial', {
            'fields': ('capacity_kw', 'contract_value')
        }),
        ('Dates', {
            'fields': ('survey_date', 'target_commissioning_date', 'activated_at', 'created_at')
        }),
        ('External', {
            'fields': ('zoho_crm_id',),
            'classes': ('collapse',),
        }),
    )


@admin.register(ProjectPhase)
class ProjectPhaseAdmin(admin.ModelAdmin):
    list_display = ['project', 'phase_order', 'phase_name', 'created_at']
    list_filter  = ['project']


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ['task_name', 'phase', 'assigned_role', 'status', 'due_date', 'completed_at']
    list_filter  = ['assigned_role', 'status']
    search_fields = ['task_name']


@admin.register(Milestone)
class MilestoneAdmin(admin.ModelAdmin):
    list_display = ['project', 'title', 'status', 'due_date', 'assigned_to']
    list_filter  = ['status']


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'role', 'phone_number', 'is_active', 'created_by']
    list_filter  = ['role', 'is_active']
    search_fields = ['user__username', 'user__first_name', 'user__last_name']
