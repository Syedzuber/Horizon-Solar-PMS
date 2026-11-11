"""NTF-DCG 4c: a challan is Received only when every line is recorded.

RULING (a), 4 Nov 2026. Received means every line has a recorded received quantity — an
explicit 0 counts as recorded, a blank does not. Before this, recalculate_dc_status()
left blank lines out of the rollup, so a challan with 2 of 7 lines confirmed in full read
Received; and a Received challan hides the SE's GRN form and refuses confirm_grn, so the
other 5 lines could never be recorded by the engineer.

WHAT IS PINNED HERE
  * the rule (derive_dc_status, through recalculate_dc_status): all recorded and green →
    Received; one blank → Partially Received; an explicit 0 → Rejected (Q1: the severity
    rule is unchanged, a recorded 0 is red); none recorded → Expected; red with a blank
    line still Rejected; amber with a blank line still Partially Received;
  * the SE's path end to end: a blank unarrived line leaves the challan Partially
    Received with the GRN form still on the page, and the later full total closes it;
  * a DC edit removing the one unreceived line closes a Partially Received challan;
  * S5: typing 0 saves 0; a stored 0 renders as "0.00" on both GRN forms (SE confirm,
    SCM override), and posted back as rendered it is not a change — no receipt, the
    line's confirmer and date stay;
  * dc_status_check: the reason it gives, and that it writes nothing.

WHAT IS NOT: mirrors and phase rules read line quantities, not the challan status, and
are pinned by their own suites, which this session did not edit. This module never names
the delivery-mirror derivation (its caller list is closed, tests_design_mirror_derivation).

Run with:
    python manage.py test projects.tests_dc_status_rule --settings=solarpms.test_settings
"""
import re
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .management.commands.dc_status_check import (
    REASON_RULE_DRIFT, REASON_UNRECORDED, challan_difference,
)
from .models import (
    DCLineItem, DeliveryChallan, GoodsReceipt, Project, ProjectPhase, Task,
    derive_dc_status, recalculate_dc_status,
)
from .tests_grn_damage import with_grn_photo

BUCKET = 'Horizon-PMS-Delivery-test'


def _profile(username, role):
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title(), last_name='Test')
    profile = user.profile          # auto-created by the post_save signal
    profile.role = role
    profile.save(update_fields=['role'])
    return profile


@override_settings(SUPABASE_DELIVERY_BUCKET=BUCKET)
class DcStatusRuleBase(TestCase):
    """One live Residential site, an SE holding a task on it (confirm_grn's scope), SCM,
    and a challan with THREE lines: panels 10, inverters 2, cable 50. Three so a test can
    record some, leave one blank, and still have a line to put a 0 on."""

    @classmethod
    def setUpTestData(cls):
        cls.scm = _profile('dcs_scm', 'SCM')
        cls.pm = _profile('dcs_pm', 'PM')
        cls.se = _profile('dcs_se', 'Site Engineer')
        cls.site = Project.objects.create(
            project_id='DCS-SITE', customer_name='DCS Customer', customer_phone='9876543210',
            site_address='1 Sun Road', city='Delhi', project_type='Residential',
            dc_capacity_kw=Decimal('5.00'), status='Active', assigned_pm=cls.pm,
            target_commissioning_date=date.today() + timedelta(days=90))
        phase = ProjectPhase.objects.create(project=cls.site, phase_name='Install',
                                            phase_order=1)
        Task.objects.create(phase=phase, task_name='Receive material', task_order=1,
                            assigned_role=Task.SITE_ENGINEER, assigned_to=cls.se)

    def setUp(self):
        # The DC page and the GRN views reach the delivery bucket for photos; no network.
        self.storage = mock.MagicMock()
        for target in ('projects.delivery_storage._client', 'projects.design_storage._client'):
            patcher = mock.patch(target, return_value=self.storage)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = Client(SERVER_NAME='localhost')
        self.dc = DeliveryChallan.objects.create(
            project=self.site, dc_number='DCS-1', dc_date=date.today(), created_by=self.scm)
        self.panels = DCLineItem.objects.create(
            challan=self.dc, boq_category='Solar Modules', item_description='Mono 540 Wp',
            ordered_quantity=Decimal('10'), unit='Nos')
        self.inverters = DCLineItem.objects.create(
            challan=self.dc, boq_category='Inverter', item_description='5 kW inverter',
            ordered_quantity=Decimal('2'), unit='Nos')
        self.cable = DCLineItem.objects.create(
            challan=self.dc, boq_category='BOS', item_description='DC cable 4 sq mm',
            ordered_quantity=Decimal('50'), unit='m')

    # -- helpers -------------------------------------------------------------

    def record(self, line, received, damaged=0):
        """Write a line's receipt straight to the columns, for the pure-rule tests. The
        view tests below go through confirm_grn instead."""
        line.received_quantity = Decimal(received)
        line.damaged_quantity = damaged
        line.save(update_fields=['received_quantity', 'damaged_quantity'])

    def status(self):
        recalculate_dc_status(self.dc)
        self.dc.refresh_from_db()
        return self.dc.status

    def grn_payload(self, values):
        """{line: (received, damaged)} as the GRN form posts it."""
        data = {}
        for line, (received, damaged) in values.items():
            data[f'received_qty_{line.pk}'] = received
            data[f'damaged_qty_{line.pk}'] = damaged
        return data

    def confirm(self, values):
        """An SE GRN; GRN-P: a raise gets a delivery photo (tests_grn_damage.with_grn_photo)."""
        self.client.force_login(self.se.user)
        return self.client.post(
            reverse('confirm_grn', args=[self.site.project_id, self.dc.pk]),
            with_grn_photo(self.grn_payload(values)))

    def override(self, values):
        self.client.force_login(self.scm.user)
        return self.client.post(
            reverse('override_grn', args=[self.site.project_id, self.dc.pk]),
            self.grn_payload(values))

    def page(self, profile):
        self.client.force_login(profile.user)
        response = self.client.get(
            reverse('delivery_challan_detail', args=[self.site.project_id, self.dc.pk]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def form_html(self, page, view_name):
        """The one GRN form posting to `view_name`, from its <form to its </form>."""
        action = reverse(view_name, args=[self.site.project_id, self.dc.pk])
        match = re.search(r'<form[^>]*action="%s".*?</form>' % re.escape(action), page, re.S)
        self.assertIsNotNone(match, f'no {view_name} form on the page')
        return match.group(0)

    def received_value(self, form, line):
        """The value attribute of `line`'s received box in `form`."""
        match = re.search(r'name="received_qty_%d"[^>]*?value="([^"]*)"' % line.pk, form, re.S)
        self.assertIsNotNone(match, f'no received box for line {line.pk}')
        return match.group(1)


class RuleTests(DcStatusRuleBase):
    """S1–S4 through recalculate_dc_status (and derive_dc_status, which it calls)."""

    def test_all_lines_recorded_in_full_is_received(self):
        self.record(self.panels, '10')
        self.record(self.inverters, '2')
        self.record(self.cable, '50')
        self.assertEqual(self.status(), DeliveryChallan.RECEIVED)

    def test_one_line_unrecorded_is_partially_received(self):
        # The DC 24 shape: every recorded line complete, one blank. Before 4c: Received.
        self.record(self.panels, '10')
        self.record(self.inverters, '2')
        self.assertEqual(self.status(), DeliveryChallan.PARTIALLY_RECEIVED)

    def test_an_explicit_zero_on_the_unrecorded_line_is_rejected(self):
        # Q1: a recorded 0 counts as recorded, and is red under the unchanged severity
        # rule ("nothing received"), so the challan is Rejected, not Received.
        self.record(self.panels, '10')
        self.record(self.inverters, '2')
        self.record(self.cable, '0')
        self.assertEqual(self.status(), DeliveryChallan.REJECTED)

    def test_no_line_recorded_is_expected_as_before(self):
        self.assertEqual(self.status(), DeliveryChallan.EXPECTED)

    def test_rejected_is_unaffected_by_a_blank_line(self):
        # Short AND damaged on one line: red, Rejected — a blank line does not soften it.
        self.record(self.panels, '8', damaged=1)
        self.assertEqual(self.status(), DeliveryChallan.REJECTED)

    def test_amber_with_a_blank_line_stays_partially_received(self):
        self.record(self.panels, '8')
        self.assertEqual(self.status(), DeliveryChallan.PARTIALLY_RECEIVED)

    def test_the_pure_rule_reads_only_the_rows_passed(self):
        # dc_status_check passes the recorded lines alone to ask the pre-4c question.
        self.record(self.panels, '10')
        lines = list(self.dc.line_items.all())
        self.assertEqual(derive_dc_status(lines), DeliveryChallan.PARTIALLY_RECEIVED)
        recorded = [line for line in lines if line.received_quantity is not None]
        self.assertEqual(derive_dc_status(recorded), DeliveryChallan.RECEIVED)
        self.assertEqual(derive_dc_status([]), DeliveryChallan.EXPECTED)


class SiteEngineerPathTests(DcStatusRuleBase):
    """The rule as the engineer meets it, through the real views."""

    def test_a_blank_unarrived_line_keeps_the_form_open_until_its_total_is_entered(self):
        response = self.confirm({self.panels: ('10', '0'), self.inverters: ('2', '0'),
                                 self.cable: ('', '')})
        self.assertEqual(response.status_code, 302)
        self.dc.refresh_from_db()
        self.assertEqual(self.dc.status, DeliveryChallan.PARTIALLY_RECEIVED)
        self.cable.refresh_from_db()
        self.assertIsNone(self.cable.received_quantity, 'blank means not recorded')

        # The SE form is still on the page — before 4c it was hidden here for good.
        self.form_html(self.page(self.se), 'confirm_grn')

        # The cable arrives: the full total entered later closes the challan.
        self.confirm({self.cable: ('50', '0')})
        self.dc.refresh_from_db()
        self.assertEqual(self.dc.status, DeliveryChallan.RECEIVED)

    def test_typing_zero_saves_zero(self):
        self.confirm({self.panels: ('10', '0'), self.inverters: ('2', '0'),
                      self.cable: ('0', '0')})
        self.cable.refresh_from_db()
        self.assertEqual(self.cable.received_quantity, Decimal('0'))
        self.assertEqual(self.cable.grn_confirmed_by, self.se)
        self.dc.refresh_from_db()
        self.assertEqual(self.dc.status, DeliveryChallan.REJECTED)


class DcEditTests(DcStatusRuleBase):
    """A DC edit recalculates through recalculate_dc_status, so the 4c rule applies."""

    def test_removing_the_unreceived_line_closes_a_partially_received_challan(self):
        self.confirm({self.panels: ('10', '0'), self.inverters: ('2', '0')})
        self.dc.refresh_from_db()
        self.assertEqual(self.dc.status, DeliveryChallan.PARTIALLY_RECEIVED)

        lines = list(self.dc.line_items.order_by('pk'))
        data = {
            'revision_count': '0',
            'line_ids': ','.join(str(line.pk) for line in lines),
            'editable_line_ids': str(self.cable.pk),
            'vendor_id': '', 'po_number': '', 'issued_from_warehouse': '',
            'dc_number': self.dc.dc_number, 'dc_date': self.dc.dc_date.isoformat(),
            'expected_delivery_date': '', 'notes': '',
            'reason': 'Cable dropped from this challan by the vendor',
            f'line_{self.cable.pk}_remove': '1',
        }
        for line in lines:
            data[f'line_{line.pk}_description'] = line.item_description
            data[f'line_{line.pk}_unit'] = line.unit
        data[f'line_{self.cable.pk}_category'] = self.cable.boq_category
        data[f'line_{self.cable.pk}_qty'] = str(self.cable.ordered_quantity)

        self.client.force_login(self.scm.user)
        self.client.post(reverse('delivery_challan_edit',
                                 args=[self.site.project_id, self.dc.pk]), data)
        self.assertFalse(DCLineItem.objects.filter(pk=self.cable.pk).exists())
        self.dc.refresh_from_db()
        self.assertEqual(self.dc.status, DeliveryChallan.RECEIVED)


class ZeroDisplayTests(DcStatusRuleBase):
    """S5: a stored 0 shows as 0.00 (Q3) on both forms and is not a change posted back."""

    def setUp(self):
        super().setUp()
        # Recorded through the real SE path: two lines in full, the cable as 0. The
        # challan is Rejected (red), so the SE form is still shown (it hides on Received).
        self.confirm({self.panels: ('10', '0'), self.inverters: ('2', '0'),
                      self.cable: ('0', '0')})
        self.cable.refresh_from_db()
        self.assertEqual(self.cable.received_quantity, Decimal('0'))

    def test_a_recorded_zero_renders_as_zero_on_the_se_form(self):
        form = self.form_html(self.page(self.se), 'confirm_grn')
        self.assertEqual(self.received_value(form, self.cable), '0.00')
        self.assertEqual(self.received_value(form, self.panels), '10.00')

    def test_a_recorded_zero_renders_as_zero_on_the_scm_form(self):
        form = self.form_html(self.page(self.scm), 'override_grn')
        self.assertEqual(self.received_value(form, self.cable), '0.00')

    def test_an_unrecorded_line_still_renders_blank(self):
        line = DCLineItem.objects.create(
            challan=self.dc, boq_category='Structure', item_description='Rail 4 m',
            ordered_quantity=Decimal('12'), unit='Nos')
        form = self.form_html(self.page(self.se), 'confirm_grn')
        self.assertEqual(self.received_value(form, line), '')

    def _assert_zero_reposted_is_not_a_change(self, post):
        receipts = GoodsReceipt.objects.count()
        before = DCLineItem.objects.filter(pk=self.cable.pk).values(
            'received_quantity', 'damaged_quantity', 'grn_date', 'grn_confirmed_by',
            'grn_on_behalf').get()
        # Exactly what the form renders for the line: "0.00" received, "0" damaged.
        post({self.cable: ('0.00', '0')})
        self.assertEqual(GoodsReceipt.objects.count(), receipts,
                         'a 0 posted back unchanged must not write a receipt')
        after = DCLineItem.objects.filter(pk=self.cable.pk).values(
            'received_quantity', 'damaged_quantity', 'grn_date', 'grn_confirmed_by',
            'grn_on_behalf').get()
        self.assertEqual(after, before)

    def test_the_se_form_reposting_a_zero_is_not_a_change(self):
        self._assert_zero_reposted_is_not_a_change(self.confirm)

    def test_the_scm_form_reposting_a_zero_is_not_a_change(self):
        self._assert_zero_reposted_is_not_a_change(self.override)


class DcStatusCheckTests(DcStatusRuleBase):
    """S6: report-only; one line per difference, with its reason; writes nothing."""

    def _force_status(self, status):
        # A row saved under an older rule: the stored status written past the rule.
        DeliveryChallan.objects.filter(pk=self.dc.pk).update(status=status)
        self.dc.refresh_from_db()

    def run_check(self):
        out = StringIO()
        call_command('dc_status_check', stdout=out)
        return out.getvalue()

    def test_a_matching_challan_is_not_reported(self):
        self.record(self.panels, '10')
        recalculate_dc_status(self.dc)
        self.dc.refresh_from_db()
        self.assertIsNone(challan_difference(self.dc, list(self.dc.line_items.all())))
        self.assertIn('Differences: 0.', self.run_check())

    def test_received_with_blank_lines_is_reported_as_unrecorded(self):
        self.record(self.panels, '10')
        self._force_status(DeliveryChallan.RECEIVED)   # what the pre-4c rule stored
        self.assertEqual(challan_difference(self.dc, list(self.dc.line_items.all())),
                         (DeliveryChallan.PARTIALLY_RECEIVED, REASON_UNRECORDED))
        output = self.run_check()
        self.assertIn('DCS-SITE  DC DCS-1 (id %d): stored Received -> rule Partially '
                      'Received  lines recorded 1 / 3  UNRECORDED' % self.dc.pk, output)
        self.assertIn('Differences: 1.', output)

    def test_a_status_the_recorded_lines_never_gave_is_rule_drift(self):
        # The local DC 2 shape: every line recorded, short, yet stored Received.
        self.record(self.panels, '1')
        self.record(self.inverters, '1')
        self.record(self.cable, '1')
        self._force_status(DeliveryChallan.RECEIVED)
        self.assertEqual(challan_difference(self.dc, list(self.dc.line_items.all())),
                         (DeliveryChallan.PARTIALLY_RECEIVED, REASON_RULE_DRIFT))
        self.assertIn('lines recorded 3 / 3  RULE DRIFT', self.run_check())

    def test_the_check_writes_nothing(self):
        self.record(self.panels, '10')
        self._force_status(DeliveryChallan.RECEIVED)
        self.run_check()
        self.dc.refresh_from_db()
        self.assertEqual(self.dc.status, DeliveryChallan.RECEIVED)
