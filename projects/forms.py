import re
from datetime import date

from django import forms
from django.contrib.auth.models import User
from django.utils import timezone
from django.utils.dateparse import parse_date
from .models import UserProfile, Project, ProjectPhase, Task, Vendor, VendorCategory, Program, BOQItemMaster
from .utils import roles_for_phase


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


class AdminUserEditForm(forms.Form):
    """Full user edit form for the Admin Panel — includes username and optional password reset."""

    first_name = forms.CharField(max_length=50)
    last_name  = forms.CharField(max_length=50)
    username   = forms.CharField(max_length=150)
    email      = forms.EmailField()
    phone_number = forms.CharField(max_length=10)
    role       = forms.ChoiceField(choices=UserProfile.ROLE_CHOICES)
    is_design_head = forms.BooleanField(
        required=False,
        label='Design Head',
        help_text='Can reassign any Design-role task, independent of role.',
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
    )
    is_design_qc = forms.BooleanField(
        required=False,
        label='Design QC',
        help_text=('Reviews RESCO designs at the FIRST gate, before the Design Head. '
                   'Independent of role. Holding both flags is allowed, but one person '
                   'can never record both verdicts on the same site.'),
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
    )
    is_qaqc = forms.BooleanField(
        required=False,
        label='QA/QC',
        help_text=('May approve or reject a submitted RESCO task on any site they can '
                   'already see, and raises a punch point when they reject one. '
                   'Independent of role, and does NOT confer the PM-only power to '
                   'waive a punch point.'),
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
    )
    new_password = forms.CharField(
        min_length=8,
        required=False,
        widget=forms.PasswordInput,
        label='New password',
        help_text='Leave blank to keep the current password.',
    )
    confirm_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput,
        label='Confirm new password',
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

    def clean_username(self):
        value = self.cleaned_data['username'].strip().lower()
        if not re.fullmatch(r'[a-z0-9._]+', value):
            raise forms.ValidationError(
                'Username may only contain lowercase letters, digits, dots, and underscores.'
            )
        qs = User.objects.filter(username=value)
        if self._instance_user:
            qs = qs.exclude(pk=self._instance_user.pk)
        if qs.exists():
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
        pw  = cleaned.get('new_password', '')
        cpw = cleaned.get('confirm_password', '')
        if pw and pw != cpw:
            self.add_error('confirm_password', 'Passwords do not match.')
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


# ---------------------------------------------------------------------------
# Typed dates
# ---------------------------------------------------------------------------
#
# A date a human types is plausible when it falls between TYPED_DATE_FLOOR and five
# years from today. Every parser the views used (date.fromisoformat, parse_date,
# strptime, forms.DateField) accepts '0026-09-17' as the year 26, and a browser date
# picker lets a two-digit year through, so production collected due dates in the
# years 20 and 26. This is the one rule for every view that stores a typed date.
#
# IT LIVES IN THE VIEWS AND FORMS, NOT IN A MODEL CONSTRAINT. A constraint would
# refuse the existing bad rows on their next save — the very save that corrects them.
# System timestamps (created_at, completed_at, approved_at ...) never come through here.
#
# THE CEILING MOVES. A date accepted today can fall outside the range later; nothing
# re-checks stored rows. `manage.py list_implausible_dates` prints the range it used.

TYPED_DATE_FLOOR = date(2020, 1, 1)


def typed_date_ceiling(today=None):
    """Five years from today (timezone.localdate()). 29 Feb maps to 28 Feb."""
    today = today or timezone.localdate()
    try:
        return today.replace(year=today.year + 5)
    except ValueError:
        return today.replace(year=today.year + 5, day=28)


def check_typed_date(value, *, today=None):
    """Validate one typed date. Returns (date, None) or (None, message).

    `value` is the raw POST string or an already-parsed date. Empty input returns
    (None, None): whether a date is required is the caller's rule, not this one's.
    Never raises — parse_date returns None for a string that does not look like a
    date but RAISES ValueError for one that does and is impossible ('2026-02-30'),
    and both must come back as a message, not a 500.
    """
    if value is None or value == '':
        return None, None
    if isinstance(value, date):
        parsed = value
    else:
        raw = str(value).strip()
        if not raw:
            return None, None
        try:
            parsed = parse_date(raw)
        except ValueError:
            parsed = None
        if parsed is None:
            return None, f'"{raw}" is not a valid date. Pick a date from the calendar.'
    ceiling = typed_date_ceiling(today)
    if parsed < TYPED_DATE_FLOOR or parsed > ceiling:
        return None, (
            f'Enter a date between {TYPED_DATE_FLOOR:%d %b %Y} and {ceiling:%d %b %Y}. '
            f'{parsed:%d %b %Y} is outside that range — check the year.'
        )
    return parsed, None


def _clean_typed_date(value):
    """clean_<field> body for a forms.DateField: DateField has already parsed it."""
    _, error = check_typed_date(value)
    if error:
        raise forms.ValidationError(error)
    return value


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
            'dc_capacity_kw',
            'ac_capacity_kw',
            'contract_value',
            'survey_date',
            'target_commissioning_date',
            'zoho_crm_id',
        ]
        labels = {
            'dc_capacity_kw': 'DC Capacity (kWp)',
            'ac_capacity_kw': 'AC Capacity (kWp)',
        }
        widgets = {
            'site_address':              forms.Textarea(attrs={'rows': 3}),
            'survey_date':               forms.DateInput(attrs={'type': 'date'}),
            'target_commissioning_date': forms.DateInput(attrs={'type': 'date'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # OPEX projects are NEVER standalone going forward — every new OPEX site is
        # created under a Program via the dedicated OPEX site path (opex_site_create).
        # Drop OPEX from this generic form's choices so it can't be selected here.
        # Enforced at the form layer, never as a DB NOT NULL on program (that would
        # break existing legacy program=null rows). See also clean_project_type.
        self.fields['project_type'].choices = [
            (value, label) for value, label in self.fields['project_type'].choices
            if value != 'OPEX'
        ]

    def clean_customer_phone(self):
        value = self.cleaned_data['customer_phone'].strip()
        _validate_phone(value)
        return value

    def clean_survey_date(self):
        return _clean_typed_date(self.cleaned_data.get('survey_date'))

    def clean_target_commissioning_date(self):
        return _clean_typed_date(self.cleaned_data.get('target_commissioning_date'))

    def clean_project_type(self):
        # Defense-in-depth: reject a hand-crafted POST that smuggles OPEX past the
        # trimmed <select> above.
        value = self.cleaned_data.get('project_type')
        if value == 'OPEX':
            raise forms.ValidationError(
                'RESCO projects must be created under a Program, not from this form.'
            )
        return value

    def clean_dc_capacity_kw(self):
        value = self.cleaned_data.get('dc_capacity_kw')
        if value is not None and value <= 0:
            raise forms.ValidationError('DC capacity must be greater than zero.')
        return value

    def clean_ac_capacity_kw(self):
        value = self.cleaned_data.get('ac_capacity_kw')
        if value is not None and value <= 0:
            raise forms.ValidationError('AC capacity must be greater than zero.')
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
            'dc_capacity_kw',
            'ac_capacity_kw',
            'contract_value',
            'survey_date',
            'target_commissioning_date',
            'zoho_crm_id',
        ]
        labels = {
            'dc_capacity_kw': 'DC Capacity (kWp)',
            'ac_capacity_kw': 'AC Capacity (kWp)',
        }
        widgets = {
            'site_address':              forms.Textarea(attrs={'rows': 3}),
            'survey_date':               forms.DateInput(attrs={'type': 'date'}),
            'target_commissioning_date': forms.DateInput(attrs={'type': 'date'}),
        }

    def clean_customer_phone(self):
        value = self.cleaned_data['customer_phone'].strip()
        _validate_phone(value)
        return value

    def clean_survey_date(self):
        return _clean_typed_date(self.cleaned_data.get('survey_date'))

    def clean_target_commissioning_date(self):
        return _clean_typed_date(self.cleaned_data.get('target_commissioning_date'))

    def clean_dc_capacity_kw(self):
        value = self.cleaned_data.get('dc_capacity_kw')
        if value is not None and value <= 0:
            raise forms.ValidationError('DC capacity must be greater than zero.')
        return value

    def clean_ac_capacity_kw(self):
        value = self.cleaned_data.get('ac_capacity_kw')
        if value is not None and value <= 0:
            raise forms.ValidationError('AC capacity must be greater than zero.')
        return value

    def clean_contract_value(self):
        value = self.cleaned_data.get('contract_value')
        if value is not None and value <= 0:
            raise forms.ValidationError('Contract value must be greater than zero.')
        return value


class PostActivationFieldEditForm(forms.ModelForm):
    """Narrow edit form for the site fields a PM/Coordinator may change AFTER a
    project leaves Draft (see views.project_field_edit). Deliberately exposes ONLY
    the capacity/identity/coordinate fields plus target commissioning date and an
    optional free-text reason — never the customer/scope fields on ProjectEditForm.

    CONTRACT VALUE WAS REMOVED FROM THIS FORM — commercial, out of phase 1. The
    COLUMN IS UNTOUCHED and still carries every rupee it did: Project.contract_value
    stays on the model, keeps feeding the Finance and CEO dashboard totals and the
    payment-milestone amounts, and is still editable while a project is Draft via
    ProjectEditForm. It is only no longer editable AFTER activation, and no longer
    shown on the overview header.

    That removal also retired `clean_contract_value`, which refused a change once
    payment-milestone amounts existed (set_milestone_amounts validates
    M1+M2+M3 == contract_value and nothing reconciles them here). The invariant it
    protected is now protected more simply — this form does not offer the field, so
    there is no post-activation change to guard against. IF contract_value EVER
    COMES BACK TO THIS FORM, THAT CHECK MUST COME BACK WITH IT.

    Positive-value validation mirrors ProjectEditForm.
    """

    reason = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'rows': 2, 'class': 'form-control form-control-sm',
                                     'placeholder': 'Optional — why is this changing?'}),
    )

    class Meta:
        model = Project
        # Order matches the modal's layout and the upload template's column order.
        fields = [
            'dc_capacity_kw', 'ac_capacity_kw', 'ivrs_no', 'site_name',
            'latitude', 'longitude', 'target_commissioning_date',
        ]
        labels = {
            'dc_capacity_kw': 'DC Capacity (kWp)',
            'ac_capacity_kw': 'AC Capacity (kWp)',
            'ivrs_no':        'IVRS No.',
            'site_name':      'Site Name',
        }
        widgets = {
            'dc_capacity_kw':            forms.NumberInput(attrs={'class': 'form-control form-control-sm', 'step': '0.01'}),
            'ac_capacity_kw':            forms.NumberInput(attrs={'class': 'form-control form-control-sm', 'step': '0.01'}),
            'ivrs_no':                   forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'site_name':                 forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            # step=any so the browser never rounds a six-decimal coordinate on submit.
            'latitude':                  forms.NumberInput(attrs={'class': 'form-control form-control-sm', 'step': 'any'}),
            'longitude':                 forms.NumberInput(attrs={'class': 'form-control form-control-sm', 'step': 'any'}),
            'target_commissioning_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control form-control-sm'}),
        }

    def clean_dc_capacity_kw(self):
        value = self.cleaned_data.get('dc_capacity_kw')
        if value is not None and value <= 0:
            raise forms.ValidationError('DC capacity must be greater than zero.')
        return value

    def clean_ac_capacity_kw(self):
        value = self.cleaned_data.get('ac_capacity_kw')
        if value is not None and value <= 0:
            raise forms.ValidationError('AC capacity must be greater than zero.')
        return value

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The bounds move with the calendar, so they are set per instance, not in Meta.
        self.fields['target_commissioning_date'].widget.attrs.update(
            min=TYPED_DATE_FLOOR.isoformat(), max=typed_date_ceiling().isoformat())

    def clean_target_commissioning_date(self):
        return _clean_typed_date(self.cleaned_data.get('target_commissioning_date'))

    def clean_contract_value(self):
        value = self.cleaned_data.get('contract_value')
        if value is not None and value <= 0:
            raise forms.ValidationError('Contract value must be greater than zero.')
        # During clean_<field>, self.instance still holds the pre-save DB value
        # (construct_instance runs later in _post_clean), so this compares new vs old.
        if value != self.instance.contract_value and \
                self.instance.milestones.filter(amount__isnull=False).exists():
            raise forms.ValidationError(
                'Contract value cannot be changed after payment milestone amounts are set. '
                'Reconcile the M1/M2/M3 milestone amounts first.'
            )
        return value


class TaskAddForm(forms.Form):
    """Add one task to a live project, ASSIGNED, in a role its phase actually uses.

    WHY THE FIELDS VALIDATE AGAINST THE FULL VOCABULARY AND clean() NARROWS. The role
    and assignee a person may pick depend on the phase, and the phase is only known
    once the form is bound. So the fields accept any known role and any active
    profile, `role_options` / `assignee_options` carry the narrowed lists the template
    renders, and clean() is the ONE place the phase-role and role-assignee rules are
    enforced. The narrowed selects are convenience; both add-task templates are
    `novalidate` and a hand-made POST skips them entirely.

    THE ASSIGNEE RULE IS task_assign's RULE: an active profile whose role matches the
    task's role. A form that could create a task that endpoint would refuse to assign
    has created a task nobody can move.
    """

    phase         = forms.ModelChoiceField(queryset=ProjectPhase.objects.none())
    task_name     = forms.CharField(max_length=200)
    assigned_role = forms.ChoiceField(choices=Task.ROLE_CHOICES)
    assigned_to   = forms.ModelChoiceField(
        queryset=UserProfile.objects.filter(is_active=True),
        label='Assign To',
    )
    due_date      = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'type': 'date'}),
    )

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        if project is not None:
            self.fields['phase'].queryset = ProjectPhase.objects.filter(project=project)

        # Narrowed lists for the template, from the raw (possibly unvalidated) input.
        phase = self._selected_phase()
        self.phase_roles = roles_for_phase(phase) if phase is not None else []
        labels = dict(Task.ROLE_CHOICES)
        self.role_options = [(role, labels[role]) for role in self.phase_roles]

        role = self._raw('assigned_role')
        if len(self.phase_roles) == 1:
            # Exactly one role: preselected. More than one: the template leads with a
            # blank option — never a silent first choice.
            self.initial['assigned_role'] = self.phase_roles[0]
            if not role:
                role = self.phase_roles[0]
        self.selected_role = role if role in self.phase_roles else ''
        self.assignee_options = (
            UserProfile.objects.filter(role=_profile_role_for(self.selected_role), is_active=True)
            .select_related('user')
            if self.selected_role else UserProfile.objects.none()
        )

    def _raw(self, name):
        source = self.data if self.is_bound else self.initial
        value = source.get(name, '')
        return str(value.pk if hasattr(value, 'pk') else value or '').strip()

    def _selected_phase(self):
        pk = self._raw('phase')
        if not pk.isdigit():
            return None
        return self.fields['phase'].queryset.filter(pk=pk).first()

    def clean_due_date(self):
        return _clean_typed_date(self.cleaned_data.get('due_date'))

    def clean(self):
        cleaned = super().clean()
        phase    = cleaned.get('phase')
        role     = cleaned.get('assigned_role')
        assignee = cleaned.get('assigned_to')

        if phase is not None and role:
            allowed = roles_for_phase(phase)
            if role not in allowed:
                labels = dict(Task.ROLE_CHOICES)
                self.add_error('assigned_role', (
                    f"'{labels.get(role, role)}' is not a role used in "
                    f"{phase.phase_name}. Choose one of: "
                    f"{', '.join(labels[r] for r in allowed)}."
                ))
                return cleaned

        if role and assignee is not None and assignee.role != _profile_role_for(role):
            self.add_error('assigned_to', (
                f"{assignee.user.get_full_name() or assignee.user.username} is "
                f"{assignee.role}, not {dict(Task.ROLE_CHOICES).get(role, role)}. "
                f"Choose someone in the task's role."
            ))
        return cleaned


def _profile_role_for(task_role):
    """Task.assigned_role -> UserProfile.role, through views' one mapping.

    Imported at call time: views imports this module, so a module-level import would
    be a cycle. The mapping is not restated here — two copies drift.
    """
    from .views import _TASK_TO_PROFILE_ROLE
    return _TASK_TO_PROFILE_ROLE.get(task_role, task_role)


# ---------------------------------------------------------------------------
# Vendor forms
# ---------------------------------------------------------------------------

_GST_RE = re.compile(
    r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$'
)


class VendorForm(forms.ModelForm):

    class Meta:
        model  = Vendor
        fields = [
            'name', 'contact_person', 'phone', 'email', 'address',
            'gst_number', 'msme_status', 'msme_number', 'categories',
        ]
        widgets = {
            'address':    forms.Textarea(attrs={'rows': 3, 'class': 'form-control'}),
            'categories': forms.CheckboxSelectMultiple(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name == 'categories':
                continue
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault('class', 'form-check-input')
            else:
                field.widget.attrs.setdefault('class', 'form-control')
        self.fields['categories'].queryset = VendorCategory.objects.all()
        self.fields['categories'].error_messages['required'] = 'Select at least one category.'

    def clean_gst_number(self):
        value = (self.cleaned_data.get('gst_number') or '').strip().upper()
        if value and not _GST_RE.fullmatch(value):
            raise forms.ValidationError(
                'Enter a valid 15-character GST number (e.g. 09ABCDE1234F1Z5).'
            )
        return value or None

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('msme_status') and not (cleaned.get('msme_number') or '').strip():
            self.add_error('msme_number', 'MSME number is required when MSME status is checked.')
        return cleaned


# ---------------------------------------------------------------------------
# BOQ Item Master (catalogue)
# ---------------------------------------------------------------------------

_ITEM_CODE_RE = re.compile(r'^[A-Z0-9][A-Z0-9\-_]*$')


class BOQItemMasterForm(forms.ModelForm):
    """Create / edit one catalogue entry. `code` is create-only — it is the stable
    identifier BOQ rows and grouped procurement refer to, so the edit view drops the
    field rather than letting it be reassigned (see admin_boq_item_edit)."""

    class Meta:
        model  = BOQItemMaster
        fields = ['code', 'project_type', 'description', 'unit', 'category',
                  'is_active', 'is_mandatory', 'sort_order']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault('class', 'form-check-input')
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault('class', 'form-select')
            else:
                field.widget.attrs.setdefault('class', 'form-control')
        # PART 11: without this field on the form, every item an admin created would land
        # on the Residential template by default, whatever they meant.
        self.fields['project_type'].help_text = (
            'Which template this item belongs to. Residential items are pre-populated onto '
            'every new Residential BOQ; RESCO items are offered in the RESCO BOQ picker.'
        )
        self.fields['category'].help_text = (
            'Residential: display grouping only. RESCO: groups the picker and the saved '
            'sheet, so it should match an existing RESCO category exactly.'
        )
        self.fields['unit'].help_text     = 'e.g. Nos, Mtr, Kg, Set, Lot. Required — quantities cannot be summed across sites without it.'
        self.fields['sort_order'].help_text = 'Position in the standard BOQ template; also becomes the line item serial number.'
        self.fields['is_mandatory'].help_text = (
            'RESCO only. A mandatory item is added to every new RESCO BOQ and cannot be '
            'removed by the designer. Not used for Residential — every active Residential '
            'item is already pre-populated onto every new Residential BOQ.'
        )

    def clean(self):
        """Refuse `is_mandatory` on anything but an OPEX row.

        WHY A CROSS-FIELD RULE AND NOT A MODEL CONSTRAINT: the meaning is about how the two
        BOQ paths differ, not about a value being invalid in the database. A Residential row
        carrying this flag is not corrupt, it is merely a claim nothing will ever act on —
        and a CHECK would have needed a data migration to prove itself against 244 live
        rows for no behavioural gain.

        `project_type` IS NOT ALWAYS ON THIS FORM, and this rule must not assume it is. The
        Design Head's catalogue screen is OPEX-only: both its views delete the field and set
        'OPEX' on the instance AFTER validation, so at this point neither cleaned_data nor
        self.instance holds a usable value there — self.instance carries the model default
        'Residential' on the create path, which would refuse exactly the item the flag
        exists for.

        SO WHEN THE FIELD IS ABSENT, THE RULE STANDS DOWN. The only view that removes
        `project_type` is the one that guarantees OPEX, which makes the check redundant
        rather than skipped. Anything that removes the field WITHOUT forcing the value would
        slip past this — that pairing is the contract, and it is why the deletion and the
        assignment sit two lines apart in design_views rather than in different functions.

        Deliberately does NOT touch `code`: the Admin edit view and both of the Head's
        delete that field, and a rule reading it would break on all three.
        """
        cleaned = super().clean()
        if not cleaned.get('is_mandatory'):
            return cleaned
        # ACTIVE-NESS IS TESTED FIRST, AND ABOVE THE `project_type` EARLY RETURN BELOW.
        # The Design Head's catalogue screens delete `project_type` from the form but keep
        # `is_active`; placing this after that return would leave the combination reachable
        # on exactly the screen most likely to produce it. Guarded on field presence for
        # the same reason the rule below is — a form that does not offer the field cannot
        # be judged on it.
        if 'is_active' in self.fields and not cleaned.get('is_active'):
            raise forms.ValidationError({
                'is_mandatory': 'A mandatory item cannot be inactive. The picker offers '
                                'active items only, so the flag could never be satisfied — '
                                'clear one or the other.',
            })
        if 'project_type' not in self.fields:
            return cleaned
        if cleaned.get('project_type') != 'OPEX':
            raise forms.ValidationError({
                'is_mandatory': 'Only RESCO items can be marked mandatory. Every active '
                                'Residential item is already added to every new '
                                'Residential BOQ, so the flag would do nothing there.',
            })
        return cleaned

    def clean_code(self):
        value = (self.cleaned_data.get('code') or '').strip().upper()
        if not _ITEM_CODE_RE.fullmatch(value):
            raise forms.ValidationError(
                'Use uppercase letters, digits, hyphen or underscore only (e.g. ITM-038).'
            )
        return value

    def clean_description(self):
        return (self.cleaned_data.get('description') or '').strip()

    def clean_unit(self):
        value = (self.cleaned_data.get('unit') or '').strip()
        if not value:
            raise forms.ValidationError('Unit is required.')
        return value


# ---------------------------------------------------------------------------
# Programs (OPEX tender / multi-site CAPEX contract parent)
# ---------------------------------------------------------------------------

_bs = {'class': 'form-control'}          # module-level so the nested Meta.widgets can see it
_CODE_STRIP_RE = re.compile(r'[^A-Z0-9]')
# Reserved: a short_tender_code of 'HRP' would blur OPEX tender IDs into the legacy
# HRP-prefixed format. Rejected case-insensitively (spec: reserved-code guard).
_RESERVED_TENDER_CODES = {'HRP'}

# Tender codes accept a hyphen; site codes do not. See normalize_tender_code below for
# why these are two rules and not one.
_TENDER_CODE_RE = re.compile(r'\A[A-Z0-9-]+\Z')


def normalize_program_code(value):
    """Uppercase + strip to [A-Z0-9] (drops spaces/hyphens/punctuation). THE SITE-CODE
    RULE — `OpexSiteForm.clean_site_code` and the bulk-upload duplicate detector.

    THIS MUST STAY STRICTER THAN THE TENDER-CODE RULE, and the reason is not cosmetic.
    A site_code IS the project_id verbatim (OpexSiteForm.clean: `project_id = code`),
    and project_id is what `utils.generate_project_id` slices to recover the running
    serial out of the auto-generated `HRP-{PREFIX}-{YEAR}-{NNN}` namespace:

        suffix = existing_id[len(id_prefix):]

    Stripping the hyphen is what makes it STRUCTURALLY IMPOSSIBLE for a hand-entered
    or bulk-uploaded site code to land inside that namespace and skew the next serial.
    The reserved-code guard on 'HRP' is the second line of defence; this is the first.
    Do not relax it to match the tender-code rule "for consistency" — they are
    deliberately different, and only one of them ends up in a URL and a serial parser.
    """
    return _CODE_STRIP_RE.sub('', (value or '').upper())


def normalize_tender_code(value):
    """Uppercase + trim, then REJECT anything outside [A-Z0-9-]. THE TENDER-CODE RULE —
    `ProgramForm.clean_short_tender_code` only. Returns '' for an empty input; the
    OPEX-required check lives in ProgramForm.clean().

    TWO DIFFERENCES FROM normalize_program_code, both intended:

    1. THE HYPHEN IS KEPT. `short_tender_code` no longer composes a project_id — that
       stopped when the `{short_tender_code}-{site_code}` scheme was dropped and
       site_code became the whole ID. Every remaining reader is a display badge, a
       uniqueness filter, an admin search, or the bulk-template filename, so a hyphen
       reaches nothing that splits, slices or routes on it. `HRP-2026` is now a legal
       tender code; `HRP 2026` and `HRP/2026` are not, because a space or a slash in
       anything code-shaped is how a URL segment gets broken later.

    2. IT REJECTS RATHER THAN STRIPS. The old shared normalizer silently swallowed
       every illegal character — 'HRP 2026' saved quietly as 'HRP2026' and the user was
       never told their code had changed. Bad input now says so.

    Lowercase is still accepted and uppercased here, not rejected: case is a typing
    convenience, not a mistake worth an error message.
    """
    code = (value or '').strip().upper()
    if not code:
        return ''
    if not _TENDER_CODE_RE.match(code):
        raise forms.ValidationError(
            'Tender code may contain only letters, digits and hyphens — '
            'no spaces, slashes or other punctuation.'
        )
    return code


class ProgramForm(forms.ModelForm):
    """Create / edit a Program (OPEX tender or multi-site CAPEX contract).

    OPEX-only rules (spec §2 / §4): short_tender_code is required, normalized,
    reserved-code-guarded, and globally unique across ALL Programs INCLUDING
    soft-deleted ones. tender_reference_number, when supplied, is likewise unique
    soft-delete-aware. All uniqueness checks are explicit .filter().exists() queries
    returning a clear message — never a raw DB error.

    THAT UNIQUENESS RULE USED TO BE JUSTIFIED as "short_tender_code is a building block
    of a globally-unique site project_id, so a soft-deleted collision would still
    produce colliding IDs". THAT IS NO LONGER TRUE and the sentence has been removed:
    the `{short_tender_code}-{site_code}` composition was dropped and `site_code` is now
    the whole project_id (see OpexSiteForm.clean). The tender code composes nothing.

    THE RULE ITSELF STAYS, on its own merits — the code identifies a tender across every
    screen that shows one, and two live tenders sharing a code is a reporting problem
    whether or not it is an ID problem. Keeping the check while correcting its stated
    reason is deliberate: the next person to read this should not "simplify" it away on
    the strength of a rationale that no longer holds.

    See normalize_tender_code above for why this field accepts a hyphen and site_code
    does not.
    """

    class Meta:
        model = Program
        # PHASE 1 — SEVEN FIELDS. Eleven others were REMOVED FROM THIS LIST, not from
        # the model and not from the database:
        #
        #     expected_completion_date, tender_reference_number, bid_value, award_date,
        #     ppa_reference, ppa_signed_date, ppa_per_unit_rate,
        #     ppa_escalation_percentage, ppa_escalation_frequency,
        #     financing_partner_name, financing_assistance_type
        #
        # Every one is still a column on Program, is empty on every row today, and can
        # be brought back by adding its name here and its markup to program_form.html.
        # See EXECUTION_MODULE_DEFERRED.md for why hidden rather than dropped.
        #
        # REMOVING THEM FROM Meta.fields IS THE WHOLE POINT — hiding them in the
        # template alone would leave them ON the form, and a ModelForm field absent
        # from the POST body cleans to None/'' and construct_instance writes THAT back.
        # Template-only hiding would silently wipe these columns on every save.
        fields = [
            'program_type', 'name', 'client_name', 'status',
            'short_tender_code', 'total_capacity', 'planned_site_count',
        ]
        widgets = {
            'program_type':              forms.Select(attrs={'class': 'form-select'}),
            'status':                    forms.Select(attrs={'class': 'form-select'}),
            'name':                      forms.TextInput(attrs=_bs),
            'client_name':               forms.TextInput(attrs=_bs),
            'short_tender_code':         forms.TextInput(attrs={**_bs, 'placeholder': 'e.g. IPGCL26 or HRP-2026'}),
            'total_capacity':            forms.NumberInput(attrs={**_bs, 'step': '0.01'}),
            'planned_site_count':        forms.NumberInput(attrs={**_bs, 'min': '0'}),
        }
        labels = {
            'total_capacity':      'Total planned capacity (MW)',
            'short_tender_code':   'Short tender code (RESCO)',
            'planned_site_count':  'Planned site count',
        }

    def clean_short_tender_code(self):
        # normalize_TENDER_code, not normalize_program_code — the tender-code rule keeps
        # hyphens and rejects everything else, where the site-code rule strips. See both
        # docstrings; the difference is load-bearing.
        return normalize_tender_code(self.cleaned_data.get('short_tender_code'))

    def clean(self):
        cleaned = super().clean()
        program_type = cleaned.get('program_type')
        code = cleaned.get('short_tender_code', '')

        if program_type == 'OPEX':
            # If clean_short_tender_code already rejected the FORMAT, the key is missing
            # from cleaned_data and `code` is ''. Saying "required" on top of "may contain
            # only letters, digits and hyphens" reads as two separate faults when the user
            # made one — they DID enter a code, it just had a space in it. Report the
            # specific error alone.
            if 'short_tender_code' in self.errors:
                pass
            elif not code:
                self.add_error('short_tender_code',
                               'Short tender code is required for a RESCO tender.')
            else:
                if code in _RESERVED_TENDER_CODES:
                    self.add_error('short_tender_code',
                                   "'HRP' is reserved and cannot be used as a tender code.")
                else:
                    # Soft-delete-aware global uniqueness — query the UNFILTERED manager
                    # (no is_deleted filter) so a soft-deleted Program's code can't be
                    # reused into a colliding site project_id.
                    dupes = Program.objects.filter(short_tender_code=code)
                    if self.instance.pk:
                        dupes = dupes.exclude(pk=self.instance.pk)
                    if dupes.exists():
                        self.add_error('short_tender_code',
                                       f"Tender code '{code}' is already used by another Program.")

            # tender_reference_number uniqueness (when provided), soft-delete-aware.
            #
            # DORMANT IN PHASE 1 AND DELIBERATELY LEFT STANDING. The field is no longer
            # on Meta.fields, so cleaned.get() returns None, `ref` is '' and the whole
            # block short-circuits — it costs one dict lookup per submit. It stays
            # because it is the ONLY uniqueness rule for that column, and the day the
            # field returns to the form it must return already guarded, not silently
            # unguarded. Do not "clean it up" without putting it back with the field.
            ref = (cleaned.get('tender_reference_number') or '').strip()
            if ref:
                ref_dupes = Program.objects.filter(tender_reference_number=ref)
                if self.instance.pk:
                    ref_dupes = ref_dupes.exclude(pk=self.instance.pk)
                if ref_dupes.exists():
                    self.add_error('tender_reference_number',
                                   f"Tender reference '{ref}' is already used by another Program.")

        elif program_type == 'CAPEX':
            # CAPEX never carries a short_tender_code — clear any stray value so it
            # never participates in the OPEX uniqueness space.
            cleaned['short_tender_code'] = ''

        return cleaned


class OpexSiteForm(forms.ModelForm):
    """Create ONE OPEX site under a specific OPEX Program — the Program-scoped
    counterpart to ProjectCreateForm.

    Unlike the generic create flow (which lets Project.save() generate the ID), this
    form captures site_code and uses it DIRECTLY as the site's globally-unique
    project_id — no tender-code prefix is prepended. Teams control site-code naming
    entirely (e.g. 'MB0003', 'RJJP001') and the code entered IS the stored ID.
    The view sets project_id explicitly before save() so generate_project_id() is
    bypassed entirely (spec: OPEX must not go through the suffix-parser).
    Every check is pre-save with a clear message — a duplicate site_code or an
    over-length ID never surfaces as a raw IntegrityError:
      • site_code is normalized (upper + [A-Z0-9]).
      • site_code is unique WITHIN the Program, checked soft-delete-aware (unfiltered
        manager) so a soft-deleted site can't have its code reused into a colliding ID.
      • the site_code must fit project_id's 30-char column.
      • the site_code is re-checked for global uniqueness (defensive backstop).
    On success, `self.composed_project_id` holds the value for the view to persist.
    """

    class Meta:
        model = Project
        # customer_name is intentionally NOT on this form — it is auto-set to
        # program.client_name in the view (frozen at creation, like project_id).
        # survey_date / target_commissioning_date are dropped for OPEX sites (they stay
        # null and every reader guards for that). customer_contact_person is reused as the
        # Site In-Charge Name; customer_phone / customer_email are reinterpreted as the
        # Site In-Charge phone/email (see the dual-meaning notes on the model fields).
        # The five phase-1 site fields are here as well as on the post-activation modal
        # ON PURPOSE. The bulk upload can set all five, and a form that cannot enter or
        # correct what the upload can set is the asymmetry that becomes a support
        # question. contract_value is NOT here and never was — Residential-only.
        fields = [
            'site_code', 'site_name', 'customer_contact_person', 'customer_phone',
            'customer_email', 'site_address', 'city', 'state',
            'dc_capacity_kw', 'ac_capacity_kw', 'ivrs_no', 'latitude', 'longitude',
        ]
        labels = {
            'dc_capacity_kw': 'DC Capacity (kWp)',
            'ac_capacity_kw': 'AC Capacity (kWp)',
            'ivrs_no': 'IVRS No.',
            'site_name': 'Site Name',
            'site_code': 'Site Code',
            'customer_contact_person': 'Site In-Charge Name',
            'customer_phone': 'Site In-Charge Phone',
            'customer_email': 'Site In-Charge Email',
        }
        widgets = {
            'site_code':                 forms.TextInput(attrs={**_bs, 'placeholder': 'e.g. S045'}),
            'site_name':                 forms.TextInput(attrs=_bs),
            'customer_contact_person':   forms.TextInput(attrs=_bs),
            'customer_phone':            forms.TextInput(attrs={**_bs, 'maxlength': '10'}),
            'customer_email':            forms.EmailInput(attrs=_bs),
            'site_address':              forms.Textarea(attrs={**_bs, 'rows': 3}),
            'city':                      forms.TextInput(attrs=_bs),
            'state':                     forms.TextInput(attrs=_bs),
            'dc_capacity_kw':            forms.NumberInput(attrs={**_bs, 'step': '0.01'}),
            'ac_capacity_kw':            forms.NumberInput(attrs={**_bs, 'step': '0.01'}),
            'ivrs_no':                   forms.TextInput(attrs=_bs),
            # step=any so the browser never rounds a six-decimal coordinate on submit.
            'latitude':                  forms.NumberInput(attrs={**_bs, 'step': 'any'}),
            'longitude':                 forms.NumberInput(attrs={**_bs, 'step': 'any'}),
        }

    def __init__(self, *args, program=None, **kwargs):
        # `program` is the parent OPEX Program; required to validate site_code
        # uniqueness within the tender and to compose the project_id.
        self.program = program
        self.composed_project_id = None
        super().__init__(*args, **kwargs)
        # Site In-Charge Name and Phone are optional for OPEX sites — live tenders
        # (e.g. government OPEX contracts) often don't have in-charge contacts at
        # the time of bulk upload.  The model keeps both blank=True so this is safe.
        self.fields['customer_contact_person'].required = False
        self.fields['customer_phone'].required = False

    def clean_customer_phone(self):
        value = (self.cleaned_data.get('customer_phone') or '').strip()
        if not value:
            # Phone is optional for OPEX sites; skip format validation when blank.
            return value
        _validate_phone(value)
        return value

    def clean_dc_capacity_kw(self):
        value = self.cleaned_data.get('dc_capacity_kw')
        if value is not None and value <= 0:
            raise forms.ValidationError('DC capacity must be greater than zero.')
        return value

    def clean_ac_capacity_kw(self):
        value = self.cleaned_data.get('ac_capacity_kw')
        if value is not None and value <= 0:
            raise forms.ValidationError('AC capacity must be greater than zero.')
        return value

    def clean_site_code(self):
        code = normalize_program_code(self.cleaned_data.get('site_code'))
        if not code:
            raise forms.ValidationError('Site code is required.')
        return code

    def clean(self):
        cleaned = super().clean()
        code = cleaned.get('site_code')
        if not code or self.program is None:
            return cleaned

        # Uniqueness WITHIN this tender — unfiltered manager includes soft-deleted sites
        # so a deleted site's code (which still reserves its global project_id) can't be reused.
        if self.program.sites.filter(site_code=code).exists():
            self.add_error('site_code', f"Site code {code} is already used in this tender.")
            return cleaned

        # The site_code IS the project_id — no tender-code prefix.
        # Teams define site codes (e.g. 'MB0003', 'RJJP001') and they are stored as-is.
        project_id = code
        if len(project_id) > 30:
            self.add_error(
                'site_code',
                f"Site code '{project_id}' is {len(project_id)} characters — "
                f"the maximum is 30."
            )
            return cleaned

        # Defensive global-uniqueness backstop (should already be guaranteed by the two
        # uniqueness rules above); never let it reach the DB as an IntegrityError.
        if Project.objects.filter(project_id=project_id).exists():
            self.add_error('site_code', f"A project with ID '{project_id}' already exists.")
            return cleaned

        self.composed_project_id = project_id
        return cleaned
