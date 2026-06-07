from django.db import models
from django.contrib.auth.models import User


class Project(models.Model):

    PROJECT_TYPE_CHOICES = [
        ('residential', 'Residential'),
        ('opex', 'OPEX'),
        ('capex', 'CAPEX'),
    ]

    STATUS_CHOICES = [
        ('enquiry', 'Enquiry'),
        ('design', 'Design'),
        ('procurement', 'Procurement'),
        ('installation', 'Installation'),
        ('testing', 'Testing & Commissioning'),
        ('completed', 'Completed'),
        ('on_hold', 'On Hold'),
        ('cancelled', 'Cancelled'),
    ]

    # Core fields
    project_code    = models.CharField(max_length=20, unique=True)
    name            = models.CharField(max_length=200)
    project_type    = models.CharField(max_length=20, choices=PROJECT_TYPE_CHOICES)
    status          = models.CharField(max_length=20, choices=STATUS_CHOICES, default='enquiry')

    # Customer
    customer_name   = models.CharField(max_length=200)
    customer_phone  = models.CharField(max_length=20, blank=True)
    customer_email  = models.EmailField(blank=True)
    site_address    = models.TextField()
    city            = models.CharField(max_length=100)
    state           = models.CharField(max_length=100, default='Uttar Pradesh')

    # Technical
    capacity_kwp    = models.DecimalField(max_digits=8, decimal_places=2, help_text='System capacity in kWp')
    panel_count     = models.IntegerField(null=True, blank=True)
    inverter_type   = models.CharField(max_length=100, blank=True)

    # Financial
    contract_value  = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    amount_received = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # Team
    project_manager = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name='managed_projects')

    # Dates
    start_date      = models.DateField(null=True, blank=True)
    expected_completion = models.DateField(null=True, blank=True)
    actual_completion   = models.DateField(null=True, blank=True)

    # Subsidy (for residential PM Suryaghar)
    subsidy_applicable  = models.BooleanField(default=False)
    subsidy_amount      = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    subsidy_status      = models.CharField(max_length=50, blank=True)

    # Meta
    notes           = models.TextField(blank=True)
    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.project_code} — {self.name}"

    @property
    def balance_amount(self):
        if self.contract_value:
            return self.contract_value - self.amount_received
        return None


class Milestone(models.Model):

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('delayed', 'Delayed'),
    ]

    project     = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='milestones')
    title       = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    due_date    = models.DateField(null=True, blank=True)
    completed_date = models.DateField(null=True, blank=True)
    status      = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    order       = models.IntegerField(default=0)

    class Meta:
        ordering = ['order', 'due_date']

    def __str__(self):
        return f"{self.project.project_code} — {self.title}"


class ProjectDocument(models.Model):

    DOC_TYPE_CHOICES = [
        ('contract',     'Contract'),
        ('drawing',      'Drawing / Layout'),
        ('approval',     'Government Approval'),
        ('subsidy',      'Subsidy Document'),
        ('invoice',      'Invoice'),
        ('photo',        'Site Photo'),
        ('other',        'Other'),
    ]

    project     = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='documents')
    doc_type    = models.CharField(max_length=20, choices=DOC_TYPE_CHOICES)
    title       = models.CharField(max_length=200)
    file        = models.FileField(upload_to='project_docs/%Y/%m/')
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    notes       = models.TextField(blank=True)

    def __str__(self):
        return f"{self.project.project_code} — {self.title}"