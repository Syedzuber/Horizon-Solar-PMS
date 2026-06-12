from django.db import models
from django.contrib.auth.models import User


class Project(models.Model):

    PROJECT_TYPE_CHOICES = [
        ('Residential', 'Residential'),
        ('OPEX',        'OPEX'),
        ('CAPEX',       'CAPEX'),
    ]

    STATUS_CHOICES = [
        ('Draft',        'Draft'),
        ('Active',       'Active'),
        ('In Progress',  'In Progress'),
        ('Commissioned', 'Commissioned'),
        ('On Hold',      'On Hold'),
        ('Cancelled',    'Cancelled'),
    ]

    project_id                = models.CharField(max_length=20, unique=True, editable=False, blank=True)
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
        on_delete=models.PROTECT,
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
        on_delete=models.SET_NULL,
        related_name='design_projects',
        limit_choices_to={'role': 'Design'},
    )
    survey_date               = models.DateField(blank=True, null=True)
    target_commissioning_date = models.DateField(blank=True, null=True)
    status                    = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Draft')
    zoho_crm_id               = models.CharField(max_length=50, blank=True, null=True)
    zoho_deal_id              = models.CharField(max_length=100, blank=True, default='')
    customer_contact_person   = models.CharField(max_length=255, blank=True, default='')
    created_at                = models.DateTimeField(auto_now_add=True)
    created_by                = models.ForeignKey(
        User,
        related_name='created_projects',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    activated_at              = models.DateTimeField(blank=True, null=True)
    commissioned_at           = models.DateField(null=True, blank=True)

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
        if not self.project_id:
            from django.db import transaction
            from .utils import generate_project_id
            with transaction.atomic():
                self.project_id = generate_project_id(self.project_type)
                super().save(*args, **kwargs)
        else:
            super().save(*args, **kwargs)


class ProjectPhase(models.Model):

    project     = models.ForeignKey(Project, related_name='phases', on_delete=models.CASCADE)
    phase_name  = models.CharField(max_length=100)
    phase_order = models.PositiveIntegerField()
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['phase_order']

    def __str__(self):
        return f"{self.project.project_id} — {self.phase_name}"


class Task(models.Model):

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

    INTERNAL = 'Internal'
    EXTERNAL = 'External'

    TYPE_CHOICES = [
        (INTERNAL, 'Internal'),
        (EXTERNAL, 'External'),
    ]

    phase         = models.ForeignKey(ProjectPhase, related_name='tasks', on_delete=models.CASCADE)
    task_name     = models.CharField(max_length=200)
    task_order    = models.PositiveIntegerField()
    assigned_role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=PM)
    assigned_to   = models.ForeignKey(
        'UserProfile',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='assigned_tasks',
    )
    status        = models.CharField(max_length=20, choices=STATUS_CHOICES, default=NOT_STARTED)
    task_type     = models.CharField(max_length=10, choices=TYPE_CHOICES, default=INTERNAL)
    duration_days = models.PositiveIntegerField(default=1)
    due_date      = models.DateField(blank=True, null=True)
    completed_at  = models.DateTimeField(blank=True, null=True)
    created_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['task_order']

    def __str__(self):
        return self.task_name


class Milestone(models.Model):

    STATUS_CHOICES = [
        ('pending',     'Pending'),
        ('in_progress', 'In Progress'),
        ('completed',   'Completed'),
        ('delayed',     'Delayed'),
    ]

    project        = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='old_milestones')
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

    DOC_TYPE_CHOICES = [
        ('contract',  'Contract'),
        ('drawing',   'Drawing / Layout'),
        ('approval',  'Government Approval'),
        ('subsidy',   'Subsidy Document'),
        ('invoice',   'Invoice'),
        ('photo',     'Site Photo'),
        ('other',     'Other'),
    ]

    project     = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='documents')
    doc_type    = models.CharField(max_length=20, choices=DOC_TYPE_CHOICES)
    title       = models.CharField(max_length=200)
    file        = models.FileField(upload_to='project_docs/%Y/%m/')
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    notes       = models.TextField(blank=True)

    def __str__(self):
        return f"{self.project.project_id} — {self.title}"


class DueDateChangeLog(models.Model):
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
    is_active    = models.BooleanField(default=True)
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
    name = models.CharField(max_length=50, unique=True)

    class Meta:
        ordering = ['name']
        verbose_name_plural = 'Vendor Categories'

    def __str__(self):
        return self.name


class Vendor(models.Model):
    name           = models.CharField(max_length=200)
    contact_person = models.CharField(max_length=100)
    phone          = models.CharField(max_length=15)
    email          = models.EmailField(null=True, blank=True)
    gst_number     = models.CharField(max_length=15, null=True, blank=True)
    msme_status    = models.BooleanField(default=False)
    msme_number    = models.CharField(max_length=50, null=True, blank=True)
    address        = models.TextField(null=True, blank=True)
    categories     = models.ManyToManyField(VendorCategory, related_name='vendors')
    is_active      = models.BooleanField(default=True)
    created_by     = models.ForeignKey(
        'UserProfile',
        on_delete=models.SET_NULL,
        null=True,
    )
    created_at     = models.DateTimeField(auto_now_add=True)
    updated_at     = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_active', 'name']

    def __str__(self):
        return self.name


def get_standard_boq_items():
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
    version      = models.IntegerField(default=1)
    notes        = models.TextField(null=True, blank=True)

    def __str__(self):
        return f"BOQ — {self.project.project_id} (v{self.version})"


class BOQItem(models.Model):

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
        related_name='preferred_items',
    )
    uom              = models.CharField(max_length=10, choices=UOM_CHOICES)
    boq_quantity     = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    ordered_quantity = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    ordered_vendor   = models.ForeignKey(
        Vendor,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ordered_items',
    )
    is_standard_item = models.BooleanField(default=True)

    class Meta:
        ordering = ['serial_no']

    def __str__(self):
        return f"{self.boq.project.project_id} — Item {self.serial_no}: {self.description[:50]}"


class BOQRevision(models.Model):

    boq        = models.ForeignKey(BOQ, on_delete=models.CASCADE, related_name='revisions')
    revised_by = models.ForeignKey('UserProfile', on_delete=models.SET_NULL, null=True)
    revised_at = models.DateTimeField(auto_now_add=True)
    version    = models.IntegerField()
    reason     = models.TextField()
    snapshot   = models.JSONField()

    class Meta:
        ordering = ['-version']

    def __str__(self):
        return f"BOQ {self.boq.project.project_id} — v{self.version} by {self.revised_by}"


class Notification(models.Model):

    recipient  = models.ForeignKey(
        'UserProfile',
        on_delete=models.CASCADE,
        related_name='notifications',
    )
    message    = models.TextField()
    link       = models.CharField(max_length=200, blank=True)
    is_read    = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Notification → {self.recipient} — {self.message[:60]}"


class PaymentMilestone(models.Model):
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
    amount               = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    amount_received      = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    variance_reason      = models.CharField(max_length=255, blank=True, default='')
    due_date             = models.DateField(null=True, blank=True)
    invoice_date         = models.DateField(null=True, blank=True)
    received_date        = models.DateField(null=True, blank=True)
    status               = models.CharField(max_length=20, choices=STATUS_CHOICES, default=PENDING)
    created_by           = models.ForeignKey(UserProfile, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ['milestone_name']

    def __str__(self):
        return f"{self.project.project_id} — {self.milestone_name}"
