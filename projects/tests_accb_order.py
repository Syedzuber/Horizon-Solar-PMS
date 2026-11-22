"""
B-3b verification — configured ACCBs are orderable from the SCM group order page (and,
through the same code, from the purchases workspace's Add PO / PI).

WHAT THIS FILE PROVES
---------------------
  * ONE ORDERABLE ROW PER CONFIGURATION. Two configurations in one group are two rows and
    raise two order lines, each with its generated description, its accb_config_key and
    its summed count; the same configuration on two sites (feeders in a different order)
    is one row of 2.
  * THE SERVER TRUSTS ONLY THE AMOUNT. Description and count are rebuilt from the BOQ; a
    posted description or count is ignored. A priced amount under an ACCB field that is not
    in the submitted sites' requirement is refused with nothing saved; a blank one is
    ignored. A bad ACCB amount is refused.
  * CATALOGUE ORDERS ARE UNCHANGED by ACCBs being present.
  * THE ONE READER OF ORDER LINES (the order detail page, B-3b pre-flight P3) keeps two
    ACCB lines apart.
  * REVISION SNAPSHOTS CARRY THE ACCB TEXT (and only the text — Zuber, 22 Nov).
  * THE RAISE PAGE STAYS AT 14 QUERIES with ACCB rows orderable.

Run with:
    python manage.py test projects.tests_accb_order --settings=solarpms.test_settings
"""
import json
import re
from decimal import Decimal
from unittest.mock import patch

from django.urls import reverse

from .accb import ACCB_CONFIGURED_CODE, accb_config_key
from .models import BOQ, BOQItemMaster, VendorOrder, VendorOrderLine
from .order_views import (
    ACCB_GONE_MESSAGE, _parse_requirement, _picker_context, accb_amount_field,
)
from .tests_accb_config import EX_TYPE1, EX_TYPE1_TEXT, EX_TYPE2, EX_TYPE2_TEXT, cfg
from .tests_purchases_workspace import WorkspaceFixture
from .tests_vendor_order_group import _Capture
from .tests_vendor_order_raise import _assert_keyed_redirect, _client
from .views import _accb_create, _boq_snapshot

#: EX_TYPE2 with its incomers entered the other way round: the same thing to buy.
EX_TYPE2_REORDERED = cfg('Type-2', True, [('MCCB', 63), ('MCCB', 100)], [('MCCB', 150)])

KEY1 = accb_config_key(EX_TYPE1)
KEY2 = accb_config_key(EX_TYPE2)
FIELD1 = accb_amount_field(KEY1)
FIELD2 = accb_amount_field(KEY2)


class ACCBOrderFixture(WorkspaceFixture):
    """GroupRaiseFixture's tenders, groups and sites (via WorkspaceFixture, so Add PO / PI
    can be posted too), plus the OPX-273 catalogue row. Migrations are off in the test
    settings, so the row migration 0149 seeds is created here."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.accb_master = BOQItemMaster.objects.create(
            code=ACCB_CONFIGURED_CODE, description='ACCB (configured)', unit='Nos',
            category='ACCB', project_type='OPEX', sort_order=273)

    def accb(self, site, config):
        """An ACCB written by the designer view's own create helper."""
        return _accb_create(BOQ.objects.get(project=site), self.accb_master, config,
                            self.designer)

    def seed_two_configurations(self):
        """A1: Type-2. A2: the same Type-2 in a different feeder order, and a Type-1."""
        self.accb(self.a1, EX_TYPE2)
        self.accb(self.a2, EX_TYPE2_REORDERED)
        self.accb(self.a2, EX_TYPE1)

    def raise_a1_a2(self, **kwargs):
        return self.post(sites=[(self.a1, self.locked_a), (self.a2, self.locked_a)],
                         programs=[self.tender], **kwargs)

    def accb_lines(self, order=None):
        lines = VendorOrderLine.objects.filter(accb_config_key__isnull=False)
        if order is not None:
            lines = lines.filter(order=order)
        return {line.accb_config_key: line for line in lines}


class ACCBRequirementRowTests(ACCBOrderFixture):

    def test_two_configurations_are_two_orderable_rows(self):
        self.seed_two_configurations()
        rows = {row['description']: row for row in
                _picker_context(self.tender, {})['accb_requirement']}
        self.assertEqual(set(rows), {EX_TYPE1_TEXT, EX_TYPE2_TEXT})
        self.assertEqual(rows[EX_TYPE1_TEXT]['field'], FIELD1)
        self.assertEqual(rows[EX_TYPE2_TEXT]['field'], FIELD2)
        self.assertNotEqual(FIELD1, FIELD2)
        # Safe in a field name: the prefix and 16 hex digits, whatever the key holds.
        for field in (FIELD1, FIELD2):
            self.assertRegex(field, r'^accb_amount_[0-9a-f]{16}$')

    def test_the_same_configuration_on_two_sites_is_one_row_required_2(self):
        self.accb(self.a1, EX_TYPE2)
        self.accb(self.a2, EX_TYPE2_REORDERED)
        rows = _picker_context(self.tender, {})['accb_requirement']
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]['contrib']),
                         {str(self.a1.pk): '1.00', str(self.a2.pk): '1.00'})
        errors = []
        lines, _ = _parse_requirement({}, [self.a1.pk, self.a2.pk], errors)
        self.assertEqual(errors, [])
        accb = [line for line in lines if line['accb_config_key']]
        self.assertEqual(len(accb), 1)
        self.assertEqual(accb[0]['quantity'], Decimal('2'))
        self.assertEqual(accb[0]['accb_config_key'], KEY2)

    def test_the_page_draws_accb_rows_in_the_requirement_table_with_their_own_field(self):
        self.seed_two_configurations()
        response = _client(self.scm).get(self.url(self.tender), follow=True)
        html = response.content.decode()
        table = html.split('id="voRequirement"', 1)[1].split('</table>', 1)[0]
        for field in (FIELD1, FIELD2):
            self.assertIn(f'name="{field}"', table)
        self.assertEqual(len(re.findall(r'<tr class="js-vo-req js-vo-accb d-none" '
                                        r'data-whole="1"', table)), 2)
        # The OPX-273 catalogue field is never drawn: it would be one field for every ACCB.
        self.assertNotIn(f'name="amount_{self.accb_master.pk}"', html)
        self.assertNotIn('voAccbCard', html)

    def test_two_configurations_that_share_a_field_name_refuse_rather_than_guess(self):
        self.seed_two_configurations()
        errors = []
        with patch('projects.order_views.accb_amount_field', return_value='accb_amount_x'):
            lines, priced = _parse_requirement({'accb_amount_x': '10'},
                                               [self.a1.pk, self.a2.pk], errors)
        self.assertEqual((lines, priced), ([], None))
        self.assertEqual(len(errors), 1)
        self.assertIn('could not be told apart', errors[0])


class ACCBRaiseTests(ACCBOrderFixture):

    def test_raising_two_configurations_gives_two_order_lines(self):
        self.seed_two_configurations()
        response, _ = self.raise_a1_a2(
            order_total='1000000',
            amounts={self.opex_module: '600000'},
            **{FIELD1: '40000', FIELD2: '90000'})
        order = VendorOrder.objects.get()
        self.assertRedirects(response, reverse('vendor_order_detail', args=[order.pk]),
                             fetch_redirect_response=False)
        accb = self.accb_lines(order)
        self.assertEqual(set(accb), {KEY1, KEY2})
        type2, type1 = accb[KEY2], accb[KEY1]
        self.assertEqual((type2.item_description, type2.quantity, type2.amount),
                         (EX_TYPE2_TEXT, Decimal('2'), Decimal('90000')))
        self.assertEqual((type1.item_description, type1.quantity, type1.amount),
                         (EX_TYPE1_TEXT, Decimal('1'), Decimal('40000')))
        for line in (type1, type2):
            self.assertEqual(line.item_master, self.accb_master)
            self.assertEqual((line.item_code, line.item_unit, line.item_category),
                             (ACCB_CONFIGURED_CODE, 'Nos', 'ACCB'))
            self.assertIsNone(line.boq_item)
        # The two catalogue lines are there as before, with no configuration.
        catalogue = order.lines.filter(accb_config_key__isnull=True)
        self.assertEqual(sorted(line.item_code for line in catalogue), ['OPX-901', 'OPX-902'])
        self.assertEqual(order.lines.count(), 4)

    def test_an_unpriced_accb_is_still_an_order_line(self):
        self.accb(self.a1, EX_TYPE1)
        self.raise_a1_a2()
        line = self.accb_lines()[KEY1]
        self.assertIsNone(line.amount)
        self.assertEqual(line.quantity, Decimal('1'))

    def test_a_posted_description_or_count_is_ignored(self):
        self.accb(self.a1, EX_TYPE2)
        self.accb(self.a2, EX_TYPE2_REORDERED)
        self.raise_a1_a2(**{FIELD2: '5000',
                            f'qty_{FIELD2}': '999', f'description_{FIELD2}': 'Cheap ACCB',
                            'accb_config_key': 'Type-9|SPD|IN:|OUT:',
                            f'qty_{self.accb_master.pk}': '999'})
        line = self.accb_lines()[KEY2]
        self.assertEqual(line.item_description, EX_TYPE2_TEXT)
        self.assertEqual(line.quantity, Decimal('2'))
        self.assertEqual(line.accb_config_key, KEY2)

    def test_a_priced_accb_not_on_the_submitted_sites_is_refused_with_nothing_saved(self):
        # The Type-1 is on A3, which is not submitted: its field is a real field name,
        # just not one of this requirement's (an ACCB removed after the page was drawn
        # looks exactly like this).
        self.accb(self.a1, EX_TYPE2)
        self.accb(self.a3, EX_TYPE1)
        response, storage = self.raise_a1_a2(**{FIELD2: '5000', FIELD1: '7000'})
        self.assertEqual(response.status_code, 400)
        self.assertIn(ACCB_GONE_MESSAGE, response.content.decode())
        self.assertNothingCreated()
        storage.storage.from_.return_value.upload.assert_not_called()

    def test_a_crafted_accb_field_is_refused_with_nothing_saved(self):
        response, _ = self.raise_a1_a2(**{'accb_amount_0123456789abcdef': '1'})
        self.assertEqual(response.status_code, 400)
        self.assertIn(ACCB_GONE_MESSAGE, response.content.decode())
        self.assertNothingCreated()

    def test_a_blank_accb_field_not_in_the_requirement_is_ignored(self):
        self.accb(self.a3, EX_TYPE1)
        self.raise_a1_a2(**{FIELD1: '', 'accb_amount_0123456789abcdef': '  '})
        order = VendorOrder.objects.get()
        self.assertEqual(self.accb_lines(order), {})
        self.assertEqual(order.lines.count(), 2)

    def test_a_bad_accb_amount_is_refused(self):
        self.accb(self.a1, EX_TYPE1)
        response, _ = self.raise_a1_a2(**{FIELD1: '-5'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('an item amount must be more than 0, or left blank.',
                      response.content.decode())
        self.assertNothingCreated()

    def test_a_refused_submission_gives_the_accb_amount_back(self):
        self.accb(self.a1, EX_TYPE1)
        response, _ = self.raise_a1_a2(payment_amount=None, **{FIELD1: '4321'})
        self.assertEqual(response.status_code, 400)
        html = response.content.decode()
        self.assertIn(f'name="{FIELD1}"', html)
        self.assertIn('value="4321"', html)
        self.assertRegex(html, r'<details class="card[^"]*" id="voRequirement" open>')

    def test_catalogue_lines_are_unchanged_by_accbs_being_present(self):
        amounts = {self.opex_module: '600000', self.opex_inverter: '100000'}
        self.raise_a1_a2(amounts=amounts, po_number='PO-BEFORE')
        self.seed_two_configurations()
        self.raise_a1_a2(amounts=amounts, po_number='PO-AFTER')

        def catalogue(po):
            return [(line.item_master_id, line.item_code, line.item_description,
                     line.item_unit, line.item_category, line.quantity, line.amount,
                     line.accb_config_key)
                    for line in VendorOrderLine.objects.filter(
                        order__po_number=po, accb_config_key__isnull=True)]

        self.assertEqual(catalogue('PO-AFTER'), catalogue('PO-BEFORE'))
        self.assertEqual(len(catalogue('PO-BEFORE')), 2)
        self.assertEqual(VendorOrder.objects.get(po_number='PO-BEFORE').lines.count(), 2)
        self.assertEqual(VendorOrder.objects.get(po_number='PO-AFTER').lines.count(), 4)
        self.assertEqual(VendorOrder.objects.get(po_number='PO-AFTER').total_amount,
                         VendorOrder.objects.get(po_number='PO-BEFORE').total_amount)

    def test_add_po_pi_in_the_purchases_workspace_orders_accbs_too(self):
        """The workspace shares _parse_requirement and the requirement partial (Zuber,
        22 Nov: yes, through the shared code, no flag)."""
        self.accb(self.a1, EX_TYPE1)
        response = _client(self.scm).get(reverse('purchases_new'), follow=True)
        self.assertIn(f'name="{FIELD1}"', response.content.decode())
        self.post_new(sites=[(self.a1, self.locked_a)], programs=[self.tender],
                      project_type='OPEX', **{FIELD1: '3000'})
        line = self.accb_lines()[KEY1]
        self.assertEqual((line.item_description, line.quantity, line.amount),
                         (EX_TYPE1_TEXT, Decimal('1'), Decimal('3000')))


class ACCBOrderReaderTests(ACCBOrderFixture):
    """Pre-flight P3: the order detail page is the only reader of order lines. It must
    show two ACCB lines with different configurations as two rows."""

    def test_the_order_detail_page_shows_two_accb_lines_apart(self):
        self.seed_two_configurations()
        self.raise_a1_a2(**{FIELD1: '40000', FIELD2: '90000'})
        order = VendorOrder.objects.get()
        response = _client(self.scm).get(reverse('vendor_order_detail', args=[order.pk]))
        self.assertEqual(response.status_code, 200)
        accb = [line for line in response.context['lines'] if line.accb_config_key]
        self.assertEqual(sorted(line.accb_config_key for line in accb), sorted([KEY1, KEY2]))
        html = response.content.decode()
        self.assertEqual(html.count(EX_TYPE1_TEXT), 1)
        self.assertEqual(html.count(EX_TYPE2_TEXT), 1)
        self.assertIn('2.00 Nos', html)
        self.assertIn('90000.00', html)
        self.assertIn('40000.00', html)


class ACCBSnapshotTests(ACCBOrderFixture):

    def test_a_revision_snapshot_carries_the_accb_text_only(self):
        """Zuber, 22 Nov: snapshots keep the ACCB TEXT, not the configuration.
        _boq_snapshot() is what every BOQRevision writer stores."""
        self.accb(self.a1, EX_TYPE2)
        rows = _boq_snapshot(BOQ.objects.get(project=self.a1))
        accb = [row for row in rows if row['description'] == EX_TYPE2_TEXT]
        self.assertEqual(len(accb), 1)
        self.assertEqual(accb[0]['uom'], 'Nos')
        self.assertEqual(accb[0]['boq_quantity'], 1.0)
        # Text only: no configuration, key or feeder data rides along.
        for row in rows:
            self.assertFalse({'feeders', 'config_key', 'accb_config', 'accb_type'} & set(row))


class ACCBRaisePageCostTests(ACCBOrderFixture):

    def test_the_raise_page_stays_at_14_queries_with_accb_rows_orderable(self):
        self.seed_two_configurations()
        client = _client(self.scm)
        opened = client.get(self.url(self.tender), follow=True)
        _assert_keyed_redirect(self, opened, reverse('vendor_order_create_group'))
        keyed = opened.redirect_chain[0][0]
        with _Capture(self) as page:
            response = client.get(keyed)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['accb_requirement']), 2)
        # The same 14 tests_vendor_order_group pins without ACCBs: the ACCB rows come out
        # of aggregate_group_boq()'s existing per-site query.
        self.assertEqual(page.count, 14)
