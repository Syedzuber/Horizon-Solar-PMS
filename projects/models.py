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

    project_id                = models.CharField(max_length=20, unique=True, editable=False, blank=True)  # Auto-generated: HRP-{PREFIX}-{YEAR}-{NNN}
    customer_name             = models.CharField(max_length=100)
    customer_phone            = models.CharField(max_length=10)
    customer_email            = models.EmailField(blank=True, null=True)
    site_address              = models.TextField()
    city                      = models.CharField(max_length=50)
    state                     = models.CharField(max_length=50, default='Uttar Pradesh')
    project_type              = models.CharField(max_length=20, choices=PROJECT_TYPE_CHOICES)
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
    assigned_site_engineer    = models.ForeignKey(
        'UserProfile',
        limit_choices_to={'role': 'Site Engineer', 'is_active': True},
        related_name='se_projects',
        on_delete=models.PROTECT,
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
    survey_date               = models.DateField(blank=True, null=True)
    target_commissioning_date = models.DateField(blank=True, null=True)
    status                    = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Draft')
    zoho_crm_id               = models.CharField(max_length=50, blank=True, null=True)
    zoho_deal_id              = models.CharField(max_length=100, blank=True, default='')  # Stores Zoho Record Id for duplicate webhook guard
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

    def save(self, *args, **kwargs):
        # Generate project_id only on first save; subsequent saves skip ID generation
        if not self.project_id:
            # import inside method to avoid circular import at module level
            from django.db import transaction
            from .utils import generate_project_id
            with transaction.atomic():
                self.project_id = generate_project_id(self.project_type)
                super().save(*args, **kwargs)
        else:
            super().save(*args, **kwargs)


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

    phase         = models.ForeignKey(ProjectPhase, related_name='tasks', on_delete=models.CASCADE)
    task_name     = models.CharField(max_length=200)
    task_order    = models.PositiveIntegerField()  # Ascending within phase; drives due-date chain
    assigned_role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=PM)
    assigned_to   = models.ForeignKey(
        'UserProfile',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,  # Reassign rather than block when a user is removed
        related_name='assigned_tasks',
    )
    status        = models.CharField(max_length=20, choices=STATUS_CHOICES, default=NOT_STARTED)
    task_type     = models.CharField(max_length=10, choices=TYPE_CHOICES, default=INTERNAL)
    duration_days = models.PositiveIntegerField(default=1)  # Calendar days used in due-date chain calculation
    due_date      = models.DateField(blank=True, null=True)
    completed_at  = models.DateTimeField(blank=True, null=True)  # Set when status transitions to Done
    created_at    = models.DateTimeField(auto_now_add=True)

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


class UserProfile(models.Model):
    """Extends Django's User with a role and phone number. One profile per user."""

    ROLE_CHOICES = [
        ('Admin',         'Admin'),
        ('PM',            'PM'),
        ('Site Engineer', 'Site Engineer'),
        ('Design',        'Design'),
        ('Finance',       'Finance'),
        ('SCM',           'SCM'),
        ('CEO',           'CEO'),
        ('BD',            'BD'),
    ]

    user         = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    role         = models.CharField(max_length=20, choices=ROLE_CHOICES, blank=True)
    phone_number = models.CharField(max_length=10, blank=True)
    is_active    = models.BooleanField(default=True)  # Soft deactivation — keeps history without deleting the user
    created_at   = models.DateTimeField(auto_now_add=True)
    created_by   = models.ForeignKey(
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


def get_standard_boq_items():
    """
    Return the 37-item standard BOQ template for a Residential solar project.
    Used when auto-creating a BOQ for Design users; serial numbers must stay stable.
    """
    return [
        {'serial_no':  1, 'category': 'Solar Modules', 'description': '595Wp Solar modules DCR',                                                                           'uom': 'Nos'},
        {'serial_no':  2, 'category': 'Solar Modules', 'description': 'Module Transport',                                                                                  'uom': 'Nos'},
        {'serial_no':  3, 'category': 'Structure',     'description': 'Module Mounting Structure with STAAD report HDGI/GI',                                               'uom': 'LOT'},
        {'serial_no':  4, 'category': 'Structure',     'description': 'Module Mounting Structures transport',                                                              'uom': 'LOT'},
        {'serial_no':  5, 'category': 'Inverter',      'description': '10 kW Grid-Tie Inverter Single Phase',                                                              'uom': 'Nos'},
        {'serial_no':  6, 'category': 'Inverter',      'description': 'Data Logger if required',                                                                           'uom': 'Nos'},
        {'serial_no':  7, 'category': 'Inverter',      'description': 'Inverter Transport',                                                                                'uom': 'Nos'},
        {'serial_no':  8, 'category': 'BOS',           'description': '1CX4sqmm XLPE Tin Cu string cables RED',                                                           'uom': 'Mtr'},
        {'serial_no':  9, 'category': 'BOS',           'description': '1CX4sqmm XLPE Tin Cu string cables Black',                                                         'uom': 'Mtr'},
        {'serial_no': 10, 'category': 'BOS',           'description': '4C X 6mm2 XLPE Tin Cu cables',                                                                     'uom': 'Mtr'},
        {'serial_no': 11, 'category': 'BOS',           'description': '1CX6mm2 Earthing cable Green',                                                                     'uom': 'Mtr'},
        {'serial_no': 12, 'category': 'BOS',           'description': 'MC4 Connectors Male and Female',                                                                   'uom': 'Nos'},
        {'serial_no': 13, 'category': 'BOS',           'description': 'PVC Conduit 25MM',                                                                                 'uom': 'Mtr'},
        {'serial_no': 14, 'category': 'BOS',           'description': 'Flexible Conduit 25MM GI/PVC',                                                                     'uom': 'Mtr'},
        {'serial_no': 15, 'category': 'BOS',           'description': 'PVC Elbow 25MM',                                                                                   'uom': 'Nos'},
        {'serial_no': 16, 'category': 'BOS',           'description': 'PVC Tee 25MM',                                                                                     'uom': 'Nos'},
        {'serial_no': 17, 'category': 'BOS',           'description': 'PVC Nail Clip 25MM 150PCS',                                                                        'uom': 'Pkt'},
        {'serial_no': 18, 'category': 'BOS',           'description': 'CU Pin LUG 4 SQMM',                                                                               'uom': 'Nos'},
        {'serial_no': 19, 'category': 'BOS',           'description': 'CU Ring LUG 4 SQMM',                                                                              'uom': 'Nos'},
        {'serial_no': 20, 'category': 'BOS',           'description': 'CU Pin LUG 6 SQMM',                                                                               'uom': 'Nos'},
        {'serial_no': 21, 'category': 'BOS',           'description': 'CU Ring LUG 6 SQMM',                                                                              'uom': 'Nos'},
        {'serial_no': 22, 'category': 'BOS',           'description': 'Cable Tie 300MM UV resistant',                                                                     'uom': 'Pkt'},
        {'serial_no': 23, 'category': 'BOS',           'description': 'PVC Tape Red Blue Black Yellow Green',                                                             'uom': 'Nos'},
        {'serial_no': 24, 'category': 'BOS',           'description': 'Silver Spray Paint',                                                                               'uom': 'Nos'},
        {'serial_no': 25, 'category': 'BOS',           'description': 'Lockfix for fixing fastener in RCC roof 500ML',                                                    'uom': 'Nos'},
        {'serial_no': 26, 'category': 'BOS',           'description': 'HSV Hilti M12 Mechanical Wedge Anchors L-100MM',                                                   'uom': 'Nos'},
        {'serial_no': 27, 'category': 'BOS',           'description': 'Fasteners Inverter Mounting',                                                                      'uom': 'Nos'},
        {'serial_no': 28, 'category': 'BOS',           'description': 'ACDB-10KW 3P MCB4P 16 AMPS',                                                                      'uom': 'Nos'},
        {'serial_no': 29, 'category': 'BOS',           'description': 'Copper Bonded Earthing Rod 1MTR chemical earthing',                                                'uom': 'Nos'},
        {'serial_no': 30, 'category': 'BOS',           'description': 'Earthing Compound bag 25KG ECOSOLX',                                                               'uom': 'Nos'},
        {'serial_no': 31, 'category': 'BOS',           'description': 'Heavy duty synthetic circular chamber',                                                            'uom': 'Nos'},
        {'serial_no': 32, 'category': 'BOS',           'description': 'Conventional Lightning Arrestor 1Mtr IEC62305',                                                    'uom': 'Nos'},
        {'serial_no': 33, 'category': 'BOS',           'description': 'PU Foam Sealant Spray 750ml for joint filling',                                                    'uom': 'Nos'},
        {'serial_no': 34, 'category': 'BOS',           'description': 'Module Cleaning System without motor',                                                             'uom': 'Nos'},
        {'serial_no': 35, 'category': 'BOS',           'description': 'Site Installation charges including civil work',                                                    'uom': 'Nos'},
        {'serial_no': 36, 'category': 'BOS',           'description': 'Miscellaneous net metering transportation rubber mat fire extinguisher warning boards',             'uom': 'Nos'},
        {'serial_no': 37, 'category': 'BOS',           'description': 'Contingency',                                                                                      'uom': 'LS'},
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
    description      = models.TextField()
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


def recalculate_dc_status(challan):
    """
    Recalculate and save DeliveryChallan status based on line item receipt state.
    Called after every GRN confirmation to keep DC status in sync.
    Must be called ONCE after all line items are saved — never inside the save loop.
    """
    items     = challan.line_items.all()
    confirmed = items.filter(received_quantity__isnull=False)

    if confirmed.count() == 0:
        # No items confirmed yet
        challan.status = DeliveryChallan.EXPECTED
    elif confirmed.count() < items.count():
        # Some but not all items confirmed
        challan.status = DeliveryChallan.PARTIALLY_RECEIVED
    else:
        # All items confirmed — check if any are damaged or partial
        has_damage = confirmed.filter(
            condition__in=[DCLineItem.DAMAGED, DCLineItem.PARTIAL]
        ).exists()
        challan.status = (
            DeliveryChallan.PARTIALLY_RECEIVED if has_damage
            else DeliveryChallan.RECEIVED
        )
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

        has_damage = items.filter(
            condition__in=['Damaged', 'Partial']
        ).exists()

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


def log_activity(project, actor, action, entity_type='', entity_id=None):
    """
    Write one ActivityLog entry. Silently swallows exceptions so a failed log
    never aborts the primary operation that called it.
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
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"ActivityLog failed: {e}")
