"""
The contractor attendance entry form (ATT-B1) — one form for add and edit.

It reads and checks what the page posts; projects/attendance.py writes. Date, project
and recorder are not fields at all (they are set by the writer and never change), so no
posted value can reach them. The photo field is read here only for presence on create;
its type / size / content checks are qaqc_views.validate_visit_photo's, run by the upload.
"""
from django import forms

from .models import VENDOR_BILLABLE_KINDS, ContractorAttendance, Vendor
from .qaqc_views import parse_photo_taken_at

#: The contractor picker's value for "Contractor not listed" — never a Vendor pk.
UNLISTED = 'unlisted'


def contractor_choices(current=None):
    """Active vendors of kind contractor or both, by name — the contractor bill's rule —
    plus `current` when editing an entry whose contractor has since been deactivated or
    re-kinded, so the edit can keep it (edge case 9). A deactivated contractor is never
    offered to a new entry."""
    vendors = list(Vendor.objects.filter(kind__in=VENDOR_BILLABLE_KINDS, is_active=True)
                   .order_by('name'))
    if current is not None and all(v.pk != current.pk for v in vendors):
        vendors.append(current)
    return vendors


class AttendanceForm(forms.Form):
    contractor = forms.CharField(required=True, error_messages={
        'required': 'Pick a contractor, or choose "Contractor not listed".'})
    unlisted_contractor_name = forms.CharField(max_length=120, required=False)
    trade = forms.ChoiceField(choices=ContractorAttendance.TRADE_CHOICES, error_messages={
        'required': 'Pick a trade.', 'invalid_choice': 'Pick a trade.'})
    trade_remark = forms.CharField(max_length=120, required=False)
    headcount = forms.IntegerField(
        min_value=ContractorAttendance.HEADCOUNT_MIN, max_value=ContractorAttendance.HEADCOUNT_MAX,
        error_messages={
            'required': 'Enter the headcount.',
            'invalid': 'Headcount must be a whole number.',
            'min_value': f'Headcount must be between {ContractorAttendance.HEADCOUNT_MIN} '
                         f'and {ContractorAttendance.HEADCOUNT_MAX}.',
            'max_value': f'Headcount must be between {ContractorAttendance.HEADCOUNT_MIN} '
                         f'and {ContractorAttendance.HEADCOUNT_MAX}.',
        })
    time_in = forms.TimeField(input_formats=['%H:%M', '%H:%M:%S'], error_messages={
        'required': 'Enter the time in.', 'invalid': 'Enter the time in as HH:MM.'})
    time_out = forms.TimeField(input_formats=['%H:%M', '%H:%M:%S'], required=False,
                               error_messages={'invalid': 'Enter the time out as HH:MM.'})
    photo = forms.FileField(required=False)
    photo_taken_at = forms.CharField(required=False)

    def __init__(self, *args, entry=None, **kwargs):
        """`entry` is the entry being edited, or None on create. On create the photo is
        required and time out is not offered (ruling 4: the crew is arriving)."""
        super().__init__(*args, **kwargs)
        self.entry = entry
        self.vendors = contractor_choices(entry.contractor if entry is not None
                                          and entry.contractor_id else None)

    def clean_contractor(self):
        """The posted pick as a Vendor, or None for "Contractor not listed". A pk that is
        not in the picker (inactive, a supplier, made up) is refused."""
        value = self.cleaned_data['contractor'].strip()
        if value == UNLISTED:
            return None
        for vendor in self.vendors:
            if str(vendor.pk) == value:
                return vendor
        raise forms.ValidationError(
            'Pick a contractor from the list, or choose "Contractor not listed".')

    def clean_photo_taken_at(self):
        # Browser-read EXIF time, informational only: anything unreadable is NULL, never
        # an error (edge case 12).
        return parse_photo_taken_at(self.cleaned_data.get('photo_taken_at'))

    def clean(self):
        cleaned = super().clean()
        if 'contractor' in cleaned and cleaned['contractor'] is None \
                and not (cleaned.get('unlisted_contractor_name') or '').strip():
            self.add_error('unlisted_contractor_name', "Type the contractor's name.")
        if cleaned.get('trade') == ContractorAttendance.OTHER \
                and not (cleaned.get('trade_remark') or '').strip():
            self.add_error('trade_remark', 'Say what the trade is when you pick Other.')
        if self.entry is None:
            # Create: no time out yet, and a group photo is mandatory (ruling 5).
            cleaned['time_out'] = None
            if not cleaned.get('photo'):
                self.add_error('photo', 'Add a group photo of the crew.')
        time_in, time_out = cleaned.get('time_in'), cleaned.get('time_out')
        if time_in is not None and time_out is not None and time_out <= time_in:
            self.add_error('time_out', 'Time out must be later than time in, on the same day.')
        return cleaned
