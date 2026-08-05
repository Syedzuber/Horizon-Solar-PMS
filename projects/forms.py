import re
from django import forms
from django.contrib.auth.models import User
from .models import UserProfile, Project, ProjectPhase, Task, Vendor, VendorCategory, Program, BOQItemMaster


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
        help_text=('Reviews OPEX designs at the FIRST gate, before the Design Head. '
                   'Independent of role. Holding both flags is allowed, but one person '
                   'can never record both verdicts on the same site.'),
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

    def clean_project_type(self):
        # Defense-in-depth: reject a hand-crafted POST that smuggles OPEX past the
        # trimmed <select> above.
        value = self.cleaned_data.get('project_type')
        if value == 'OPEX':
            raise forms.ValidationError(
                'OPEX projects must be created under a Program, not from this form.'
            )
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


class PostActivationFieldEditForm(forms.ModelForm):
    """Narrow edit form for the three business fields a PM/Coordinator may change
    AFTER a project leaves Draft (see views.project_field_edit). Deliberately
    exposes ONLY capacity / contract value / target commissioning date plus an
    optional free-text reason — never the customer/scope fields on ProjectEditForm.

    Positive-value validation mirrors ProjectEditForm. clean_contract_value also
    blocks a *change* to contract_value once payment-milestone amounts exist,
    because set_milestone_amounts validated M1+M2+M3 == contract_value and nothing
    reconciles them here (accepted design gap — the block keeps the invariant safe).
    """

    reason = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'rows': 2, 'class': 'form-control form-control-sm',
                                     'placeholder': 'Optional — why is this changing?'}),
    )

    class Meta:
        model = Project
        fields = ['capacity_kw', 'contract_value', 'target_commissioning_date']
        labels = {'capacity_kw': 'Capacity (kW)'}
        widgets = {
            'capacity_kw':               forms.NumberInput(attrs={'class': 'form-control form-control-sm', 'step': '0.01'}),
            'contract_value':            forms.NumberInput(attrs={'class': 'form-control form-control-sm', 'step': '0.01'}),
            'target_commissioning_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control form-control-sm'}),
        }

    def clean_capacity_kw(self):
        value = self.cleaned_data.get('capacity_kw')
        if value is not None and value <= 0:
            raise forms.ValidationError('Capacity must be greater than zero.')
        return value

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
                  'is_active', 'sort_order']

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
            'every new Residential BOQ; OPEX items are offered in the OPEX BOQ picker.'
        )
        self.fields['category'].help_text = (
            'Residential: display grouping only. OPEX: groups the picker and the saved '
            'sheet, so it should match an existing OPEX category exactly.'
        )
        self.fields['unit'].help_text     = 'e.g. Nos, Mtr, Kg, Set, Lot. Required — quantities cannot be summed across sites without it.'
        self.fields['sort_order'].help_text = 'Position in the standard BOQ template; also becomes the line item serial number.'

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


def normalize_program_code(value):
    """Uppercase + strip to [A-Z0-9] (drops spaces/hyphens/punctuation). Shared by
    short_tender_code here and by the OPEX site_code in the Program-scoped creation
    path, so both halves of a composed project_id normalize identically. A hyphen
    inside a token would blur the single `{code}-{site}` delimiter — hence stripped."""
    return _CODE_STRIP_RE.sub('', (value or '').upper())


class ProgramForm(forms.ModelForm):
    """Create / edit a Program (OPEX tender or multi-site CAPEX contract).

    OPEX-only rules (spec §2 / §4): short_tender_code is required, normalized,
    reserved-code-guarded, and globally unique across ALL Programs INCLUDING
    soft-deleted ones (it is a building block of a globally-unique site project_id,
    so a soft-deleted collision would still produce colliding IDs). tender_reference_number,
    when supplied, is likewise unique soft-delete-aware. All uniqueness checks are
    explicit .filter().exists() queries returning a clear message — never a raw DB error.
    """

    class Meta:
        model = Program
        fields = [
            'program_type', 'name', 'client_name', 'status',
            'short_tender_code', 'total_capacity', 'expected_completion_date',
            'planned_site_count',
            # OPEX-specific
            'tender_reference_number', 'bid_value', 'award_date',
            'ppa_reference', 'ppa_signed_date', 'ppa_per_unit_rate',
            'ppa_escalation_percentage', 'ppa_escalation_frequency',
            # CAPEX-specific placeholders
            'financing_partner_name', 'financing_assistance_type',
        ]
        widgets = {
            'program_type':              forms.Select(attrs={'class': 'form-select'}),
            'status':                    forms.Select(attrs={'class': 'form-select'}),
            'name':                      forms.TextInput(attrs=_bs),
            'client_name':               forms.TextInput(attrs=_bs),
            'short_tender_code':         forms.TextInput(attrs={**_bs, 'placeholder': 'e.g. IPGCL26'}),
            'total_capacity':            forms.NumberInput(attrs={**_bs, 'step': '0.01'}),
            'expected_completion_date':  forms.DateInput(attrs={**_bs, 'type': 'date'}),
            'planned_site_count':        forms.NumberInput(attrs={**_bs, 'min': '0'}),
            'tender_reference_number':   forms.TextInput(attrs=_bs),
            'bid_value':                 forms.NumberInput(attrs={**_bs, 'step': '0.01'}),
            'award_date':                forms.DateInput(attrs={**_bs, 'type': 'date'}),
            'ppa_reference':             forms.TextInput(attrs=_bs),
            'ppa_signed_date':           forms.DateInput(attrs={**_bs, 'type': 'date'}),
            'ppa_per_unit_rate':         forms.NumberInput(attrs={**_bs, 'step': '0.0001'}),
            'ppa_escalation_percentage': forms.NumberInput(attrs={**_bs, 'step': '0.01'}),
            'ppa_escalation_frequency':  forms.TextInput(attrs={**_bs, 'placeholder': 'e.g. Annual'}),
            'financing_partner_name':    forms.TextInput(attrs=_bs),
            'financing_assistance_type': forms.TextInput(attrs=_bs),
        }
        labels = {
            'total_capacity':      'Total planned capacity (MW)',
            'short_tender_code':   'Short tender code (OPEX)',
            'planned_site_count':  'Planned site count',
        }

    def clean_short_tender_code(self):
        return normalize_program_code(self.cleaned_data.get('short_tender_code'))

    def clean(self):
        cleaned = super().clean()
        program_type = cleaned.get('program_type')
        code = cleaned.get('short_tender_code', '')

        if program_type == 'OPEX':
            if not code:
                self.add_error('short_tender_code',
                               'Short tender code is required for an OPEX tender.')
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
        fields = [
            'site_code', 'customer_contact_person', 'customer_phone', 'customer_email',
            'site_address', 'city', 'state', 'capacity_kw',
        ]
        labels = {
            'capacity_kw': 'Capacity (kW)',
            'site_code': 'Site Code',
            'customer_contact_person': 'Site In-Charge Name',
            'customer_phone': 'Site In-Charge Phone',
            'customer_email': 'Site In-Charge Email',
        }
        widgets = {
            'site_code':                 forms.TextInput(attrs={**_bs, 'placeholder': 'e.g. S045'}),
            'customer_contact_person':   forms.TextInput(attrs=_bs),
            'customer_phone':            forms.TextInput(attrs={**_bs, 'maxlength': '10'}),
            'customer_email':            forms.EmailInput(attrs=_bs),
            'site_address':              forms.Textarea(attrs={**_bs, 'rows': 3}),
            'city':                      forms.TextInput(attrs=_bs),
            'state':                     forms.TextInput(attrs=_bs),
            'capacity_kw':               forms.NumberInput(attrs={**_bs, 'step': '0.01'}),
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

    def clean_capacity_kw(self):
        value = self.cleaned_data.get('capacity_kw')
        if value is not None and value <= 0:
            raise forms.ValidationError('Capacity must be greater than zero.')
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
