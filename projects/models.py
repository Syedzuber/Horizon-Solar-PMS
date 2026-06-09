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
    capacity_kw               = models.DecimalField(max_digits=6, decimal_places=2)
    contract_value            = models.DecimalField(max_digits=12, decimal_places=2)
    assigned_pm               = models.ForeignKey(
        'UserProfile',
        limit_choices_to={'role': 'PM'},
        related_name='pm_projects',
        on_delete=models.PROTECT,
    )
    assigned_site_engineer    = models.ForeignKey(
        'UserProfile',
        limit_choices_to={'role': 'Site Engineer', 'is_active': True},
        related_name='se_projects',
        on_delete=models.PROTECT,
    )
    survey_date               = models.DateField(blank=True, null=True)
    target_commissioning_date = models.DateField()
    status                    = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Draft')
    zoho_crm_id               = models.CharField(max_length=50, blank=True, null=True)
    created_at                = models.DateTimeField(auto_now_add=True)
    created_by                = models.ForeignKey(
        User,
        related_name='created_projects',
        on_delete=models.PROTECT,
    )
    activated_at              = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.project_id} - {self.customer_name}"

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

    phase         = models.ForeignKey(ProjectPhase, related_name='tasks', on_delete=models.CASCADE)
    task_name     = models.CharField(max_length=200)
    task_order    = models.PositiveIntegerField()
    assigned_role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=PM)
    status        = models.CharField(max_length=20, choices=STATUS_CHOICES, default=NOT_STARTED)
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

    project        = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='milestones')
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


class UserProfile(models.Model):

    ROLE_CHOICES = [
        ('Admin',         'Admin'),
        ('PM',            'PM'),
        ('Site Engineer', 'Site Engineer'),
        ('Design',        'Design'),
        ('Finance',       'Finance'),
        ('SCM',           'SCM'),
        ('CEO',           'CEO'),
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
