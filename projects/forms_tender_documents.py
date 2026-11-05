"""
TD-B2 — tender documents: the upload form (rulings R3, R7, R9, B2-b).

One file per document (B2-b). The file is checked here with validate_dc_document — the
challan documents' check, unchanged (R3) — so a refused file is a form error and the view
never calls storage for it. store_tender_document_file() runs the same check again before
it uploads; the second run is the storage wrapper's own guarantee, not this form's.

WHO MAY SUBMIT IT is not this form's question: the views ask
user_can_manage_tender_documents() before they build it, on every request.
"""
from django import forms

from .delivery_storage import DeliveryStorageError, validate_dc_document
from .models import (
    TENDER_DOC_CATEGORY_CHOICES, TENDER_DOC_MANAGEMENT, TENDER_DOC_SITE_TEAM,
    TENDER_DOC_VISIBILITY_CHOICES, TenderDocument,
)

#: R9 — the tick's refusal when Site team is chosen without it.
NO_PRICES_REQUIRED = 'Site Engineers will see this file. Confirm it contains no prices.'


def active_tender_documents(program):
    """`program`'s documents that are neither voided nor superseded — the ones a new
    upload may replace (R7) and the manage page lists as current. Newest first, as the
    model orders them."""
    # Both terms are columns on the row: voided_at IS NULL and superseded_by IS NULL.
    # A superseded row is history whatever happens to its replacement, so it never comes
    # back into this list (one level only, R7).
    return TenderDocument.objects.filter(program=program, voided_at__isnull=True,
                                         superseded_by__isnull=True)


class TenderDocumentChoiceField(forms.ModelChoiceField):
    """The "Replaces" drop-down: title, file name and upload date, so two documents with
    the same title can be told apart."""

    def label_from_instance(self, obj):
        return f'{obj.title} — {obj.original_filename} ({obj.uploaded_at:%d %b %Y})'


class TenderDocumentUploadForm(forms.Form):
    """Upload one tender document, optionally replacing an active one (R7).

    `program` is required: it limits "Replaces" to that Program's active documents, so a
    document of another tender, a voided one or an already-replaced one is refused here as
    an invalid choice, before any file is stored. The view re-checks the chosen row under
    a lock, because it can change between this check and the write.
    """

    title = forms.CharField(max_length=200, strip=True)
    category = forms.ChoiceField(choices=TENDER_DOC_CATEGORY_CHOICES)
    visibility = forms.ChoiceField(choices=TENDER_DOC_VISIBILITY_CHOICES,
                                   initial=TENDER_DOC_MANAGEMENT)
    no_prices_confirmed = forms.BooleanField(required=False)
    file = forms.FileField()
    replaces = TenderDocumentChoiceField(queryset=TenderDocument.objects.none(),
                                         required=False, empty_label='Nothing — a new document',
                                         widget=forms.Select(attrs={'class': 'form-select'}))

    def __init__(self, *args, program, **kwargs):
        super().__init__(*args, **kwargs)
        self.program = program
        # Title order reads best in a drop-down; the model's default is newest first.
        self.fields['replaces'].queryset = (active_tender_documents(program)
                                            .order_by('title', '-uploaded_at'))

    def clean_file(self):
        """The challan documents' check (R3): PDF, JPG or PNG, not empty, at most 20 MB,
        and leading bytes that match the extension. Its message names the file."""
        uploaded = self.cleaned_data['file']
        try:
            validate_dc_document(uploaded)
        except DeliveryStorageError as exc:
            raise forms.ValidationError(str(exc))
        return uploaded

    def clean(self):
        """R9: a Site team document needs the no-prices tick. For Management only the
        tick is dropped, so the view never stores a confirmation nobody was asked for."""
        cleaned = super().clean()
        if cleaned.get('visibility') == TENDER_DOC_SITE_TEAM:
            if not cleaned.get('no_prices_confirmed'):
                self.add_error('no_prices_confirmed', NO_PRICES_REQUIRED)
        else:
            cleaned['no_prices_confirmed'] = False
        return cleaned
