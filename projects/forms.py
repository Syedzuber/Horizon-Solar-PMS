import re
from django import forms
from django.contrib.auth.models import User
from .models import UserProfile, Project, ProjectPhase, Task


class UserCreateForm(forms.Form):
    first_name = forms.CharField(
        max_length=50,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )
    last_name = forms.CharField(
        max_length=50,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )
    username = forms.CharField(
        max_length=50,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={'class': 'form-control'}),
    )
    password = forms.CharField(
        min_length=8,
        widget=forms.PasswordInput(attrs={'class': 'form-control'}),
    )
    role = forms.ChoiceField(
        choices=UserProfile.ROLE_CHOICES,
        widget=forms.Select(attrs={'class': 'form-control'}),
    )
    phone_number = forms.CharField(
        max_length=10,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )
    is_active = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
    )

    def clean_first_name(self):
        value = self.cleaned_data['first_name'].strip()
        if not re.fullmatch(r'[A-Za-z]+', value):
            raise forms.ValidationError('First name must contain letters only.')
        return value

    def clean_last_name(self):
        value = self.cleaned_data['last_name'].strip()
        if not re.fullmatch(r'[A-Za-z]+', value):
            raise forms.ValidationError('Last name must contain letters only.')
        return value

    def clean_username(self):
        value = self.cleaned_data['username'].strip().lower()
        if not re.fullmatch(r'[a-z0-9._]+', value):
            raise forms.ValidationError(
                'Username may only contain lowercase letters, digits, dots, and underscores.'
            )
        if User.objects.filter(username=value).exists():
            raise forms.ValidationError('This username is already taken.')
        return value

    def clean_phone_number(self):
        value = self.cleaned_data['phone_number'].strip()
        if not re.fullmatch(r'[0-9]{10}', value):
            raise forms.ValidationError('Phone number must be exactly 10 digits.')
        if value[0] not in '6789':
            raise forms.ValidationError('Phone number must start with 6, 7, 8, or 9.')
        return value

    def clean(self):
        cleaned = super().clean()
        role = cleaned.get('role')
        if role == 'Admin':
            already_exists = UserProfile.objects.filter(role='Admin').exists()
            if already_exists:
                raise forms.ValidationError('Only one Admin account is permitted.')
        return cleaned


class UserEditForm(forms.Form):
    first_name = forms.CharField(
        max_length=50,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )
    last_name = forms.CharField(
        max_length=50,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )
    role = forms.ChoiceField(
        choices=UserProfile.ROLE_CHOICES,
        widget=forms.Select(attrs={'class': 'form-control'}),
    )
    phone_number = forms.CharField(
        max_length=10,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )
    is_active = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
    )

    email = forms.EmailField(
        widget=forms.EmailInput(attrs={'class': 'form-control'}),
    )

    def __init__(self, *args, instance_user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._instance_user = instance_user

    def clean_first_name(self):
        value = self.cleaned_data['first_name'].strip()
        if not re.fullmatch(r'[A-Za-z]+', value):
            raise forms.ValidationError('First name must contain letters only.')
        return value

    def clean_last_name(self):
        value = self.cleaned_data['last_name'].strip()
        if not re.fullmatch(r'[A-Za-z]+', value):
            raise forms.ValidationError('Last name must contain letters only.')
        return value

    def clean_phone_number(self):
        value = self.cleaned_data['phone_number'].strip()
        if not re.fullmatch(r'[0-9]{10}', value):
            raise forms.ValidationError('Phone number must be exactly 10 digits.')
        if value[0] not in '6789':
            raise forms.ValidationError('Phone number must start with 6, 7, 8, or 9.')
        return value

    def clean(self):
        cleaned = super().clean()
        role = cleaned.get('role')
        if role == 'Admin' and self._instance_user:
            already_exists = UserProfile.objects.filter(role='Admin').exclude(
                user=self._instance_user
            ).exists()
            if already_exists:
                raise forms.ValidationError('Only one Admin account is permitted.')
        return cleaned


# ---------------------------------------------------------------------------
# Project forms
# ---------------------------------------------------------------------------

_PHONE_RE = re.compile(r'[0-9]{10}')


def _validate_phone(value):
    if not _PHONE_RE.fullmatch(value):
        raise forms.ValidationError('Phone number must be exactly 10 digits.')
    if value[0] not in '6789':
        raise forms.ValidationError('Phone number must start with 6, 7, 8, or 9.')


class ProjectCreateForm(forms.ModelForm):

    class Meta:
        model = Project
        fields = [
            'customer_name',
            'customer_phone',
            'customer_email',
            'site_address',
            'city',
            'state',
            'project_type',
            'capacity_kw',
            'contract_value',
            'assigned_site_engineer',
            'survey_date',
            'target_commissioning_date',
            'zoho_crm_id',
        ]
        labels = {
            'capacity_kw': 'Capacity (kW)',
        }
        widgets = {
            'site_address':              forms.Textarea(attrs={'rows': 3}),
            'survey_date':               forms.DateInput(attrs={'type': 'date'}),
            'target_commissioning_date': forms.DateInput(attrs={'type': 'date'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['assigned_site_engineer'].queryset = (
            UserProfile.objects.filter(role='Site Engineer', is_active=True)
            .select_related('user')
        )
        self.fields['assigned_site_engineer'].label_from_instance = (
            lambda obj: f"{obj.user.get_full_name() or obj.user.username}"
        )

    def clean_customer_phone(self):
        value = self.cleaned_data['customer_phone'].strip()
        _validate_phone(value)
        return value

    def clean_capacity_kw(self):
        value = self.cleaned_data.get('capacity_kw')
        if value is not None and value <= 0:
            raise forms.ValidationError('Capacity must be greater than zero.')
        return value

    def clean_contract_value(self):
        value = self.cleaned_data.get('contract_value')
        if value is not None and value <= 0:
            raise forms.ValidationError('Contract value must be greater than zero.')
        return value


class ProjectEditForm(forms.ModelForm):
    """Same as ProjectCreateForm but project_type is excluded (shown as read-only text in template)."""

    class Meta:
        model = Project
        fields = [
            'customer_name',
            'customer_phone',
            'customer_email',
            'site_address',
            'city',
            'state',
            'capacity_kw',
            'contract_value',
            'assigned_site_engineer',
            'survey_date',
            'target_commissioning_date',
            'zoho_crm_id',
        ]
        labels = {
            'capacity_kw': 'Capacity (kW)',
        }
        widgets = {
            'site_address':              forms.Textarea(attrs={'rows': 3}),
            'survey_date':               forms.DateInput(attrs={'type': 'date'}),
            'target_commissioning_date': forms.DateInput(attrs={'type': 'date'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['assigned_site_engineer'].queryset = (
            UserProfile.objects.filter(role='Site Engineer', is_active=True)
            .select_related('user')
        )
        self.fields['assigned_site_engineer'].label_from_instance = (
            lambda obj: f"{obj.user.get_full_name() or obj.user.username}"
        )

    def clean_customer_phone(self):
        value = self.cleaned_data['customer_phone'].strip()
        _validate_phone(value)
        return value

    def clean_capacity_kw(self):
        value = self.cleaned_data.get('capacity_kw')
        if value is not None and value <= 0:
            raise forms.ValidationError('Capacity must be greater than zero.')
        return value

    def clean_contract_value(self):
        value = self.cleaned_data.get('contract_value')
        if value is not None and value <= 0:
            raise forms.ValidationError('Contract value must be greater than zero.')
        return value


class TaskAddForm(forms.Form):

    phase         = forms.ModelChoiceField(queryset=ProjectPhase.objects.none())
    task_name     = forms.CharField(max_length=200)
    assigned_role = forms.ChoiceField(choices=Task.ROLE_CHOICES)
    due_date      = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'type': 'date'}),
    )

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        if project is not None:
            self.fields['phase'].queryset = ProjectPhase.objects.filter(project=project)
