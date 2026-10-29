"""
DC-K: the delivery challan create form's duplicate-submission key (submission_guard's
?key=, stored on DeliveryChallan.client_uuid).

WHAT IS PINNED HERE:
  * GET: no key (or a malformed one) is a 302 to the same page with a fresh ?key=; a
    key renders the form with that key in the hidden client_uuid field; a key that
    already made a challan is answered with that challan and the "already submitted"
    message;
  * POST: the same key twice makes one challan, one set of lines and one upload — the
    second lands on the first challan with the message and reaches no storage; a
    refused POST re-renders with the SAME key and the corrected POST creates the
    challan with no repeat message; the key answers whichever project it was used on;
  * the race: a twin that commits the key while this request uploads makes this
    request's create() fail on the unique index — its uploads are discarded, nothing of
    it is written, and it lands on the twin's challan; an IntegrityError that is not
    the key is still raised, uploads discarded;
  * whoever cannot see the first challan's site gets the message and a 403 that names
    nothing about the challan;
  * a keyless POST still creates, unkeyed (the known gap in SECONDARY_FINDINGS), and
    any number of unkeyed challans coexist;
  * the page carries the hidden field and the submit-disable script.

Storage is never reached: upload_dc_document and discard_unrecorded_dc_document are
patched where the view imported them.

Run with:
    python manage.py test projects.tests_dc_create_guard --settings=solarpms.test_settings
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .models import DCLineItem, DeliveryChallan, DeliveryChallanAttachment, Project
from .submission_guard import ALREADY_SUBMITTED

BUCKET = 'Horizon-PMS-Delivery-test'
PDF = b'%PDF-1.4 challan\n'


def _profile(username, role):
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title(), last_name='Test')
    profile = user.profile          # auto-created by the post_save signal
    profile.role = role
    profile.save(update_fields=['role'])
    return profile


def _project(project_id):
    return Project.objects.create(
        project_id=project_id, customer_name=project_id, customer_phone='9876543210',
        site_address='1 Sun Road', city='Delhi', project_type='Residential',
        dc_capacity_kw=Decimal('5.00'), status='Active',
        target_commissioning_date=date.today() + timedelta(days=90))


def _stored(n):
    """What upload_dc_document returns: the attachment row's fields."""
    return {'original_name': f'dc-{n}.pdf', 'bucket': BUCKET,
            'path': f'DCK-SITE/challan/{uuid.uuid4().hex}.pdf', 'size': 2048,
            'mime': 'application/pdf'}


@override_settings(SUPABASE_DELIVERY_BUCKET=BUCKET)
class DcCreateGuardTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.scm = _profile('dck_scm', 'SCM')
        cls.site = _project('DCK-SITE')
        cls.other = _project('DCK-OTHER')

    def setUp(self):
        self.upload = mock.MagicMock(side_effect=lambda f, p: _stored(self.upload.call_count))
        self.discard = mock.MagicMock()
        for target, value in (('projects.views.upload_dc_document', self.upload),
                              ('projects.views.discard_unrecorded_dc_document', self.discard)):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = Client(SERVER_NAME='localhost')
        self.client.force_login(self.scm.user)
        self.url = reverse('create_delivery_challan', args=[self.site.project_id])
        self.key = uuid.uuid4()

    def post(self, key=None, files=True, url=None, **overrides):
        data = {
            'dc_number': 'DCK-1', 'dc_date': date.today().isoformat(),
            'line_item_category_0': DCLineItem.CATEGORY_CHOICES[0][0],
            'line_item_description_0': 'Mono PERC 540 Wp', 'line_item_qty_0': '12',
            'line_item_unit_0': 'Nos', 'no_prices_confirmed': '1',
        }
        if key is not None:
            data['client_uuid'] = str(key)
        if files:
            data['challan_documents'] = [
                SimpleUploadedFile('dc.pdf', PDF, content_type='application/pdf')]
        data.update(overrides)
        return self.client.post(url or self.url, data)

    def texts(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def detail(self, challan):
        return reverse('delivery_challan_detail',
                       args=[challan.project.project_id, challan.pk])

    # ── GET ──────────────────────────────────────────────────────────────────

    def test_get_without_a_key_redirects_to_a_keyed_url(self):
        for url in (self.url, f'{self.url}?key=not-a-uuid'):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                target = urlparse(response['Location'])
                self.assertEqual(target.path, self.url)
                uuid.UUID(parse_qs(target.query)['key'][0])    # a valid key, or raises

    def test_get_with_a_key_renders_it_in_the_hidden_field(self):
        response = self.client.get(f'{self.url}?key={self.key}')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'name="client_uuid" value="{self.key}"')

    def test_get_with_a_used_key_answers_with_the_first_challan(self):
        challan = DeliveryChallan.objects.create(
            project=self.site, dc_number='USED-1', dc_date=date.today(),
            client_uuid=self.key)
        response = self.client.get(f'{self.url}?key={self.key}')
        self.assertRedirects(response, self.detail(challan), fetch_redirect_response=False)
        self.assertTrue(any(ALREADY_SUBMITTED in t for t in self.texts(response)))

    # ── POST ─────────────────────────────────────────────────────────────────

    def test_the_same_key_twice_makes_one_challan_and_one_upload(self):
        first = self.post(self.key)
        challan = DeliveryChallan.objects.get()
        self.assertRedirects(first, self.detail(challan), fetch_redirect_response=False)
        self.assertEqual(challan.client_uuid, self.key)
        # Land on the challan as a browser would, so its success message is read and
        # cannot be mistaken for the second response's.
        self.client.get(self.detail(challan))

        second = self.post(self.key)
        self.assertRedirects(second, self.detail(challan), fetch_redirect_response=False)
        texts = self.texts(second)
        self.assertTrue(any(ALREADY_SUBMITTED in t for t in texts))
        self.assertFalse(any('created successfully' in t for t in texts))
        self.assertEqual(DeliveryChallan.objects.count(), 1)
        self.assertEqual(DCLineItem.objects.count(), 1)
        self.assertEqual(DeliveryChallanAttachment.objects.count(), 1)
        self.assertEqual(self.upload.call_count, 1)
        self.discard.assert_not_called()

    def test_a_refused_post_keeps_its_key_and_the_corrected_post_creates(self):
        refused = self.post(self.key, dc_number='')
        self.assertEqual(refused.status_code, 200)
        self.assertContains(refused, f'name="client_uuid" value="{self.key}"')
        self.assertFalse(DeliveryChallan.objects.exists())
        self.upload.assert_not_called()

        corrected = self.post(self.key)
        challan = DeliveryChallan.objects.get()
        self.assertRedirects(corrected, self.detail(challan), fetch_redirect_response=False)
        self.assertEqual(challan.client_uuid, self.key)
        texts = self.texts(corrected)
        self.assertFalse(any(ALREADY_SUBMITTED in t for t in texts))
        self.assertTrue(any('created successfully' in t for t in texts))

    def test_a_key_used_on_another_project_answers_with_that_challan(self):
        challan = DeliveryChallan.objects.create(
            project=self.other, dc_number='OTH-1', dc_date=date.today(),
            client_uuid=self.key)
        response = self.post(self.key)
        self.assertRedirects(response, self.detail(challan), fetch_redirect_response=False)
        self.assertEqual(DeliveryChallan.objects.count(), 1)
        self.upload.assert_not_called()

    def test_someone_who_cannot_see_the_first_challan_gets_a_403_naming_nothing(self):
        challan = DeliveryChallan.objects.create(
            project=self.other, dc_number='SECRET-DC', dc_date=date.today(),
            client_uuid=self.key)
        with mock.patch('projects.views.user_can_view_project', return_value=False):
            response = self.post(self.key)
        self.assertEqual(response.status_code, 403)
        body = response.content.decode()
        self.assertIn(ALREADY_SUBMITTED, body)
        self.assertNotIn('SECRET-DC', body)
        self.assertNotIn(self.other.project_id, body)
        self.assertNotIn(f'/{challan.pk}/', body)
        self.assertEqual(DeliveryChallan.objects.count(), 1)
        self.upload.assert_not_called()

    def test_keyless_posts_still_create_unkeyed(self):
        for n, key in enumerate((None, 'not-a-uuid')):
            with self.subTest(key=key):
                self.post(key, dc_number=f'NOKEY-{n}')
        self.assertEqual(
            list(DeliveryChallan.objects.order_by('pk').values_list('client_uuid', flat=True)),
            [None, None])

    # ── The race ─────────────────────────────────────────────────────────────

    def test_a_twin_that_commits_first_discards_this_requests_uploads(self):
        # The twin passes the same key check and commits while this request is still
        # uploading — simulated by writing its challan from inside the upload.
        twin = {}

        def upload_while_twin_commits(file_obj, project):
            twin['challan'] = DeliveryChallan.objects.create(
                project=self.site, dc_number='TWIN', dc_date=date.today(),
                client_uuid=self.key)
            twin['stored'] = _stored(1)
            return twin['stored']

        self.upload.side_effect = upload_while_twin_commits
        response = self.post(self.key)
        self.assertRedirects(response, self.detail(twin['challan']),
                             fetch_redirect_response=False)
        self.assertTrue(any(ALREADY_SUBMITTED in t for t in self.texts(response)))
        self.discard.assert_called_once_with(twin['stored'])
        # Nothing of the loser survived its rolled-back block.
        self.assertEqual(list(DeliveryChallan.objects.values_list('dc_number', flat=True)),
                         ['TWIN'])
        self.assertFalse(DCLineItem.objects.exists())
        self.assertFalse(DeliveryChallanAttachment.objects.exists())

    def test_an_integrity_error_that_is_not_the_key_is_still_raised(self):
        with mock.patch('projects.views.DCLineItem.objects.create',
                        side_effect=IntegrityError('some other constraint')):
            with self.assertRaises(IntegrityError):
                self.post(self.key)
        self.assertEqual(self.discard.call_count, 1)
        self.assertFalse(DeliveryChallan.objects.exists())

    # ── The page ─────────────────────────────────────────────────────────────

    def test_the_page_carries_the_submit_disable_script(self):
        response = self.client.get(f'{self.url}?key={self.key}')
        self.assertContains(response, 'id="dcSubmit"')
        self.assertContains(response, 'Saving…')
        self.assertContains(response, "addEventListener('pageshow'")
        # The template comment beside the hidden field must not render as text.
        self.assertNotContains(response, 'DC-K: the page')
