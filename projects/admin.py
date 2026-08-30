from django.contrib import admin
from django.utils import timezone
from .models import (
    Project, Milestone, ProjectDocument, ProjectPhase, Task, UserProfile,
    NotificationLog, SystemSettings,
    Checklist, ChecklistItem, ChecklistTaskLink, ChecklistItemCompletion,
    Program,
    DesignAssignment, DueDateCommitment, DesignAttempt, ArkaSubmission,
    DesignFile, DesignChangeRequest,
    TaskTemplate, TaskTemplatePhase, TaskTemplateTask,
)
from .utils import assign_task_to


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
    # DO NOT REMOVE — R-10. `status` is deliberately read-only here, and an unhelpfully
    # read-only field is exactly what a future maintainer will want to delete.
    #
    # Same reason as TaskAdmin below, plus one this admin has and that one does not.
    #
    # First: every project status change must go through the view layer so
    # record_transition() writes the StatusTransition row in the same transaction (R-2).
    # ModelAdmin saves the form field straight to the column, so an admin edit would move
    # the project and leave no ledger row — and a gap in the ledger cannot be
    # reconstructed afterwards.
    #
    # Second, and worse: ACTIVATION IS A VIEW-LAYER ACTION AND THIS ADMIN IS NOT AN
    # ACTIVATION ROUTE. project_activate() is the only path that attaches the phase and
    # task template and stamps activated_at. Typing 'Active' into this form did none of
    # that — it left the project Active and empty, a state the product itself cannot
    # produce, with nothing raising. An admin who cannot set status also cannot activate
    # a project here, and that is the correct outcome, not a lost capability.
    #
    # Reading `status` is still fine: it stays in list_display and list_filter, which are
    # read paths. It must never appear in list_editable, which writes past this.
    #
    # On the ADD form Django omits readonly fields entirely, so a new project takes the
    # model default, Project.status's 'Draft' — the same value project_create gives it.
    readonly_fields = ['project_id', 'status', 'created_at', 'activated_at', 'deleted_at']
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
    # DO NOT REMOVE — R-10. `status` is deliberately read-only here, and an unhelpfully
    # read-only field is exactly what a future maintainer will want to delete.
    #
    # Every task status change must go through the view layer so record_transition()
    # writes the StatusTransition row in the same transaction (R-2). ModelAdmin saves the
    # form field straight to the column, so an admin edit would move the task and leave no
    # ledger row — and a gap in the ledger cannot be reconstructed afterwards. Reading
    # `status` is still fine: it stays in list_display and list_filter, which are read
    # paths. It must never appear in list_editable, which writes past this.
    #
    # On the ADD form Django omits readonly fields entirely, so a new task takes the model
    # default, Task.NOT_STARTED ('Not Started') — the same value it would have had.
    readonly_fields = ['status']

    def save_model(self, request, obj, form, change):
        """Route Task.assigned_to through the assignment chokepoint.

        The admin change form writes every field at once, so this saves the row
        with assigned_to at its previous value and lets assign_task_to() apply
        the new one. Net DB state is identical; the point is that the admin
        stops being the one path that skips the chokepoint — so the reminders
        session's assigned_at stamping will fire here too.

        ModelAdmin.save_model is not a model save() override. The architecture
        ban covers Model.save() and signals, which fire implicitly on every
        write; this is an explicit, admin-only interception of one field.

        notify is False: the admin sends nothing today and must keep doing so.
        """
        if 'assigned_to' not in form.changed_data:
            super().save_model(request, obj, form, change)
            return

        new_assignee = obj.assigned_to
        # On add there is no previous value; on change, form.initial holds the pk.
        obj.assigned_to_id = form.initial.get('assigned_to') if change else None
        super().save_model(request, obj, form, change)
        assign_task_to(obj, new_assignee, notify=False)


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
    """Items are CONTENT of a checklist version (R-7) — editable only while it is a
    draft. Same rule, and the same reason, as TaskTemplatePhase/TaskTemplateTask: the
    model's save() already raises, and these hooks stop the admin offering the form at
    all so a user gets "you may not change this" rather than a 500 on save."""

    model = ChecklistItem
    extra = 1
    fields = ['order', 'label']

    def _is_draft(self, obj):
        return obj is None or obj.is_editable

    def has_add_permission(self, request, obj=None):
        if not self._is_draft(obj):
            return False
        return super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        if not self._is_draft(obj):
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if not self._is_draft(obj):
            return False
        return super().has_delete_permission(request, obj)


class ChecklistTaskLinkInline(admin.TabularInline):
    # NOT version content. The link records which checklist FAMILY is assigned to a task
    # name; status records which version of it is live. Locking it to drafts would make
    # a task's assignment un-editable the moment its checklist went live, and v1 and v2
    # cannot both hold a link to the same task_name — unique_together forbids it.
    model = ChecklistTaskLink
    extra = 1
    fields = ['task_name', 'project_type']


@admin.register(Checklist)
class ChecklistAdmin(admin.ModelAdmin):
    list_display  = ['name', 'code', 'version_no', 'status', 'effective_from',
                     'created_by', 'created_at']
    list_filter   = ['status', 'code']
    search_fields = ['name', 'code']
    # status is set by activate(), which archives the outgoing version in the same
    # transaction. Editing it here would activate a version without archiving its
    # predecessor and hit the partial unique constraint as an IntegrityError.
    readonly_fields = ['status', 'created_at']
    inlines       = [ChecklistItemInline, ChecklistTaskLinkInline]

    def has_change_permission(self, request, obj=None):
        # The version's OWN row (its name, effective_from) is editable while draft.
        if obj is not None and not obj.is_editable:
            return False
        return super().has_change_permission(request, obj)


@admin.register(ChecklistTaskLink)
class ChecklistTaskLinkAdmin(admin.ModelAdmin):
    list_display = ['task_name', 'project_type', 'checklist']
    list_filter  = ['project_type']
    search_fields = ['task_name']


@admin.register(ChecklistItemCompletion)
class ChecklistItemCompletionAdmin(admin.ModelAdmin):
    # R-8 read path: the column shows the text that was ANSWERED, not the item's current
    # label. 'item' used to sit here and rendered ChecklistItem.__str__ — which reworded
    # itself under the reader, and renders as None once the item has been deleted.
    list_display  = ['answered_text', 'task', 'is_checked', 'checked_by', 'checked_at']
    list_filter   = ['is_checked']
    readonly_fields = ['item', 'item_text_snapshot', 'task', 'checked_by', 'checked_at',
                       'created_at']

    @admin.display(description='Answered text')
    def answered_text(self, obj):
        # Falls back to the live label only for a row that was never checked and so
        # never took a snapshot.
        return obj.item_text_snapshot or (obj.item.label if obj.item_id else '—')


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
    readonly_fields = ['current_attempt_number', 'survey_link_added_at',
                       'created_at', 'updated_at']


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


# ---------------------------------------------------------------------------
# Versioned task templates (R-7)
#
# The only authoring surface for a template until a real UI exists (phase 1 at the
# earliest). Everything here refuses to edit a version that is not a draft, so the
# admin cannot walk into the TemplateVersionLocked guard on the models and turn it
# into a 500. Editing an active template means adding version+1 as a draft, changing
# that, and activating it.
# ---------------------------------------------------------------------------


class _DraftOnlyContentAdmin(admin.ModelAdmin):
    """Shared authority rules for template CONTENT (phases and tasks).

    Content of an active or archived version is immutable (R-7). The model's save()
    already raises; these two hooks stop the admin from offering the form at all, so a
    user gets "you may not change this" rather than a server error on save.
    """

    def _template_of(self, obj):
        raise NotImplementedError

    def has_change_permission(self, request, obj=None):
        if obj is not None and not self._template_of(obj).is_editable:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and not self._template_of(obj).is_editable:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(TaskTemplate)
class TaskTemplateAdmin(admin.ModelAdmin):
    list_display  = ['code', 'version_no', 'label', 'project_type', 'status',
                     'effective_from', 'created_by', 'created_at']
    list_filter   = ['code', 'project_type', 'status']
    search_fields = ['code', 'label']
    # status is set by activate(), which archives the outgoing version in the same
    # transaction. Editing it here would activate a version without archiving its
    # predecessor and hit the partial unique constraint as an IntegrityError.
    readonly_fields = ['status', 'created_at']

    def has_change_permission(self, request, obj=None):
        # The version's OWN row (its label, effective_from) is editable while draft.
        if obj is not None and not obj.is_editable:
            return False
        return super().has_change_permission(request, obj)


@admin.register(TaskTemplatePhase)
class TaskTemplatePhaseAdmin(_DraftOnlyContentAdmin):
    list_display  = ['template', 'sort_order', 'code', 'label']
    list_filter   = ['template']
    search_fields = ['code', 'label']
    raw_id_fields = ['template']

    def _template_of(self, obj):
        return obj.template


@admin.register(TaskTemplateTask)
class TaskTemplateTaskAdmin(_DraftOnlyContentAdmin):
    list_display  = ['phase', 'sort_order', 'label', 'assigned_role', 'task_type',
                     'duration_days', 'is_payment_milestone']
    list_filter   = ['phase__template', 'assigned_role', 'task_type', 'is_payment_milestone']
    search_fields = ['code', 'label']
    raw_id_fields = ['phase']

    def _template_of(self, obj):
        return obj.phase.template
