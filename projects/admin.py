from django.contrib import admin
from .models import Project, Milestone, ProjectDocument


class MilestoneInline(admin.TabularInline):
    model = Milestone
    extra = 3
    fields = ['title', 'due_date', 'status', 'assigned_to']


class DocumentInline(admin.TabularInline):
    model = ProjectDocument
    extra = 1
    fields = ['doc_type', 'title', 'file']


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display    = ['project_code', 'name', 'project_type', 'status',
                       'customer_name', 'city', 'capacity_kwp', 'contract_value', 'project_manager']
    list_filter     = ['project_type', 'status', 'city', 'subsidy_applicable']
    search_fields   = ['project_code', 'name', 'customer_name', 'customer_phone']
    readonly_fields = ['created_at', 'updated_at']
    inlines         = [MilestoneInline, DocumentInline]

    fieldsets = (
        ('Project Info', {
            'fields': ('project_code', 'name', 'project_type', 'status', 'project_manager')
        }),
        ('Customer Details', {
            'fields': ('customer_name', 'customer_phone', 'customer_email',
                       'site_address', 'city', 'state')
        }),
        ('Technical', {
            'fields': ('capacity_kwp', 'panel_count', 'inverter_type')
        }),
        ('Financial', {
            'fields': ('contract_value', 'amount_received')
        }),
        ('Subsidy (PM Suryaghar)', {
            'fields': ('subsidy_applicable', 'subsidy_amount', 'subsidy_status'),
            'classes': ('collapse',)
        }),
        ('Dates', {
            'fields': ('start_date', 'expected_completion', 'actual_completion')
        }),
        ('Notes', {
            'fields': ('notes', 'created_at', 'updated_at')
        }),
    )


@admin.register(Milestone)
class MilestoneAdmin(admin.ModelAdmin):
    list_display = ['project', 'title', 'status', 'due_date', 'assigned_to']
    list_filter  = ['status']