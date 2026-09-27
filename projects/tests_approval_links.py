"""Approvals 3b — PO/PI records covered by an approved pre-order approval
(approvals.link_order_to_approval / unlink_order_from_approval, the approval page's
"PO/PI records covered" section, and the PO/PI record page's read-only block).

What this file pins, and why each matters:

  * EVERY REFUSAL WRITES NOTHING: not approved, not a pre-order request, a record of
    another vendor, already linked, not SCM; a removal without a reason, of a link
    already removed, or by someone who is not SCM.
  * THE VENDOR RULE APPLIES ONLY WHEN THE APPROVAL NAMES A VENDOR: a vendorless approval
    covers records of any vendor (Zuber's ruling, 27 Sep).
  * MANY-TO-MANY BOTH WAYS, and RELINKING after a removal works — the uniqueness counts
    active links only.
  * THE DATABASE HOLDS THE TWO CONSTRAINTS on its own: raw writes raise IntegrityError.
  * NO LEDGER ROW AND NO NOTIFICATION: the request's status does not move.
  * ANY SCM USER (D-A22) may link and remove; PM, Design and Finance get a 403 with a body.
  * HISTORY shows each link and each removal exactly once, from the link rows.
  * THE PO/PI RECORD PAGE names covering approvals, linking each only where the viewer may
    open it, and draws nothing at all when there are none.

Run with:
    python manage.py test projects.tests_approval_links --settings=solarpms.test_settings
and under the real settings (Postgres, migrations applied):
    python manage.py test projects.tests_approval_links
"""
import re
from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .approvals import (
    ApprovalRefused, apply_approval_decision, create_approval_request,
    link_order_to_approval, unlink_order_from_approval,
)
from .models import (
    AppendOnlyViolation, ApprovalOrderLink, ApprovalStep, StatusTransition, Vendor,
    VendorOrder,
    APPROVAL_KIND_MATERIAL_PRE_DISPATCH, APPROVAL_KIND_MATERIAL_PRE_ORDER,
    APPROVAL_PARTY_DESIGN, APPROVAL_PARTY_PM, APPROVAL_STEP_APPROVED,
    APPROVAL_STEP_SUPERSEDED, SUBJECT_APPROVAL_REQUEST,
)


def _profile(username, role, **flags):
    """A post_save signal creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.replace('_', ' ').title())
    profile = user.profile
    profile.role = role
    profile.is_active = True
    for field, value in flags.items():
        setattr(profile, field, value)
    profile.save()
    return profile


class LinkFixture(TestCase):
    """Two vendors: AL Vendor with two PO/PI records, AL Other with one. None names a
    site, so only the portfolio roles may open them."""

    @classmethod
    def setUpTestData(cls):
        cls.scm      = _profile('al_scm', 'SCM')
        cls.scm_b    = _profile('al_scm_b', 'SCM')
        cls.pm       = _profile('al_pm', 'PM')
        cls.head     = _profile('al_head', 'Design', is_design_head=True)
        cls.ceo      = _profile('al_ceo', 'CEO')
        cls.finance  = _profile('al_finance', 'Finance')
        cls.vendor = Vendor.objects.create(name='AL Vendor', contact_person='R',
                                           phone='9000000011')
        cls.other_vendor = Vendor.objects.create(name='AL Other', contact_person='O',
                                                 phone='9000000012')
        cls.order = VendorOrder.objects.create(
            vendor=cls.vendor, project_type='Residential', total_amount=Decimal('1000'),
            created_by=cls.scm, po_number='PO-AL-1', pi_number='PI-AL-1')
        cls.order_2 = VendorOrder.objects.create(
            vendor=cls.vendor, project_type='Residential', total_amount=Decimal('2000'),
            created_by=cls.scm, po_number='PO-AL-2', pi_number='')
        cls.other_order = VendorOrder.objects.create(
            vendor=cls.other_vendor, project_type='Residential',
            total_amount=Decimal('3000'), created_by=cls.scm, po_number='PO-OTHER')

    def setUp(self):
        self.approval = self.approved_pre_order('Modules pre-order', self.vendor)

    # ── helpers ─────────────────────────────────────────────────────────────

    def step(self, approval, party):
        approval.refresh_from_db()
        return ApprovalStep.objects.exclude(verdict=APPROVAL_STEP_SUPERSEDED).get(
            request=approval, party=party, round=approval.current_round)

    def raise_pre_order(self, title, vendor):
        return create_approval_request(
            kind=APPROVAL_KIND_MATERIAL_PRE_ORDER, raised_by=self.scm, title=title,
            description='Propose Waaree 545 Wp.', pm_assignee=self.pm,
            design_signoff_required=True, design_assignee=self.head, vendor=vendor,
            material={'proposed_make': 'Waaree'})

    def approve(self, approval):
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_APPROVED, self.pm)
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_DESIGN),
                                APPROVAL_STEP_APPROVED, self.head)
        approval.refresh_from_db()
        return approval

    def approved_pre_order(self, title, vendor):
        return self.approve(self.raise_pre_order(title, vendor))

    def client_for(self, profile):
        client = Client(SERVER_NAME='localhost')
        client.force_login(profile.user)
        return client

    def messages_of(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def assert_forbidden(self, response):
        self.assertEqual(response.status_code, 403)
        self.assertIn(b'Access denied', response.content)   # _forbidden's body, not empty

    def assert_refused(self, call, message):
        before = list(ApprovalOrderLink.objects.order_by('pk').values())
        with self.assertRaisesMessage(ApprovalRefused, message):
            call()
        self.assertEqual(list(ApprovalOrderLink.objects.order_by('pk').values()), before)


# ===========================================================================
# The chokepoint
# ===========================================================================

class LinkRefusalTests(LinkFixture):

    def test_an_open_pre_order_is_refused(self):
        open_one = self.raise_pre_order('Still open', self.vendor)
        self.assert_refused(lambda: link_order_to_approval(open_one, self.order, self.scm),
                            'has not been approved')

    def test_a_pre_dispatch_request_is_refused(self):
        dispatch = self.approve(create_approval_request(
            kind=APPROVAL_KIND_MATERIAL_PRE_DISPATCH, raised_by=self.scm,
            title='Dispatch', description='Lot 1.', pm_assignee=self.pm,
            design_signoff_required=True, design_assignee=self.head, vendor=self.vendor,
            material={'vendor_order': self.order}))
        self.assert_refused(lambda: link_order_to_approval(dispatch, self.order, self.scm),
                            'Only a pre-order approval covers PO/PI records.')

    def test_a_record_of_another_vendor_is_refused(self):
        self.assert_refused(
            lambda: link_order_to_approval(self.approval, self.other_order, self.scm),
            'The PO/PI record was placed with a different vendor.')

    def test_a_record_already_linked_is_refused(self):
        link_order_to_approval(self.approval, self.order, self.scm)
        self.assert_refused(
            lambda: link_order_to_approval(self.approval, self.order, self.scm_b),
            'That PO/PI record is already linked to this approval.')
        self.assertEqual(ApprovalOrderLink.objects.count(), 1)

    def test_someone_who_is_not_scm_is_refused(self):
        for profile in (self.pm, self.head, self.finance, self.ceo):
            self.assert_refused(
                lambda: link_order_to_approval(self.approval, self.order, profile),
                'Only SCM can link a PO/PI record to an approval.')

    def test_no_record_is_refused(self):
        self.assert_refused(lambda: link_order_to_approval(self.approval, None, self.scm),
                            'Choose the PO/PI record to link.')


class UnlinkRefusalTests(LinkFixture):

    def setUp(self):
        super().setUp()
        self.link = link_order_to_approval(self.approval, self.order, self.scm)

    def test_a_removal_without_a_reason_is_refused(self):
        for note in ('', '   '):
            self.assert_refused(lambda: unlink_order_from_approval(self.link, self.scm, note),
                                'Say why this link is being removed.')

    def test_a_link_already_removed_is_refused(self):
        unlink_order_from_approval(self.link, self.scm, 'Wrong record.')
        self.assert_refused(
            lambda: unlink_order_from_approval(self.link, self.scm_b, 'Again.'),
            'That link was already removed.')

    def test_someone_who_is_not_scm_is_refused(self):
        for profile in (self.pm, self.head, self.finance, self.ceo):
            self.assert_refused(
                lambda: unlink_order_from_approval(self.link, profile, 'Not mine.'),
                'Only SCM can remove a PO/PI record link.')


class LinkWriteTests(LinkFixture):

    def test_a_vendorless_approval_links_records_of_two_vendors(self):
        vendorless = self.approved_pre_order('No vendor yet', None)
        link_order_to_approval(vendorless, self.order, self.scm)
        link_order_to_approval(vendorless, self.other_order, self.scm)
        self.assertEqual(
            set(ApprovalOrderLink.objects.filter(approval=vendorless)
                .values_list('vendor_order__vendor_id', flat=True)),
            {self.vendor.pk, self.other_vendor.pk})

    def test_an_approval_with_a_vendor_still_refuses_a_mismatched_record(self):
        link_order_to_approval(self.approval, self.order, self.scm)
        self.assert_refused(
            lambda: link_order_to_approval(self.approval, self.other_order, self.scm),
            'The PO/PI record was placed with a different vendor.')

    def test_one_approval_covers_several_records(self):
        link_order_to_approval(self.approval, self.order, self.scm)
        link_order_to_approval(self.approval, self.order_2, self.scm)
        self.assertEqual(
            set(self.approval.order_links.values_list('vendor_order_id', flat=True)),
            {self.order.pk, self.order_2.pk})

    def test_one_record_is_covered_by_several_approvals(self):
        second = self.approved_pre_order('Modules, top-up', self.vendor)
        link_order_to_approval(self.approval, self.order, self.scm)
        link_order_to_approval(second, self.order, self.scm_b)
        self.assertEqual(
            set(self.order.approval_links.values_list('approval_id', flat=True)),
            {self.approval.pk, second.pk})

    def test_a_link_records_who_and_when(self):
        link = link_order_to_approval(self.approval, self.order, self.scm_b)
        self.assertEqual(link.linked_by, self.scm_b)
        self.assertIsNotNone(link.linked_at)
        self.assertIsNone(link.removed_at)

    def test_removal_keeps_the_row_with_who_when_and_why(self):
        link = link_order_to_approval(self.approval, self.order, self.scm)
        removed = unlink_order_from_approval(link, self.scm_b, '  Wrong record.  ')
        self.assertEqual(removed.pk, link.pk)
        self.assertEqual(removed.removed_by, self.scm_b)
        self.assertIsNotNone(removed.removed_at)
        self.assertEqual(removed.removal_note, 'Wrong record.')
        self.assertEqual(ApprovalOrderLink.objects.count(), 1)

    def test_relinking_after_removal_works(self):
        first = link_order_to_approval(self.approval, self.order, self.scm)
        unlink_order_from_approval(first, self.scm, 'Linked by mistake.')
        second = link_order_to_approval(self.approval, self.order, self.scm_b)
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(ApprovalOrderLink.objects.filter(
            approval=self.approval, vendor_order=self.order).count(), 2)
        self.assertEqual(ApprovalOrderLink.objects.filter(
            approval=self.approval, vendor_order=self.order,
            removed_at__isnull=True).get(), second)

    def test_no_ledger_row_and_no_notification(self):
        ledger = StatusTransition.objects.filter(
            subject_type=SUBJECT_APPROVAL_REQUEST, subject_id=self.approval.pk).count()
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            link = link_order_to_approval(self.approval, self.order, self.scm)
            unlink_order_from_approval(link, self.scm, 'Wrong record.')
        self.assertEqual(callbacks, [])
        self.assertEqual(StatusTransition.objects.filter(
            subject_type=SUBJECT_APPROVAL_REQUEST, subject_id=self.approval.pk).count(),
            ledger)
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, 'approved')

    def test_the_row_is_append_only(self):
        link = link_order_to_approval(self.approval, self.order, self.scm)
        link.removal_note = 'edited'
        with self.assertRaises(AppendOnlyViolation):
            link.save()
        with self.assertRaises(AppendOnlyViolation):
            link.delete()
        self.assertTrue(ApprovalOrderLink.objects.filter(pk=link.pk, removal_note='').exists())


class LinkConstraintTests(LinkFixture):
    """Raw writes, past the chokepoint: the database holds both rules itself."""

    def test_two_active_links_of_one_pair_raise(self):
        ApprovalOrderLink.objects.create(approval=self.approval, vendor_order=self.order,
                                         linked_by=self.scm)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ApprovalOrderLink.objects.create(approval=self.approval,
                                             vendor_order=self.order, linked_by=self.scm)

    def test_a_removed_link_does_not_block_an_active_one(self):
        ApprovalOrderLink.objects.create(
            approval=self.approval, vendor_order=self.order, linked_by=self.scm,
            removed_by=self.scm, removed_at=timezone.now(), removal_note='Gone.')
        ApprovalOrderLink.objects.create(approval=self.approval, vendor_order=self.order,
                                         linked_by=self.scm)
        self.assertEqual(ApprovalOrderLink.objects.count(), 2)

    def test_removal_fields_are_all_set_or_all_empty(self):
        now = timezone.now()
        partial = [
            {'removed_at': now},
            {'removed_by': self.scm},
            {'removal_note': 'Why'},
            {'removed_at': now, 'removed_by': self.scm},              # no reason
            {'removed_at': now, 'removal_note': 'Why'},              # no who
            {'removed_by': self.scm, 'removal_note': 'Why'},         # no when
        ]
        for fields in partial:
            with self.subTest(fields=sorted(fields)):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    ApprovalOrderLink.objects.create(
                        approval=self.approval, vendor_order=self.order,
                        linked_by=self.scm, **fields)

    def test_an_update_that_stamps_half_a_removal_raises(self):
        link = link_order_to_approval(self.approval, self.order, self.scm)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ApprovalOrderLink.objects.filter(pk=link.pk).update(
                removed_at=timezone.now(), removed_by=self.scm)


# ===========================================================================
# The approval page
# ===========================================================================

class LinkViewTests(LinkFixture):

    def link_url(self, approval=None):
        return reverse('approval_link_order', args=[(approval or self.approval).pk])

    def detail(self, profile, approval=None):
        return self.client_for(profile).get(
            reverse('approval_detail', args=[(approval or self.approval).pk]))

    def test_any_scm_user_links_and_removes(self):
        # scm_b raised nothing and is named on nothing (D-A22).
        client = self.client_for(self.scm_b)
        response = client.post(self.link_url(), {'vendor_order': self.order.pk})
        self.assertEqual(response.status_code, 302)
        link = ApprovalOrderLink.objects.get(approval=self.approval)
        self.assertEqual(link.linked_by, self.scm_b)

        response = client.post(reverse('approval_unlink_order', args=[link.pk]),
                               {'note': 'Wrong PO.'})
        self.assertEqual(response.status_code, 302)
        link.refresh_from_db()
        self.assertEqual((link.removed_by, link.removal_note), (self.scm_b, 'Wrong PO.'))

    def test_pm_design_and_finance_get_403_and_nothing_is_written(self):
        link = link_order_to_approval(self.approval, self.order, self.scm)
        before = list(ApprovalOrderLink.objects.order_by('pk').values())
        for profile in (self.pm, self.head, self.finance):
            client = self.client_for(profile)
            with self.subTest(role=profile.role):
                self.assert_forbidden(client.post(self.link_url(),
                                                  {'vendor_order': self.order_2.pk}))
                self.assert_forbidden(client.post(
                    reverse('approval_unlink_order', args=[link.pk]), {'note': 'No.'}))
        self.assertEqual(list(ApprovalOrderLink.objects.order_by('pk').values()), before)

    def test_anonymous_is_sent_to_login(self):
        link = link_order_to_approval(self.approval, self.order, self.scm)
        anon = Client(SERVER_NAME='localhost')
        for url in (self.link_url(), reverse('approval_unlink_order', args=[link.pk])):
            response = anon.post(url, {'note': 'x', 'vendor_order': self.order.pk})
            self.assertEqual(response.status_code, 302)
            self.assertIn('login', response['Location'])

    def test_a_refusal_is_a_message_on_the_request(self):
        response = self.client_for(self.scm).post(
            self.link_url(), {'vendor_order': self.other_order.pk})
        self.assertEqual(response['Location'],
                         reverse('approval_detail', args=[self.approval.pk]))
        self.assertIn('The PO/PI record was placed with a different vendor.',
                      self.messages_of(response))
        self.assertFalse(ApprovalOrderLink.objects.exists())

    def test_a_removal_without_a_reason_is_a_message(self):
        link = link_order_to_approval(self.approval, self.order, self.scm)
        response = self.client_for(self.scm).post(
            reverse('approval_unlink_order', args=[link.pk]), {'note': ' '})
        self.assertIn('Say why this link is being removed.', self.messages_of(response))
        link.refresh_from_db()
        self.assertIsNone(link.removed_at)

    def test_the_section_lists_active_and_struck_through_removed_links(self):
        removed = link_order_to_approval(self.approval, self.order, self.scm)
        unlink_order_from_approval(removed, self.scm_b, 'Superseded PO.')
        link_order_to_approval(self.approval, self.order_2, self.scm)
        content = self.detail(self.scm).content.decode()
        self.assertIn('PO/PI records covered', content)
        self.assertIn('PO PO-AL-2', content)
        self.assertRegex(content, r'<s><a href="[^"]+" class="text-muted">AL Vendor · '
                                  r'PO PO-AL-1')
        self.assertIn('Removed by Al Scm B', content)
        self.assertIn('Reason: Superseded PO.', content)
        self.assertIn(reverse('approval_unlink_order', args=[
            self.approval.order_links.get(removed_at__isnull=True).pk]), content)

    def test_the_picker_offers_only_the_vendors_unlinked_records(self):
        link_order_to_approval(self.approval, self.order, self.scm)
        content = self.detail(self.scm).content.decode()
        picker = content.split('id="apLinkOrder"', 1)[1].split('</select>', 1)[0]
        self.assertIn(f'value="{self.order_2.pk}"', picker)
        self.assertNotIn(f'value="{self.order.pk}"', picker)
        self.assertNotIn(f'value="{self.other_order.pk}"', picker)
        self.assertNotIn('<optgroup', picker)

    def test_a_vendorless_approval_picker_groups_every_vendor(self):
        vendorless = self.approved_pre_order('No vendor yet', None)
        content = self.detail(self.scm, vendorless).content.decode()
        picker = content.split('id="apLinkOrder"', 1)[1].split('</select>', 1)[0]
        self.assertIn('<optgroup label="AL Vendor">', picker)
        self.assertIn('<optgroup label="AL Other">', picker)
        for order in (self.order, self.order_2, self.other_order):
            self.assertIn(f'value="{order.pk}"', picker)
        # Newest first within a vendor.
        self.assertLess(picker.index(f'value="{self.order_2.pk}"'),
                        picker.index(f'value="{self.order.pk}"'))

    def test_readers_who_are_not_scm_see_the_section_without_forms(self):
        link_order_to_approval(self.approval, self.order, self.scm)
        for profile in (self.pm, self.ceo):
            with self.subTest(role=profile.role):
                content = self.detail(profile).content.decode()
                self.assertIn('PO/PI records covered', content)
                self.assertIn('PO PO-AL-1', content)
                self.assertNotIn('apLinkOrder', content)
                self.assertNotIn('Remove link', content)

    def test_no_section_on_an_open_pre_order_or_another_kind(self):
        open_one = self.raise_pre_order('Still open', self.vendor)
        self.assertNotIn(b'PO/PI records covered', self.detail(self.scm, open_one).content)
        dispatch = create_approval_request(
            kind=APPROVAL_KIND_MATERIAL_PRE_DISPATCH, raised_by=self.scm,
            title='Dispatch', description='Lot 1.', pm_assignee=self.pm,
            vendor=self.vendor, material={'vendor_order': self.order,
                                          'pre_order_request': self.approval})
        self.assertNotIn(b'PO/PI records covered', self.detail(self.scm, dispatch).content)

    def test_history_shows_each_link_and_removal_once(self):
        link = link_order_to_approval(self.approval, self.order, self.scm)
        unlink_order_from_approval(link, self.scm_b, 'Wrong record.')
        content = self.detail(self.ceo).content.decode()
        history = content.split('>History</div>', 1)[1]
        linked = 'Linked PO/PI record AL Vendor · PO PO-AL-1 · PI PI-AL-1'
        removed = 'Removed link to PO/PI record AL Vendor · PO PO-AL-1 · PI PI-AL-1'
        self.assertEqual(history.count(linked), 1)
        self.assertEqual(history.count(removed), 1)
        self.assertIn(f'<strong>{linked}</strong> · Al Scm ·', history)
        self.assertIn(f'<strong>{removed}</strong> · Al Scm B ·', history)
        self.assertIn('Wrong record.', history)
        self.assertLess(history.index(linked), history.index(removed))
        self.assertIn(' IST', history)


# ===========================================================================
# The PO/PI record page
# ===========================================================================

class CoverageBlockTests(LinkFixture):

    def page(self, profile, order=None):
        response = self.client_for(profile).get(
            reverse('vendor_order_detail', args=[(order or self.order).pk]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_a_reader_of_the_approval_gets_a_link(self):
        link_order_to_approval(self.approval, self.order, self.scm)
        content = self.page(self.ceo)
        self.assertIn('Covered by pre-order approval', content)
        self.assertIn(f'<a href="{reverse("approval_detail", args=[self.approval.pk])}">'
                      f'Modules pre-order</a>', content)

    def test_a_reader_who_may_not_open_the_approval_gets_plain_text(self):
        # Finance reads every PO/PI record but no approval (APPROVAL_PORTFOLIO_ROLES).
        link_order_to_approval(self.approval, self.order, self.scm)
        content = self.page(self.finance)
        self.assertIn('Covered by pre-order approval', content)
        self.assertIn('Modules pre-order', content)
        self.assertNotIn(reverse('approval_detail', args=[self.approval.pk]), content)

    def test_every_active_approval_is_listed_and_a_removed_one_is_not(self):
        second = self.approved_pre_order('Modules, top-up', self.vendor)
        gone = self.approved_pre_order('Withdrawn cover', self.vendor)
        link_order_to_approval(self.approval, self.order, self.scm)
        link_order_to_approval(second, self.order, self.scm)
        unlink_order_from_approval(link_order_to_approval(gone, self.order, self.scm),
                                   self.scm, 'Not this PO.')
        content = self.page(self.scm)
        self.assertIn('Modules pre-order', content)
        self.assertIn('Modules, top-up', content)
        self.assertNotIn('Withdrawn cover', content)

    def test_the_page_draws_nothing_when_there_are_no_links(self):
        # A removed link counts as none.
        unlink_order_from_approval(link_order_to_approval(self.approval, self.order_2,
                                                          self.scm),
                                   self.scm, 'Wrong PO.')
        for order in (self.order, self.order_2):
            content = self.page(self.scm, order)
            self.assertNotIn('Covered by pre-order approval', content)
            self.assertNotIn('Modules pre-order', content)
            # Between the Totals card and the Lines card there is only whitespace.
            self.assertRegex(content, re.compile(
                r'Invoiced</dt>.*?</dl>\s*</div>\s*</div>\s*</div>\s*</div>\s*'
                r'<div class="card shadow-sm mb-3">\s*'
                r'<div class="card-header fw-semibold">Lines</div>', re.DOTALL))
