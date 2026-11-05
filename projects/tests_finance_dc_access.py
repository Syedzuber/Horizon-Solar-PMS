"""NTF-DCG 5a — Finance reads delivery challans, their receipts and documents, read-only.

Ruling N5: the GRN email (Session 5c) links Finance to the challan page so they can
reconcile vendor payments against the receipt documents. Until 5a they got a 403.

WHAT IS PINNED
--------------
  * F1: user_can_view_delivery_challan() = a role in DC_DETAIL_ROLES (Finance now in it)
    + user_can_view_project(); a READ_ONLY_DC_ROLES role (Finance) only while both the
    login and the profile are active. user_can_open_dc_document() is that audience.
    CEO, System Admin, Design and BD are still outside it.
  * F1 (Q1): no write gate reads DC_DETAIL_ROLES or the read helpers — by source and by
    a Finance POST to each write view being refused with nothing written.
  * F2: Finance opens the page (200), sees the receipts, the challan and receipt
    documents and the live damage photos, and is shown no write control — no GRN form,
    no override, no Edit DC, no upload, no withdraw, no Raise Issue modal (Q4).
  * F3: Finance opens a challan document, a receipt document and a live damage photo,
    each open logged; withdrawn files are not listed for Finance and open as a 404.
  * Q5: a deactivated Finance profile gets 403 on the page and 404 on a document; a
    deactivated login cannot reach the page at all (sent to sign in).
  * F5 (Q2): the GRN forms, the DC-creation and DC-edit pages name Finance as a reader.

Storage is stubbed exactly as tests_grn_damage stubs it (the Supabase client is a mock;
any HTTP request fails the test).

Run with:
    python manage.py test projects.tests_finance_dc_access --settings=solarpms.test_settings
"""
import ast
import inspect
import textwrap

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from . import dc_edit, permissions, views
from .models import (
    RECEIPT_DOCUMENT_LR, RECEIPT_DOCUMENT_SIGNED_DC, ActivityLog, DCLineDamagePhoto,
    DeliveryChallan, DeliveryChallanAttachment, GoodsReceipt, Issue, UserProfile,
)
from .permissions import (
    DC_DETAIL_ROLES, READ_ONLY_DC_ROLES, user_can_open_dc_document,
    user_can_view_delivery_challan, user_has_dc_read_role,
)
from .tests_grn_damage import PDF, SIGNED, _profile
from .tests_receipt_attachments import ReceiptDocumentBase, pdf

LR_NAME = 'lr-bilty.pdf'


class FinanceDcBase(ReceiptDocumentBase):
    """GrnDamageBase's site and challan (Finance profile included), plus: one receipt
    with a live signed DC copy and a withdrawn LR, a live and a withdrawn challan
    document, and a live and a withdrawn damage photo."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ceo = _profile('fdc_ceo', 'CEO')
        cls.sysadmin = _profile('fdc_sysadmin', 'System Admin')
        cls.design = _profile('fdc_design', 'Design')
        cls.bd = _profile('fdc_bd', 'BD')
        cls.issue = Issue.objects.create(project=cls.site, delivery_challan=cls.dc,
                                         title='Damaged delivery — DC GD-1', status=Issue.OPEN)
        cls.live_photo = cls.photo(cls.panels, issue=cls.issue, name='live.jpg')
        cls.gone_photo = cls.photo(cls.panels, issue=cls.issue, name='gone.jpg',
                                   withdrawn_by=cls.scm)

    def setUp(self):
        super().setUp()
        # The receipt and its documents go through the real confirm_grn, so the rows are
        # exactly what a Site Engineer's submission writes (storage is the mock).
        self.confirm_docs({self.panels: ('10', '0', [])},
                          [(RECEIPT_DOCUMENT_SIGNED_DC, pdf()),
                           (RECEIPT_DOCUMENT_LR, pdf(LR_NAME))])
        self.receipt = GoodsReceipt.objects.get()
        self.signed_copy, self.lr = self.documents()
        DeliveryChallanAttachment.objects.filter(pk=self.lr.pk).update(
            withdrawn_at=timezone.now(), withdrawn_by=self.scm,
            withdrawn_reason='Shows the freight amount')
        self.challan_live = self.challan_document('vendor-dc.pdf')
        self.challan_gone = self.challan_document('priced-dc.pdf', withdrawn=True)
        self.bucket_api.reset_mock()

    def url(self, name, *extra):
        return reverse(name, args=[self.site.project_id, self.dc.pk, *extra])

    def open_document(self, profile, document):
        self.login(profile)
        return self.client.get(self.url('delivery_challan_attachment_open', document.pk))

    def open_photo(self, profile, photo):
        self.login(profile)
        return self.client.get(self.url('dc_damage_photo_open', photo.pk))

    def page_for(self, profile):
        self.login(profile)
        return self.client.get(self.url('delivery_challan_detail'))


# ---------------------------------------------------------------------------
# F1 — the read audience
# ---------------------------------------------------------------------------

class ReadAudienceTests(FinanceDcBase):

    def test_finance_is_a_read_only_dc_role(self):
        self.assertIn('Finance', DC_DETAIL_ROLES)
        self.assertEqual(READ_ONLY_DC_ROLES, ('Finance',))

    def test_who_reads_the_challan(self):
        # Finance holds no task and no assignment here: portfolio-wide, as on every site.
        readers = (self.scm, self.admin, self.pm, self.coord, self.se, self.finance)
        outsiders = (self.ceo, self.sysadmin, self.design, self.bd, self.se_elsewhere)
        for profile in readers + outsiders:
            with self.subTest(role=profile.role, user=profile.user.username):
                expected = profile in readers
                self.assertEqual(user_can_view_delivery_challan(profile.user, self.site),
                                 expected)
                self.assertEqual(user_can_open_dc_document(profile.user, self.site), expected)

    def test_open_document_is_exactly_the_read_audience_for_every_role(self):
        for role, _label in UserProfile.ROLE_CHOICES:
            with self.subTest(role=role):
                profile = _profile(f'fdc_role_{role.replace(" ", "_").lower()}', role)
                self.assertEqual(user_can_open_dc_document(profile.user, self.site),
                                 user_can_view_delivery_challan(profile.user, self.site))

    def test_finance_reads_every_project(self):
        self.assertTrue(user_can_view_delivery_challan(self.finance.user, self.other))

    def test_a_deactivated_finance_profile_or_login_reads_nothing(self):
        for flag in ('profile', 'user'):
            with self.subTest(deactivated=flag):
                profile = _profile(f'fdc_fin_off_{flag}', 'Finance')
                target = profile if flag == 'profile' else profile.user
                target.is_active = False
                target.save(update_fields=['is_active'])
                self.assertFalse(user_has_dc_read_role(profile.user))
                self.assertFalse(user_can_view_delivery_challan(profile.user, self.site))
                self.assertFalse(user_can_open_dc_document(profile.user, self.site))

    def test_the_active_check_is_finance_only(self):
        # Every pre-5a reader is admitted as before, active flags unasked — 5a widens
        # for Finance and narrows nobody.
        UserProfile.objects.filter(pk=self.scm.pk).update(is_active=False)
        self.scm.refresh_from_db()
        self.assertTrue(user_has_dc_read_role(self.scm.user))


# ---------------------------------------------------------------------------
# F2 — Finance opens the page and reads it
# ---------------------------------------------------------------------------

class FinancePageTests(FinanceDcBase):

    def test_finance_opens_the_page_and_sees_receipts_and_documents(self):
        response = self.page_for(self.finance)
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        self.assertIn('id="dc-receipts"', page)
        self.assertIn(self.receipt.number, page)
        # The receipt's live document and the challan's live document, with open links.
        for document in (self.signed_copy, self.challan_live):
            self.assertIn(document.original_name, page)
            self.assertIn(self.url('delivery_challan_attachment_open', document.pk), page)
        self.assertIn('live.jpg', page)
        self.assertIn(self.url('dc_damage_photo_open', self.live_photo.pk), page)

    def test_withdrawn_files_are_not_listed_for_finance(self):
        page = self.page_for(self.finance).content.decode()
        for document in (self.lr, self.challan_gone):
            self.assertNotIn(document.original_name, page)
            self.assertNotIn(self.url('delivery_challan_attachment_open', document.pk), page)
        self.assertNotIn('gone.jpg', page)
        self.assertNotIn(self.url('dc_damage_photo_open', self.gone_photo.pk), page)
        # SCM still sees all three: the hiding is Finance's, not the fixture's.
        scm_page = self.page_for(self.scm).content.decode()
        for name in (LR_NAME, 'priced-dc.pdf', 'gone.jpg'):
            self.assertIn(name, scm_page)

    def test_finance_is_shown_no_write_control(self):
        page = self.page_for(self.finance).content.decode()
        write_urls = [
            self.url('confirm_grn'), self.url('override_grn'),
            self.url('delivery_challan_edit'), self.url('create_delivery_issue'),
            self.url('delivery_challan_attachment_withdraw', self.signed_copy.pk),
            self.url('delivery_challan_attachment_withdraw', self.challan_live.pk),
            self.url('dc_damage_photo_withdraw', self.live_photo.pk),
        ]
        for url in write_urls:
            with self.subTest(url=url):
                self.assertNotIn(url, page)
        # Markup, not words: the page's CSS comments name "the SCM Edit GRN modal".
        for marker in ('>Edit DC<', '#editGrnModal', 'id="editGrnModal"',
                       '#raiseDcIssueModal', 'id="raiseDcIssueModal"', '>Withdraw<',
                       'name="withdrawn_reason"', 'type="file"', 'name="received_qty_',
                       'name="receipt_doc_1"', 'name="damage_photos_'):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, page)

    def test_the_raise_issue_modal_is_still_there_for_its_own_roles(self):
        # Q4 moved the modal under its button's condition; nobody who had it lost it.
        for profile in (self.se, self.scm, self.pm, self.coord, self.admin):
            with self.subTest(role=profile.role):
                page = self.page_for(profile).content.decode()
                self.assertIn('id="raiseDcIssueModal"', page)
                self.assertIn(self.url('create_delivery_issue'), page)

    def test_a_deactivated_finance_profile_gets_403(self):
        UserProfile.objects.filter(pk=self.finance.pk).update(is_active=False)
        self.assertEqual(self.page_for(self.finance).status_code, 403)
        self.assertEqual(self.open_document(self.finance, self.signed_copy).status_code, 404)
        self.assertFalse(ActivityLog.objects.filter(action_code='dc_document_opened').exists())

    def test_a_deactivated_finance_login_is_sent_to_sign_in(self):
        user = self.finance.user
        user.is_active = False
        user.save(update_fields=['is_active'])
        response = self.page_for(self.finance)
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])

    def test_ceo_and_design_are_still_refused_the_page(self):
        for profile in (self.ceo, self.sysadmin, self.design, self.bd):
            with self.subTest(role=profile.role):
                self.assertEqual(self.page_for(profile).status_code, 403)


# ---------------------------------------------------------------------------
# F3 — opening files, each open logged
# ---------------------------------------------------------------------------

class FinanceOpensFilesTests(FinanceDcBase):

    def assert_open_logged(self, code, entity_type, pk):
        row = ActivityLog.objects.get(action_code=code)
        self.assertEqual((row.actor, row.entity_type, row.entity_id),
                         (self.finance, entity_type, pk))

    def test_finance_opens_a_receipt_document_and_the_open_is_logged(self):
        response = self.open_document(self.finance, self.signed_copy)
        self.assertRedirects(response, SIGNED, fetch_redirect_response=False)
        self.assert_open_logged('dc_document_opened', 'DeliveryChallanAttachment',
                                self.signed_copy.pk)

    def test_finance_opens_a_challan_document_and_the_open_is_logged(self):
        response = self.open_document(self.finance, self.challan_live)
        self.assertRedirects(response, SIGNED, fetch_redirect_response=False)
        self.assert_open_logged('dc_document_opened', 'DeliveryChallanAttachment',
                                self.challan_live.pk)

    def test_finance_opens_a_live_damage_photo_and_the_open_is_logged(self):
        # Q3: the damage-photo gate is unchanged — it already admitted every viewer.
        response = self.open_photo(self.finance, self.live_photo)
        self.assertRedirects(response, SIGNED, fetch_redirect_response=False)
        self.assert_open_logged('dc_damage_photo_opened', 'DCLineDamagePhoto',
                                self.live_photo.pk)

    def test_withdrawn_files_are_a_404_for_finance_and_log_nothing(self):
        for document in (self.lr, self.challan_gone):
            with self.subTest(document=document.original_name):
                self.assertEqual(self.open_document(self.finance, document).status_code, 404)
        self.assertEqual(self.open_photo(self.finance, self.gone_photo).status_code, 404)
        self.assertFalse(ActivityLog.objects.filter(
            action_code__in=('dc_document_opened', 'dc_damage_photo_opened')).exists())
        self.bucket_api.create_signed_url.assert_not_called()


# ---------------------------------------------------------------------------
# F2 / Q1 — every write view refuses Finance; none reads the read audience
# ---------------------------------------------------------------------------

class FinanceWritesRefusedTests(FinanceDcBase):

    def snapshot(self):
        """Everything a write on this challan could change, in comparable form."""
        self.panels.refresh_from_db()
        self.dc.refresh_from_db()
        return {
            'receipts': list(GoodsReceipt.objects.values_list('pk', flat=True)),
            'line': (self.panels.received_quantity, self.panels.damaged_quantity,
                     self.panels.grn_confirmed_by_id, self.panels.grn_notes),
            'challan': (self.dc.dc_number, self.dc.status, self.dc.notes),
            'challans': DeliveryChallan.objects.count(),
            'attachments': list(DeliveryChallanAttachment.objects
                                .values_list('pk', 'withdrawn_at')),
            'photos': list(DCLineDamagePhoto.objects.values_list('pk', 'withdrawn_at')),
            'revisions': self.dc.revisions.count(),
            'issues': Issue.objects.count(),
        }

    def post_as_finance(self, url, data):
        self.login(self.finance)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(url, data)

    def test_every_write_view_refuses_finance_and_writes_nothing(self):
        upload = SimpleUploadedFile('finance.pdf', PDF, content_type='application/pdf')
        cases = {
            'confirm_grn': (self.url('confirm_grn'),
                            self.payload({self.inverter: ('2', '0', [])},
                                         [(RECEIPT_DOCUMENT_SIGNED_DC, pdf('f1.pdf'))])),
            'override_grn': (self.url('override_grn'),
                             self.payload({self.inverter: ('2', '0', [])},
                                          reason='Finance should never get here')),
            'delivery_challan_edit (with a document upload)': (
                self.url('delivery_challan_edit'),
                {'dc_number': 'FIN-EDIT', 'reason': 'Finance should never get here',
                 'challan_documents': upload, 'no_prices_confirmed': '1'}),
            'create_delivery_challan (with a document upload)': (
                reverse('create_delivery_challan', args=[self.site.project_id]),
                {'dc_number': 'FIN-NEW', 'dc_date': '2026-11-05',
                 'challan_documents': SimpleUploadedFile('n.pdf', PDF,
                                                         content_type='application/pdf'),
                 'no_prices_confirmed': '1'}),
            'delivery_challan_attachment_withdraw (receipt document)': (
                self.url('delivery_challan_attachment_withdraw', self.signed_copy.pk),
                {'withdrawn_reason': 'Finance should never get here'}),
            'delivery_challan_attachment_withdraw (challan document)': (
                self.url('delivery_challan_attachment_withdraw', self.challan_live.pk),
                {'withdrawn_reason': 'Finance should never get here'}),
            'dc_damage_photo_withdraw': (
                self.url('dc_damage_photo_withdraw', self.live_photo.pk),
                {'withdrawn_reason': 'Finance should never get here'}),
        }
        before = self.snapshot()
        for name, (url, data) in cases.items():
            with self.subTest(view=name):
                response = self.post_as_finance(url, data)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(self.snapshot(), before, 'nothing written')
        self.assertEqual(self.uploads(), [], 'nothing uploaded')
        # The site's feed, not the actor's: force_login itself logs a user_login row.
        self.assertFalse(ActivityLog.objects.filter(project=self.site,
                                                    actor=self.finance).exists())

    def test_finance_cannot_open_the_dc_edit_form(self):
        self.login(self.finance)
        self.assertEqual(self.client.get(self.url('delivery_challan_edit')).status_code, 403)

    def test_no_write_gate_reads_the_read_audience(self):
        # Q1: the read list widened for Finance; this pins that no write path follows it.
        # Source-level, so a future edit that routes a write gate through the read
        # helpers fails here even before any role is added to the list. The names the
        # CODE uses (ast), not its docstrings, which mention the read helpers by design.
        read_names = {'DC_DETAIL_ROLES', 'READ_ONLY_DC_ROLES', 'user_has_dc_read_role',
                      'user_can_view_delivery_challan', 'user_can_open_dc_document'}
        write_gates = (
            views.confirm_grn, views.override_grn, views.delivery_challan_edit,
            views.create_delivery_challan, views.delivery_challan_attachment_withdraw,
            views.dc_damage_photo_withdraw, dc_edit.save_challan_edit,
            permissions.user_can_edit_dc, permissions.user_can_manage_dc_documents,
        )
        for gate in write_gates:
            tree = ast.parse(textwrap.dedent(inspect.getsource(inspect.unwrap(gate))))
            used = ({node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
                    | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)})
            with self.subTest(gate=gate.__name__):
                self.assertTrue(used, 'the parse found the body')
                self.assertEqual(used & read_names, set())


# ---------------------------------------------------------------------------
# F5 (Q2) — the forms say Finance reads the files
# ---------------------------------------------------------------------------

class HelpTextTests(FinanceDcBase):

    def test_both_grn_forms_name_finance(self):
        # The SE's form (shown while the challan is not Received — setUp's receipt left
        # the inverter line blank) and SCM's override modal.
        self.dc.refresh_from_db()
        self.assertEqual(self.dc.status, DeliveryChallan.PARTIALLY_RECEIVED)
        for profile in (self.se, self.scm):
            with self.subTest(role=profile.role):
                page = self.page_for(profile).content.decode()
                self.assertIn('Signed challan copy, LR/bilty or delivery photo. Site '
                              'Engineers and Finance will see this file — no rates or '
                              'amounts.', page)
                self.assertIn('These documents show no rates, values or amounts. Site '
                              'Engineers and Finance will see them.</label>', page)
                self.assertNotIn('Site Engineers will see', page)

    def test_dc_create_and_edit_pages_name_finance(self):
        self.login(self.scm)
        for url in (reverse('create_delivery_challan', args=[self.site.project_id]),
                    self.url('delivery_challan_edit')):
            with self.subTest(url=url):
                # follow: the create page sends a GET without ?key= to one (DC-K).
                response = self.client.get(url, follow=True)
                self.assertEqual(response.status_code, 200)
                page = response.content.decode()
                self.assertIn('Site Engineers and Finance will see these documents.', page)
                self.assertIn('Site Engineers and Finance will see them.', page)
                self.assertNotIn('Site Engineers will see', page)
