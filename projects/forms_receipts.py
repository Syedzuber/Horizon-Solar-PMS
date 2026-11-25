"""
FIN-M-B5-3 — the form behind the "Record a client receipt" page (views_receipts.py).

EARLY, FRIENDLY MESSAGES ONLY. Every field runs the WRITER'S OWN rule where one exists —
receipts.clean_receipt_amount() and receipts.clean_receipt_date() — so a mistake is shown
beside the field it belongs to, in the writer's exact words, before anything is uploaded.
receipts.record_receipt() still checks everything again and is the final word; whatever
it refuses after this form has passed (a race, a failed upload) is shown at the top.

The proof is checked here with validate_dc_document — the challan documents' check,
unchanged — as forms_tender_documents.py does, so a wrong file type or an oversized file
is a field error and the view never calls storage for it. store_receipt_proof() runs the
same check again before it uploads; that second run is the storage wrapper's guarantee.

WHO MAY SUBMIT IT is not this form's question: the view asks user_can_record_receipt()
before it builds the form, on every request.
"""
from django import forms

from .delivery_storage import DeliveryStorageError, validate_dc_document
from .models import PaymentReceipt
# MODE_REQUIRED and OTHER_NEEDS_NOTE are the writer's own constants (FIN-M-B5-4a): the
# form places them beside their field, the writer (_clean_figures) stays the final check,
# and one wording serves both. Re-exported under these names for the B5-3 tests.
from .receipts import (
    MODE_REQUIRED, OTHER_NEEDS_NOTE, REFERENCE_LIMIT, ReceiptRefused, clean_receipt_amount,
    clean_receipt_date,
)

REFERENCE_TOO_LONG = f'The reference can be at most {REFERENCE_LIMIT} characters.'


class ReceiptRecordForm(forms.Form):
    """Amount, date received, mode, reference, note and an optional proof (FR18, FR29),
    plus the E4 "Record anyway" tick and the key of the warnings it acknowledges.

    Amount and date are not `required` at the Django level: an empty box falls through
    to the writer's own rule, whose message ("Enter the amount received.") is the one
    shown, rather than Django's generic "This field is required."."""

    # Text, not DecimalField: the writer's rule reads text (it allows "1,25,000.50" and
    # refuses three decimals with its own wording); DecimalField would reword both.
    amount = forms.CharField(required=False, max_length=30, strip=True)
    # No initial value (E3): the date received must be typed, never assumed to be today.
    date_received = forms.DateField(
        required=False,
        error_messages={'invalid': 'Enter the date the money was received, as a date.'})
    mode = forms.ChoiceField(
        choices=PaymentReceipt.MODE_CHOICES, required=False,
        error_messages={'invalid_choice': MODE_REQUIRED})
    reference = forms.CharField(
        required=False, max_length=REFERENCE_LIMIT, strip=True,
        error_messages={'max_length': REFERENCE_TOO_LONG})
    note = forms.CharField(required=False, strip=True, widget=forms.Textarea)
    proof = forms.FileField(required=False)
    # E4: ticked to save despite the warnings shown. Trusted only together with
    # warning_key: the view recomputes the warnings on every POST and saves on the tick
    # alone only when they are still exactly the ones the page showed.
    record_anyway = forms.BooleanField(required=False)
    warning_key = forms.CharField(required=False, max_length=64, widget=forms.HiddenInput)

    def clean_amount(self):
        """The writer's amount rule (FR3, FR29): > 0, at most two decimals, fits the column.
        Returns the Decimal the writer will store."""
        try:
            return clean_receipt_amount(self.cleaned_data.get('amount'))
        except ReceiptRefused as exc:
            raise forms.ValidationError(str(exc))

    def clean_date_received(self):
        """The writer's date rule (FR13, FR29): required, not after today in IST."""
        try:
            return clean_receipt_date(self.cleaned_data.get('date_received'))
        except ReceiptRefused as exc:
            raise forms.ValidationError(str(exc))

    def clean_mode(self):
        """One of the five modes (FR18); blank is refused in the writer's words."""
        mode = self.cleaned_data.get('mode')
        if not mode:
            raise forms.ValidationError(MODE_REQUIRED)
        return mode

    def clean_proof(self):
        """Optional (FR17). When given: PDF, JPG or PNG, not empty, at most 20 MB, leading
        bytes matching the extension — the challan documents' check. Its message names the
        file."""
        uploaded = self.cleaned_data.get('proof')
        if not uploaded:
            return None
        try:
            validate_dc_document(uploaded)
        except DeliveryStorageError as exc:
            raise forms.ValidationError(str(exc))
        return uploaded

    def clean(self):
        """FR18: "Other" says nothing by itself, so the note is required with it. Shown
        beside the note box."""
        cleaned = super().clean()
        if cleaned.get('mode') == PaymentReceipt.OTHER and not cleaned.get('note'):
            self.add_error('note', OTHER_NEEDS_NOTE)
        return cleaned
