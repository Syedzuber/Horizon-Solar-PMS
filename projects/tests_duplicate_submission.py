"""Duplicate submission — the URL-held key, the content-match warning and the Back
button, on the five create paths and the documents append (28 Sep 2026).

What this file pins, and why each matters:

  * THE KEY LIVES IN THE URL. A form GET without a valid ?key= is redirected to the same
    URL (every other parameter kept) with a fresh one, and the page renders that key —
    the same key every time the URL is fetched. Back and reload therefore cannot mint a
    new key, which is how a re-fetched form made a second record (audit A2, b_refetch).
  * A KEY MATCH IS NEVER SILENT (D2). A repeated key — a second POST, or a GET of a URL
    whose key already made a record — goes to that record with a message and a link to a
    fresh form. If the person cannot open the record, the message still appears, with a
    403 that names nothing about it.
  * A FRESH FORM WITH THE SAME VALUES WARNS (D3). A second record that looks like a
    recent one re-renders the form with a warning (200, same key, nothing written,
    nothing uploaded); confirm_duplicate=1 makes exactly one record. Different values, a
    rejected payment, or a match older than the window do not warn.
  * DOCUMENTS HAVE NO KEY. The same file on the same order within the window warns, with
    "Attach anyway"; a different file does not.

Run with:
    python manage.py test projects.tests_duplicate_submission --settings=solarpms.test_settings
"""
import re
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone

from .models import (
    PaymentRequest, Vendor, VendorOrder, VendorOrderDocument, VENDOR_ORDER_DOC_PI,
)
from .submission_guard import ALREADY_SUBMITTED
from .tests_purchases_workspace import WorkspaceFixture
from .tests_vendor_order_raise import RaiseFixture, _client, _pdf

_RENDERED_KEY = re.compile(r'name="client_uuid" value="([0-9a-f-]{36})"')


def _rendered_key(response):
    match = _RENDERED_KEY.search(response.content.decode())
    return uuid.UUID(match.group(1)) if match else None


def _location(response):
    parsed = urlparse(response['Location'])
    return parsed.path, parse_qs(parsed.query)


def _messages(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


class _Fixture(WorkspaceFixture):
    """WorkspaceFixture (RaiseFixture's Residential project and roles, GroupRaiseFixture's
    tenders and RESCO sites) plus a second vendor, a fresh storage stub per test, and one
    SCM client."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.vendor2 = Vendor.objects.create(name='Second Modules', contact_person='S',
                                            phone='9000000002')

    def setUp(self):
        super().setUp()
        self.storage = MagicMock()
        self.scm_client = _client(self.scm)

    def assertNothingUploaded(self):
        self.storage.storage.from_.return_value.upload.assert_not_called()

    def assertAlreadySubmitted(self, response, record_url):
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], record_url)
        # The first submission's own success message is still queued (these tests do not
        # follow its redirect), so look for exactly one D2 message, and it is the last.
        texts = _messages(response)
        said = [text for text in texts if ALREADY_SUBMITTED in text]
        self.assertEqual(len(said), 1)
        self.assertEqual(texts[-1], said[0])
        self.assertIn('Open a fresh form', said[0])
        # The fresh form's link carries no key, so opening it issues a new one.
        self.assertNotIn('key=', said[0])


class _PathCases:
    """The cases every create path answers alike. A subclass says where its form is, what
    a valid submission is, which rows it counts, and what "different" means."""

    #: Query parameters the form is opened with, to prove the key redirect keeps them.
    def form_params(self):
        return {}

    def form_url(self, **params):
        raise NotImplementedError

    def post_url(self):
        raise NotImplementedError

    def values(self, **changes):
        raise NotImplementedError

    def made(self):
        raise NotImplementedError

    def different(self):
        raise NotImplementedError

    def record_url(self):
        """The detail page of the record the last successful POST created."""
        raise NotImplementedError

    # ── driving the form ───────────────────────────────────────────────────────

    def open_form(self):
        """GET the form as a person would — no key — follow the key redirect, and return
        (keyed URL, rendered key)."""
        first = self.scm_client.get(self.form_url(**self.form_params()))
        self.assertEqual(first.status_code, 302)
        page = self.scm_client.get(first['Location'])
        self.assertEqual(page.status_code, 200)
        return first['Location'], _rendered_key(page)

    def submit(self, key, confirm=False, **changes):
        data = self.values(**changes)
        data['client_uuid'] = str(key)
        if confirm:
            data['confirm_duplicate'] = '1'
        with patch('projects.supabase_storage.get_supabase_client', return_value=self.storage):
            return self.scm_client.post(self.post_url(), data)

    def submit_first(self):
        _, key = self.open_form()
        response = self.submit(key)
        self.assertEqual(response.status_code, 302)
        return key

    # ── the URL-held key ───────────────────────────────────────────────────────

    def test_get_without_key_redirects_to_the_same_url_with_a_valid_key(self):
        response = self.scm_client.get(self.form_url(**self.form_params()))
        self.assertEqual(response.status_code, 302)
        path, query = _location(response)
        self.assertEqual(path, urlparse(self.form_url()).path)
        uuid.UUID(query['key'][0])
        for name, value in self.form_params().items():
            self.assertEqual(query[name], [str(value)])

    def test_get_with_the_same_key_twice_renders_the_same_key(self):
        keyed, key = self.open_form()
        again = self.scm_client.get(keyed)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(_rendered_key(again), key)
        self.assertIn(str(key), keyed)

    def test_a_malformed_key_is_treated_as_absent(self):
        response = self.scm_client.get(self.form_url(key='not-a-uuid', **self.form_params()))
        self.assertEqual(response.status_code, 302)
        _, query = _location(response)
        self.assertNotEqual(query['key'], ['not-a-uuid'])
        uuid.UUID(query['key'][0])

    # ── D2: a key match ────────────────────────────────────────────────────────

    def test_the_same_key_twice_goes_to_the_existing_record_with_the_message(self):
        key = self.submit_first()
        before, record = self.made(), self.record_url()
        response = self.submit(key)
        self.assertAlreadySubmitted(response, record)
        self.assertEqual(self.made(), before)

    def test_the_same_key_with_different_values_goes_to_the_first_record(self):
        key = self.submit_first()
        before, record = self.made(), self.record_url()
        response = self.submit(key, **self.different())
        self.assertAlreadySubmitted(response, record)
        self.assertEqual(self.made(), before)

    def test_back_to_the_keyed_url_after_submitting_goes_to_the_record(self):
        """audit A2 b_refetch: the browser re-fetches the form's history entry, which now
        carries the key that already made a record."""
        keyed, key = self.open_form()
        self.assertEqual(self.submit(key).status_code, 302)
        before, record = self.made(), self.record_url()
        response = self.scm_client.get(keyed)
        self.assertAlreadySubmitted(response, record)
        self.assertEqual(self.made(), before)

    def test_a_key_match_on_a_record_they_cannot_open_is_a_403_that_names_nothing(self):
        key = self.submit_first()
        record = self.record_url()
        with patch('projects.submission_guard.user_can_view_vendor_order', return_value=False):
            response = self.submit(key)
        self.assertEqual(response.status_code, 403)
        body = response.content.decode()
        self.assertIn(ALREADY_SUBMITTED, body)
        # No link to the record. The only link is the fresh form the person was already
        # on (for a payment its URL contains the order's path; it names no payment).
        self.assertNotIn(f'href="{record}"', body)
        self.assertEqual(body.count('href="'), 1)

    # ── D3: the content-match warning ──────────────────────────────────────────

    def test_a_fresh_form_with_the_same_values_warns_and_writes_nothing(self):
        self.submit_first()
        self.storage.reset_mock()
        before = self.made()
        _, key = self.open_form()
        response = self.submit(key)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="voDuplicate"')
        self.assertContains(response, 'name="confirm_duplicate"')
        self.assertContains(response, self.record_url())
        # The same key comes back, so the override is still one submission.
        self.assertEqual(_rendered_key(response), key)
        self.assertEqual(self.made(), before)
        self.assertNothingUploaded()

    def test_the_override_makes_exactly_one_record(self):
        self.submit_first()
        before = self.made()
        _, key = self.open_form()
        self.assertEqual(self.submit(key).status_code, 200)
        response = self.submit(key, confirm=True)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.made(), before + 1)
        # And the override's key is now spent like any other.
        self.assertEqual(self.submit(key, confirm=True).status_code, 302)
        self.assertEqual(self.made(), before + 1)

    def test_different_values_do_not_warn(self):
        self.submit_first()
        before = self.made()
        _, key = self.open_form()
        response = self.submit(key, **self.different())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.made(), before + 1)


# ---------------------------------------------------------------------------
# The three paths that create a PO / PI record
# ---------------------------------------------------------------------------

class _OrderPathCases(_PathCases):

    def made(self):
        return VendorOrder.objects.count()

    def different(self):
        return {'vendor_id': str(self.vendor2.pk)}

    def record_url(self):
        return reverse('vendor_order_detail', args=[VendorOrder.objects.latest('pk').pk])

    def test_a_match_older_than_the_window_does_not_warn(self):
        self.submit_first()
        VendorOrder.objects.update(created_at=timezone.now() - timedelta(minutes=31))
        before = self.made()
        _, key = self.open_form()
        self.assertEqual(self.submit(key).status_code, 302)
        self.assertEqual(self.made(), before + 1)

    # ── reference numbers: a disagreement rules a match out, a blank never does ──

    def test_a_different_po_number_does_not_warn(self):
        self.submit_first()
        before = self.made()
        _, key = self.open_form()
        self.assertEqual(self.submit(key, po_number='PO-DIFFERENT').status_code, 302)
        self.assertEqual(self.made(), before + 1)

    def test_the_same_po_number_warns(self):
        self.submit_first()
        before = self.made()
        _, key = self.open_form()
        po = self.values()['po_number']
        response = self.submit(key, po_number=po)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="voDuplicate"')
        self.assertEqual(self.made(), before)

    def test_a_blank_po_number_on_one_record_still_warns(self):
        """The first record was entered without its PO number; the second has one."""
        self.submit_first()
        VendorOrder.objects.update(po_number='')
        before = self.made()
        _, key = self.open_form()
        response = self.submit(key)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="voDuplicate"')
        self.assertEqual(self.made(), before)

    def test_another_persons_record_does_not_warn(self):
        """The order rule is the same person's re-fetch; a colleague's record is not it."""
        self.submit_first()
        VendorOrder.objects.update(created_by=self.pm)
        before = self.made()
        _, key = self.open_form()
        self.assertEqual(self.submit(key).status_code, 302)
        self.assertEqual(self.made(), before + 1)


class ResidentialRaiseDuplicateTests(_OrderPathCases, _Fixture):
    """vendor_order_create."""

    def form_url(self, **params):
        base = reverse('vendor_order_create', args=[self.project.pk])
        return f'{base}?{"&".join(f"{k}={v}" for k, v in params.items())}' if params else base

    def post_url(self):
        return reverse('vendor_order_create', args=[self.project.pk])

    def values(self, **changes):
        # RaiseFixture's own payload; GroupRaiseFixture overrides the name on this class.
        return RaiseFixture.payload(self, **changes)

    def test_a_different_pi_number_does_not_warn(self):
        """The PI number is held to the PO number's rule. This payload is the one that
        carries both, so the PI case is pinned here."""
        self.submit_first()
        before = self.made()
        _, key = self.open_form()
        self.assertEqual(self.submit(key, pi_number='PI-DIFFERENT').status_code, 302)
        self.assertEqual(self.made(), before + 1)


class GroupRaiseDuplicateTests(_OrderPathCases, _Fixture):
    """vendor_order_create_group, opened from a tender (?program= must survive)."""

    def form_params(self):
        return {'program': self.tender.pk}

    def form_url(self, **params):
        base = reverse('vendor_order_create_group')
        return f'{base}?{"&".join(f"{k}={v}" for k, v in params.items())}' if params else base

    def post_url(self):
        return reverse('vendor_order_create_group')

    def values(self, **changes):
        return self.payload(sites=[(self.a1, self.locked_a)], **changes)


class PurchasesNewDuplicateTests(_OrderPathCases, _Fixture):
    """purchases_new, recorded against nothing, with a first payment."""

    def form_params(self):
        return {'program': self.tender.pk}

    def form_url(self, **params):
        base = reverse('purchases_new')
        return f'{base}?{"&".join(f"{k}={v}" for k, v in params.items())}' if params else base

    def post_url(self):
        return reverse('purchases_new')

    def values(self, **changes):
        return self.new_payload(pay='1000', project_type='Residential', **changes)


# ---------------------------------------------------------------------------
# The two paths that raise a payment request against an existing record
# ---------------------------------------------------------------------------

class _PaymentPathCases(_PathCases):

    def setUp(self):
        super().setUp()
        self.order = self.record(total='100000')

    def made(self):
        return PaymentRequest.objects.count()

    def different(self):
        return {'payment_amount': '1500'}

    def record_url(self):
        return reverse('vendor_order_detail', args=[self.order.pk])

    def test_a_rejected_payment_re_raised_with_the_same_amount_does_not_warn(self):
        self.submit_first()
        PaymentRequest.objects.update(status=PaymentRequest.REJECTED, decision_reason='no')
        before = self.made()
        _, key = self.open_form()
        response = self.submit(key)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.made(), before + 1)

    def test_any_requester_counts(self):
        """Payments match by order and amount whoever raised the first."""
        self.submit_first()
        PaymentRequest.objects.update(requested_by=self.finance.user)
        _, key = self.open_form()
        self.assertEqual(self.submit(key).status_code, 200)

    def test_a_refusal_is_still_a_400_without_the_warning(self):
        _, key = self.open_form()
        response = self.submit(key, payment_amount='100000.01')
        self.assertEqual(response.status_code, 400)
        self.assertNotContains(response, 'id="voDuplicate"', status_code=400)


class AddPaymentDuplicateTests(_PaymentPathCases, _Fixture):
    """vendor_order_add_payment."""

    def form_url(self, **params):
        base = reverse('vendor_order_add_payment', args=[self.order.pk])
        return f'{base}?{"&".join(f"{k}={v}" for k, v in params.items())}' if params else base

    def post_url(self):
        return reverse('vendor_order_add_payment', args=[self.order.pk])

    def values(self, **changes):
        data = {'payment_amount': '1000', 'payment_note': 'tranche'}
        data.update(changes)
        return data


class PurchasesPayDuplicateTests(_PaymentPathCases, _Fixture):
    """purchases_pay, opened with the record preselected (?order= must survive)."""

    def form_params(self):
        return {'order': self.order.pk}

    def form_url(self, **params):
        base = reverse('purchases_pay')
        return f'{base}?{"&".join(f"{k}={v}" for k, v in params.items())}' if params else base

    def post_url(self):
        return reverse('purchases_pay')

    def values(self, **changes):
        data = {'order': str(self.order.pk), 'payment_amount': '1000',
                'payment_note': 'tranche'}
        data.update(changes)
        return data


# ---------------------------------------------------------------------------
# The documents append — no key column, so the content match is its only guard
# ---------------------------------------------------------------------------

class DocumentsAppendDuplicateTests(_Fixture):

    def setUp(self):
        super().setUp()
        self.order = self.record(total='100000')
        self.url = reverse('vendor_order_add_documents', args=[self.order.pk])

    def post_docs(self, name='pi.pdf', confirm=False):
        data = {'doc_type_0': VENDOR_ORDER_DOC_PI, 'doc_file_0': _pdf(name)}
        if confirm:
            data['confirm_duplicate'] = '1'
        with patch('projects.supabase_storage.get_supabase_client', return_value=self.storage):
            return self.scm_client.post(self.url, data)

    def test_a_double_post_leaves_one_set_of_rows_and_warns(self):
        self.assertEqual(self.post_docs().status_code, 302)
        self.storage.reset_mock()
        response = self.post_docs()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="voDuplicate"')
        self.assertContains(response, 'pi.pdf (Proforma invoice)')
        self.assertContains(response, 'Attach anyway')
        self.assertEqual(VendorOrderDocument.objects.filter(order=self.order).count(), 1)
        self.assertNothingUploaded()

    def test_attach_anyway_attaches(self):
        self.post_docs()
        response = self.post_docs(confirm=True)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(VendorOrderDocument.objects.filter(order=self.order).count(), 2)

    def test_a_different_file_does_not_warn(self):
        self.post_docs()
        response = self.post_docs(name='pi-revised.pdf')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(VendorOrderDocument.objects.filter(order=self.order).count(), 2)

    def test_a_match_older_than_the_window_does_not_warn(self):
        self.post_docs()
        VendorOrderDocument.objects.update(uploaded_at=timezone.now() - timedelta(minutes=31))
        self.assertEqual(self.post_docs().status_code, 302)
        self.assertEqual(VendorOrderDocument.objects.filter(order=self.order).count(), 2)
