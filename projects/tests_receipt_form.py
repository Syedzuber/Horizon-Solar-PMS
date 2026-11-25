"""
FIN-M-B5-3 — the "Record a client receipt" page (views_receipts.receipt_record), its form
(forms_receipts.ReceiptRecordForm) and the milestone card's "Record receipt" button.

Pins: the CLIENT_RECEIPTS_LIVE switch (off: no button, 404 for everyone); who sees the
button and may GET/POST (site BD on M1, PM side and Finance on M1-M3; 403 for everyone
else who can see the site, 404 for those who cannot); every field error beside its field
with values kept, nothing written and no file stored (IST edge included); FR24 (no agreed
amount); the E4 duplicate and overpayment warnings and their "Record anyway" tick, which
is never trusted alone; the proof file; the redirect and success message; the list; the
fixed query counts (the button adds none to the overview); and E8 — no side effects on
the milestone or its tasks (the save's one alert, "Receipt to confirm", is FIN-M-B5-4b's,
asserted in the E8 test).

Run with:
    python manage.py test projects.tests_receipt_form --settings=solarpms.test_settings
"""
import datetime
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import receipts as receipts_module
from . import views_receipts
from .forms_receipts import MODE_REQUIRED, OTHER_NEEDS_NOTE
from .models import (
    ActivityLog, Notification, NotificationLog, PaymentMilestone, PaymentReceipt, Project,
    ProjectPhase, StatusTransition, SUBJECT_PAYMENT_RECEIPT, Task,
)
from .permissions import user_can_record_receipt
from .receipt_storage import StoredProof
from .receipts import confirm_receipt, query_receipt, record_receipt, reject_receipt

LIVE = override_settings(CLIENT_RECEIPTS_LIVE=True)
SAVED = 'Receipt recorded. Finance will check it against the bank.'

PDF = b'%PDF-1.4 tiny'
JPG = b'\xff\xd8\xff\xe0' + b'\x00' * 32
STORED = StoredProof('delivery-bkt', 'receipt_proofs/1/abc.pdf', 'proof.pdf',
                     'application/pdf', len(PDF))


def _profile(username, role, *, active=True):
    """A post_save signal creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title(), last_name='Test')
    profile = user.profile
    profile.role = role
    profile.is_active = active
    profile.save()
    return profile


def _pdf(name='proof.pdf'):
    return SimpleUploadedFile(name, PDF, content_type='application/pdf')


def _jpg(name='photo.jpg'):
    return SimpleUploadedFile(name, JPG, content_type='image/jpeg')


class ReceiptFormBase(TestCase):
    """One Active Residential site (BD set, PM assigned, an active and an inactive
    coordinator, an SE holding a task) with M1-M3, and a person in every other role."""

    def setUp(self):
        self.fin = _profile('rf_fin', 'Finance')
        self.fin_b = _profile('rf_fin_b', 'Finance')
        self.ceo = _profile('rf_ceo', 'CEO')
        self.admin_p = _profile('rf_admin', 'Admin')
        self.sysadmin = _profile('rf_sysadmin', 'System Admin')
        self.se = _profile('rf_se', 'Site Engineer')
        self.se_elsewhere = _profile('rf_se_x', 'Site Engineer')
        self.pm = _profile('rf_pm', 'PM')
        self.coord = _profile('rf_coord', 'Project Coordinator')
        self.coord_off = _profile('rf_coord_off', 'Project Coordinator', active=False)
        self.bd = _profile('rf_bd', 'BD')
        self.bd_other = _profile('rf_bd_other', 'BD')
        self.project = self.site()
        self.m1, self.m2, self.m3 = self.milestones(self.project)

    def site(self, *, status='Active', project_type='Residential', amounts=(None, None, None),
             is_deleted=False, name='RF Site'):
        project = Project.objects.create(
            customer_name=name, status=status, project_type=project_type,
            customer_phone='9876543210', site_address='1 Sun Road', city='Lucknow',
            state='Uttar Pradesh', dc_capacity_kw=Decimal('5.00'), assigned_pm=self.pm,
            cascade_scheduling=False, bd=self.bd, is_deleted=is_deleted)
        project.coordinators.add(self.coord, self.coord_off)
        phase = ProjectPhase.objects.create(project=project, phase_name='Install',
                                            phase_order=1)
        Task.objects.create(phase=phase, task_name='Mount structure', task_order=1,
                            assigned_role=Task.SITE_ENGINEER, assigned_to=self.se)
        for n, amount in enumerate(amounts, start=1):
            PaymentMilestone.objects.create(
                project=project, milestone_name=f'M{n}',
                amount=Decimal(amount) if amount is not None else None)
        return project

    def milestones(self, project):
        return list(PaymentMilestone.objects.filter(project=project).order_by('milestone_name'))

    def client_for(self, profile):
        client = Client(SERVER_NAME='localhost')
        client.force_login(profile.user)
        return client

    def url(self, milestone, project=None):
        return reverse('receipt_record',
                       args=[(project or milestone.project).project_id, milestone.pk])

    def overview(self, project=None):
        return reverse('project_overview', args=[(project or self.project).project_id])

    def data(self, **overrides):
        values = {'amount': '50000', 'date_received': timezone.localdate().isoformat(),
                  'mode': PaymentReceipt.UPI, 'reference': '', 'note': ''}
        values.update(overrides)
        return {k: v for k, v in values.items() if v is not None}

    def post(self, profile, milestone, **overrides):
        return self.client_for(profile).post(self.url(milestone), self.data(**overrides))

    def counts(self):
        return (PaymentReceipt.objects.count(),
                StatusTransition.objects.filter(subject_type=SUBJECT_PAYMENT_RECEIPT).count(),
                ActivityLog.objects.filter(action_code='receipt_recorded').count())

    def button(self, milestone):
        """The card button's link for `milestone`, as the overview prints it."""
        return f'href="{self.url(milestone)}"'

    def record(self, who, milestone, amount='50000', date=None, **kw):
        """A receipt recorded straight through the writer, for set-up."""
        return record_receipt(who.user, milestone, amount,
                              date or timezone.localdate(), PaymentReceipt.UPI, **kw).receipt


# ===========================================================================
# 1. The switch (E1)
# ===========================================================================

class SwitchTests(ReceiptFormBase):

    @override_settings(CLIENT_RECEIPTS_LIVE=False)
    def test_off_no_button_and_404_for_every_role(self):
        everyone = (self.fin, self.pm, self.coord, self.bd, self.ceo, self.admin_p,
                    self.sysadmin, self.se, self.bd_other)
        for who in everyone:
            with self.subTest(who=who.user.username):
                client = self.client_for(who)
                html = client.get(self.overview()).content.decode()
                for m in (self.m1, self.m2, self.m3):
                    self.assertNotIn(self.button(m), html)
                self.assertNotIn('Record receipt', html)
                self.assertEqual(client.get(self.url(self.m1)).status_code, 404)
                self.assertEqual(client.post(self.url(self.m1), self.data()).status_code, 404)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_default_is_off(self):
        from django.conf import settings
        self.assertFalse(settings.CLIENT_RECEIPTS_LIVE)
        self.assertFalse(receipts_module.receipts_live())

    @LIVE
    def test_on_button_for_the_right_roles(self):
        html = self.client_for(self.fin).get(self.overview()).content.decode()
        for m in (self.m1, self.m2, self.m3):
            self.assertIn(self.button(m), html)


# ===========================================================================
# 2. Who sees the button and may GET/POST (FR12, FR28, D6)
# ===========================================================================

@LIVE
class WhoTests(ReceiptFormBase):

    def visible_buttons(self, who, project=None):
        project = project or self.project
        html = self.client_for(who).get(self.overview(project)).content.decode()
        return {m.milestone_name for m in self.milestones(project) if self.button(m) in html}

    def test_buttons_per_role(self):
        expected = {
            self.bd: {'M1'},
            self.pm: {'M1', 'M2', 'M3'},
            self.coord: {'M1', 'M2', 'M3'},
            self.fin: {'M1', 'M2', 'M3'},
            self.bd_other: set(), self.coord_off: set(), self.ceo: set(),
            self.admin_p: set(), self.sysadmin: set(), self.se: set(),
        }
        for who, names in expected.items():
            with self.subTest(who=who.user.username):
                self.assertEqual(self.visible_buttons(who), names)

    def test_allowed_people_open_the_page(self):
        for who, ms in ((self.bd, [self.m1]), (self.pm, [self.m1, self.m2, self.m3]),
                        (self.coord, [self.m1, self.m2, self.m3]),
                        (self.fin, [self.m1, self.m2, self.m3])):
            for m in ms:
                with self.subTest(who=who.user.username, m=m.milestone_name):
                    self.assertEqual(self.client_for(who).get(self.url(m)).status_code, 200)

    def test_refused_people_get_403_and_nothing_is_written(self):
        refused = [(self.bd_other, self.m1), (self.coord_off, self.m2), (self.ceo, self.m1),
                   (self.admin_p, self.m1), (self.sysadmin, self.m1), (self.se, self.m1),
                   (self.bd, self.m2), (self.bd, self.m3)]
        with mock.patch.object(receipts_module, 'store_receipt_proof') as store:
            for who, m in refused:
                with self.subTest(who=who.user.username, m=m.milestone_name):
                    client = self.client_for(who)
                    self.assertEqual(client.get(self.url(m)).status_code, 403)
                    data = self.data()
                    data['proof'] = _pdf()
                    self.assertEqual(client.post(self.url(m), data).status_code, 403)
        store.assert_not_called()
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_hand_made_post_by_site_bd_on_m2_is_403(self):
        """FR12: the BD records M1 only. No button on M2, and a POST made by hand is
        refused before the form or the store is reached."""
        with mock.patch.object(receipts_module, 'store_receipt_proof') as store:
            data = self.data()
            data['proof'] = _pdf()
            response = self.client_for(self.bd).post(self.url(self.m2), data)
        self.assertEqual(response.status_code, 403)
        store.assert_not_called()
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_someone_who_cannot_see_the_site_gets_404(self):
        # An SE with no task on this site cannot see it: the page does not exist for them.
        client = self.client_for(self.se_elsewhere)
        self.assertEqual(client.get(self.url(self.m1)).status_code, 404)
        self.assertEqual(client.post(self.url(self.m1), self.data()).status_code, 404)

    def test_cancelled_site_no_button_and_refused(self):
        project = self.site(status='Cancelled', name='Cancelled')
        m1 = self.milestones(project)[0]
        for who in (self.fin, self.pm, self.bd):
            with self.subTest(who=who.user.username):
                self.assertEqual(self.visible_buttons(who, project), set())
                client = self.client_for(who)
                self.assertEqual(client.get(self.url(m1)).status_code, 403)
                self.assertEqual(client.post(self.url(m1), self.data()).status_code, 403)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_deleted_site_is_404(self):
        project = self.site(is_deleted=True, name='Deleted')
        m1 = self.milestones(project)[0]
        for who in (self.fin, self.pm):
            client = self.client_for(who)
            self.assertEqual(client.get(self.url(m1)).status_code, 404)
            self.assertEqual(client.post(self.url(m1), self.data()).status_code, 404)

    def test_non_residential_site_is_404(self):
        project = self.site(project_type='OPEX', name='Tender site')
        m1 = self.milestones(project)[0]
        self.assertEqual(self.client_for(self.fin).get(self.url(m1)).status_code, 404)

    def test_milestone_of_another_site_is_404(self):
        other_m1 = self.milestones(self.site(name='Other'))[0]
        url = reverse('receipt_record', args=[self.project.project_id, other_m1.pk])
        self.assertEqual(self.client_for(self.fin).get(url).status_code, 404)

    def test_can_manage_argument(self):
        """The card passes project_overview's management answer; the helper trusts it for
        the PM side only, and still asks the site and role first."""
        self.assertFalse(user_can_record_receipt(self.coord.user, self.m2, can_manage=False))
        self.assertTrue(user_can_record_receipt(self.coord.user, self.m2, can_manage=True))
        self.assertFalse(user_can_record_receipt(self.ceo.user, self.m2, can_manage=True))
        self.assertTrue(user_can_record_receipt(self.fin.user, self.m2, can_manage=False))


# ===========================================================================
# 3. Field errors (FR3, FR29, E7)
# ===========================================================================

@LIVE
class FieldErrorTests(ReceiptFormBase):

    def assert_refused_beside(self, field, message, **overrides):
        with mock.patch.object(receipts_module, 'store_receipt_proof') as store:
            data = self.data(**overrides)
            data['proof'] = _pdf()
            response = self.client_for(self.pm).post(self.url(self.m1), data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['form'].errors.get(field), [message])
        self.assertContains(response, f'>{message}</div>')
        self.assertContains(response, 'Your proof file was not kept')
        store.assert_not_called()
        self.assertEqual(self.counts(), (0, 0, 0))
        return response

    def test_each_bad_input_beside_its_field(self):
        tomorrow = (timezone.localdate() + datetime.timedelta(days=1)).isoformat()
        cases = [
            ('amount', 'Enter the amount received.', dict(amount='')),
            ('amount', 'The amount must be more than zero.', dict(amount='0')),
            ('amount', 'The amount must be more than zero.', dict(amount='-5')),
            ('amount', 'The amount can have at most two decimal places (paise).',
             dict(amount='100.005')),
            ('date_received', 'Enter the date the money was received.',
             dict(date_received='')),
            ('date_received', 'The date received cannot be after today.',
             dict(date_received=tomorrow)),
            ('mode', MODE_REQUIRED, dict(mode='')),
            ('note', OTHER_NEEDS_NOTE, dict(mode=PaymentReceipt.OTHER, note='')),
        ]
        for field, message, overrides in cases:
            with self.subTest(field=field, overrides=overrides):
                self.assert_refused_beside(field, message, **overrides)

    def test_typed_values_are_kept(self):
        response = self.assert_refused_beside(
            'mode', MODE_REQUIRED, mode='', amount='12345.50', reference='UTR-991',
            note='paid at site', date_received='2026-11-01')
        self.assertContains(response, 'value="12345.50"')
        self.assertContains(response, 'value="UTR-991"')
        self.assertContains(response, 'value="2026-11-01"')
        self.assertContains(response, 'paid at site</textarea>')

    def test_other_keeps_its_choice_and_marks_the_note_required(self):
        response = self.assert_refused_beside('note', OTHER_NEEDS_NOTE,
                                              mode=PaymentReceipt.OTHER, note='')
        self.assertRegex(response.content.decode(),
                         r'value="other" autocomplete="off"\s+checked')
        self.assertTrue(response.context['note_required'])

    def test_date_has_no_default(self):
        response = self.client_for(self.pm).get(self.url(self.m1))
        html = response.content.decode()
        self.assertRegex(html, r'name="date_received" id="rr-date"[^>]*value=""')
        self.assertRegex(html, r'name="amount" id="rr-amount"[^>]*value=""')

    def test_ist_edge(self):
        # 19:00 UTC on 24 Nov = 00:30 IST on 25 Nov: the UTC date is still the 24th, but
        # the IST day is the 25th, so the 25th is accepted and the 26th refused.
        after_midnight = datetime.datetime(2026, 11, 24, 19, 0, tzinfo=datetime.timezone.utc)
        with mock.patch('django.utils.timezone.now', return_value=after_midnight):
            refused = self.client_for(self.pm).post(
                self.url(self.m1), self.data(date_received='2026-11-26'))
            self.assertEqual(refused.context['form'].errors['date_received'],
                             ['The date received cannot be after today.'])
            saved = self.client_for(self.pm).post(
                self.url(self.m1), self.data(date_received='2026-11-25'))
        self.assertEqual(saved.status_code, 302)
        self.assertEqual(PaymentReceipt.objects.get().date_received, datetime.date(2026, 11, 25))


# ===========================================================================
# 4. No agreed amount (FR24)
# ===========================================================================

@LIVE
class NoAgreedAmountTests(ReceiptFormBase):

    def test_header_says_so_and_any_amount_saves_without_warning(self):
        response = self.client_for(self.pm).get(self.url(self.m1))
        self.assertContains(response, 'No agreed amount yet. You can still record what was '
                                      'received.')
        response = self.client_for(self.pm).post(self.url(self.m1),
                                                 self.data(amount='9999999'), follow=True)
        self.assertEqual(PaymentReceipt.objects.count(), 1)
        levels = [m.level_tag for m in response.context['messages']]
        self.assertNotIn('warning', levels)

    def test_agreed_amount_and_totals_in_indian_grouping(self):
        PaymentMilestone.objects.filter(pk=self.m1.pk).update(amount=Decimal('150000.00'))
        r = self.record(self.pm, self.m1, amount='25000')
        query_receipt(self.fin.user, r, 'which account?')
        confirm_receipt(self.fin.user, self.record(self.coord, self.m1, amount='100000'))
        response = self.client_for(self.pm).get(self.url(self.m1))
        self.assertContains(response, 'Agreed amount: <strong>₹1,50,000</strong>')
        self.assertContains(response, 'Confirmed so far: <strong>₹1,00,000</strong>')
        self.assertContains(response, 'Not yet confirmed: <strong>₹25,000</strong>')


# ===========================================================================
# 5. Duplicate and overpayment warnings (E4, E5, FR20)
# ===========================================================================

@LIVE
class WarningTests(ReceiptFormBase):

    def first_tap(self, who=None, milestone=None, **overrides):
        response = self.client_for(who or self.pm).post(self.url(milestone or self.m1),
                                                        self.data(**overrides))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['warning_key'])
        return response

    def second_tap(self, key, who=None, milestone=None, **overrides):
        data = self.data(**overrides)
        data.update(record_anyway='on', warning_key=key)
        return self.client_for(who or self.pm).post(self.url(milestone or self.m1), data)

    def test_duplicate_warns_then_tick_saves(self):
        self.record(self.fin, self.m1, amount='50000')
        before = self.counts()
        response = self.first_tap(amount='50000')
        self.assertContains(response, 'This may already be on record.')
        self.assertContains(response, '₹50,000 received')
        self.assertContains(response, 'name="record_anyway"')
        # The tick is never drawn already ticked.
        self.assertContains(response, 'id="rr-anyway" value="on">')
        self.assertEqual(self.counts(), before)
        saved = self.second_tap(response.context['warning_key'], amount='50000')
        self.assertEqual(saved.status_code, 302)
        self.assertEqual(PaymentReceipt.objects.count(), 2)

    def test_same_reference_warns(self):
        self.record(self.fin, self.m1, amount='1000', reference='UTR-55')
        response = self.first_tap(amount='2000', reference='utr-55')
        self.assertContains(response, 'ref UTR-55')

    def test_rejected_receipt_does_not_warn(self):
        r = self.record(self.pm, self.m1, amount='50000')
        reject_receipt(self.fin.user, r, 'wrong site')
        response = self.client_for(self.pm).post(self.url(self.m1), self.data(amount='50000'))
        self.assertEqual(response.status_code, 302)

    def test_overpayment_warns_then_tick_saves(self):
        PaymentMilestone.objects.filter(pk=self.m1.pk).update(amount=Decimal('100000.00'))
        self.record(self.fin, self.m1, amount='60000', date=datetime.date(2026, 11, 1))
        response = self.first_tap(amount='50000')
        self.assertContains(response, 'With this receipt, receipts on M1 total ₹1,10,000')
        self.assertEqual(PaymentReceipt.objects.count(), 1)
        data = self.data(amount='50000')
        data.update(record_anyway='on', warning_key=response.context['warning_key'])
        saved = self.client_for(self.pm).post(self.url(self.m1), data, follow=True)
        self.assertEqual(PaymentReceipt.objects.count(), 2)
        # The ticked warning is not repeated under the success message.
        self.assertEqual([m.level_tag for m in saved.context['messages']], ['success'])

    def test_exactly_equal_does_not_warn(self):
        PaymentMilestone.objects.filter(pk=self.m1.pk).update(amount=Decimal('100000.00'))
        self.record(self.fin, self.m1, amount='60000', date=datetime.date(2026, 11, 1))
        response = self.client_for(self.pm).post(self.url(self.m1), self.data(amount='40000'))
        self.assertEqual(response.status_code, 302)

    def test_tick_without_a_warning_simply_saves(self):
        response = self.second_tap('stale-or-made-up', amount='777')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PaymentReceipt.objects.count(), 1)

    def test_tick_is_not_trusted_when_the_warnings_changed(self):
        self.record(self.fin, self.m1, amount='50000')
        key = self.first_tap(amount='50000').context['warning_key']
        # Another identical receipt is recorded before the second tap.
        self.record(self.coord, self.m1, amount='50000')
        response = self.second_tap(key, amount='50000')
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.context['warning_key'], key)
        self.assertEqual(len(response.context['duplicates']), 2)
        self.assertEqual(PaymentReceipt.objects.count(), 2)

    def test_double_tap_of_record_anyway_saves_once(self):
        self.record(self.fin, self.m1, amount='50000')
        key = self.first_tap(amount='50000').context['warning_key']
        self.assertEqual(self.second_tap(key, amount='50000').status_code, 302)
        # The same POST again: the first save is now a new duplicate, so it re-renders.
        self.assertEqual(self.second_tap(key, amount='50000').status_code, 200)
        self.assertEqual(PaymentReceipt.objects.count(), 2)

    def test_warning_appearing_only_under_the_lock_shows_under_success(self):
        PaymentMilestone.objects.filter(pk=self.m1.pk).update(amount=Decimal('100.00'))
        with mock.patch.object(views_receipts, 'overpayment_warnings', return_value=()):
            response = self.client_for(self.pm).post(self.url(self.m1),
                                                     self.data(amount='500'), follow=True)
        tags = [(m.level_tag, str(m)) for m in response.context['messages']]
        self.assertEqual(tags[0], ('success', SAVED))
        self.assertEqual(tags[1][0], 'warning')
        self.assertIn('more than the agreed amount of ₹100, by ₹400', tags[1][1])

    def test_reattach_notice_when_a_file_was_chosen(self):
        self.record(self.fin, self.m1, amount='50000')
        data = self.data(amount='50000')
        data['proof'] = _pdf()
        with mock.patch.object(receipts_module, 'store_receipt_proof') as store:
            response = self.client_for(self.pm).post(self.url(self.m1), data)
        self.assertContains(response, 'Your proof file was not kept')
        store.assert_not_called()


# ===========================================================================
# 6. The proof (FR17, FR31)
# ===========================================================================

@LIVE
class ProofTests(ReceiptFormBase):

    def post_file(self, upload):
        data = self.data()
        data['proof'] = upload
        return self.client_for(self.pm).post(self.url(self.m1), data)

    def test_pdf_and_jpg_reach_the_writer(self):
        for upload in (_pdf(), _jpg()):
            with self.subTest(name=upload.name), \
                    mock.patch.object(receipts_module, 'store_receipt_proof',
                                      return_value=STORED) as store:
                response = self.post_file(upload)
                self.assertEqual(response.status_code, 302)
                store.assert_called_once()
                self.assertEqual(store.call_args.args[1].name, upload.name)
            PaymentReceipt.objects.all().delete()

    def test_wrong_type_and_too_large_refused_beside_the_field(self):
        big = SimpleUploadedFile('big.pdf', b'%PDF-' + b'0' * (20 * 1024 * 1024),
                                 content_type='application/pdf')
        cases = [
            (SimpleUploadedFile('notes.txt', b'hello', content_type='text/plain'),
             'notes.txt: must be a PDF, JPG or PNG.'),
            (SimpleUploadedFile('photo.heic', b'....ftypheic', content_type='image/heic'),
             'photo.heic: must be a PDF, JPG or PNG.'),
            (SimpleUploadedFile('fake.pdf', b'MZ not a pdf', content_type='application/pdf'),
             'fake.pdf: the contents are not a .pdf file, although the name says so.'),
            (big, 'big.pdf: 20.0 MB — the limit is 20 MB.'),
        ]
        for upload, message in cases:
            with self.subTest(name=upload.name), \
                    mock.patch.object(receipts_module, 'store_receipt_proof') as store:
                response = self.post_file(upload)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['form'].errors['proof'], [message])
                self.assertContains(response, f'>{message}</div>')
                store.assert_not_called()
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_writer_refusal_after_upload_discards_and_shows_at_top(self):
        with mock.patch.object(receipts_module, 'store_receipt_proof', return_value=STORED), \
                mock.patch.object(receipts_module, 'discard_receipt_proof') as discard, \
                mock.patch.object(receipts_module, 'user_can_record_receipt',
                                  side_effect=[True, False]):
            response = self.post_file(_pdf())
        self.assertEqual(response.status_code, 200)
        discard.assert_called_once_with(STORED.bucket, STORED.path)
        self.assertTrue(response.context['form'].non_field_errors())
        self.assertContains(response, 'alert alert-danger')
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_upload_failure_shows_at_top(self):
        from .delivery_storage import DeliveryStorageError
        with mock.patch.object(receipts_module, 'store_receipt_proof',
                               side_effect=DeliveryStorageError('Upload to storage failed: '
                                                                'X. Nothing was saved.')):
            response = self.post_file(_pdf())
        self.assertEqual(list(response.context['form'].non_field_errors()),
                         ['Upload to storage failed: X. Nothing was saved.'])
        self.assertEqual(self.counts(), (0, 0, 0))


# ===========================================================================
# 7. After the save (E6) and the list (E3, rulings 5 and 7)
# ===========================================================================

@LIVE
class AfterSaveAndListTests(ReceiptFormBase):

    def test_redirect_message_and_waiting_row(self):
        client = self.client_for(self.bd)  # one client: the message rides on its session
        response = client.post(self.url(self.m1), self.data(amount='12000'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, self.overview() + '#payment-milestones')
        page = client.get(response.url)
        self.assertContains(page, SAVED)
        listing = client.get(self.url(self.m1))
        self.assertContains(listing, '₹12,000')
        self.assertContains(listing, 'Waiting for Finance')
        self.assertContains(listing, 'Recorded by Rf_Bd Test')

    def test_list_is_this_milestone_only_newest_first_no_proof_link(self):
        older = self.record(self.pm, self.m1, amount='111', date=datetime.date(2026, 11, 1))
        newer = self.record(self.pm, self.m1, amount='2222', date=datetime.date(2026, 11, 2))
        PaymentReceipt.objects.filter(pk=older.pk).update(
            recorded_at=timezone.now() - datetime.timedelta(days=1))
        self.record(self.pm, self.m2, amount='333')
        html = self.client_for(self.pm).get(self.url(self.m1)).content.decode()
        self.assertIn('₹2,222', html)
        self.assertIn('₹111', html)
        self.assertNotIn('₹333', html)
        self.assertLess(html.find(f'rr-row-{newer.pk}'), html.find(f'rr-row-{older.pk}'))
        self.assertNotIn('receipt_proofs', html)
        self.assertNotIn('proof_open', html)

    def test_status_words(self):
        queried = self.record(self.pm, self.m1, amount='101')
        query_receipt(self.fin.user, queried, 'which bank?')
        confirmed = self.record(self.pm, self.m1, amount='102')
        confirm_receipt(self.fin.user, confirmed)
        rejected = self.record(self.pm, self.m1, amount='103')
        reject_receipt(self.fin.user, rejected, 'duplicate')
        self.record(self.pm, self.m1, amount='104')
        html = self.client_for(self.pm).get(self.url(self.m1)).content.decode()
        for words in ('Queried by Finance', 'Confirmed', 'Rejected', 'Waiting for Finance'):
            self.assertIn(words, html)

    def test_amount_needed_row_is_shown_as_old_receipt(self):
        """Ruling 5: an amount_needed row (made later by FIN-M-B5-7) shows that money is
        already on record. Made directly here, as B5-7's command will."""
        PaymentReceipt.objects.create(milestone=self.m1, status=PaymentReceipt.AMOUNT_NEEDED,
                                      source=PaymentReceipt.MIGRATED, recorded_by=None)
        response = self.client_for(self.pm).get(self.url(self.m1))
        self.assertContains(response, 'Old receipt - Finance to add the amount')
        self.assertContains(response, 'Amount not entered yet')
        self.assertContains(response, '1 old receipt on record without an amount yet')
        self.assertContains(response, 'migrated — entered by Finance before 17 Nov')


# ===========================================================================
# 8. Query counts
# ===========================================================================

class QueryCountTests(ReceiptFormBase):

    def overview_queries(self, who, project):
        client = self.client_for(who)
        client.get(self.overview(project))  # warm the session and first-hit caches
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(self.overview(project))
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries)

    def test_the_button_adds_no_query_to_the_overview(self):
        # FIN-M-B5-6 (ruling Q10, 25 Nov): the switch-on card now reads its milestones'
        # receipts — ONE query (client_money.milestone_money) for the whole card, whatever
        # the number of milestones. Pinned EXACTLY at one, not "at most": the button itself
        # still adds none, and any other growth would be a regression.
        one = self.site(amounts=(None,), name='One milestone')
        for project in (one, self.project):
            for who in (self.bd, self.pm, self.coord, self.fin, self.ceo):
                with self.subTest(milestones=project.milestones.count(),
                                  who=who.user.username):
                    with override_settings(CLIENT_RECEIPTS_LIVE=False):
                        off = self.overview_queries(who, project)
                    with override_settings(CLIENT_RECEIPTS_LIVE=True):
                        on = self.overview_queries(who, project)
                    self.assertEqual(on, off + 1)

    @LIVE
    def test_record_page_is_a_fixed_count(self):
        def page_queries():
            client = self.client_for(self.coord)
            client.get(self.url(self.m1))
            with CaptureQueriesContext(connection) as ctx:
                response = client.get(self.url(self.m1))
            self.assertEqual(response.status_code, 200)
            return len(ctx.captured_queries)

        counts = [page_queries()]
        self.record(self.pm, self.m1, amount='1')
        counts.append(page_queries())
        for n in range(9):
            self.record(self.pm, self.m1, amount=str(10 + n))
        counts.append(page_queries())
        self.assertEqual(PaymentReceipt.objects.filter(milestone=self.m1).count(), 10)
        self.assertEqual(len(set(counts)), 1, counts)


# ===========================================================================
# 9. No side effects (E8)
# ===========================================================================

@LIVE
class NoSideEffectTests(ReceiptFormBase):

    def test_save_touches_no_milestone_or_task_and_sends_only_receipt_to_confirm(self):
        """E8: the save leaves the milestone and its tasks alone. Its one alert (FR36,
        FIN-M-B5-4b) is "Receipt to confirm" to the other active Finance user, sent after
        commit — so the callbacks are executed and the alert asserted, not assumed away."""
        def milestone_row():
            return PaymentMilestone.objects.filter(pk=self.m1.pk).values(
                'status', 'amount', 'amount_received', 'received_date', 'invoice_amount')[0]

        def tasks():
            return list(Task.objects.filter(phase__project=self.project)
                        .values_list('pk', 'status', 'assigned_to_id', 'due_date'))

        before = (milestone_row(), tasks())
        client = self.client_for(self.fin)
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(self.url(self.m1), self.data())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PaymentReceipt.objects.count(), 1)
        self.assertEqual((milestone_row(), tasks()), before)
        # rf_fin recorded, so only rf_fin_b (the other active Finance user) is told.
        self.assertEqual(list(Notification.objects.values_list('recipient', flat=True)),
                         [self.fin_b.pk])
        self.assertEqual(set(NotificationLog.objects.values_list('event_code', flat=True)),
                         {'receipt_to_confirm'})
