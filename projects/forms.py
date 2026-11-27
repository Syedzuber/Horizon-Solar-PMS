import re
from datetime import date

from django import forms
from django.db.models import Q
from django.contrib.auth.models import User
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.functional import cached_property
from .models import UserProfile, Project, ProjectPhase, Task, Vendor, VendorCategory, Program, BOQItemMaster, StockLocation, VENDOR_KIND_SUPPLIER
from .models import ActivityLog, BOQFormulaRule, BOQItem, DesignSettings, log_activity
# B-3a: the configured-ACCB catalogue code is locked on every catalogue form.
from .accb import ACCB_CONFIGURED_CODE, ACCB_CATALOGUE_LOCKED_MESSAGE
from .units import UNIT_CHOICES, canonical_unit
from .utils import roles_for_phase
from .permissions import (
    FINANCE_HEAD_HOLDER_ROLES,
    PAYMENT_APPROVER_ROLES, active_bd_profiles, profile_is_active_bd,
    qaqc_engineer_assignment_refusal, reserved_task_names,
)


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
        # RESCO AND CAPEX: user_can_approve_task's is_qaqc arm has no type test, so the
        # flag reaches every two-step type (CAPEX-2), not RESCO alone.
        help_text=('May approve or reject submitted tasks on RESCO and CAPEX sites they '
                   'can view. A rejection raises a punch point. Independent of role, and '
                   'does NOT confer the PM-only power to waive a punch point.'),
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
    )
    is_warehouse_keeper = forms.BooleanField(
        required=False,
        label='Warehouse keeper',
        help_text=('May be named keeper of a warehouse. Holding the flag grants '
                   'nothing on its own; being named keeper does not set it.'),
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
    )
    is_payment_approver = forms.BooleanField(
        required=False,
        label='Payment approver',
        help_text=('May approve, hold or reject payment requests they did not raise. '
                   'Finance and CEO users only.'),
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
    )
    # FIN-H-B1 (H10). Separate from is_payment_approver above: ticking one never ticks,
    # clears or requires the other.
    is_finance_head = forms.BooleanField(
        required=False,
        label='Finance Head',
        help_text=('Finance Head: sees the Finance Head dashboard. Finance role only. '
                   'Does not grant payment approval.'),
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
        # O6: the approver flag belongs to Finance and CEO only. Checked against the role
        # being SAVED, so changing a flag holder's role to anything else is refused until
        # the box is cleared — the flag cannot outlive the role that justified it.
        if cleaned.get('is_payment_approver') and role and role not in PAYMENT_APPROVER_ROLES:
            self.add_error('is_payment_approver',
                           f'Only a Finance or CEO user may be a payment approver. '
                           f'Untick "Payment approver" to save this user as {role}.')
        # FIN-H-B1 (H10): only a Finance user may hold Finance Head. Checked against the
        # role being SAVED in this same submission, as O6 does above, so moving a Finance
        # Head to another role is refused until the box is cleared. Other roles are
        # refused because the flag means "Head of the Finance team" and nothing else.
        if cleaned.get('is_finance_head') and role and role not in FINANCE_HEAD_HOLDER_ROLES:
            self.add_error('is_finance_head',
                           'Finance Head can only be set on a Finance user.')
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


class SiteBdForm(forms.Form):
    """The overview's "Set BD" modal (FIN-M-B5-1). Not a ModelForm: the write goes
    through site_bd.set_site_bd(), which locks the row, refuses a stale form and logs
    old -> new — a ModelForm.save() would do none of that.

    CHOICES: every active BD (permissions.active_bd_profiles()), plus the site's current
    BD even if they are now inactive or no longer BD, so opening and saving the form
    never silently blanks the seat. Never a PM or any other role. Blank = "Not set" and
    is allowed (clears the seat).

    `expected_bd` carries the BD the form showed; the writer refuses if the seat changed
    since (two administrators editing at once).
    """

    bd = forms.ModelChoiceField(
        queryset=UserProfile.objects.none(),
        required=False,
        empty_label='Not set',
        label='BD',
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    expected_bd = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, project, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project
        current_id = project.bd_id
        # Active BDs, or the current holder by pk — one query when the select renders or
        # validates. The pk__in subquery keeps it a single SQL statement.
        allowed = Q(pk__in=active_bd_profiles().values('pk'))
        if current_id is not None:
            allowed |= Q(pk=current_id)
        field = self.fields['bd']
        field.queryset = (UserProfile.objects.filter(allowed).select_related('user')
                          .order_by('user__first_name', 'user__last_name', 'user__username'))
        field.label_from_instance = _bd_choice_label
        if not self.is_bound:
            self.initial.setdefault('bd', current_id)
            self.initial.setdefault('expected_bd', '' if current_id is None else str(current_id))

    def clean_expected_bd(self):
        """The BD pk the form was opened with, or None for an empty seat. A tampered
        value is refused rather than guessed at."""
        value = (self.cleaned_data.get('expected_bd') or '').strip()
        if not value:
            return None
        if not value.isdigit():
            raise forms.ValidationError('Reload the page and try again.')
        return int(value)


def _bd_choice_label(profile):
    """A BD option: the person's name, marked when they are the current holder but no
    longer an active BD (the only way such a person is in the list)."""
    name = profile.user.get_full_name() or profile.user.username
    return name if profile_is_active_bd(profile) else f'{name} (inactive)'


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

    THE ASSIGNEE RULE IS task_assign's RULE, from the same helper
    (views.task_assignee_candidates): an active profile whose role matches the task's
    role, plus this project's coordinators on a PM task. A form that could create a
    task that endpoint would refuse to assign has created a task nobody can move.
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
        self.project = project
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

    @cached_property
    def assignee_options(self):
        """The dropdown's people, each carrying `option_label` ("(Coordinator)" as in the
        Assign modal). Read only when the template draws it, so a POST that validates
        and redirects pays no query for it.

        The site's QA/QC engineer is not offered (CL-A); clean() refuses them as well.
        `project` is always set by the add-task views; without it there is no
        coordinator list to read, so nobody is offered."""
        if not self.selected_role or self.project is None:
            return []
        return _labelled_assignee_candidates(self.selected_role, self.project)

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

    def clean_task_name(self):
        """Refuse a name other code finds tasks by (FIN-M-RN2).

        The invoice-due alert, the invoice step and the payment-confirmation sync look
        their task up by name, so a second "Customer Handover" on a site would be matched
        alongside the first. Same set and wording as the rename refusal (FIN-M-RN), and
        the same case-insensitive comparison. Every project type, by ruling: the
        task -> milestone receipt sync is not limited to Residential, so a stray
        "Advance Payment Confirmation" could mark an M1 Received anywhere. The field has
        already stripped surrounding spaces."""
        name = self.cleaned_data.get('task_name', '')
        if name.casefold() in reserved_task_names():
            raise forms.ValidationError(
                f'"{name}" is a name the finance workflow looks tasks up by. '
                f'Choose a different name.')
        return name

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

        # CL-A, the other end: the site's QA/QC engineer may not hold a Site Engineer
        # task on it; any other role they may (v2.5). Refused, not warned — the PM ends
        # or replaces that assignment first. Asked BEFORE the candidate check below,
        # which leaves the engineer out of Site Engineer tasks and would otherwise refuse
        # them with the generic message instead of naming the assignment to end (the
        # order task_assign and duplicate-for-locations use).
        if self.project is not None:
            refusal = qaqc_engineer_assignment_refusal(assignee, self.project, role)
            if refusal:
                self.add_error('assigned_to', refusal)
                return cleaned

        # The assignee must be someone task_assign would accept for this role on this
        # project. No project, no candidate list (the views always pass one).
        if role and assignee is not None and (
                self.project is None
                or not _assignee_candidates(role, self.project).filter(pk=assignee.pk).exists()):
            choose = ("Choose a PM or one of this project's coordinators."
                      if role == Task.PM else "Choose someone in the task's role.")
            self.add_error('assigned_to', (
                f"{assignee.user.get_full_name() or assignee.user.username} is "
                f"{assignee.role}, not {dict(Task.ROLE_CHOICES).get(role, role)}. "
                f"{choose}"
            ))
        return cleaned


def _assignee_candidates(task_role, project):
    """views.task_assignee_candidates(): the one assignee rule, not restated here — two
    copies drift.

    Imported at call time to avoid a circular import: views imports this module, so a
    module-level import would be a cycle.
    """
    from .views import task_assignee_candidates
    return task_assignee_candidates(task_role, project)


def _labelled_assignee_candidates(task_role, project):
    """_assignee_candidates() as a list labelled by views._assignee_option_label(), for
    the Add Task dropdown.

    Imported at call time to avoid a circular import, as above.
    """
    from .views import _labelled_assignees
    return _labelled_assignees(_assignee_candidates(task_role, project))


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
            'gst_number', 'msme_status', 'msme_number', 'categories', 'kind',
        ]
        widgets = {
            'address':    forms.Textarea(attrs={'rows': 3, 'class': 'form-control'}),
            'categories': forms.CheckboxSelectMultiple(),
            'kind':       forms.Select(attrs={'class': 'form-select'}),
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
        # Not required, so a POST that predates the field (seed_order_demo posts this form
        # without it) still saves: clean_kind() reads a blank as "supplier" on add and as
        # "unchanged" on edit. The page's select always sends a value.
        self.fields['kind'].required = False
        self.fields['kind'].label = 'Kind'
        self.fields['kind'].help_text = ('Only a contractor (or a vendor that is both) can '
                                         'be named on a contractor bill.')

    def clean_kind(self):
        value = self.cleaned_data.get('kind')
        if value:
            return value
        if self.instance.pk is not None:
            return self.instance.kind
        return VENDOR_KIND_SUPPLIER

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

# THE LONGEST CATEGORY AND UNIT A SITE BOQ LINE CAN HOLD (B-0). Every line-creation path
# (opex_boq_entry, _boq_upload_apply, boq_correct, Residential seeding) copies both
# straight from the catalogue row, and the catalogue columns are wider (64 / 20) than the
# line columns (20 / 10). A longer value would save here and then crash the first BOQ save
# that used it. Read from BOQItem, not typed, so the two can never drift apart.
_LINE_CATEGORY_MAX = BOQItem._meta.get_field('category').max_length
_LINE_UOM_MAX = BOQItem._meta.get_field('uom').max_length

#: Ruling 12: once an item is on a site BOQ its unit is fixed. Shared with the tests.
UNIT_LOCKED_MESSAGE = ("This item is already used on site BOQs, so its unit can't change. "
                       "Retire it and create a new item with the right unit.")


def active_catalogue_positions(project_type, exclude_pk=None):
    """`project_type`'s ACTIVE catalogue items in position order, leaving out `exclude_pk`
    (the item being edited, so its own current place is never reported as taken or as its
    own neighbour). ONE QUERY: the clash check and both neighbours are read from this list
    in Python, so the form costs one query whatever it looks up (the Head's edit page has a
    pinned count).

    ACTIVE ONLY, because positions are about what the picker and the BOQ screens show, and a
    retired item is in neither (DW-4 O5: a retired item's position does not block either).
    Ties on a position (the pre-reorder duplicate 22) resolve by code, as every reader does.
    """
    items = BOQItemMaster.objects.filter(project_type=project_type, is_active=True)
    if exclude_pk is not None:
        items = items.exclude(pk=exclude_pk)
    return list(items.order_by('sort_order', 'code').only('code', 'sort_order', 'description'))


def catalogue_neighbours(positions, position):
    """(the item just before `position`, the item just after it) in `positions`
    (active_catalogue_positions()), either None at an end. No query."""
    before = [item for item in positions if item.sort_order < position]
    after = [item for item in positions if item.sort_order > position]
    return (before[-1] if before else None), (after[0] if after else None)


def _catalogue_place(item):
    return f'{item.code} ({item.sort_order}, {item.description})'


def catalogue_position_help(project_type, position, positions=None):
    """The position field's help text: what the number does, and which items it falls
    between (DW-4 O5), so the Head can see where an item will land before saving.
    `positions` is active_catalogue_positions(); read here when not given."""
    text = 'Where this item appears in the catalogue, the picker and every BOQ screen, lowest first.'
    if project_type == 'OPEX':
        text += ' Positions go up in steps of 10, so an item can be put between two others.'
    if position is None:
        return text
    if positions is None:
        positions = active_catalogue_positions(project_type)
    before, after = catalogue_neighbours(positions, position)
    if before and after:
        text += f' Now between {_catalogue_place(before)} and {_catalogue_place(after)}.'
    elif before:
        text += f' At the end, after {_catalogue_place(before)}.'
    elif after:
        text += f' At the start, before {_catalogue_place(after)}.'
    return text


class BOQItemMasterForm(forms.ModelForm):
    """Create / edit one catalogue entry. `code` is create-only — it is the stable
    identifier BOQ rows and grouped procurement refer to, so the edit view drops the
    field rather than letting it be reassigned (see admin_boq_item_edit).

    B-0 RULES, for both the Admin and the Design Head screen:
    - the unit is a dropdown of projects/units.py UNITS, the one shared list;
    - category and unit fit a BOQ line's columns (20 / 10);
    - a stored old spelling ('No.', 'Mtr', 'LOT') opens as its list code and saves as it,
      which is not a unit change;
    - an item already on a BOQ line cannot change its unit (ruling 12).
    """

    # A FORM FIELD, NOT MODEL `choices`. The column stays free text so no migration is
    # needed and the stored values that predate the list stay readable; the list is
    # enforced where values enter. The blank first option is deliberate: a new item must
    # choose, and an item whose stored unit has no mapping opens with nothing selected.
    unit = forms.ChoiceField(
        choices=[('', 'Choose a unit')] + list(UNIT_CHOICES),
        error_messages={'required': 'Choose a unit from the list.',
                        'invalid_choice': 'Choose a unit from the list.'},
    )

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
            'sheet, so it should match an existing RESCO category exactly. '
            f'At most {_LINE_CATEGORY_MAX} characters — the most a site BOQ line holds.'
        )
        self.fields['category'].widget.attrs['maxlength'] = _LINE_CATEGORY_MAX
        self.fields['unit'].help_text = (
            'Required — quantities cannot be summed across sites without it. Once the item '
            'is on a site BOQ its unit is fixed: to change it, retire the item and add a '
            'new one.'
        )
        # AN OLD SPELLING OPENS AS ITS LIST CODE. ModelForm's initial is the stored text,
        # which matches no option for 'No.' / 'Mtr' / 'LOT' and would render as "Choose a
        # unit". Showing the mapped code means saving the form stores it (a spelling fix
        # the log records). A value with no mapping maps to '' and opens unselected, so the
        # form cannot save until someone picks a unit (B-0 T1).
        if self.instance.pk:
            self.initial['unit'] = canonical_unit(self.instance.unit) or ''
        # DW-4: the general sentence; each catalogue view calls describe_position() to add
        # the items either side, which needs the template the item belongs to.
        self.fields['sort_order'].help_text = catalogue_position_help(None, None)
        self.fields['is_mandatory'].help_text = (
            'RESCO only. A mandatory item is added to every new RESCO BOQ and cannot be '
            'removed by the designer. Not used for Residential — every active Residential '
            'item is already pre-populated onto every new Residential BOQ.'
        )

    def _project_type(self, cleaned):
        """The template this item belongs to. The Head's screens delete `project_type` and
        force 'OPEX' after validation (see clean() below), so its absence means OPEX."""
        if 'project_type' in self.fields:
            return cleaned.get('project_type')
        return 'OPEX'

    def _check_position(self, cleaned):
        """DW-4 (O5, answer 4): refuse a position another ACTIVE item of the same template
        already holds, naming that item.

        ONLY WHEN THE POSITION IS CHANGED (or the item is new). Until the reorder is applied
        OPX-022 and OPX-208 share 22; checking every save would make both uneditable.

        WHY ACTIVE ONLY: a retired item is in neither the picker nor any new BOQ, so its
        number blocks nothing; WHY SAME TEMPLATE: Residential and RESCO positions are two
        separate lists (ITM-001 and OPX-001 both started at 1). No database constraint backs
        this (no migration, ruling O5); two saves racing for one free number could both pass,
        which leaves a tie that every reader breaks by code, so nothing is lost.
        """
        position = cleaned.get('sort_order')
        project_type = self._project_type(cleaned)
        if position is None or not project_type:
            return
        if self.instance.pk and 'sort_order' not in self.changed_data:
            return
        positions = self._positions(project_type)
        clash = next((item for item in positions if item.sort_order == position), None)
        if clash is None:
            return
        _before, after = catalogue_neighbours(positions, position)
        if after is None:
            hint = f' {position + 10} would come after it, at the end.'
        elif after.sort_order - position >= 2:
            hint = (f' {position + (after.sort_order - position) // 2} would sit between '
                    f'{clash.code} and {after.code}.')
        else:
            hint = ''
        self.add_error('sort_order', f'Position {position} is already used by {clash.code} '
                                     f'({clash.description}). Choose a free position.{hint}')

    def describe_position(self, project_type):
        """Set the position field's help text to name the items either side of the value
        the form shows: the posted value on a refused save, otherwise the initial one."""
        if 'sort_order' not in self.fields:
            return
        raw = self.data.get('sort_order') if self.is_bound else self.initial.get('sort_order')
        try:
            position = int(raw)
        except (TypeError, ValueError):
            position = None
        self.fields['sort_order'].help_text = catalogue_position_help(
            project_type, position,
            self._positions(project_type) if position is not None else None)

    def _positions(self, project_type):
        """active_catalogue_positions() for this form, read once per template and kept, so
        a refused save (the check, then the help text) still costs one query."""
        cache = self.__dict__.setdefault('_dw4_positions', {})
        if project_type not in cache:
            cache[project_type] = active_catalogue_positions(project_type, self.instance.pk)
        return cache[project_type]

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
        # B-3 Q2: OPX-273 is locked on both catalogue screens. Both edit views already
        # refuse it before building this form; this is the backstop for any other caller,
        # and the one place that covers the mandatory flag — a mandatory OPX-273 would make
        # the picker save put a bare ACCB line on every sheet.
        if self.instance.pk and self.instance.code == ACCB_CONFIGURED_CODE:
            raise forms.ValidationError(ACCB_CATALOGUE_LOCKED_MESSAGE)
        # DW-4 (O5, answer 4): placed before the mandatory rules' early returns so it runs on
        # every save, Head and Admin screens alike.
        self._check_position(cleaned)
        if not cleaned.get('is_mandatory'):
            return cleaned
        # B-2c (C2): a CALCULATED item cannot be mandatory. Anyone who may edit the catalogue
        # is refused, because the two flags contradict each other: mandatory puts the item
        # on every new RESCO BOQ for the designer to fill in by hand, while an active formula
        # rule writes its quantity and removes its line at 0. The loader refuses the other
        # direction (a rule on a mandatory item). Only an existing item can be a target.
        if self.instance.pk:
            rule = (BOQFormulaRule.objects
                    .filter(target_id=self.instance.pk, is_active=True).first())
            if rule is not None:
                raise forms.ValidationError({
                    'is_mandatory': f'This item is calculated by a formula rule '
                                    f'("{rule.summary}"), so it cannot be mandatory. Switch '
                                    f'the rule off first (load_formula_rules --deactivate).',
                })
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

    def clean_category(self):
        value = self.cleaned_data.get('category') or ''
        if len(value) > _LINE_CATEGORY_MAX:
            raise forms.ValidationError(
                f'Category can be at most {_LINE_CATEGORY_MAX} characters. A site BOQ line '
                f'holds no more, so a longer one would stop the BOQ from saving.')
        return value

    def clean_unit(self):
        """A list code that fits a BOQ line, and, on an item already in use, the unit it
        already had (ruling 12).

        The dropdown has already refused a blank or an off-list value by the time this
        runs. The length check below cannot fail with today's list (the longest code is 5
        characters); it is here so a longer code added to units.py later is refused at the
        catalogue, not at the first BOQ save that copies it.

        "IN USE" MEANS ANY BOQ LINE AT ALL, deleted sites included (B-0 P5, Zuber's
        ruling). A site that is restored brings its lines back, and they would then show a
        unit the catalogue no longer agrees with. That is why this does NOT apply the
        is_deleted filter every other site queryset carries.

        THE COMPARISON IS AGAINST THE STORED UNIT'S LIST CODE, not its raw text. 'No.'
        saved as 'Nos' is the same unit spelled the list's way, so it passes; 'No.' saved
        as 'Set' does not. A stored value with no mapping has no list code, so on an item
        in use every choice is a change and is refused. That item can only be retired.

        RACE: a designer can add the first line between this check and the save. The
        window is one request wide and the outcome is an unused-at-check item changing
        unit once, which is what the rule allowed a moment earlier. A row lock would not
        close it either, because line creation does not lock the catalogue row.
        """
        value = self.cleaned_data.get('unit') or ''
        if len(value) > _LINE_UOM_MAX:
            raise forms.ValidationError(
                f'Unit can be at most {_LINE_UOM_MAX} characters, the most a site BOQ line '
                f'holds.')
        if (self.instance.pk
                and value != canonical_unit(self.instance.unit)
                and self.instance.boq_items.exists()):
            raise forms.ValidationError(UNIT_LOCKED_MESSAGE)
        return value


#: DW-5 (Q3): why a module item's two module fields cannot change once a BOQ line uses it.
MODULE_FIELDS_LOCKED_MESSAGE = (
    'This item is on {count} site BOQ line{plural}, so whether it is a module and its '
    'wattage are fixed: changing them would change those sites\' module counts. To change '
    'them, retire this item and add a new one.')
#: DW-5 (Q13): the item the default wattage points at cannot be retired or re-rated.
MODULE_DEFAULT_LOCKED_MESSAGE = (
    '{wp} Wp is the default module wattage in Design settings. Change Design settings '
    'first.')


def module_wattage_taken_by(module_wp, exclude_pk=None):
    """The ACTIVE RESCO module item that already has this wattage, or None (Q4: one item
    per wattage). One query."""
    qs = BOQItemMaster.objects.filter(project_type='OPEX', is_active=True, is_module=True,
                                      module_wp=module_wp)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.order_by('sort_order', 'code').first()


class HeadCatalogueItemForm(BOQItemMasterForm):
    """DW-5 (M2): the Design Head's catalogue form — the shared form plus the two module
    fields. A SUBCLASS so the Admin's catalogue screen, which uses BOQItemMasterForm, is not
    offered them (Zuber, Q2): group membership follows `is_module`, and the module group is
    the Head's.

    The template shows the two fields only while the category reads "Module"; this clean()
    is what holds whatever the page showed.
    """

    class Meta(BOQItemMasterForm.Meta):
        fields = BOQItemMasterForm.Meta.fields + ['is_module', 'module_wp']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['is_module'].label = 'Module'
        self.fields['is_module'].help_text = (
            'A solar PV module, one item per wattage. Module items are entered in the BOQ '
            'picker\'s Modules pop-up and join the "all modules" group, which the earthing '
            'rule and the Mark BOQ complete check read.')
        self.fields['module_wp'].label = 'Module wattage (Wp)'
        self.fields['module_wp'].help_text = 'Whole watts, e.g. 640. Required for a module item.'

    def clean(self):
        cleaned = super().clean()
        is_module = bool(cleaned.get('is_module'))
        module_wp = cleaned.get('module_wp')
        instance = self.instance
        if is_module and (cleaned.get('category') or '').strip().lower() != 'module':
            self.add_error('is_module', 'A module item belongs to the Module category.')
        # A wattage may be missing only on an existing module item that never had one
        # (OPX-001, whose lines predate wattages).
        legacy = instance.pk and instance.is_module and instance.module_wp is None
        if is_module and module_wp is None and not legacy:
            self.add_error('module_wp', 'Enter the module\'s wattage in Wp.')
        if not is_module and module_wp is not None:
            self.add_error('module_wp', 'A wattage is for module items only: tick Module, or '
                                        'clear the wattage.')
        changed = instance.pk and (is_module != instance.is_module
                                   or module_wp != instance.module_wp)
        # Q3: fixed once in use, like the unit (ruling 12), and for the same reason: the
        # lines already counted under this item would silently change meaning. "In use" is
        # any BOQ line, deleted sites included, as clean_unit() counts it.
        if changed:
            count = instance.boq_items.count()
            if count:
                self.add_error(None, MODULE_FIELDS_LOCKED_MESSAGE.format(
                    count=count, plural='' if count == 1 else 's'))
        # Q4: one ACTIVE item per wattage, so the pop-up's dropdown never offers two "640 Wp".
        # RACE: two Head saves for one wattage could both pass; the second would leave two
        # items the dropdown labels alike, which the next edit of either is refused on. One
        # Head edits the catalogue, so this is not worth a database constraint.
        if is_module and module_wp and cleaned.get('is_active', True):
            other = module_wattage_taken_by(module_wp, exclude_pk=instance.pk)
            if other is not None:
                self.add_error('module_wp', f'{module_wp} Wp is already {other.code} '
                                            f'({other.description}). Each wattage is one item.')
        # Q13: the default wattage must keep an active item behind it.
        if instance.pk and instance.is_module and instance.is_active and instance.module_wp:
            leaves = (not cleaned.get('is_active', True) or not is_module
                      or module_wp != instance.module_wp)
            if leaves:
                if DesignSettings.current().default_module_wp == instance.module_wp:
                    self.add_error(None, MODULE_DEFAULT_LOCKED_MESSAGE.format(
                        wp=instance.module_wp))
        return cleaned


class DesignSettingsForm(forms.Form):
    """DW-5 (M8): the Head's design settings. One value today: the default module wattage
    the Modules pop-up preselects. A DROPDOWN of the wattages active module items have, so a
    wattage with no item cannot be chosen at all (the form refuses one posted anyway)."""

    default_module_wp = forms.TypedChoiceField(
        coerce=int, label='Default module wattage',
        help_text='Preselected on a new row in the BOQ picker\'s Modules pop-up. A change '
                  'applies from the next use; BOQs already saved are not changed.',
        error_messages={'invalid_choice': 'Choose a wattage an active module item has.'})

    def __init__(self, *args, wattages=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['default_module_wp'].choices = [(wp, f'{wp} Wp') for wp in wattages]
        self.fields['default_module_wp'].widget.attrs['class'] = 'form-select'


# ---------------------------------------------------------------------------
# Catalogue edit log — before and after values (B-0 T3)
#
# Every catalogue add, edit, retire and reactivate, on both screens and in the
# normalise_catalogue_units command, writes its ActivityLog text through these helpers so
# the format is one format. ActivityLog.action is free text of 255 characters; there is no
# structured column, and adding one would be a migration. So:
#   "<prefix>: unit: No. → Nos; category: 'MMS' → 'BOS'"
# lists only the fields that changed. Text values are quoted and shortened to about 95
# characters with "…" (approved). If the line would still run past 255, the description
# change is written as an entry of its own, so it is not cut off by log_activity.
# ---------------------------------------------------------------------------

#: (field, the name a reader sees), in the order an entry lists them. `code` is absent: it
#: is create-only and every entry's prefix already names it.
_CATALOGUE_LOG_FIELDS = (
    ('description',  'description'),
    ('unit',         'unit'),
    ('category',     'category'),
    ('project_type', 'type'),
    ('is_active',    'active'),
    ('is_mandatory', 'mandatory'),
    ('sort_order',   'sort order'),
    # DW-5 (M2): logged like the others. A create lists them only for a module item, so the
    # entry for every other new item reads exactly as before.
    ('is_module',    'module'),
    ('module_wp',    'wattage'),
)
_CATALOGUE_LOG_MODULE = {'is_module', 'module_wp'}
_CATALOGUE_LOG_QUOTED = {'description', 'category'}
_CATALOGUE_LOG_VALUE_MAX = 95
_ACTION_MAX = ActivityLog._meta.get_field('action').max_length


def catalogue_snapshot(item):
    """The logged fields of `item` as they are now. Taken BEFORE a form is validated: a
    ModelForm writes its cleaned values onto the instance during is_valid(), so a snapshot
    taken afterwards would already hold the new values."""
    return {name: getattr(item, name) for name, _label in _CATALOGUE_LOG_FIELDS}


def _catalogue_log_value(name, value, limit):
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    if name == 'project_type':
        return dict(Project.PROJECT_TYPE_CHOICES).get(value, value)
    if name == 'module_wp':
        return 'not recorded' if value is None else f'{value} Wp'
    text = str(value)
    if name in _CATALOGUE_LOG_QUOTED:
        if len(text) > limit:
            text = text[:limit - 1] + '…'
        return f"'{text}'"
    return text


def catalogue_change_entries(prefix, before, after):
    """The log text for one catalogue save: a list of one entry, or two when the
    description change needs a line of its own. `before` is None for a create, which
    lists every field as "→ value". A save that changed nothing still yields one entry
    saying so, because every save was logged before B-0 and still is."""
    # Two description values must fit one entry beside the prefix. With a short code
    # ('OPX-038') this gives the approved 95; a long code shortens the values rather than
    # letting log_activity cut the entry's tail.
    overhead = len(f"{prefix}: description: '' → ''")
    limit = max(20, min(_CATALOGUE_LOG_VALUE_MAX, (_ACTION_MAX - overhead) // 2))
    parts = {}
    for name, label in _CATALOGUE_LOG_FIELDS:
        if before is None and name in _CATALOGUE_LOG_MODULE and not after['is_module']:
            continue
        new = _catalogue_log_value(name, after[name], limit)
        if before is None:
            parts[name] = f'{label}: → {new}'
        elif before[name] != after[name]:
            parts[name] = f'{label}: {_catalogue_log_value(name, before[name], limit)} → {new}'
    if not parts:
        return [f'{prefix}: no fields changed']
    whole = f"{prefix}: {'; '.join(parts.values())}"
    if len(whole) <= _ACTION_MAX or 'description' not in parts or len(parts) == 1:
        return [whole]
    description = parts.pop('description')
    return [f'{prefix}: {description}', f"{prefix}: {'; '.join(parts.values())}"]


def log_catalogue_change(actor, item, prefix, before, action_code=''):
    """Write the before/after entries for one catalogue save. `actor` None is a system
    write (the normalise command), which names itself in `prefix`."""
    for text in catalogue_change_entries(prefix, before, catalogue_snapshot(item)):
        log_activity(None, actor, text, entity_type='BOQItemMaster', entity_id=item.pk,
                     action_code=action_code)


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


#: SNAP-3a ruling Q1 (20 Nov 2026): the only role that may switch a tender in or out of the
#: daily snapshot. Admin alone, as on the 51 role_required(['Admin']) screens and
#: TENDER_DOC_ADMIN_ROLES. System Admin cannot open program_edit at all, and a PM or the
#: tender's creator, who can, must not decide what management sees.
SNAPSHOT_SWITCH_ROLES = frozenset({'Admin'})


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
        #
        # SNAP-3a: in_daily_snapshot is listed here but REMOVED in __init__ unless the
        # view passes snapshot_switch=True (Admin, edit form only). See __init__.
        fields = [
            'program_type', 'name', 'client_name', 'status',
            'short_tender_code', 'total_capacity', 'planned_site_count',
            'in_daily_snapshot',
        ]
        widgets = {
            'program_type':              forms.Select(attrs={'class': 'form-select'}),
            'status':                    forms.Select(attrs={'class': 'form-select'}),
            'name':                      forms.TextInput(attrs=_bs),
            'client_name':               forms.TextInput(attrs=_bs),
            'short_tender_code':         forms.TextInput(attrs={**_bs, 'placeholder': 'e.g. IPGCL26 or HRP-2026'}),
            'total_capacity':            forms.NumberInput(attrs={**_bs, 'step': '0.01'}),
            'planned_site_count':        forms.NumberInput(attrs={**_bs, 'min': '0'}),
            'in_daily_snapshot':         forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
        labels = {
            'total_capacity':      'Total planned capacity (MW)',
            'short_tender_code':   'Short tender code (RESCO)',
            'planned_site_count':  'Planned site count',
            'in_daily_snapshot':   'Include in daily snapshot',
        }

    #: Shown for any program_type outside the offered choices — in practice a hand-
    #: posted CAPEX. "New Project" is the button label on the PM dashboard and the
    #: Admin projects list.
    PROGRAM_TYPE_REFUSED = ('A Program can only be RESCO. CAPEX projects stand alone: '
                            'create them from New Project, outside any Program.')

    def __init__(self, *args, snapshot_switch=False, **kwargs):
        super().__init__(*args, **kwargs)
        # SNAP-3a: the "Include in daily snapshot" switch is on the form ONLY when the
        # view asks for it, which program_edit does for SNAPSHOT_SWITCH_ROLES (Admin)
        # alone. Everyone else, the create form and the seed commands get a form without
        # it, so the column is never written: construct_instance skips a field missing
        # from cleaned_data. This is also why it is opt-in rather than opt-out. A
        # checkbox absent from the POST body cleans to False, so a form that carried it
        # without rendering it (or a seed command's dict) would switch the tender OFF
        # on every save.
        if not snapshot_switch:
            del self.fields['in_daily_snapshot']
        # CAPEX STANDS ALONE (CAPEX-3). No CAPEX site can join a Program — every add-site
        # path 404s on a non-OPEX Program — so a CAPEX Program is an empty shell. The
        # limit lives HERE, on the form, and not on Program.PROGRAM_TYPE_CHOICES: the
        # model keeps CAPEX so an existing CAPEX row stays valid and untouched. A
        # posted CAPEX fails the choice check and gets the message above, never the
        # stock "Select a valid choice", which would name the stored code.
        field = self.fields['program_type']
        field.choices = [(value, label) for value, label in field.choices
                         if value != 'CAPEX']
        field.error_messages['invalid_choice'] = self.PROGRAM_TYPE_REFUSED

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
            # Unreachable since CAPEX-3 (__init__ drops the choice). Kept so the rule
            # holds if a CAPEX Program is ever allowed again.
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


class StockLocationForm(forms.ModelForm):
    """Add / edit one warehouse from the admin panel (see stock_location_create).

    `is_active` is NOT on this form: retirement has its own POST (stock_location_toggle)
    so that saving a name can never close a warehouse by an unticked box.

    THE KEEPER MUST ALREADY HOLD THE FLAG. The dropdown offers only profiles with
    `is_warehouse_keeper=True`, and a posted id outside that set fails as an invalid
    choice — the flag is granted on the user edit screen, never as a side effect of
    being picked here. That also covers a keeper who has since lost the flag: re-saving
    their warehouse refuses until someone who holds it is chosen, or the field is
    cleared.
    """

    class Meta:
        model  = StockLocation
        fields = ['code', 'name', 'keeper']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        keeper = self.fields['keeper']
        keeper.required = False
        keeper.queryset = (UserProfile.objects
                           .filter(is_warehouse_keeper=True)
                           .select_related('user')
                           .order_by('user__first_name', 'user__last_name', 'user__username'))
        keeper.label_from_instance = (
            lambda p: p.user.get_full_name() or p.user.username)
        keeper.empty_label = '— No keeper —'
