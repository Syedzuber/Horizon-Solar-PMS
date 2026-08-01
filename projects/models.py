from django.db import models
from django.contrib.auth.models import User


class Project(models.Model):
    """Central record for a solar EPC project. Created from Zoho webhook or by a PM."""

    PROJECT_TYPE_CHOICES = [
        ('Residential', 'Residential'),
        ('OPEX',        'OPEX'),
        ('CAPEX',       'CAPEX'),
    ]

    # Lifecycle: Draft → Active → In Progress → Commissioned (or On Hold / Cancelled)
    STATUS_CHOICES = [
        ('Draft',        'Draft'),
        ('Active',       'Active'),
        ('In Progress',  'In Progress'),
        ('Commissioned', 'Commissioned'),
        ('On Hold',      'On Hold'),
        ('Cancelled',    'Cancelled'),
    ]

    # Widened 20 → 30 to hold the OPEX tender-based format `{short_tender_code}-{site_code}`
    # (e.g. IPGCL26-S045) alongside the legacy auto-generated HRP-{PREFIX}-{YEAR}-{NNN}.
    # Additive/safe — nothing depends on the old width. Two formats coexist by design.
    project_id                = models.CharField(max_length=30, unique=True, editable=False, blank=True)
    # 200 mirrors Program.client_name — an OPEX site auto-copies client_name into this
    # field at creation, so the widths must match or a long client entity name would
    # overflow. (Originally 200, later narrowed to 100; this restores it.) For OPEX the
    # value is the frozen parent-tender client name; for Residential/CAPEX it is the
    # typed customer name.
    customer_name             = models.CharField(max_length=200)
    # For Residential/CAPEX this is the customer's phone; for OPEX sites (created via
    # OpexSiteForm) it is REINTERPRETED as the Site In-Charge phone. Same column, dual
    # meaning by project_type — see OpexSiteForm and opex_site_form.html.
    customer_phone            = models.CharField(max_length=10)
    # Dual meaning like customer_phone: customer email (Residential/CAPEX) vs Site
    # In-Charge email (OPEX).
    customer_email            = models.EmailField(blank=True, null=True)
    site_address              = models.TextField()
    city                      = models.CharField(max_length=50)
    state                     = models.CharField(max_length=50, default='Uttar Pradesh')
    project_type              = models.CharField(max_length=20, choices=PROJECT_TYPE_CHOICES)
    # First and only PARENT FK on Project. Nullable/forward-only: every pre-existing
    # project stays program=null (no backfill). on_delete=PROTECT so a Program can
    # never cascade-delete or orphan its child sites (see Program soft-delete rules).
    # Type-match / Residential-exclusion invariants are enforced in _validate_program_link().
    program                   = models.ForeignKey(
        'Program',
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='sites',
    )
    # OPEX-under-Program only: user-entered, matches the tender's official site list.
    # NOT auto-generated. Combined with Program.short_tender_code to compose project_id.
    site_code                 = models.CharField(max_length=30, null=True, blank=True)
    capacity_kw               = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    contract_value            = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    assigned_pm               = models.ForeignKey(
        'UserProfile',
        limit_choices_to={'role': 'PM'},
        related_name='pm_projects',
        on_delete=models.PROTECT,  # Prevent accidental deletion of a PM who owns projects
        null=True,
        blank=True,
    )
    assigned_design           = models.ForeignKey(
        'UserProfile',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,  # Losing a design user does not block the project
        related_name='design_projects',
        limit_choices_to={'role': 'Design'},
    )
    # Additive-only: Coordinators share the PM's execution authority on this project.
    # This NEVER replaces assigned_pm — see permissions.user_can_manage_project().
    # M2M to UserProfile (matching assigned_pm's type) so ownership comparisons stay
    # on one canonical pattern. Assign via .add()/.remove(), never overwrite assigned_pm.
    coordinators              = models.ManyToManyField(
        'UserProfile',
        related_name='coordinated_projects',
        blank=True,
        limit_choices_to={'role': 'Project Coordinator'},
    )
    survey_date               = models.DateField(blank=True, null=True)
    target_commissioning_date = models.DateField(blank=True, null=True)
    status                    = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Draft')
    zoho_crm_id               = models.CharField(max_length=50, blank=True, null=True)
    zoho_deal_id              = models.CharField(max_length=100, blank=True, default='')  # Stores Zoho Record Id for duplicate webhook guard
    # Originally a Zoho-only field (contact person on the CRM deal). Now ALSO reused as
    # the Site In-Charge Name for OPEX sites created via OpexSiteForm — where the form
    # makes it required even though the model keeps blank=True (Zoho/other paths may omit
    # it). Dual meaning by project_type, same pattern as customer_phone/customer_email.
    customer_contact_person   = models.CharField(max_length=255, blank=True, default='')
    created_at                = models.DateTimeField(auto_now_add=True)
    created_by                = models.ForeignKey(
        User,
        related_name='created_projects',
        on_delete=models.PROTECT,
        null=True,
        blank=True,  # Null when created by Zoho webhook (no authenticated user)
    )
    activated_at              = models.DateTimeField(blank=True, null=True)  # Set when PM activates; used as due-date chain anchor
    commissioned_at           = models.DateField(null=True, blank=True)  # Set when project status changes to Commissioned
    cascade_scheduling        = models.BooleanField(
        default=False,
        help_text="When True, task due dates chain automatically. Cannot be reverted once enabled.",
    )
    is_deleted                = models.BooleanField(default=False)
    deleted_at                = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.project_id} - {self.customer_name}"

    def get_current_phase(self):
        """First phase that still has an incomplete task; works with prefetched data."""
        for phase in self.phases.all():
            for task in phase.tasks.all():
                if task.status != 'Done':
                    return phase.phase_name
        return None

    def _validate_program_link(self):
        """Enforce the Program-linkage invariants at save time (spec §4 edge cases).

        No-op for the vast majority of rows (program is null). When a site IS linked
        to a Program, two rules hold unconditionally, at EVERY save path (form, view,
        webhook, shell), because they live here rather than on any one form:
          • Residential projects may NEVER belong to a Program.
          • A site's project_type must equal its Program's program_type
            (no CAPEX site under an OPEX Program, or vice-versa).
        Raises ValidationError so callers surface a clear message, never an
        IntegrityError. `project_type` is validated, not `program.program_type`,
        against the reused OPEX/CAPEX vocabulary they share.
        """
        if self.program_id is None:
            return
        from django.core.exceptions import ValidationError
        if self.project_type == 'Residential':
            raise ValidationError('Residential projects cannot be linked to a Program.')
        program_type = self.program.program_type
        if self.project_type != program_type:
            raise ValidationError(
                f'Type mismatch: a {self.project_type} site cannot belong to a '
                f'{program_type} Program.'
            )

    def save(self, *args, **kwargs):
        # Program-link invariants run on every save (cheap; short-circuits when program is null)
        self._validate_program_link()
        # Generate project_id only on first save; subsequent saves skip ID generation.
        # NOTE: OPEX sites created under a Program set project_id EXPLICITLY before
        # save() (Program-scoped creation path), so they skip this generator entirely.
        if not self.project_id:
            # import inside method to avoid circular import at module level
            from django.db import transaction
            from .utils import generate_project_id
            with transaction.atomic():
                self.project_id = generate_project_id(self.project_type)
                super().save(*args, **kwargs)
        else:
            super().save(*args, **kwargs)


class Program(models.Model):
    """Parent record grouping multiple sites under one OPEX tender or one multi-site
    CAPEX contract. Each child site remains an ordinary Project (via Project.program)
    running its own independent survey→design→BOQ→execution lifecycle — this model
    only groups and reports; it owns no per-site workflow.

    Forward-only: introduced for newly awarded tenders / signed contracts. Existing
    projects are never backfilled (they stay program=null). Soft-deleted the same way
    as Project (is_deleted + deleted_at), with NO custom manager, so uniqueness checks
    that must include soft-deleted rows keep working (see the OPEX creation path)."""

    # Reuses Project's project_type vocabulary, restricted to OPEX/CAPEX — same casing,
    # same values (Residential never has a Program). This is what the type-match check
    # in Project._validate_program_link compares against directly.
    PROGRAM_TYPE_CHOICES = [
        ('OPEX',  'OPEX'),
        ('CAPEX', 'CAPEX'),
    ]
    # Match Project.STATUS_CHOICES exactly (same title-case, same semantic role).
    STATUS_CHOICES = Project.STATUS_CHOICES

    program_type             = models.CharField(max_length=20, choices=PROGRAM_TYPE_CHOICES)
    name                     = models.CharField(max_length=200)      # e.g. "IPGCL Delhi Tender"
    client_name              = models.CharField(max_length=200)      # Govt subsidiary (OPEX) or commercial client (CAPEX)
    status                   = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Draft')

    # OPEX only, required — the human-entered short code (e.g. IPGCL26) used to compose
    # each site's globally-unique project_id. Globally unique across ALL Programs
    # (incl. soft-deleted) — enforced by explicit, soft-delete-aware validation at the
    # entry form, NOT a DB unique constraint (a partial "unique when non-blank" index
    # can't be expressed cleanly and would raise a raw IntegrityError). Blank for CAPEX.
    short_tender_code        = models.CharField(max_length=20, blank=True, default='')

    # Generic — both OPEX and CAPEX. Storage/comparison targets only, no automation.
    total_capacity           = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text='Total planned capacity in MW (storage only).',
    )
    expected_completion_date = models.DateField(null=True, blank=True)
    planned_site_count       = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='Target site count from the tender/contract. Informational ONLY — '
                  'never enforced as a cap. The live actual count comes from the rollup.',
    )

    # OPEX-specific (nullable; populate only when program_type == 'OPEX').
    tender_reference_number  = models.CharField(max_length=100, blank=True, default='')
    bid_value                = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    award_date               = models.DateField(null=True, blank=True)
    ppa_reference            = models.CharField(max_length=100, blank=True, default='')
    ppa_signed_date          = models.DateField(null=True, blank=True)
    ppa_per_unit_rate        = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    ppa_escalation_percentage = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    ppa_escalation_frequency = models.CharField(max_length=50, blank=True, default='')
    # ^ PPA fields are STORAGE ONLY. No escalation calculation is performed anywhere —
    #   that ownership (PMS vs external billing) is a deferred Finance decision.

    # CAPEX-specific placeholders (nullable; storage only). Full financing/loan design
    # AND logic are deferred entirely to the future CAPEX workflow spec.
    financing_partner_name    = models.CharField(max_length=200, blank=True, default='')
    financing_assistance_type = models.CharField(max_length=100, blank=True, default='')

    created_at               = models.DateTimeField(auto_now_add=True)
    updated_at               = models.DateTimeField(auto_now=True)
    created_by               = models.ForeignKey(
        User, related_name='created_programs', on_delete=models.PROTECT, null=True, blank=True,
    )
    is_deleted               = models.BooleanField(default=False)
    deleted_at               = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        label = self.tender_reference_number if self.program_type == 'OPEX' else self.name
        return f"{label or self.name} ({self.program_type})"

    @property
    def reference_display(self):
        """Human-facing identifier: OPEX shows its tender reference number; CAPEX has
        no formal reference number and shows its name. There is no auto-generated
        program_id by design."""
        if self.program_type == 'OPEX' and self.tender_reference_number:
            return self.tender_reference_number
        return self.name


# ---------------------------------------------------------------------------
# Program rollup — COMPUTE-LIVE only (spec §4: "do not denormalize a counter that
# can silently drift"). Stage = Project.status (the one status vocabulary already on
# every site); grouped in a single query per Program, soft-deleted sites excluded.
# ---------------------------------------------------------------------------

def program_rollup_annotations():
    """Aggregate-queryset annotations for the Program LIST view (avoids N+1 across
    programs — one query annotates every program's live site counts). Yields
    `site_total` plus one `site_<status>` count per Project status. Mirror of
    get_program_rollup so list and detail never disagree."""
    from django.db.models import Count, Q
    anno = {
        'site_total': Count('sites', filter=Q(sites__is_deleted=False), distinct=True),
    }
    for status, _ in Project.STATUS_CHOICES:
        key = 'site_' + status.lower().replace(' ', '_')
        anno[key] = Count(
            'sites',
            filter=Q(sites__is_deleted=False, sites__status=status),
            distinct=True,
        )
    return anno


def get_program_rollup(program):
    """Live site-count-by-stage rollup for ONE Program (detail page — single-object,
    no N+1). Returns {'total': int, 'by_status': {status: count, ...}} covering every
    Project.status value (zero-filled). Excludes soft-deleted sites, so adding,
    removing, or soft-deleting a site is reflected immediately with no stored counter."""
    from django.db.models import Count
    by_status = {status: 0 for status, _ in Project.STATUS_CHOICES}
    total = 0
    rows = (
        program.sites.filter(is_deleted=False)
        .values('status')
        .annotate(n=Count('id'))
    )
    for row in rows:
        by_status[row['status']] = row['n']
        total += row['n']
    return {'total': total, 'by_status': by_status}


class ProjectPhase(models.Model):
    """Ordered phase within a project (e.g. Design, Procurement, Installation)."""

    project     = models.ForeignKey(Project, related_name='phases', on_delete=models.CASCADE)
    phase_name  = models.CharField(max_length=100)
    phase_order = models.PositiveIntegerField()  # Ascending; lower = earlier in the project
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['phase_order']

    def __str__(self):
        return f"{self.project.project_id} — {self.phase_name}"


class Task(models.Model):
    """A single unit of work within a phase, owned by a role and tracked to completion."""

    # Role constants — mirror UserProfile.ROLE_CHOICES where relevant
    PM            = 'PM'
    SITE_ENGINEER = 'Site Engineer'
    FINANCE       = 'Finance'
    SCM           = 'SCM'
    BD            = 'BD / Sales'
    DESIGN        = 'Design'

    ROLE_CHOICES = [
        (PM,            'PM'),
        (SITE_ENGINEER, 'Site Engineer'),
        (FINANCE,       'Finance'),
        (SCM,           'SCM'),
        (BD,            'BD / Sales'),
        (DESIGN,        'Design'),
    ]

    NOT_STARTED = 'Not Started'
    IN_PROGRESS = 'In Progress'
    DONE        = 'Done'
    BLOCKED     = 'Blocked'

    STATUS_CHOICES = [
        (NOT_STARTED, 'Not Started'),
        (IN_PROGRESS, 'In Progress'),
        (DONE,        'Done'),
        (BLOCKED,     'Blocked'),
    ]

    # Internal = work done by Horizon team; External = depends on a third party (DISCOM, customer)
    INTERNAL = 'Internal'
    EXTERNAL = 'External'

    TYPE_CHOICES = [
        (INTERNAL, 'Internal'),
        (EXTERNAL, 'External'),
    ]

    phase                = models.ForeignKey(ProjectPhase, related_name='tasks', on_delete=models.CASCADE)
    task_name            = models.CharField(max_length=200)
    task_order           = models.PositiveIntegerField()  # Ascending within phase; drives due-date chain
    assigned_role        = models.CharField(max_length=20, choices=ROLE_CHOICES, default=PM)
    assigned_to          = models.ForeignKey(
        'UserProfile',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,  # Reassign rather than block when a user is removed
        related_name='assigned_tasks',
    )
    status               = models.CharField(max_length=20, choices=STATUS_CHOICES, default=NOT_STARTED)
    task_type            = models.CharField(max_length=10, choices=TYPE_CHOICES, default=INTERNAL)
    duration_days        = models.PositiveIntegerField(default=1)  # Calendar days used in due-date chain calculation
    due_date             = models.DateField(blank=True, null=True)
    completed_at         = models.DateTimeField(blank=True, null=True)  # Set when status transitions to Done
    blocked_since        = models.DateTimeField(blank=True, null=True)  # Set when status transitions TO 'Blocked'; cleared on un-block so re-blocks re-age from zero
    is_payment_milestone = models.BooleanField(default=False)  # When marked Done, triggers payment_notification to Finance
    created_at           = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['task_order']

    def __str__(self):
        return self.task_name

    @property
    def active_attachment_count(self):
        # Silently returns 0 if attachments relation does not exist yet (e.g. during migration)
        try:
            return self.attachments.filter(is_deleted=False).count()
        except Exception:
            return 0


class Milestone(models.Model):
    """Legacy milestone model — superseded by PaymentMilestone. Kept for schema compatibility."""

    STATUS_CHOICES = [
        ('pending',     'Pending'),
        ('in_progress', 'In Progress'),
        ('completed',   'Completed'),
        ('delayed',     'Delayed'),
    ]

    project        = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='old_milestones')  # related_name='old_milestones' avoids clash with PaymentMilestone
    title          = models.CharField(max_length=200)
    description    = models.TextField(blank=True)
    due_date       = models.DateField(null=True, blank=True)
    completed_date = models.DateField(null=True, blank=True)
    status         = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    assigned_to    = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    order          = models.IntegerField(default=0)

    class Meta:
        ordering = ['order', 'due_date']

    def __str__(self):
        return f"{self.project.project_id} — {self.title}"


class ProjectDocument(models.Model):
    """File (document or photo) attached to a project. Stored in Supabase; soft-deleted here."""

    DOCUMENT = 'Document'
    PHOTO    = 'Photo'
    FILE_TYPE_CHOICES = [(DOCUMENT, 'Document'), (PHOTO, 'Photo')]

    project      = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='documents')
    uploaded_by  = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='project_documents',
    )
    file_name    = models.CharField(max_length=255)
    file_url     = models.URLField(max_length=1000)  # Public Supabase URL for direct browser access
    supabase_path = models.CharField(max_length=500)  # Path within the Supabase bucket; used for deletion
    file_type    = models.CharField(max_length=20, choices=FILE_TYPE_CHOICES)
    file_size_kb = models.PositiveIntegerField(default=0)
    uploaded_at  = models.DateTimeField(auto_now_add=True)
    is_deleted   = models.BooleanField(default=False)  # Soft delete — purge_deleted_files command hard-deletes after FILE_RETENTION_DAYS
    deleted_at   = models.DateTimeField(null=True, blank=True)
    deleted_by   = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='deleted_project_documents',
    )

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return f"{self.project.project_id} — {self.file_name}"


class DueDateChangeLog(models.Model):
    """Audit trail entry written every time a task's due_date changes."""

    task       = models.ForeignKey('Task', on_delete=models.CASCADE, related_name='due_date_changes')
    old_date   = models.DateField(null=True, blank=True)
    new_date   = models.DateField(null=True, blank=True)
    changed_by = models.ForeignKey('UserProfile', on_delete=models.SET_NULL, null=True)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-changed_at']

    def __str__(self):
        return f"{self.task.task_name}: {self.old_date} → {self.new_date}"


class ProjectFieldEditLog(models.Model):
    """Audit trail for post-activation edits to a project's business fields.

    One row is written per CHANGED field each time a PM/Coordinator edits a
    non-Draft (Activated+) project's capacity, contract value, or target
    commissioning date via the post-activation field-edit endpoint. No-op edits
    (new == old) are skipped by the view, so this table records real changes only.

    Values are stored as text so a single log uniformly diffs decimals and dates.
    Mirrors DueDateChangeLog: actor -> UserProfile, on_delete=SET_NULL,
    auto_now_add timestamp, newest-first ordering.
    """

    CAPACITY_KW               = 'capacity_kw'
    CONTRACT_VALUE            = 'contract_value'
    TARGET_COMMISSIONING_DATE = 'target_commissioning_date'
    # Field names deliberately match Project field names so the view can getattr()
    # old/new values by iterating these choices.
    FIELD_CHOICES = [
        (CAPACITY_KW,               'Capacity (kW)'),
        (CONTRACT_VALUE,            'Contract Value'),
        (TARGET_COMMISSIONING_DATE, 'Target Commissioning Date'),
    ]

    project    = models.ForeignKey('Project', on_delete=models.CASCADE, related_name='field_edit_logs')
    field_name = models.CharField(max_length=32, choices=FIELD_CHOICES)
    old_value  = models.TextField(blank=True, default='')  # text for uniform diffing across types
    new_value  = models.TextField(blank=True, default='')
    edited_by  = models.ForeignKey('UserProfile', on_delete=models.SET_NULL, null=True)
    edited_at  = models.DateTimeField(auto_now_add=True)
    reason     = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-edited_at']

    def __str__(self):
        return f"{self.project.project_id} {self.field_name}: {self.old_value} → {self.new_value}"


class UserProfile(models.Model):
    """Extends Django's User with a role and phone number. One profile per user."""

    ROLE_CHOICES = [
        ('Admin',               'Admin'),
        ('System Admin',        'System Admin'),
        ('PM',                  'PM'),
        ('Project Coordinator', 'Project Coordinator'),
        ('Site Engineer',       'Site Engineer'),
        ('Design',        'Design'),
        # 'Design Head' WAS HERE (added Part 1, migration 0048) AND WAS DELIBERATELY
        # REMOVED (Part 6.5b, migration 0053). Do not add it back without reading
        # DESIGN_HEAD_ROLE_MIGRATION_AUDIT.md first.
        #
        # It was never held by any user on either database, but it was assignable from
        # five Admin surfaces, and assigning it broke an account in ways that named no
        # cause: no @role_required decorator admits it, so the Design dashboard 302s
        # away; it matches no Task.assigned_role, so the holder cannot change a task's
        # status, its due date, or tick a checklist item; user_can_edit_project_boq()
        # refuses it, so BOQ authorship is lost; six `role='Design'` querysets stop
        # offering the user as a designer; and _SA_EDITABLE_ROLE_CHOICES omits it, so a
        # System Admin cannot even edit that user's phone number afterwards.
        #
        # DESIGN HEAD AUTHORITY REMAINS `is_design_head` (below), which is what all
        # eighteen design-module views actually gate on. The role would have added a
        # SECOND authority mechanism beside a working one, not replaced it.
        #
        # The role-string branches in permissions.user_can_view_project() and
        # user_can_view_project_boq() are deliberately LEFT IN PLACE — they are harmless,
        # and a future phase may reintroduce the role on purpose.
        ('Finance',       'Finance'),
        ('SCM',           'SCM'),
        ('CEO',           'CEO'),
        ('BD',            'BD'),
    ]

    user                    = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    role                    = models.CharField(max_length=20, choices=ROLE_CHOICES, blank=True)
    phone_number            = models.CharField(max_length=10, blank=True)
    is_active               = models.BooleanField(default=True)  # Soft deactivation — keeps history without deleting the user
    is_design_head          = models.BooleanField(default=False)  # Grants Design-task reassignment via task_assign_design_head, independent of role
    # PART 9 — the FIRST of the two design review gates. A FLAG, NOT A ROLE, for exactly
    # the reasons the 6.5a audit established for `is_design_head` above: a new
    # ROLE_CHOICES value costs its holder every Task.assigned_role match, BOQ write, the
    # System Admin edit path, and portfolio-wide task visibility. This sits on a
    # role='Design' user and costs none of that.
    #
    # HOLDING BOTH FLAGS IS ALLOWED BUT DOES NOT LET ONE PERSON CLEAR A SITE: settled
    # decision 2 refuses the second verdict on the same artifact from the same person, so
    # a dual-flag holder may record either gate's verdict and never both. Enforced in
    # design_views, not here — a flag says what you may be, not what you may do next.
    is_design_qc            = models.BooleanField(default=False)
    # Names a deputy who may act for the Design Head during absence. STORAGE ONLY in this
    # session — nothing reads this field yet; the acting-for rules are Part 4. Self-FK so
    # the deputy is itself a UserProfile; SET_NULL so deactivating the deputy's profile
    # never deletes the Design Head's own row.
    design_head_deputy      = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='deputy_for',
    )
    email_notifications     = models.BooleanField(default=True)
    whatsapp_notifications  = models.BooleanField(default=True)
    created_at              = models.DateTimeField(auto_now_add=True)
    created_by              = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_users',
    )

    def __str__(self):
        return f"{self.user.username} ({self.role})"


class VendorCategory(models.Model):
    """Lookup table for vendor categories (e.g. Solar Modules, Inverter, Structure)."""

    name = models.CharField(max_length=50, unique=True)

    class Meta:
        ordering = ['name']
        verbose_name_plural = 'Vendor Categories'

    def __str__(self):
        return self.name


class Vendor(models.Model):
    """Supplier / vendor in the master list. Used as make preferences in BOQ items."""

    name           = models.CharField(max_length=200)
    contact_person = models.CharField(max_length=100)
    phone          = models.CharField(max_length=15)
    email          = models.EmailField(null=True, blank=True)
    gst_number     = models.CharField(max_length=15, null=True, blank=True)
    msme_status    = models.BooleanField(default=False)
    msme_number    = models.CharField(max_length=50, null=True, blank=True)
    address        = models.TextField(null=True, blank=True)
    categories     = models.ManyToManyField(VendorCategory, related_name='vendors')
    is_active      = models.BooleanField(default=True)  # Inactive vendors hidden from BOQ dropdowns but kept for history
    created_by     = models.ForeignKey(
        'UserProfile',
        on_delete=models.SET_NULL,
        null=True,
    )
    created_at     = models.DateTimeField(auto_now_add=True)
    updated_at     = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_active', 'name']  # Active vendors surface first in lists

    def __str__(self):
        return self.name


class VendorBrand(models.Model):
    """
    A brand/make label supplied by a vendor, optionally scoped to one supply category.

    A vendor can have multiple brands for different categories — e.g. a single vendor
    may supply "Waaree" solar modules and "Polycab" BOS cables. These appear as
    separate entries in the BOQ Make/Preference dropdown, filtered to the item's category.

    If category is null the brand appears in all categories the vendor supplies.
    Vendors with no VendorBrand entries fall back to displaying the company name.
    """

    vendor     = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name='brands')
    make_brand = models.CharField(max_length=200)
    # Optional — scope this brand label to one supply category.
    # Null means the brand shows across every category this vendor is assigned to.
    category   = models.ForeignKey(
        VendorCategory,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='vendor_brands',
    )

    class Meta:
        ordering = ['make_brand']

    def __str__(self):
        return f"{self.make_brand} — {self.vendor.name}"


class BOQItemMaster(models.Model):
    """
    Catalogue of BOQ line items. The single source of truth for what a standard
    BOQ line *is* — BOQItem rows reference an entry here so quantities can be
    summed across sites for grouped procurement.

    BOQItem.description stays a point-in-time snapshot: editing a catalogue row
    here never rewrites BOQ rows already created from it.

    Rows are deactivated via is_active, never deleted — BOQItem.item_master is
    SET_NULL, so a delete would silently break the aggregation join.
    """

    code        = models.CharField(max_length=32, unique=True)   # Short stable identifier, e.g. ITM-001
    description = models.CharField(max_length=255)
    unit        = models.CharField(max_length=20)                # Required — quantities cannot be aggregated across sites without a consistent unit
    category    = models.CharField(max_length=64, blank=True)    # Display grouping only; carries no logic
    is_active   = models.BooleanField(default=True)
    sort_order  = models.PositiveIntegerField(default=0)         # Also becomes BOQItem.serial_no on creation
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'code']

    def __str__(self):
        return f"{self.code} — {self.description}"


def get_standard_boq_items():
    """
    Return the standard BOQ template for a Residential solar project, read from
    the BOQItemMaster catalogue (active rows, in sort_order).
    Used when auto-creating a BOQ for Design users; serial numbers must stay stable
    — serial_no comes from the catalogue's sort_order, not from row position, so
    deactivating an entry does not renumber the rest.

    Raises RuntimeError on an empty catalogue rather than falling back to a literal
    list: a silent fallback would hide a migration that never ran.
    """
    rows = list(
        BOQItemMaster.objects
        .filter(is_active=True)
        .order_by('sort_order', 'code')
        .values('sort_order', 'category', 'description', 'unit')
    )
    if not rows:
        raise RuntimeError(
            'BOQItemMaster catalogue is empty — the standard BOQ template cannot be '
            'built. Run migrations (projects.0047) to populate it.'
        )
    return [
        {
            'serial_no':   r['sort_order'],
            'category':    r['category'],
            'description': r['description'],
            'uom':         r['unit'],
        }
        for r in rows
    ]


class BOQ(models.Model):
    """Bill of Quantities for a project. One BOQ per project (OneToOne)."""

    # Workflow: Draft → Submitted (by Design) → Acknowledged (by SCM) or Revision Requested (by PM)
    STATUS_CHOICES = [
        ('Draft',              'Draft'),
        ('Submitted',          'Submitted'),
        ('Acknowledged',       'Acknowledged'),
        ('Revision Requested', 'Revision Requested'),
    ]

    project      = models.OneToOneField(Project, on_delete=models.CASCADE, related_name='boq')
    submitted_by = models.ForeignKey(
        'UserProfile',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='submitted_boqs',
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    status       = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Draft')
    version      = models.IntegerField(default=1)  # Increments on each resubmission after revision
    notes        = models.TextField(null=True, blank=True)

    def __str__(self):
        return f"BOQ — {self.project.project_id} (v{self.version})"


class BOQItem(models.Model):
    """A single line item in a BOQ — one row in the bill of quantities table."""

    CATEGORY_CHOICES = [
        ('Solar Modules', 'Solar Modules'),
        ('Structure',     'Structure'),
        ('Inverter',      'Inverter'),
        ('BOS',           'BOS'),
        ('Other',         'Other'),
    ]

    UOM_CHOICES = [
        ('Nos',  'Nos'),
        ('LOT',  'LOT'),
        ('Mtr',  'Mtr'),
        ('Pkt',  'Pkt'),
        ('LS',   'LS'),
        ('Sets', 'Sets'),
        ('Kg',   'Kg'),
    ]

    boq              = models.ForeignKey(BOQ, on_delete=models.CASCADE, related_name='items')
    serial_no        = models.IntegerField()
    category         = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    description      = models.TextField()   # Point-in-time snapshot — never rewritten when item_master is later edited
    item_master      = models.ForeignKey(
        'BOQItemMaster',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='boq_items',   # The join used to sum quantities across sites; null for ad-hoc / legacy rows
    )
    make_preference  = models.ForeignKey(
        Vendor,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='preferred_items',  # Vendor preferred by Design for this item
    )
    uom              = models.CharField(max_length=10, choices=UOM_CHOICES)
    boq_quantity     = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)   # Quantity estimated by Design
    ordered_quantity = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)   # Actual quantity ordered by SCM
    ordered_vendor   = models.ForeignKey(
        Vendor,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ordered_items',  # Vendor actually selected by SCM when placing PO
    )
    is_standard_item = models.BooleanField(default=True)  # False for ad-hoc rows added by Design; only non-standard items can be deleted

    class Meta:
        ordering = ['serial_no']

    def __str__(self):
        return f"{self.boq.project.project_id} — Item {self.serial_no}: {self.description[:50]}"


class BOQRevision(models.Model):
    """Immutable snapshot of a BOQ at each workflow transition (submit, acknowledge, revision)."""

    boq        = models.ForeignKey(BOQ, on_delete=models.CASCADE, related_name='revisions')
    revised_by = models.ForeignKey('UserProfile', on_delete=models.SET_NULL, null=True)
    revised_at = models.DateTimeField(auto_now_add=True)
    version    = models.IntegerField()
    reason     = models.TextField()
    snapshot   = models.JSONField()  # Full item list serialised at transition time; Decimal fields coerced to float

    class Meta:
        ordering = ['-version']

    def __str__(self):
        return f"BOQ {self.boq.project.project_id} — v{self.version} by {self.revised_by}"


class Notification(models.Model):
    """In-app notification for a UserProfile. Marked read when they visit the notifications page."""

    recipient  = models.ForeignKey(
        'UserProfile',
        on_delete=models.CASCADE,
        related_name='notifications',
    )
    message    = models.TextField()
    link       = models.CharField(max_length=200, blank=True)  # Relative URL to the relevant object
    is_read    = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Notification → {self.recipient} — {self.message[:60]}"


class PaymentMilestone(models.Model):
    """M1/M2/M3 payment checkpoint for a project. Managed by Finance."""

    PENDING  = 'Pending'
    INVOICED = 'Invoiced'
    RECEIVED = 'Received'
    STATUS_CHOICES = [
        (PENDING,  'Pending'),
        (INVOICED, 'Invoiced'),
        (RECEIVED, 'Received'),
    ]
    M1 = 'M1'; M2 = 'M2'; M3 = 'M3'
    NAME_CHOICES = [(M1, 'M1'), (M2, 'M2'), (M3, 'M3')]

    project              = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='milestones')
    milestone_name       = models.CharField(max_length=10, choices=NAME_CHOICES)
    milestone_description = models.CharField(max_length=100, default='')
    amount               = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)  # Expected invoice amount
    amount_received      = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)  # Actual amount received (may differ)
    variance_reason      = models.CharField(max_length=255, blank=True, default='')  # Explanation when amount_received ≠ amount
    due_date             = models.DateField(null=True, blank=True)
    invoice_date         = models.DateField(null=True, blank=True)   # Set when Finance marks Invoiced
    received_date        = models.DateField(null=True, blank=True)   # Set when Finance marks Received
    status               = models.CharField(max_length=20, choices=STATUS_CHOICES, default=PENDING)
    created_by           = models.ForeignKey(UserProfile, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ['milestone_name']

    def __str__(self):
        return f"{self.project.project_id} — {self.milestone_name}"

class TaskAttachment(models.Model):
    """File (document or photo) attached to a specific task. Stored in Supabase; soft-deleted here."""

    DOCUMENT = 'Document'
    PHOTO    = 'Photo'
    FILE_TYPE_CHOICES = [(DOCUMENT, 'Document'), (PHOTO, 'Photo')]

    task         = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='attachments')
    uploaded_by  = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='task_attachments',
    )
    file_name    = models.CharField(max_length=255)
    file_url     = models.URLField(max_length=1000)   # Public Supabase URL for direct browser access
    supabase_path = models.CharField(max_length=500)  # Path within the Supabase bucket; used for deletion
    file_type    = models.CharField(max_length=20, choices=FILE_TYPE_CHOICES)
    file_size_kb = models.PositiveIntegerField(default=0)
    uploaded_at  = models.DateTimeField(auto_now_add=True)
    is_deleted   = models.BooleanField(default=False)  # Soft delete — purge_deleted_files command hard-deletes after FILE_RETENTION_DAYS
    deleted_at   = models.DateTimeField(null=True, blank=True)
    deleted_by   = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='deleted_task_attachments',
    )

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return f"Task {self.task_id} — {self.file_name}"


class Issue(models.Model):
    """A blocker or problem raised against a project or a specific task."""

    LOW      = 'Low'
    MEDIUM   = 'Medium'
    HIGH     = 'High'
    CRITICAL = 'Critical'
    SEVERITY_CHOICES = [
        (LOW,      'Low'),
        (MEDIUM,   'Medium'),
        (HIGH,     'High'),
        (CRITICAL, 'Critical'),
    ]

    # Workflow: Open → In Progress → Resolved (by anyone) → Closed (by PM only)
    # PM can also reopen a Resolved issue back to Open
    OPEN        = 'Open'
    IN_PROGRESS = 'In Progress'
    RESOLVED    = 'Resolved'
    CLOSED      = 'Closed'
    STATUS_CHOICES = [
        (OPEN,        'Open'),
        (IN_PROGRESS, 'In Progress'),
        (RESOLVED,    'Resolved'),
        (CLOSED,      'Closed'),
    ]

    project         = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='issues')
    task            = models.ForeignKey(Task, on_delete=models.SET_NULL, null=True, blank=True, related_name='issues')  # Null for project-level issues not tied to a task
    delivery_challan = models.ForeignKey(  # Null unless the issue was raised directly against a specific delivery
        'DeliveryChallan', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='issues',
    )
    title           = models.CharField(max_length=200)
    description     = models.TextField(blank=True, default='')
    severity        = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default=MEDIUM)
    status          = models.CharField(max_length=20, choices=STATUS_CHOICES, default=OPEN)
    raised_by       = models.ForeignKey(
        'UserProfile', on_delete=models.SET_NULL, null=True, blank=True, related_name='raised_issues',
    )
    assigned_to     = models.ForeignKey(
        'UserProfile', on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_issues',
    )
    raised_at       = models.DateTimeField(auto_now_add=True)
    due_date        = models.DateField(null=True, blank=True)
    resolved_at     = models.DateTimeField(null=True, blank=True)  # Set when status transitions to Resolved
    closed_at       = models.DateTimeField(null=True, blank=True)  # Set when PM closes the issue
    resolution_note = models.TextField(blank=True, default='')    # Required text when resolving

    class Meta:
        ordering = ['-raised_at']

    def __str__(self):
        return f"{self.project.project_id} — {self.title}"


class ActivityLog(models.Model):
    """Append-only audit log. Written by log_activity(); never edited after creation."""

    project     = models.ForeignKey(
        Project, on_delete=models.CASCADE, null=True, blank=True, related_name='activity_logs',
    )
    actor       = models.ForeignKey(
        'UserProfile', on_delete=models.SET_NULL, null=True, blank=True, related_name='activity_logs',
    )
    action      = models.CharField(max_length=255)   # Human-readable description of what happened
    # Stable machine-readable event key for querying (e.g. 'task_assigned', 'issue_resolved').
    # Blank on existing rows and on the ~80 call sites not yet retrofitted — populated only where
    # this feature logs, or where the EOD digest queries. Never string-match `action`; filter this.
    action_code = models.CharField(max_length=50, blank=True, db_index=True, default='')
    entity_type = models.CharField(max_length=50, blank=True, default='')   # e.g. 'Task', 'Issue', 'BOQ', 'File'
    entity_id   = models.PositiveIntegerField(null=True, blank=True)         # PK of the affected object
    timestamp   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M} — {self.action}"


class Comment(models.Model):
    """Threaded comment on a task or issue. One level deep: comment → reply only."""

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name='comments'
    )
    task = models.ForeignKey(
        Task, on_delete=models.CASCADE,
        null=True, blank=True, related_name='comments'
    )
    issue = models.ForeignKey(
        Issue, on_delete=models.CASCADE,
        null=True, blank=True, related_name='comments'
    )
    parent = models.ForeignKey(
        'self', on_delete=models.CASCADE,
        null=True, blank=True, related_name='replies'  # Non-null only for replies; replies cannot themselves have replies
    )
    author = models.ForeignKey(
        UserProfile, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='comments'
    )
    body       = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    is_deleted = models.BooleanField(default=False)  # Soft delete — body should be replaced with placeholder in templates
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"Comment by {self.author} at {self.created_at:%Y-%m-%d %H:%M}"


class DeliveryChallan(models.Model):
    """Delivery Challan raised by SCM for incoming materials."""

    # Status reflects aggregate receipt state across all line items
    EXPECTED           = 'Expected'
    PARTIALLY_RECEIVED = 'Partially Received'
    RECEIVED           = 'Received'
    REJECTED           = 'Rejected'
    STATUS_CHOICES = [
        (EXPECTED,           'Expected'),
        (PARTIALLY_RECEIVED, 'Partially Received'),
        (RECEIVED,           'Received'),
        (REJECTED,           'Rejected'),
    ]

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name='delivery_challans'
    )
    vendor = models.ForeignKey(
        Vendor, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='delivery_challans'
    )
    po_number = models.CharField(max_length=100, blank=True, default='')
    # po_number is a free-text reference to external PO (Excel/Zoho Inventory)
    # PO creation inside SolarPMS is Phase 2 scope — do NOT add PO model today

    dc_number = models.CharField(max_length=100)
    # dc_number: challan number printed on vendor's delivery document

    dc_date                = models.DateField()
    expected_delivery_date = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=30, choices=STATUS_CHOICES, default=EXPECTED
    )
    notes      = models.TextField(blank=True, default='')
    created_by = models.ForeignKey(
        UserProfile, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='created_challans'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"DC-{self.dc_number} ({self.project.project_id})"


class DCLineItem(models.Model):
    """Individual material line item within a Delivery Challan."""

    # BOQ categories — must match BOQItem category choices exactly
    SOLAR_MODULES = 'Solar Modules'
    STRUCTURE     = 'Structure'
    INVERTER      = 'Inverter'
    BOS           = 'BOS'
    CATEGORY_CHOICES = [
        (SOLAR_MODULES, 'Solar Modules'),
        (STRUCTURE,     'Structure'),
        (INVERTER,      'Inverter'),
        (BOS,           'BOS'),
    ]

    # Condition of received items — Damaged auto-triggers partial status
    GOOD    = 'Good'
    DAMAGED = 'Damaged'
    PARTIAL = 'Partial'
    CONDITION_CHOICES = [
        (GOOD,    'Good'),
        (DAMAGED, 'Damaged'),
        (PARTIAL, 'Partial'),
    ]

    challan          = models.ForeignKey(
        DeliveryChallan, on_delete=models.CASCADE, related_name='line_items'
    )
    boq_category     = models.CharField(max_length=50, choices=CATEGORY_CHOICES)
    item_description = models.CharField(max_length=255)
    ordered_quantity = models.DecimalField(max_digits=10, decimal_places=2)
    unit             = models.CharField(max_length=20, default='Nos')
    # received_quantity and condition filled by SE during GRN confirmation
    received_quantity = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    condition = models.CharField(
        max_length=20, choices=CONDITION_CHOICES,
        null=True, blank=True
    )
    damaged_quantity = models.PositiveIntegerField(default=0)  # How many of received_quantity arrived damaged — precise complement to received_quantity
    grn_date         = models.DateField(null=True, blank=True)
    grn_confirmed_by = models.ForeignKey(
        UserProfile, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='confirmed_line_items'
    )
    grn_notes = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['boq_category']

    def __str__(self):
        return f"{self.challan.dc_number} — {self.boq_category}: {self.item_description[:40]}"


def _dc_item_severity(received_qty, ordered_qty, damaged_qty):
    """
    Compute per-line-item severity for the DC status rollup.
    Returns 'green', 'amber', or 'red'.
    - green:  full quantity received, no damage
    - amber:  shortfall only OR full quantity with some damage (equal severity — confirmed by product owner)
    - red:    shortfall AND damage present, or nothing usable received at all
    """
    if received_qty is None:
        return None  # Not yet confirmed — excluded from rollup
    if received_qty == 0:
        return 'red'   # Nothing arrived
    if received_qty < ordered_qty and damaged_qty > 0:
        return 'red'   # Two stacked problems
    if received_qty >= ordered_qty and damaged_qty == 0:
        return 'green'
    return 'amber'  # Shortfall only, or full qty with some damage


def recalculate_dc_status(challan):
    """
    Recalculate and save DeliveryChallan.status using per-line-item severity rollup.
    Severity per item (via _dc_item_severity): green / amber / red.
    DC takes the WORST case across all confirmed items:
      green  → Received
      amber  → Partially Received
      red    → Rejected  (repurposed: severe delivery failure — shortfall+damage or nothing received)
    Must be called ONCE after all line items are saved — never inside the save loop.
    """
    items     = list(challan.line_items.all())
    confirmed = [item for item in items if item.received_quantity is not None]

    if not confirmed:
        # No items have GRN data yet
        challan.status = DeliveryChallan.EXPECTED
        challan.save()
        return

    worst = 'green'
    for item in confirmed:
        sev = _dc_item_severity(item.received_quantity, item.ordered_quantity, item.damaged_quantity)
        if sev == 'red':
            worst = 'red'
            break          # Can't get worse; short-circuit
        elif sev == 'amber':
            worst = 'amber'

    if worst == 'green':
        challan.status = DeliveryChallan.RECEIVED
    elif worst == 'amber':
        challan.status = DeliveryChallan.PARTIALLY_RECEIVED
    else:
        # 'red': severe delivery failure — quantity short AND damage, or nothing received
        challan.status = DeliveryChallan.REJECTED
    challan.save()


def get_material_status(project):
    """
    Returns per-category delivery status for a project.
    Aggregates across all DeliveryChallans for the project.
    Used by project overview panel (single-project page — no N+1 risk here).
    Do NOT call this inside a loop over multiple projects; use the annotated
    view-level queryset instead (see Bug 3 note in Day 9 spec).
    """
    # import inside function to avoid circular import at module level
    from django.db.models import Sum

    categories = ['Solar Modules', 'Structure', 'Inverter', 'BOS']
    status = {}

    for category in categories:
        items = DCLineItem.objects.filter(
            challan__project=project,
            boq_category=category
        )
        if not items.exists():
            status[category] = 'Pending'
            continue

        total_ordered = items.aggregate(s=Sum('ordered_quantity'))['s'] or 0

        # SUM of confirmed items only — SQL SUM ignores NULLs, but filter makes intent explicit
        total_received = items.filter(
            received_quantity__isnull=False
        ).aggregate(s=Sum('received_quantity'))['s'] or 0

        # Use damaged_quantity (precise numeric field) rather than condition string for damage detection
        has_damage = items.filter(damaged_quantity__gt=0).exists()

        if total_received == 0:
            status[category] = 'Pending'
        elif total_received >= total_ordered and not has_damage:
            status[category] = 'Received'
        else:
            status[category] = 'Partial'

    # Returns dict: {'Solar Modules': 'Received', 'Structure': 'Partial',
    #                'Inverter': 'Pending', 'BOS': 'Pending'}
    return status


def get_project_material_summary(project):
    """
    Returns single summary string for PM dashboard badge.
    Derived from get_material_status() aggregate.
    For PM dashboard (multi-project loop), use the view-level annotated queryset
    instead of calling this per project — it causes N+1.
    Badge colours: Pending=secondary, Partial=warning, Received=success.
    """
    status = get_material_status(project)
    values = list(status.values())

    if all(v == 'Pending' for v in values):
        return 'Pending'
    elif all(v == 'Received' for v in values):
        return 'Received'
    else:
        return 'Partial'


def log_activity(project, actor, action, entity_type='', entity_id=None, action_code=''):
    """
    Write one ActivityLog entry. Silently swallows exceptions so a failed log
    never aborts the primary operation that called it.

    action_code is an optional stable machine-readable event key. Most existing
    call sites leave it blank; supply it only where a consumer (e.g. the EOD
    digest) needs to filter on the event type instead of string-matching `action`.
    """
    try:
        # import inside function to avoid circular import at module level
        from projects.models import ActivityLog
        ActivityLog.objects.create(
            project=project,
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            action_code=action_code,
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"ActivityLog failed: {e}")


class NotificationLog(models.Model):
    """Audit trail of every notification attempt — sent, failed, or skipped by preference/switch."""

    CHANNEL_CHOICES = [
        ('in_app',   'In App'),
        ('whatsapp', 'WhatsApp'),
        ('email',    'Email'),
    ]
    STATUS_CHOICES = [
        ('sent',    'Sent'),
        ('failed',  'Failed'),
        ('skipped', 'Skipped'),
    ]

    recipient       = models.ForeignKey(
        'UserProfile', on_delete=models.CASCADE, related_name='notification_logs',
    )
    channel         = models.CharField(max_length=10, choices=CHANNEL_CHOICES)
    status          = models.CharField(max_length=10, choices=STATUS_CHOICES)
    message         = models.TextField()
    template_name   = models.CharField(max_length=100, blank=True)
    related_project = models.ForeignKey(
        'Project', null=True, blank=True, on_delete=models.SET_NULL,
    )
    actor           = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='notifications_triggered',
    )
    error_detail         = models.TextField(blank=True)
    delivery_status      = models.CharField(
        max_length=30,
        blank=True,
        default='',
        choices=[
            ('message_api_sent',      'Sent by Interakt'),
            ('message_api_delivered', 'Delivered to Phone'),
            ('message_api_read',      'Read'),
            ('message_api_failed',    'Failed at Delivery'),
        ]
    )
    interakt_message_id  = models.CharField(max_length=100, blank=True, default='')
    created_at           = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.channel}/{self.status} → {self.recipient} at {self.created_at:%Y-%m-%d %H:%M}"


class SystemSettings(models.Model):
    """Single-row global settings. Use SystemSettings.get() — never instantiate directly."""

    whatsapp_enabled              = models.BooleanField(default=False)
    email_enabled                 = models.BooleanField(default=False)
    in_app_notifications_enabled  = models.BooleanField(default=True)
    maintenance_mode              = models.BooleanField(default=False)
    cascade_scheduling_enabled    = models.BooleanField(
        default=False,
        help_text="When True, PMs can enable cascading date scheduling per project.",
    )
    gantt_client_buffer_days      = models.PositiveIntegerField(
        default=3,
        help_text="Calendar days added to each phase end in the Client Gantt view, cascading downstream.",
    )
    gantt_external_min_display_days = models.PositiveIntegerField(
        default=3,
        help_text="Minimum visual bar width (days) for external/third-party Gantt tasks so they don't render too thin.",
    )

    class Meta:
        verbose_name        = 'System Settings'
        verbose_name_plural = 'System Settings'

    def __str__(self):
        return 'System Settings'

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class PaymentRequest(models.Model):
    """A vendor payment request raised by SCM, confirmed by Finance, visible to PM. No edit/cancel by design."""

    PENDING   = 'pending'
    CONFIRMED = 'confirmed'
    STATUS_CHOICES = [
        (PENDING,   'Pending'),
        (CONFIRMED, 'Confirmed'),
    ]

    project  = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='payment_requests')
    vendor   = models.ForeignKey(
        Vendor, on_delete=models.SET_NULL, null=True,
        related_name='payment_requests',
    )
    # BOQItem FK: always scoped to this project via boq__project in queries.
    # Do not display another project's BOQ items in the raise-request form.
    boq_item = models.ForeignKey(
        BOQItem, on_delete=models.SET_NULL, null=True,
        related_name='payment_requests',
    )

    invoice_number = models.CharField(max_length=100)

    # Supabase storage — reuse same three-field pattern as ProjectDocument/TaskAttachment.
    # invoice_document is mandatory at creation: no edit/cancel flow exists for
    # PaymentRequest by design (Zuber decision, 19-June session).
    invoice_document_name = models.CharField(max_length=255)      # Original filename
    invoice_document_url  = models.URLField(max_length=1000)      # Public Supabase URL for browser access
    invoice_document_path = models.CharField(max_length=500)      # Supabase path for purge commands

    amount = models.DecimalField(max_digits=12, decimal_places=2)

    # 'note' is optional free-text context — NOT a Delivery Challan link.
    # Zuber explicitly decided against a DC FK (19-June session) to keep
    # the model simple; payment requests may not always map 1:1 to a DC.
    note = models.TextField(blank=True)

    requested_by   = models.ForeignKey(
        'auth.User', on_delete=models.PROTECT,
        related_name='raised_payment_requests',
    )
    requested_date = models.DateTimeField(auto_now_add=True)
    status         = models.CharField(max_length=20, choices=STATUS_CHOICES, default=PENDING)

    # Set on confirm — null until Finance confirms
    payment_date      = models.DateField(null=True, blank=True)
    payment_reference = models.CharField(max_length=100, blank=True)  # UTR / cheque no., set on confirm
    confirmed_by      = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='confirmed_payment_requests',
    )

    class Meta:
        ordering = ['-requested_date']

    def __str__(self):
        return f"PR-{self.pk} {self.project.project_id} — {self.vendor} ₹{self.amount}"


class DesignSubmission(models.Model):
    """Design document or drawing submitted by a Design user for a project."""

    PENDING  = 'Pending'
    APPROVED = 'Approved'
    REJECTED = 'Rejected'
    STATUS_CHOICES = [
        (PENDING,  'Pending'),
        (APPROVED, 'Approved'),
        (REJECTED, 'Rejected'),
    ]

    project      = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='design_submissions')
    submitted_by = models.ForeignKey(
        'UserProfile', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='design_submissions',
    )
    title        = models.CharField(max_length=200)
    description  = models.TextField(blank=True, default='')

    # Supabase storage — same three-field pattern as ProjectDocument / TaskAttachment
    file_name     = models.CharField(max_length=255, blank=True, default='')
    file_url      = models.URLField(max_length=1000, blank=True, default='')
    supabase_path = models.CharField(max_length=500, blank=True, default='')

    status       = models.CharField(max_length=20, choices=STATUS_CHOICES, default=PENDING)
    submitted_at = models.DateTimeField(auto_now_add=True)

    reviewed_by  = models.ForeignKey(
        'UserProfile', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='reviewed_design_submissions',
    )
    reviewed_at  = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-submitted_at']

    def __str__(self):
        return f"{self.project.project_id} — {self.title}"


class TaskDurationTemplate(models.Model):
    """
    Admin-editable default duration_days for each task in the residential project template.
    Seeded from the hardcoded values in attach_residential_template(); changes apply to
    new projects only — existing tasks are never touched.
    """

    PROJECT_TYPE_CHOICES = [
        ('residential', 'Residential'),
    ]

    project_type  = models.CharField(max_length=50, choices=PROJECT_TYPE_CHOICES, default='residential')
    phase_name    = models.CharField(max_length=100)
    task_name     = models.CharField(max_length=200)
    task_type     = models.CharField(max_length=20)   # 'Internal' or 'External'
    duration_days = models.PositiveIntegerField(default=1)
    updated_by    = models.ForeignKey(
        User,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='duration_template_edits',
    )
    updated_at    = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('project_type', 'task_name')
        ordering        = ['project_type', 'phase_name', 'task_name']

    def __str__(self):
        return f"{self.project_type} | {self.phase_name} | {self.task_name} ({self.duration_days}d)"


class Checklist(models.Model):
    """
    A named, reusable checklist template authored once in portal-admin and surfaced on
    a task by explicitly linking it to one or more (task_name, project_type) pairs via
    ChecklistTaskLink. Its line items live in ChecklistItem; per-task completion state
    lives in ChecklistItemCompletion. Replaces the prior per-task-instance checklist
    model — checklists are now reusable across every task with the linked name/type.
    """

    name       = models.CharField(max_length=200)
    is_active  = models.BooleanField(default=True)  # Inactive checklists are hidden on task detail
    created_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='created_checklists',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class ChecklistItem(models.Model):
    """
    One line item belonging to a Checklist. `order` is ascending within the checklist and
    is swapped by the admin up/down actions (no drag library). Deleting the parent
    Checklist cascades to its items (and each item cascades to its completions).
    """

    checklist = models.ForeignKey(Checklist, on_delete=models.CASCADE, related_name='items')
    label     = models.TextField()
    order     = models.PositiveIntegerField(default=0)  # Ascending within checklist; swapped by move up/down

    class Meta:
        ordering = ['order', 'pk']  # pk tiebreak keeps ordering stable when two items share an order

    def __str__(self):
        return f"Checklist {self.checklist_id} — {self.label[:40]}"


class ChecklistTaskLink(models.Model):
    """
    Assigns a Checklist to a task, keyed by (task_name, project_type) rather than a
    concrete Task row, so every task instance with that name in that project type shows
    the checklist. UNIQUE on (task_name, project_type): a given task can have at most one
    checklist. Assigning a second checklist to an already-linked pair is rejected at the
    admin-view layer with a clear error, not silently overwritten. Deleting the Checklist
    cascades away its links.
    """

    checklist    = models.ForeignKey(Checklist, on_delete=models.CASCADE, related_name='task_links')
    task_name    = models.CharField(max_length=200)
    project_type = models.CharField(max_length=20, choices=Project.PROJECT_TYPE_CHOICES)

    class Meta:
        unique_together = ('task_name', 'project_type')
        ordering        = ['project_type', 'task_name']

    def __str__(self):
        return f"{self.task_name} [{self.project_type}] → checklist {self.checklist_id}"


class ChecklistItemCompletion(models.Model):
    """
    Per-task completion state for one ChecklistItem. Keyed by (item, task) so the same
    checklist item, shown on two different tasks, is completed independently. Completing
    an item requires BOTH a tick and a photo: is_checked is only ever set True together
    with the three photo_* fields in the same save, so a checked item can never lack a
    photo. Reuses the same three-field Supabase convention as the rest of the app.
    """

    item       = models.ForeignKey(ChecklistItem, on_delete=models.CASCADE, related_name='completions')
    task       = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='checklist_completions')
    is_checked = models.BooleanField(default=False)

    # Supabase storage — same three-field convention as PaymentRequest / TaskAttachment.
    # One photo per completion; blank until the item is checked.
    photo_file_name     = models.CharField(max_length=255, blank=True, default='')   # Original filename
    photo_url           = models.URLField(max_length=1000, blank=True, default='')   # Public Supabase URL for browser access
    photo_supabase_path = models.CharField(max_length=500, blank=True, default='')   # Supabase path for purge commands

    checked_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='checked_checklist_items',
    )
    checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('item', 'task')
        ordering        = ['pk']

    def __str__(self):
        state = 'checked' if self.is_checked else 'pending'
        return f"Task {self.task_id} / item {self.item_id} — {state}"


# ===========================================================================
# OPEX Design Module — Part 1: data model only
#
# Scope note (deliberate): these models RECORD state. They contain no transition
# logic — no save() override advances a status, no signal fires, no state machine
# is enforced here. The rules that move an assignment through the statuses below
# arrive in Part 2 onward. A model that silently changes its own status is much
# harder to debug than one that does not.
#
# OPEX ONLY. Residential design work continues to run on its six design Task rows
# with PM-owned approval, entirely untouched by anything in this section. The two
# approval models genuinely differ and are deliberately not unified.
#
# Three fields carry "required when <condition>" rules. Two of them depend only on
# values in their OWN row and are enforced as DB CheckConstraints below:
#   • DesignAttempt.qc_remarks       — required when qc_verdict = 'failed'
#   • ArkaSubmission.rejection_reason — required when verdict  = 'rejected'
# The third, DueDateCommitment.change_reason ("required when this is not the first
# commitment"), depends on SIBLING ROWS — a CHECK constraint cannot see them — so it
# stays a view-layer rule and is deliberately NOT constrained here.
#
# Both constraints test for the empty string only. The fields are NOT NULL with
# default='', so there is no null case to cover, but a whitespace-only value would
# still pass; rejecting that belongs with the form validation in a later part.
# ===========================================================================

# DesignAssignment.status values. Module-level so views, forms and tests in later
# parts import one list instead of copying string literals.
DESIGN_AWAITING_SURVEY     = 'awaiting_survey'
DESIGN_SURVEY_RETURNED     = 'survey_returned'
DESIGN_AWAITING_ALLOCATION = 'awaiting_allocation'
DESIGN_ALLOCATED           = 'allocated'
DESIGN_DUE_DATE_PROPOSED   = 'due_date_proposed'
DESIGN_IN_DESIGN           = 'in_design'
DESIGN_ARKA_SUBMITTED      = 'arka_submitted'
DESIGN_ARKA_REJECTED       = 'arka_rejected'
DESIGN_ARTIFACTS_UPLOADED  = 'artifacts_uploaded'
DESIGN_IN_QC               = 'in_qc'
DESIGN_QC_FAILED           = 'qc_failed'
DESIGN_RELEASED            = 'released'

# PART 9 — the two waiting rooms between the gates. Design QC has passed the artifact
# and the Design Head has not yet ruled on it. They are separate statuses rather than a
# flag on the existing ones because the BOTTLENECK IS DIFFERENT: `arka_submitted` means
# the QC reviewer owes a verdict, `awaiting_head_arka` means the Head does, and a
# dashboard that cannot tell them apart cannot say who is holding the tender up.
DESIGN_AWAITING_HEAD_ARKA  = 'awaiting_head_arka'
DESIGN_AWAITING_HEAD_QC    = 'awaiting_head_qc'

# Order below is the LINEAR workflow order, corrected in Part 2. The Design Head
# receives the survey when a tender starts, uploads it, and only then allocates the
# site — so allocation follows survey upload rather than preceding it:
#
#   awaiting_survey -> awaiting_allocation -> allocated -> due_date_proposed -> in_design
#
# `survey_returned` is NOT part of that line any more. It is repurposed as an
# off-sequence DESIGN HOLD FLAG: the assigned designer puts the site on hold over an
# inadequate survey, which stops their clock and surfaces to the Head. It is listed
# last for that reason. (A designer cannot "return" a survey to the Head who both
# supplied it and approves the work — there is no independent party to return it to.)
#
# PART 8 NAMING. The user-facing name is "Design Hold"; it was "Blocked" until Part 8.
# The STORED VALUE is `survey_returned` and always has been — there has never been a
# stored string `blocked` to rename, so the Part 8 rename is label-only and carries no
# risk to existing rows. The model fields (`survey_returned_at`, `survey_returned_by`,
# `survey_return_reason`) keep their names deliberately: renaming them is a schema
# change with no user-visible benefit. The mismatch is recorded in
# DESIGN_MODULE_DEFERRED.md so the next reader is not surprised by it.
#
# Values are UNCHANGED — this is ordering and labelling only, so the migration is an
# AlterField on choices with no data operation.
DESIGN_ASSIGNMENT_STATUS_CHOICES = [
    (DESIGN_AWAITING_SURVEY,     'Awaiting survey'),
    (DESIGN_AWAITING_ALLOCATION, 'Awaiting allocation'),
    (DESIGN_ALLOCATED,           'Allocated'),
    (DESIGN_DUE_DATE_PROPOSED,   'Due date proposed'),
    (DESIGN_IN_DESIGN,           'In design'),
    (DESIGN_ARKA_SUBMITTED,      'Arka submitted'),
    (DESIGN_AWAITING_HEAD_ARKA,  'Arka — awaiting Design Head'),
    (DESIGN_ARKA_REJECTED,       'Arka rejected'),
    (DESIGN_ARTIFACTS_UPLOADED,  'Artifacts uploaded'),
    (DESIGN_IN_QC,               'In QC'),
    (DESIGN_AWAITING_HEAD_QC,    'QC passed — awaiting Design Head'),
    (DESIGN_QC_FAILED,           'QC failed'),
    (DESIGN_RELEASED,            'Released'),
    (DESIGN_SURVEY_RETURNED,     'Design Hold — survey inadequate'),
]

# DesignAttempt.opened_reason. The two rework loops are counted SEPARATELY and
# deliberately not collapsed into one field: a QC failure is the design failing
# review, a PM change request is the requirement changing underneath it. Rework
# rates for the two mean different things and must stay distinguishable.
ATTEMPT_REASON_INITIAL           = 'initial'
ATTEMPT_REASON_QC_FAILED         = 'qc_failed'
ATTEMPT_REASON_PM_CHANGE_REQUEST = 'pm_change_request'

DESIGN_ATTEMPT_REASON_CHOICES = [
    (ATTEMPT_REASON_INITIAL,           'Initial'),
    (ATTEMPT_REASON_QC_FAILED,         'QC failed'),
    (ATTEMPT_REASON_PM_CHANGE_REQUEST, 'PM change request'),
]

QC_PENDING = 'pending'
QC_PASSED  = 'passed'
QC_FAILED  = 'failed'

QC_VERDICT_CHOICES = [
    (QC_PENDING, 'Pending'),
    (QC_PASSED,  'Passed'),
    (QC_FAILED,  'Failed'),
]

ARKA_PENDING  = 'pending'
ARKA_APPROVED = 'approved'
ARKA_REJECTED = 'rejected'

ARKA_VERDICT_CHOICES = [
    (ARKA_PENDING,  'Pending'),
    (ARKA_APPROVED, 'Approved'),
    (ARKA_REJECTED, 'Rejected'),
]

# ===========================================================================
# PART 9 — DESIGN ERROR CATEGORIES
# ===========================================================================
# ONE category per rejection or failure, at BOTH gates, and it is MANDATORY. Multi-select
# was considered and rejected: a rejection tagged with four categories cannot be counted
# under any of them, so a multi-select field produces a report nobody can act on. Free
# text stays available ALONGSIDE the category, not instead of it — the category is what
# is countable, the text is what is readable.
#
# THE GROUP IS THE POINT, not the category. Three groups, and the split decides whether a
# rework loop is charged to the designer:
#
#   A  Designer execution   the design was wrong          -> COUNTS as designer rework
#   B  Input problem        the survey was wrong          -> does NOT count
#   C  Brief change         the requirement moved         -> does NOT count
#
# Charging a designer for rework caused by a bad survey or a changed brief is the exact
# unfairness the Part 5 comment about `pm_change_request` already warns against; this
# extends the same principle to failures that arrive through the QC gate rather than
# through a PM change request.
#
# Group membership lives HERE and is read through error_category_group(). Do not hardcode
# a group's members anywhere else — a category added to the wrong tuple in a second copy
# would silently move rework between the designer's column and the input-quality column,
# and nothing would fail.

# ── Group A — designer execution ──────────────────────────────────────────
ERR_LAYOUT               = 'layout_error'
ERR_SHADING              = 'shading'
ERR_CAPACITY             = 'capacity_error'
ERR_STRUCTURAL_CONSTRAINT = 'structural_constraint'
ERR_ACCESS_SETBACK       = 'access_setback'
ERR_STRUCTURAL_DESIGN    = 'structural_design'
ERR_ELECTRICAL_DESIGN    = 'electrical_design'
ERR_EARTHING_LIGHTNING   = 'earthing_lightning'
ERR_DRAWING_INCOMPLETE   = 'drawing_incomplete'
ERR_STANDARDS            = 'standards'
ERR_BOQ_QUANTITY         = 'boq_quantity'
ERR_BOQ_SPECIFICATION    = 'boq_specification'

# ── Group B — input problem ───────────────────────────────────────────────
ERR_SURVEY_INADEQUATE    = 'survey_inadequate'
ERR_SITE_DIFFERS         = 'site_differs'

# ── Group C — brief change ────────────────────────────────────────────────
ERR_REQUIREMENT_CHANGED  = 'requirement_changed'
ERR_SCOPE_REVISED        = 'scope_revised'

ERROR_GROUP_A = 'A'
ERROR_GROUP_B = 'B'
ERROR_GROUP_C = 'C'

#: category -> group. THE SINGLE SOURCE OF GROUP MEMBERSHIP.
_ERROR_CATEGORY_GROUPS = {
    ERR_LAYOUT:                ERROR_GROUP_A,
    ERR_SHADING:               ERROR_GROUP_A,
    ERR_CAPACITY:              ERROR_GROUP_A,
    ERR_STRUCTURAL_CONSTRAINT: ERROR_GROUP_A,
    ERR_ACCESS_SETBACK:        ERROR_GROUP_A,
    ERR_STRUCTURAL_DESIGN:     ERROR_GROUP_A,
    ERR_ELECTRICAL_DESIGN:     ERROR_GROUP_A,
    ERR_EARTHING_LIGHTNING:    ERROR_GROUP_A,
    ERR_DRAWING_INCOMPLETE:    ERROR_GROUP_A,
    ERR_STANDARDS:             ERROR_GROUP_A,
    ERR_BOQ_QUANTITY:          ERROR_GROUP_A,
    ERR_BOQ_SPECIFICATION:     ERROR_GROUP_A,
    ERR_SURVEY_INADEQUATE:     ERROR_GROUP_B,
    ERR_SITE_DIFFERS:          ERROR_GROUP_B,
    ERR_REQUIREMENT_CHANGED:   ERROR_GROUP_C,
    ERR_SCOPE_REVISED:         ERROR_GROUP_C,
}

# Rendered as one <select> at both gates. Grouped so the reviewer sees WHICH KIND of
# problem they are recording — the optgroup label is the accountability split, and a
# reviewer who can see it is far likelier to pick the honest category.
DESIGN_ERROR_CATEGORY_CHOICES = [
    ('Designer execution', [
        (ERR_LAYOUT,                'Layout error: module placement, row spacing, orientation'),
        (ERR_SHADING,               'Shading or obstruction not accounted for'),
        (ERR_CAPACITY,              'Capacity error: designed capacity wrong for available area'),
        (ERR_STRUCTURAL_CONSTRAINT, 'Roof or structural constraint violated'),
        (ERR_ACCESS_SETBACK,        'Access, walkway, or setback non-compliance'),
        (ERR_STRUCTURAL_DESIGN,     'Structural design error: foundation, mounting, wind load'),
        (ERR_ELECTRICAL_DESIGN,     'Electrical design error: string sizing, inverter matching, cable sizing'),
        (ERR_EARTHING_LIGHTNING,    'Earthing or lightning protection error'),
        (ERR_DRAWING_INCOMPLETE,    'Drawing incomplete: missing views, dimensions, annotations'),
        (ERR_STANDARDS,             'Standards non-compliance: MNRE, IS, DISCOM norms'),
        (ERR_BOQ_QUANTITY,          'BOQ quantity error'),
        (ERR_BOQ_SPECIFICATION,     'BOQ item or specification error'),
    ]),
    ('Input problem — not designer rework', [
        (ERR_SURVEY_INADEQUATE,     'Survey data inadequate or incorrect'),
        (ERR_SITE_DIFFERS,          'Site conditions differ from survey'),
    ]),
    ('Brief change — not designer rework', [
        (ERR_REQUIREMENT_CHANGED,   'Client or PM requirement changed'),
        (ERR_SCOPE_REVISED,         'Scope or capacity revised after design started'),
    ]),
]

#: Flat {value: label}, for rendering a stored category outside a form.
DESIGN_ERROR_CATEGORY_LABELS = {
    value: label
    for _group_label, entries in DESIGN_ERROR_CATEGORY_CHOICES
    for value, label in entries
}

#: Every valid category value. Used by the views to validate a posted category.
DESIGN_ERROR_CATEGORIES = tuple(DESIGN_ERROR_CATEGORY_LABELS)


def error_category_group(category):
    """Return 'A', 'B' or 'C' for an error category, or None if it has none.

    THE ONLY PLACE GROUP MEMBERSHIP IS DECIDED. Every consumer — the rework multiplier,
    the dashboards, any future quality report — asks this function rather than testing
    membership of its own tuple. Adding a category therefore means one edit, in
    _ERROR_CATEGORY_GROUPS above, and every consumer is correct immediately.

    Returns None for '' (the blank default on every non-rejected row) and for any value
    not in the map, so callers can treat "no category recorded" and "unrecognised
    category" identically — neither is a designer error, and neither should silently be
    counted as one.
    """
    if not category:
        return None
    return _ERROR_CATEGORY_GROUPS.get(category)


def category_counts_as_designer_rework(category):
    """True only for Group A. The one predicate the rework multiplier is built on.

    Group B (bad input) and Group C (moved brief) are NOT the designer's error, so an
    attempt opened by one of them is excluded from the designer's multiplier and counted
    under input quality instead — settled decision 8.
    """
    return error_category_group(category) == ERROR_GROUP_A


# ===========================================================================
# WHAT A FAILURE ACTUALLY SENDS BACK (Part 9.1)
# ===========================================================================
# Part 4 reset EVERYTHING on a failed attempt: the designer resubmitted the Arka,
# re-uploaded the CAD and re-marked the BOQ, and both gates re-approved an Arka nobody
# had disputed. That was the only safe answer while the system had no idea WHAT had
# failed — a BOQ typo and a wrong roof layout were indistinguishable.
#
# Part 9 records the cause, so "redo everything" stopped being the only option. A failure
# now names the three artifacts independently, and whatever is not named CARRIES FORWARD
# into the next attempt with its approvals intact.
#
# THE REVIEWER DECIDES, NOT THIS TABLE. The map below only pre-ticks the boxes on the
# fail form; the reviewer can tick or untick any of them before submitting, and the view
# stores what they actually chose. That is deliberate: the reviewer is the only person who
# knows whether a `layout_error` is fixable in CAD alone or needs the whole layout redone,
# and no fixed mapping can answer that. Treat these as sensible starting points, never as
# a rule.

REDO_ARKA = 'arka'
REDO_CAD  = 'cad'
REDO_BOQ  = 'boq'

DESIGN_REDO_CHOICES = (REDO_ARKA, REDO_CAD, REDO_BOQ)

#: Which boxes start ticked, per error category. See the note above — DEFAULTS ONLY.
DEFAULT_REDO_BY_CATEGORY = {
    # BOQ-scoped: the layout and the drawings were fine.
    ERR_BOQ_QUANTITY:          frozenset({REDO_BOQ}),
    ERR_BOQ_SPECIFICATION:     frozenset({REDO_BOQ}),

    # Drawing-scoped: the design is right, its expression on the sheet is not.
    ERR_DRAWING_INCOMPLETE:    frozenset({REDO_CAD}),

    # Engineering inside an accepted layout — the Arka stands, the detail does not.
    ERR_STRUCTURAL_DESIGN:     frozenset({REDO_CAD, REDO_BOQ}),
    ERR_ELECTRICAL_DESIGN:     frozenset({REDO_CAD, REDO_BOQ}),
    ERR_EARTHING_LIGHTNING:    frozenset({REDO_CAD, REDO_BOQ}),
    ERR_STANDARDS:             frozenset({REDO_CAD, REDO_BOQ}),

    # The LAYOUT itself is wrong, so the Arka goes first and everything follows it.
    ERR_LAYOUT:                frozenset(DESIGN_REDO_CHOICES),
    ERR_SHADING:               frozenset(DESIGN_REDO_CHOICES),
    ERR_CAPACITY:              frozenset(DESIGN_REDO_CHOICES),
    ERR_STRUCTURAL_CONSTRAINT: frozenset(DESIGN_REDO_CHOICES),
    ERR_ACCESS_SETBACK:        frozenset(DESIGN_REDO_CHOICES),

    # Group B and C — the inputs or the brief moved, so nothing that was drawn against
    # them can be assumed to still hold.
    ERR_SURVEY_INADEQUATE:     frozenset(DESIGN_REDO_CHOICES),
    ERR_SITE_DIFFERS:          frozenset(DESIGN_REDO_CHOICES),
    ERR_REQUIREMENT_CHANGED:   frozenset(DESIGN_REDO_CHOICES),
    ERR_SCOPE_REVISED:         frozenset(DESIGN_REDO_CHOICES),
}


def default_redo_for_category(category):
    """Which artifacts a failure of `category` suggests redoing, as a sorted list.

    Falls back to ALL THREE for an unrecognised or blank category, which is the safe
    direction: a reviewer who sees too much ticked unticks a box, whereas one who sees too
    little may not notice something is missing until QC fails again.
    """
    return sorted(DEFAULT_REDO_BY_CATEGORY.get(category, frozenset(DESIGN_REDO_CHOICES)))

DESIGN_FILE_CAD_ZIP   = 'cad_zip'
DESIGN_FILE_BOQ_EXCEL = 'boq_excel'
DESIGN_FILE_BOQ_PDF   = 'boq_pdf'

# LEGACY, Part 8. CAD used to be uploaded as two separate files. It is now ONE zip
# holding both, so nothing new is ever written with these two kinds. They stay in the
# choices — and MUST stay — because rows already carry them: removing a value that
# exists in the table makes get_kind_display() fall back to the raw string and makes
# those rows fail full_clean(). They are not migrated and not deleted; the designer
# can no longer create them, and the QC screen still lists and downloads them.
DESIGN_FILE_CAD_PDF   = 'cad_pdf'
DESIGN_FILE_CAD_DWG   = 'cad_dwg'

DESIGN_FILE_KIND_CHOICES = [
    (DESIGN_FILE_CAD_ZIP,   'CAD (zip)'),
    (DESIGN_FILE_BOQ_EXCEL, 'BOQ (Excel)'),
    (DESIGN_FILE_BOQ_PDF,   'BOQ (PDF)'),
    (DESIGN_FILE_CAD_PDF,   'CAD (PDF) — legacy'),
    (DESIGN_FILE_CAD_DWG,   'CAD (DWG) — legacy'),
]

#: Kinds that are no longer offered for upload but must remain readable.
DESIGN_FILE_LEGACY_KINDS = (DESIGN_FILE_CAD_PDF, DESIGN_FILE_CAD_DWG)

#: Every kind that counts as a CAD artifact, current or legacy. Read paths (listing,
#: download, history) use this; the progression rule deliberately does NOT — see
#: design_views._maybe_advance_to_artifacts_uploaded.
DESIGN_FILE_CAD_KINDS = (DESIGN_FILE_CAD_ZIP,) + DESIGN_FILE_LEGACY_KINDS


class DesignAssignment(models.Model):
    """One per OPEX site — the container for all design work on that site.

    OneToOne to Project rather than a new site entity: Program is the tender parent,
    Project is the site, and this hangs off the existing site row.

    SURVEY IS A FILE ONLY (settled decision 6): bucket + path, no structured fields.
    Structured survey data arrives when the survey module is built. The designer can
    return a survey as inadequate (survey_returned_*), which stops their clock — the
    clock arithmetic itself belongs to a later part.

    FILES STORE BUCKET AND PATH, NOT URLS (settled decision 8). Nothing here builds a
    URL; the signed-URL helper is Part 2. This deliberately differs from the older
    ProjectDocument / DesignSubmission three-field convention, which stores a
    long-lived public file_url.
    """

    project = models.OneToOneField(
        Project, on_delete=models.CASCADE, related_name='design_assignment',
    )

    # ── Survey (file only) ──────────────────────────────────────────────────
    survey_file_bucket = models.CharField(max_length=100, blank=True, default='')
    survey_file_path   = models.CharField(max_length=500, blank=True, default='')
    survey_uploaded_by = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='uploaded_design_surveys',
    )
    survey_uploaded_at = models.DateTimeField(null=True, blank=True)
    # Designer returns an inadequate survey; reason is free text.
    survey_returned_at   = models.DateTimeField(null=True, blank=True)
    survey_returned_by   = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='returned_design_surveys',
    )
    survey_return_reason = models.TextField(blank=True, default='')

    # ── Allocation ──────────────────────────────────────────────────────────
    assigned_to = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='design_assignments',
    )
    assigned_by = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='made_design_assignments',
    )
    assigned_at = models.DateTimeField(null=True, blank=True)

    # 0 until the first attempt is opened; mirrors the highest DesignAttempt.attempt_number.
    # Maintained by the transition logic in a later part, NOT by this model.
    current_attempt_number = models.PositiveIntegerField(default=0)

    status = models.CharField(
        max_length=30, choices=DESIGN_ASSIGNMENT_STATUS_CHOICES,
        default=DESIGN_AWAITING_SURVEY,
    )

    # ── Release ─────────────────────────────────────────────────────────────
    released_at = models.DateTimeField(null=True, blank=True)
    released_by = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='released_design_assignments',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Design — {self.project.project_id} ({self.status})"


class DueDateCommitment(models.Model):
    """A two-sided due-date handshake (settled decision 5): the designer proposes,
    the Design Head approves.

    Approved dates are NEVER edited in place. A change creates a NEW commitment row
    carrying its own change_reason, and the previous row is flipped to
    is_current=False. The number of revisions is therefore derivable by counting rows
    (count - 1), with no stored revision counter that can drift.

    A row whose approved_by / approved_at are still null is a proposal awaiting approval.
    """

    assignment = models.ForeignKey(
        DesignAssignment, on_delete=models.CASCADE, related_name='due_date_commitments',
    )
    proposed_date = models.DateField()
    proposed_by = models.ForeignKey(
        'UserProfile', on_delete=models.PROTECT, related_name='proposed_due_dates',
    )
    proposed_at = models.DateTimeField(auto_now_add=True)

    approved_by = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='approved_due_dates',
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    # Required when this is NOT the first commitment for the assignment — see the
    # conditional-requirement note at the top of this section.
    change_reason = models.TextField(blank=True, default='')

    is_current = models.BooleanField(default=True)

    class Meta:
        ordering = ['-proposed_at']
        constraints = [
            # Exactly one live commitment per assignment. Partial (condition=) so any
            # number of superseded rows may coexist with is_current=False.
            models.UniqueConstraint(
                fields=['assignment'],
                condition=models.Q(is_current=True),
                name='uniq_current_due_date_per_assignment',
            ),
        ]

    def __str__(self):
        state = 'approved' if self.approved_at else 'proposed'
        return f"Due {self.proposed_date} ({state}) — assignment {self.assignment_id}"


class DesignAttempt(models.Model):
    """One pass at designing the site. A failed QC or a PM change request closes the
    current attempt and opens a new one, with opened_reason recording WHICH of the two
    loops caused it (settled decision 3).

    qc_started_at opens the PM change request window — before QC starts there is
    nothing settled to raise a change against.

    PART 9 — WHICH FIELD IS WHICH GATE. READ THIS BEFORE TOUCHING EITHER.

        qc_verdict / qc_remarks / qc_reviewed_by / qc_reviewed_at
            THE DESIGN QC GATE — the FIRST reviewer. These fields are unchanged in name
            and in type; only their meaning is narrowed, from "the review" to "the first
            review". Renaming them was rejected: it is a schema churn with no user-visible
            benefit that would also invalidate the migration-0049 CHECK constraint, which
            continues to guard exactly the right thing (a QC failure must carry remarks).

        head_verdict / head_remarks / head_reviewed_by / head_reviewed_at
            THE DESIGN HEAD GATE — the SECOND reviewer, added by Part 9.

    THE GATES ARE SERIAL, NOT PARALLEL. `head_verdict` cannot leave 'pending' while
    `qc_verdict` is still 'pending'; the Head reviews what QC has already passed, or he
    reviews nothing. A QC FAILURE ENDS THE ATTEMPT — the Head never sees it — so a row
    with qc_verdict='failed' keeps head_verdict='pending' forever, exactly as a row closed
    by a PM change request keeps qc_verdict='pending' forever. In both cases 'pending'
    means NOT JUDGED, never "judged and undecided".

    head_started_at is stamped when the package reaches the Head's queue (QC passed), and
    is the Head-side counterpart of qc_started_at. It does NOT open a second change
    request window; qc_started_at remains the only thing that does.
    """

    assignment = models.ForeignKey(
        DesignAssignment, on_delete=models.CASCADE, related_name='attempts',
    )
    attempt_number = models.PositiveIntegerField()
    opened_reason = models.CharField(
        max_length=20, choices=DESIGN_ATTEMPT_REASON_CHOICES,
        default=ATTEMPT_REASON_INITIAL,
    )
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    # ── GATE 1: Design QC ───────────────────────────────────────────────────
    qc_verdict = models.CharField(
        max_length=10, choices=QC_VERDICT_CHOICES, default=QC_PENDING,
    )
    # Required when qc_verdict is 'failed' — see the conditional-requirement note above.
    qc_remarks = models.TextField(blank=True, default='')
    qc_reviewed_by = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='qc_reviewed_attempts',
    )
    qc_reviewed_at = models.DateTimeField(null=True, blank=True)
    # Opens the PM change request window.
    qc_started_at = models.DateTimeField(null=True, blank=True)
    # Required when qc_verdict is 'failed' (Part 9). Enforced in the view rather than by a
    # CHECK constraint — see the note on head_failure_category below.
    qc_failure_category = models.CharField(
        max_length=30, choices=DESIGN_ERROR_CATEGORY_CHOICES, blank=True, default='',
    )

    # ── GATE 2: Design Head ─────────────────────────────────────────────────
    head_verdict = models.CharField(
        max_length=10, choices=QC_VERDICT_CHOICES, default=QC_PENDING,
    )
    # Required when head_verdict is 'failed' — CHECK constraint below, mirroring 0049.
    head_remarks = models.TextField(blank=True, default='')
    head_reviewed_by = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='head_reviewed_attempts',
    )
    head_reviewed_at = models.DateTimeField(null=True, blank=True)
    # Stamped when Design QC passes and the package enters the Head's queue. The Head-side
    # counterpart of qc_started_at; it opens NO change request window of its own.
    head_started_at = models.DateTimeField(null=True, blank=True)
    # CATEGORY IS NOT CHECK-CONSTRAINED, deliberately, and this is the one asymmetry with
    # the remarks fields. A CHECK can only read its own row's columns, which is enough for
    # "failed implies remarks non-empty" — but a category must ALSO be one of a known set,
    # and encoding sixteen literals into a database constraint means a migration every
    # time the list changes. The list will change; the requirement will not. So the view
    # enforces both halves (present AND valid) and the column stays a plain CharField.
    head_failure_category = models.CharField(
        max_length=30, choices=DESIGN_ERROR_CATEGORY_CHOICES, blank=True, default='',
    )

    # ── Overturn signal (Part 9, settled decision 6) ─────────────────────────
    # STORED AT WRITE TIME, not derived. Set True when the Head's outcome differs from
    # Design QC's on this attempt — QC passed and the Head failed, or QC failed and the
    # Head passed (unreachable today, since a QC failure closes the attempt, but the
    # field does not assume that).
    #
    # WHY STORED RATHER THAN A PROPERTY: the requirement is that it be COUNTABLE per QC
    # reviewer and per tender. A Python property cannot be aggregated in a queryset, so
    # every consumer would have to reconstruct the pair comparison in its own Q object —
    # which is the same "hardcoded in a second place" failure the category group helper
    # exists to prevent. One boolean column answers it in one term:
    #
    #   DesignAttempt.objects.filter(head_overturned_qc=True).values('qc_reviewed_by')
    #   ...filter(assignment__project__program=tender, head_overturned_qc=True).count()
    head_overturned_qc = models.BooleanField(default=False)

    # ── BOQ ─────────────────────────────────────────────────────────────────
    # The design workflow RECORDS that a BOQ was submitted; it does not duplicate BOQ
    # data (settled decision 7). The BOQ / BOQItem / BOQItemMaster models are used
    # as-is and are not touched by this module.
    boq_submitted_at = models.DateTimeField(null=True, blank=True)
    boq_submitted_by = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='boq_submitted_attempts',
    )

    # ── What the failing reviewer sent back (Part 9.1) ───────────────────────
    # The artifacts the reviewer ticked on the fail form, as a sorted JSON list of
    # DESIGN_REDO_CHOICES values. Recorded on the attempt that FAILED, and it describes
    # what the NEXT attempt must redo; anything absent was carried forward.
    #
    # STORED RATHER THAN RE-DERIVED FROM THE CATEGORY, because the reviewer can override
    # the category's suggestion and their choice is the fact that matters. Re-deriving it
    # from DEFAULT_REDO_BY_CATEGORY later would silently rewrite history the first time
    # somebody edited that map.
    #
    # Empty list on every attempt that was not failed, and on every pre-Part-9.1 row —
    # which reads as "no scoped rework was recorded", not "nothing had to be redone".
    redo_required = models.JSONField(default=list, blank=True)

    class Meta:
        ordering        = ['assignment', 'attempt_number']
        unique_together = ('assignment', 'attempt_number')
        constraints = [
            # A failed QC must say why. Expressed as the VALID case: either the verdict
            # is not 'failed', or remarks are non-empty. Self-contained (reads only this
            # row's own columns), which is what makes it expressible as a CHECK.
            models.CheckConstraint(
                condition=~models.Q(qc_verdict=QC_FAILED) | ~models.Q(qc_remarks=''),
                name='qc_remarks_required_when_qc_failed',
            ),
            # PART 9 — the same rule at the Head gate. Identical shape to the one above,
            # deliberately: a failure with no remarks is not a review at either gate.
            models.CheckConstraint(
                condition=~models.Q(head_verdict=QC_FAILED) | ~models.Q(head_remarks=''),
                name='head_remarks_required_when_head_failed',
            ),
        ]

    def __str__(self):
        return f"Attempt {self.attempt_number} — assignment {self.assignment_id} ({self.qc_verdict})"


class ArkaSubmission(models.Model):
    """One Arka design submitted for approval, versioned within an attempt.

    A rejected submission is superseded by a new version in the SAME attempt (a
    rejected Arka is not itself a rework loop); is_current points at the live version.

    PART 9 — WHICH FIELD IS WHICH GATE. READ THIS BEFORE TOUCHING EITHER.

        verdict / rejection_reason / reviewed_by / reviewed_at
            THE DESIGN QC GATE — the FIRST reviewer. Unchanged in name and type; the
            meaning narrows from "the review" to "the first review". Not renamed, for the
            same reason as on DesignAttempt: the migration-0049 CHECK constraint
            `rejection_reason_required_when_rejected` already guards exactly this field
            and continues to be correct.

        head_verdict / head_rejection_reason / head_reviewed_by / head_reviewed_at
            THE DESIGN HEAD GATE — the SECOND reviewer, added by Part 9.

    THE GATES ARE SERIAL. `head_verdict` cannot leave 'pending' while `verdict` is
    'pending'. A QC REJECTION ENDS THE VERSION — the Head never sees it — so a row with
    verdict='rejected' keeps head_verdict='pending' forever. 'pending' means NOT JUDGED.

    CAD AND BOQ UPLOAD NOW GATE ON head_verdict='approved', NOT verdict='approved'
    (design_views._require_approved_arka). An Arka that only Design QC has passed is not
    yet a layout anybody may build against.
    """

    attempt = models.ForeignKey(
        DesignAttempt, on_delete=models.CASCADE, related_name='arka_submissions',
    )
    version     = models.PositiveIntegerField()
    capacity_kw = models.DecimalField(max_digits=10, decimal_places=2)
    # URLField(max_length=1000) matches the existing file_url / photo_url convention.
    # This is an external Arka link, not a storage location, so it is a URL by nature
    # and is not subject to settled decision 8 (which governs stored FILES).
    arka_link   = models.URLField(max_length=1000)

    submitted_by = models.ForeignKey(
        'UserProfile', on_delete=models.PROTECT, related_name='arka_submissions',
    )
    submitted_at = models.DateTimeField(auto_now_add=True)

    # ── GATE 1: Design QC ───────────────────────────────────────────────────
    verdict = models.CharField(
        max_length=10, choices=ARKA_VERDICT_CHOICES, default=ARKA_PENDING,
    )
    # Required when verdict is 'rejected' — see the conditional-requirement note above.
    rejection_reason = models.TextField(blank=True, default='')
    reviewed_by = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='reviewed_arka_submissions',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    # Required when verdict is 'rejected' (Part 9). View-enforced, not CHECK-constrained —
    # see the note on DesignAttempt.head_failure_category.
    qc_failure_category = models.CharField(
        max_length=30, choices=DESIGN_ERROR_CATEGORY_CHOICES, blank=True, default='',
    )

    # ── GATE 2: Design Head ─────────────────────────────────────────────────
    head_verdict = models.CharField(
        max_length=10, choices=ARKA_VERDICT_CHOICES, default=ARKA_PENDING,
    )
    # Required when head_verdict is 'rejected' — CHECK constraint below, mirroring 0049.
    head_rejection_reason = models.TextField(blank=True, default='')
    head_reviewed_by = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='head_reviewed_arka_submissions',
    )
    head_reviewed_at = models.DateTimeField(null=True, blank=True)
    head_failure_category = models.CharField(
        max_length=30, choices=DESIGN_ERROR_CATEGORY_CHOICES, blank=True, default='',
    )

    # ── Overturn signal (Part 9, settled decision 6) ─────────────────────────
    # Stored at write time; see the long note on DesignAttempt.head_overturned_qc for why
    # this is a column rather than a property. True when the Head's outcome differs from
    # Design QC's on this Arka version — QC approved and the Head rejected, or the reverse.
    head_overturned_qc = models.BooleanField(default=False)

    # ── Carried forward (Part 9.1) ──────────────────────────────────────────
    # Set when this row was COPIED onto a new attempt because the failure that opened that
    # attempt did not name the Arka as needing rework. Points at the row it was copied
    # from, on the previous attempt.
    #
    # THE COPY CARRIES THE VERDICTS, AND THIS FIELD IS WHY THAT IS HONEST. Without it the
    # new attempt would show an Arka approved at both gates with no way to tell that
    # nobody reviewed it during THIS attempt — the audit trail would silently claim two
    # verdicts that were never recorded. Every surface that renders a verdict checks this
    # and says "carried forward from attempt N" instead.
    #
    # SET_NULL rather than PROTECT: a carried copy must never be able to block the
    # deletion of the attempt it came from.
    carried_forward_from = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='carried_forward_to',
    )

    is_current = models.BooleanField(default=True)

    class Meta:
        ordering        = ['attempt', 'version']
        unique_together = ('attempt', 'version')
        constraints = [
            # Exactly one live Arka version per attempt; superseded rows coexist freely.
            models.UniqueConstraint(
                fields=['attempt'],
                condition=models.Q(is_current=True),
                name='uniq_current_arka_per_attempt',
            ),
            # A rejected Arka must say why. Same shape as the QC constraint above:
            # expressed as the valid case, and self-contained to this row.
            models.CheckConstraint(
                condition=~models.Q(verdict=ARKA_REJECTED) | ~models.Q(rejection_reason=''),
                name='rejection_reason_required_when_rejected',
            ),
            # PART 9 — the same rule at the Head gate.
            models.CheckConstraint(
                condition=(~models.Q(head_verdict=ARKA_REJECTED)
                           | ~models.Q(head_rejection_reason='')),
                name='head_rejection_reason_required_when_head_rejected',
            ),
        ]

    def __str__(self):
        return f"Arka v{self.version} — attempt {self.attempt_id} ({self.verdict})"


class DesignFile(models.Model):
    """A CAD or BOQ artifact uploaded against an attempt, versioned per kind.

    ARTIFACT PAIRING (settled decision 4): derived_from_arka is NOT NULL. Every CAD
    and BOQ artifact records which Arka version it was drawn against, so QC can never
    review a CAD produced from a superseded layout. PROTECT on that FK, so an Arka
    version that artifacts derive from cannot be deleted out from under them.

    BUCKET AND PATH, NOT URLS (settled decision 8). No URL is stored or constructed
    here; the signed-URL helper is Part 2.
    """

    attempt = models.ForeignKey(
        DesignAttempt, on_delete=models.CASCADE, related_name='design_files',
    )
    kind    = models.CharField(max_length=20, choices=DESIGN_FILE_KIND_CHOICES)
    version = models.PositiveIntegerField()

    bucket = models.CharField(max_length=100)
    path   = models.CharField(max_length=500)
    original_filename = models.CharField(max_length=255, blank=True, default='')
    size_bytes        = models.PositiveBigIntegerField(null=True, blank=True)
    content_type      = models.CharField(max_length=100, blank=True, default='')

    # ARCHIVE LISTING (Part 8). A zip hides what is inside it: without this the QC
    # reviewer downloads the file and only then finds out a sheet is missing. Read
    # from the zip CENTRAL DIRECTORY at upload — the archive is never extracted to
    # disk — and rendered on the artifacts panel so the contents are visible without
    # downloading anything.
    #
    #   [{"name": "01-layout.pdf", "size": 482113}, ...]
    #
    # `size` is the UNCOMPRESSED size of that entry. Empty list for every non-archive
    # kind and for every pre-existing cad_pdf/cad_dwg row, which reads correctly as
    # "this is not an archive" rather than "this archive is empty" — nothing else in
    # the codebase asks an empty listing to mean anything more than that.
    archive_listing = models.JSONField(default=list, blank=True)

    derived_from_arka = models.ForeignKey(
        ArkaSubmission, on_delete=models.PROTECT, related_name='derived_files',
    )

    uploaded_by = models.ForeignKey(
        'UserProfile', on_delete=models.PROTECT, related_name='uploaded_design_files',
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    is_current = models.BooleanField(default=True)
    # Points at the DesignFile that replaced this one. SET_NULL so deleting the
    # replacement never cascades away the row it superseded.
    superseded_by = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='supersedes',
    )

    # ── Carried forward (Part 9.1) ──────────────────────────────────────────
    # Set when this row was COPIED onto a new attempt because the failure that opened that
    # attempt did not name CAD as needing rework. Points at the row it was copied from.
    #
    # THE COPY SHARES THE STORED OBJECT. `bucket` and `path` are copied verbatim, so the
    # designer does not re-upload a file that was never in question and no second copy of
    # a 25 MB archive is written. Both rows address the same object, which is safe because
    # nothing in this module ever deletes one.
    carried_forward_from = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='carried_forward_to',
    )

    class Meta:
        ordering        = ['attempt', 'kind', 'version']
        unique_together = ('attempt', 'kind', 'version')

    def __str__(self):
        return f"{self.kind} v{self.version} — attempt {self.attempt_id}"


class DesignChangeRequest(models.Model):
    """A PM asking for a change after QC has started on an attempt.

    This is the second of the two rework loops (settled decision 3). resulting_attempt
    links to the attempt this request opened, so a change request and the rework it
    caused stay associated; it is null until that attempt exists.
    """

    attempt = models.ForeignKey(
        DesignAttempt, on_delete=models.CASCADE, related_name='change_requests',
    )
    # The site's assigned PM. PROTECT keeps the requester on the record.
    requested_by = models.ForeignKey(
        'UserProfile', on_delete=models.PROTECT, related_name='design_change_requests',
    )
    reason       = models.TextField()
    requested_at = models.DateTimeField(auto_now_add=True)

    resulting_attempt = models.ForeignKey(
        DesignAttempt, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='caused_by_change_request',
    )

    class Meta:
        ordering = ['-requested_at']

    def __str__(self):
        return f"Change request — attempt {self.attempt_id}"


# ---------------------------------------------------------------------------
# Part 6 — site groups (grouped procurement)
#
# PROCUREMENT NEVER HAPPENS FOR A WHOLE TENDER. It happens for a manually formed
# batch of released sites, and SCM forms it — they know order economics and lead
# times, Design does not. A group is therefore a plain container with a two-state
# lifecycle, not a stage of the design workflow.
#
# THE PER-SITE BOQ IS NOT REPLACED. Aggregation is a read — `BOQItem.boq_quantity`
# summed over `item_master` across the member sites. Nothing here copies, snapshots
# or supersedes a BOQ row, because per-site profitability and expense tracking
# (out of scope for this version) need the per-site figures to stay intact.
#
# NO save() OVERRIDES AND NO SIGNALS, matching every prior part. Both transitions —
# locking a group, and soft-removing a membership — live in the view layer next to
# the permission check that authorises them.
# ---------------------------------------------------------------------------

SITE_GROUP_DRAFT  = 'draft'
SITE_GROUP_LOCKED = 'locked'

SITE_GROUP_STATUS_CHOICES = [
    (SITE_GROUP_DRAFT,  'Draft'),
    (SITE_GROUP_LOCKED, 'Locked'),
]


class SiteGroup(models.Model):
    """A batch of released sites under one tender, procured together.

    TWO STATES ONLY. `draft` gains and loses sites freely; `locked` is a deliberate
    SCM action that freezes the member sites' BOQ quantities so the aggregate the
    purchase order is raised against cannot move underneath it.

    UNLOCKING IS NOT BUILT AND THAT IS DELIBERATE. A locked group is final. Once a
    quantity has been committed to a vendor, the correction is a variance against the
    order, not an edit to the BOQ it was raised from — and variance handling is a
    separate feature. Recorded as a known gap rather than half-built here.
    """

    program = models.ForeignKey(
        Program, on_delete=models.CASCADE, related_name='site_groups',
    )
    name   = models.CharField(max_length=120)
    status = models.CharField(
        max_length=10, choices=SITE_GROUP_STATUS_CHOICES, default=SITE_GROUP_DRAFT,
    )

    created_by = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='created_site_groups',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    locked_by = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='locked_site_groups',
    )
    locked_at = models.DateTimeField(null=True, blank=True)

    notes = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} — {self.program.name} ({self.status})"


class SiteGroupMembership(models.Model):
    """One site's place in one group.

    REMOVAL IS SOFT. The row stays with `removed_at` / `removed_by` / `removal_reason`
    set, so "which sites were in this group, and why did one leave" stays answerable
    afterwards. A hard delete would erase exactly the fact SCM needs when the aggregate
    they were working from changes under them — most importantly when a PM change
    request pulls a site out (settled decision 6).

    The partial unique constraint is the real enforcement of "a site belongs to at most
    one group at a time" (settled decision 2). The view checks it too, for a decent error
    message, but the database is what makes it true — a view check alone loses to a
    concurrent add.
    """

    group = models.ForeignKey(
        SiteGroup, on_delete=models.CASCADE, related_name='memberships',
    )
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name='group_memberships',
    )

    added_by = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='added_group_memberships',
    )
    added_at = models.DateTimeField(auto_now_add=True)

    removed_by = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='removed_group_memberships',
    )
    removed_at     = models.DateTimeField(null=True, blank=True)
    removal_reason = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['added_at']
        constraints = [
            # At most ONE live membership per project, across every group in the system.
            # Partial (condition=) so any number of removed rows may coexist with it —
            # the history is kept, the exclusivity is not weakened by keeping it.
            models.UniqueConstraint(
                fields=['project'],
                condition=models.Q(removed_at__isnull=True),
                name='uniq_active_site_group_membership',
            ),
        ]

    def __str__(self):
        state = 'removed' if self.removed_at else 'active'
        return f"{self.project.project_id} in {self.group_id} ({state})"
