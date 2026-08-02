from django.contrib import admin
from django.utils import timezone
from .models import (
    Project, Milestone, ProjectDocument, ProjectPhase, Task, UserProfile,
    NotificationLog, SystemSettings,
    Checklist, ChecklistItem, ChecklistTaskLink, ChecklistItemCompletion,
    Program,
    DesignAssignment, DueDateCommitment, DesignAttempt, ArkaSubmission,
    DesignFile, DesignChangeRequest,
)


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


@admin.register(Program)
class ProgramAdmin(admin.ModelAdmin):
    list_display  = ['name', 'program_type', 'client_name', 'status',
                     'short_tender_code', 'planned_site_count', 'is_deleted']
    list_filter   = ['program_type', 'status', 'is_deleted']
    search_fields = ['name', 'client_name', 'short_tender_code', 'tender_reference_number']
    readonly_fields = ['created_at', 'updated_at', 'deleted_at']
    fieldsets = (
        ('Program', {
            'fields': ('program_type', 'name', 'client_name', 'status',
                       'short_tender_code', 'total_capacity',
                       'expected_completion_date', 'planned_site_count')
        }),
        ('OPEX Tender', {
            'fields': ('tender_reference_number', 'bid_value', 'award_date',
                       'ppa_reference', 'ppa_signed_date', 'ppa_per_unit_rate',
                       'ppa_escalation_percentage', 'ppa_escalation_frequency'),
            'classes': ('collapse',),
        }),
        ('CAPEX Financing', {
            'fields': ('financing_partner_name', 'financing_assistance_type'),
            'classes': ('collapse',),
        }),
        ('Meta', {
            'fields': ('created_by', 'created_at', 'updated_at', 'is_deleted', 'deleted_at'),
            'classes': ('collapse',),
        }),
    )


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display    = ['project_id', 'customer_name', 'project_type', 'status',
                       'city', 'capacity_kw', 'contract_value', 'assigned_pm', 'is_deleted']
    list_filter     = ['project_type', 'status', 'city', 'state', 'is_deleted']
    search_fields   = ['project_id', 'customer_name', 'customer_phone', 'zoho_crm_id']
    readonly_fields = ['project_id', 'created_at', 'activated_at', 'deleted_at']
    inlines         = [PhaseInline, MilestoneInline, DocumentInline]
    actions         = ['soft_delete_selected', 'restore_selected']

    fieldsets = (
        ('Project Info', {
            'fields': ('project_id', 'project_type', 'status', 'assigned_pm', 'created_by')
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
        ('Deletion', {
            'fields': ('is_deleted', 'deleted_at'),
            'classes': ('collapse',),
        }),
    )

    def get_queryset(self, request):
        # Show only active projects by default; admin can filter by is_deleted to see deleted ones
        qs = super().get_queryset(request)
        if not request.GET.get('is_deleted__exact'):
            return qs.filter(is_deleted=False)
        return qs

    def get_actions(self, request):
        # Remove the built-in "Delete selected" action — its cascade-warning page is misleading
        # for a soft delete; use the explicit actions below instead.
        actions = super().get_actions(request)
        actions.pop('delete_selected', None)
        return actions

    def delete_model(self, request, obj):
        # Called when admin clicks "Delete" on the change form — soft-delete instead of hard
        obj.is_deleted = True
        obj.deleted_at = timezone.now()
        obj.save(update_fields=['is_deleted', 'deleted_at'])

    @admin.action(description='Soft delete selected projects')
    def soft_delete_selected(self, request, queryset):
        updated = queryset.filter(is_deleted=False).update(
            is_deleted=True,
            deleted_at=timezone.now(),
        )
        self.message_user(request, f'{updated} project(s) soft-deleted.')

    @admin.action(description='Restore selected projects')
    def restore_selected(self, request, queryset):
        updated = queryset.filter(is_deleted=True).update(
            is_deleted=False,
            deleted_at=None,
        )
        self.message_user(request, f'{updated} project(s) restored.')


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
    list_display  = ['user', 'role', 'phone_number', 'is_active', 'is_design_head', 'is_design_qc', 'whatsapp_notifications', 'email_notifications', 'created_by']
    list_filter   = ['role', 'is_active', 'is_design_head', 'is_design_qc']
    search_fields = ['user__username', 'user__first_name', 'user__last_name']


@admin.register(NotificationLog)
class NotificationLogAdmin(admin.ModelAdmin):
    list_display  = ['created_at', 'recipient', 'channel', 'status', 'template_name', 'related_project', 'actor']
    list_filter   = ['channel', 'status']
    search_fields = ['recipient__user__username', 'template_name', 'message']
    readonly_fields = ['recipient', 'channel', 'status', 'message', 'template_name',
                       'related_project', 'actor', 'error_detail', 'created_at']
    ordering      = ['-created_at']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class ChecklistItemInline(admin.TabularInline):
    model = ChecklistItem
    extra = 1
    fields = ['order', 'label']


class ChecklistTaskLinkInline(admin.TabularInline):
    model = ChecklistTaskLink
    extra = 1
    fields = ['task_name', 'project_type']


@admin.register(Checklist)
class ChecklistAdmin(admin.ModelAdmin):
    list_display  = ['name', 'is_active', 'created_by', 'created_at']
    list_filter   = ['is_active']
    search_fields = ['name']
    inlines       = [ChecklistItemInline, ChecklistTaskLinkInline]


@admin.register(ChecklistTaskLink)
class ChecklistTaskLinkAdmin(admin.ModelAdmin):
    list_display = ['task_name', 'project_type', 'checklist']
    list_filter  = ['project_type']
    search_fields = ['task_name']


@admin.register(ChecklistItemCompletion)
class ChecklistItemCompletionAdmin(admin.ModelAdmin):
    list_display  = ['item', 'task', 'is_checked', 'checked_by', 'checked_at']
    list_filter   = ['is_checked']
    readonly_fields = ['item', 'task', 'checked_by', 'checked_at', 'created_at']


@admin.register(SystemSettings)
class SystemSettingsAdmin(admin.ModelAdmin):
    list_display = ['whatsapp_enabled', 'email_enabled']

    def has_add_permission(self, request):
        # Only allow one row via get_or_create; block direct admin creation
        return not SystemSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


# ---------------------------------------------------------------------------
# OPEX Design Module — Part 1
#
# Registered for shell-and-admin VERIFICATION only; these are not user-facing
# screens. Audit-trail models (attempts, Arka versions, files, change requests)
# are append-only records of what happened, so their historical fields are
# read-only here — the admin must not be a side door that rewrites design
# history. Nothing below performs a status transition.
# ---------------------------------------------------------------------------

@admin.register(DesignAssignment)
class DesignAssignmentAdmin(admin.ModelAdmin):
    list_display  = ['project', 'status', 'assigned_to', 'current_attempt_number',
                     'released_at', 'updated_at']
    list_filter   = ['status']
    search_fields = ['project__project_id', 'project__customer_name',
                     'assigned_to__user__username']
    raw_id_fields = ['project']
    readonly_fields = ['current_attempt_number', 'created_at', 'updated_at']


@admin.register(DueDateCommitment)
class DueDateCommitmentAdmin(admin.ModelAdmin):
    list_display  = ['assignment', 'proposed_date', 'proposed_by', 'approved_by',
                     'approved_at', 'is_current']
    list_filter   = ['is_current']
    search_fields = ['assignment__project__project_id']
    raw_id_fields = ['assignment']
    readonly_fields = ['proposed_at']


@admin.register(DesignAttempt)
class DesignAttemptAdmin(admin.ModelAdmin):
    list_display  = ['assignment', 'attempt_number', 'opened_reason', 'qc_verdict', 'head_verdict',
                     'qc_started_at', 'qc_reviewed_at', 'closed_at']
    list_filter   = ['opened_reason', 'qc_verdict', 'head_verdict', 'head_overturned_qc']
    search_fields = ['assignment__project__project_id']
    raw_id_fields = ['assignment']
    readonly_fields = ['opened_at']


@admin.register(ArkaSubmission)
class ArkaSubmissionAdmin(admin.ModelAdmin):
    list_display  = ['attempt', 'version', 'capacity_kw', 'verdict', 'head_verdict',
                     'submitted_by', 'reviewed_by', 'head_reviewed_by', 'is_current']
    list_filter   = ['verdict', 'head_verdict', 'head_overturned_qc', 'is_current']
    search_fields = ['attempt__assignment__project__project_id']
    raw_id_fields = ['attempt']
    readonly_fields = ['submitted_at']


@admin.register(DesignFile)
class DesignFileAdmin(admin.ModelAdmin):
    list_display  = ['attempt', 'kind', 'version', 'derived_from_arka',
                     'uploaded_by', 'uploaded_at', 'is_current']
    list_filter   = ['kind', 'is_current']
    search_fields = ['attempt__assignment__project__project_id', 'original_filename', 'path']
    raw_id_fields = ['attempt', 'derived_from_arka', 'superseded_by']
    # bucket/path identify a stored object; rewriting them here would orphan the file.
    readonly_fields = ['bucket', 'path', 'original_filename', 'size_bytes',
                       'content_type', 'uploaded_at']


@admin.register(DesignChangeRequest)
class DesignChangeRequestAdmin(admin.ModelAdmin):
    # Part 4.6 — `verdict` leads, because `resulting_attempt` is no longer a proxy for
    # "resolved": a rejected request is settled with that column still empty.
    list_display  = ['attempt', 'requested_by', 'requested_at', 'verdict',
                     'decided_by', 'decided_at', 'resulting_attempt']
    list_filter   = ['verdict']
    search_fields = ['attempt__assignment__project__project_id', 'reason',
                     'rejection_reason']
    raw_id_fields = ['attempt', 'resulting_attempt', 'decided_by']
    readonly_fields = ['requested_at']
