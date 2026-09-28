"""Numeric inputs behave the same way everywhere. Number-input session, 28 Sep 2026.

WHAT IS PINNED HERE
-------------------
  * `parse_decimal_input` — the one reader. It refuses a third decimal place, a
    negative, NaN/Infinity, text and anything outside [min, max]. It never rounds.
  * Every quantity view it was routed into: a two-decimal fractional value saves
    exactly, and a three-decimal value is refused with a message — not a 500, and
    not silently rounded by the column, which is what used to happen.
  * The template policy: every <input type="number"> in projects/templates carries an
    explicit `step`, so no input falls back to the browser default of step=1.
  * The wheel guard is loaded by all three page shells.

FIXTURES ARE BORROWED, not rebuilt: the GRN base (a challan with two lines and a site
engineer who holds a task), the O2 raise fixture (storage patched) and the Part 11 base
(the real OPEX catalogue) already set up exactly the state each view needs.

Run with:
    python manage.py test projects.tests_number_input --settings=solarpms.test_settings
"""
import os
import re
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.contrib.staticfiles import finders
from django.core.exceptions import ValidationError
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from .models import (
    BOQ, BOQItem, DCLineItem, DeliveryChallan, Project, VendorOrder,
    VendorOrderLine,
)
from .number_input import decimal_field_max, parse_decimal_input
from .tests_design_part11 import Part11Base
from .tests_grn_on_behalf import GrnOnBehalfBase, _client_for
from .tests_vendor_order_raise import RaiseFixture


def _messages(response):
    return ' '.join(str(m) for m in get_messages(response.wsgi_request))


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------

class ParseDecimalInputTests(SimpleTestCase):

    def parse(self, raw, **kwargs):
        kwargs.setdefault('places', 2)
        kwargs.setdefault('field_label', 'Cable qty')
        return parse_decimal_input(raw, **kwargs)

    def assertRefused(self, raw, fragment, **kwargs):
        with self.assertRaises(ValidationError) as caught:
            self.parse(raw, **kwargs)
        message = ' '.join(caught.exception.messages)
        self.assertIn('Cable qty', message, 'the message must name the field')
        self.assertIn(fragment, message)

    def test_a_valid_value_comes_back_as_the_exact_decimal(self):
        self.assertEqual(self.parse('38.40'), Decimal('38.40'))
        self.assertEqual(self.parse('  12 '), Decimal('12'))
        self.assertEqual(self.parse('0'), Decimal('0'))

    def test_trailing_zeros_do_not_count_as_decimal_places(self):
        self.assertEqual(self.parse('38.400'), Decimal('38.40'))
        self.assertEqual(self.parse('3.0', places=0), Decimal('3'))

    def test_empty_means_none(self):
        for raw in ('', '   ', None):
            with self.subTest(raw=raw):
                self.assertIsNone(self.parse(raw))

    def test_a_third_decimal_place_is_refused_not_rounded(self):
        self.assertRefused('38.405', 'more than 2 decimal places')

    def test_a_long_input_is_measured_before_any_context_rounding(self):
        # 29 significant digits: normalize()/quantize() would round this to 1 first.
        self.assertRefused('1.0000000000000000000000000001', 'decimal places')

    def test_places_zero_means_a_whole_number(self):
        self.assertRefused('2.5', 'whole number', places=0)

    def test_a_negative_is_refused(self):
        self.assertRefused('-1', 'cannot be negative', min_value=0)
        self.assertRefused('-0.01', 'cannot be negative', min_value=0)

    def test_negative_zero_is_stored_as_plain_zero(self):
        value = self.parse('-0.00', min_value=0)
        self.assertEqual(value, 0)
        self.assertFalse(value.is_signed())

    def test_nan_infinity_and_text_are_refused(self):
        for raw in ('NaN', 'nan', 'sNaN', 'Infinity', '-inf', 'abc', '1,000', '1.2.3'):
            with self.subTest(raw=raw):
                self.assertRefused(raw, 'is not a number')

    def test_min_and_max_are_inclusive_bounds(self):
        self.assertEqual(self.parse('0.01', min_value=Decimal('0.01')), Decimal('0.01'))
        self.assertRefused('0', 'must be at least 0.01', min_value=Decimal('0.01'))
        self.assertEqual(self.parse('99.99', max_value=Decimal('99.99')), Decimal('99.99'))
        self.assertRefused('100', 'larger than the maximum', max_value=Decimal('99.99'))

    def test_column_max_is_read_from_the_field(self):
        self.assertEqual(decimal_field_max(BOQItem, 'boq_quantity'), Decimal('99999999.99'))
        self.assertEqual(decimal_field_max(VendorOrderLine, 'quantity'),
                         Decimal('9999999999.99'))


# ---------------------------------------------------------------------------
# boq_detail — design (boq_qty_) and SCM (ord_qty_)
# ---------------------------------------------------------------------------

class BoqDetailQuantityTests(TestCase):
    """A Residential BOQ, its assigned designer, and an SCM user."""

    def setUp(self):
        self.designer = self._profile('ni_design', 'Design')
        self.scm = self._profile('ni_scm', 'SCM')
        self.project = Project.objects.create(
            customer_name='NI Customer', customer_phone='9876543210',
            site_address='1 Cable Road', city='Lucknow', project_type='Residential',
            status='Active', assigned_design=self.designer)
        self.boq = BOQ.objects.create(project=self.project, notes='before')
        self.item = BOQItem.objects.create(
            boq=self.boq, serial_no=1, category='BOS', description='DC Cable 4 sq mm',
            uom='Mtr', is_standard_item=True)

    @staticmethod
    def _profile(username, role):
        user = User.objects.create_user(username=username, password='x')
        profile = user.profile
        profile.role = role
        profile.is_active = True
        profile.save()
        return profile

    def _post(self, profile, data):
        client = Client(SERVER_NAME='localhost')
        client.force_login(profile.user)
        return client.post(reverse('boq_detail', args=[self.project.project_id]), data)

    def test_design_saves_a_two_decimal_quantity_exactly(self):
        response = self._post(self.designer, {'action': 'save_design', 'notes': 'after',
                                              f'boq_qty_{self.item.pk}': '38.40'})
        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.boq_quantity, Decimal('38.40'))

    def test_design_three_decimals_is_refused_and_nothing_is_saved(self):
        response = self._post(self.designer, {'action': 'save_design', 'notes': 'after',
                                              f'boq_qty_{self.item.pk}': '38.405'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('more than 2 decimal places', _messages(response))
        self.assertIn('DC Cable 4 sq mm', _messages(response))
        self.item.refresh_from_db()
        self.boq.refresh_from_db()
        self.assertIsNone(self.item.boq_quantity, 'not rounded to 38.41')
        self.assertEqual(self.boq.notes, 'before', 'the refusal refuses the whole save')

    def test_design_negative_is_refused(self):
        # It used to be stored as typed: Decimal('-5') passed straight through.
        self._post(self.designer, {'action': 'save_design',
                                   f'boq_qty_{self.item.pk}': '-5'})
        self.item.refresh_from_db()
        self.assertIsNone(self.item.boq_quantity)

    def test_scm_saves_a_two_decimal_ordered_quantity_exactly(self):
        self.boq.status = 'Submitted'
        self.boq.save(update_fields=['status'])
        response = self._post(self.scm, {'action': 'save_scm',
                                         f'ord_qty_{self.item.pk}': '12.75'})
        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.ordered_quantity, Decimal('12.75'))

    def test_scm_three_decimals_is_refused(self):
        self.boq.status = 'Submitted'
        self.boq.save(update_fields=['status'])
        response = self._post(self.scm, {'action': 'save_scm',
                                         f'ord_qty_{self.item.pk}': '12.755'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('more than 2 decimal places', _messages(response))
        self.item.refresh_from_db()
        self.assertIsNone(self.item.ordered_quantity)


# ---------------------------------------------------------------------------
# opex_boq_entry — the OPEX picker
# ---------------------------------------------------------------------------

class OpexBoqEntryQuantityTests(Part11Base):

    def test_a_two_decimal_quantity_saves_exactly(self):
        self._login(self.designer)
        response = self._save_sheet([('OPX-001', '38.40')])
        self.assertEqual(response.status_code, 302)
        row = BOQItem.objects.get(boq__project=self.site, item_master=self._master('OPX-001'))
        self.assertEqual(row.boq_quantity, Decimal('38.40'))

    def test_three_decimals_is_refused_and_the_whole_save_rolls_back(self):
        self._login(self.designer)
        response = self._save_sheet([('OPX-001', '5'), ('OPX-002', '38.405')])
        self.assertEqual(response.status_code, 302)
        self.assertIn('more than 2 decimal places', _messages(response))
        # The BOQ this save would have created is rolled back with its rows.
        self.assertFalse(BOQ.objects.filter(project=self.site).exists())
        self.assertFalse(BOQItem.objects.filter(boq__project=self.site).exists())

    def test_three_decimals_leaves_an_existing_sheet_untouched(self):
        self._login(self.designer)
        self._save_sheet([('OPX-001', '5')])
        self._save_sheet([('OPX-001', '6.125')])
        row = BOQItem.objects.get(boq__project=self.site, item_master=self._master('OPX-001'))
        self.assertEqual(row.boq_quantity, Decimal('5'))


# ---------------------------------------------------------------------------
# create_delivery_challan, confirm_grn, override_grn
# ---------------------------------------------------------------------------

class DeliveryQuantityTests(GrnOnBehalfBase):

    def _create_challan(self, qty):
        return _client_for(self.scm).post(
            reverse('create_delivery_challan', args=[self.project.project_id]),
            {'dc_number': 'DC-NI-1', 'dc_date': date.today().isoformat(),
             'expected_delivery_date': '',
             'line_item_category_0': 'Solar Modules',
             'line_item_description_0': 'DC Cable 4 sq mm',
             'line_item_qty_0': qty, 'line_item_unit_0': 'Mtr'})

    def _confirm(self, received, damaged='0'):
        return _client_for(self.se).post(
            reverse('confirm_grn', args=[self.project.project_id, self.challan.pk]),
            {f'received_qty_{self.line_a.pk}': received,
             f'damaged_qty_{self.line_a.pk}': damaged})

    # -- create_delivery_challan --

    def test_challan_line_saves_a_two_decimal_quantity_exactly(self):
        response = self._create_challan('12.50')
        self.assertEqual(response.status_code, 302)
        line = DCLineItem.objects.get(challan__dc_number='DC-NI-1')
        self.assertEqual(line.ordered_quantity, Decimal('12.50'))

    def test_challan_three_decimals_is_refused_and_no_challan_is_created(self):
        response = self._create_challan('12.505')
        self.assertEqual(response.status_code, 200, 'the form re-renders, not a 500')
        self.assertIn('more than 2 decimal places', response.content.decode())
        self.assertFalse(DeliveryChallan.objects.filter(dc_number='DC-NI-1').exists())

    def test_challan_zero_and_negative_are_refused(self):
        # _safe_decimal() used to turn "-5" into 5; zero silently dropped the row.
        for qty in ('0', '-5'):
            with self.subTest(qty=qty):
                self._create_challan(qty)
                self.assertFalse(DeliveryChallan.objects.filter(dc_number='DC-NI-1').exists())

    # -- confirm_grn --

    def test_confirm_grn_saves_a_two_decimal_quantity_exactly(self):
        response = self._confirm('7.25')
        self.assertEqual(response.status_code, 302)
        self.line_a.refresh_from_db()
        self.assertEqual(self.line_a.received_quantity, Decimal('7.25'))

    def test_confirm_grn_three_decimals_is_refused_and_nothing_is_written(self):
        before = self._transitions().count()
        response = self._confirm('7.255')
        self.assertEqual(response.status_code, 302)
        self.assertIn('more than 2 decimal places', _messages(response))
        self.line_a.refresh_from_db()
        self.assertIsNone(self.line_a.received_quantity)
        self.assertEqual(self._transitions().count(), before)

    def test_confirm_grn_negative_is_refused_not_made_positive(self):
        self._confirm('-5')
        self.line_a.refresh_from_db()
        self.assertIsNone(self.line_a.received_quantity)

    def test_confirm_grn_fractional_damage_is_refused_not_truncated(self):
        response = self._confirm('10', damaged='2.7')
        self.assertIn('whole number', _messages(response))
        self.line_a.refresh_from_db()
        self.assertIsNone(self.line_a.received_quantity)

    # -- override_grn --

    def test_override_saves_a_two_decimal_quantity_exactly(self):
        response = self._override({f'received_qty_{self.line_a.pk}': '9.75',
                                   f'damaged_qty_{self.line_a.pk}': '0',
                                   'grn_on_behalf_reason': 'Engineer off site'})
        self.assertEqual(response.status_code, 302)
        self.line_a.refresh_from_db()
        self.assertEqual(self.line_a.received_quantity, Decimal('9.75'))

    def test_override_three_decimals_is_refused_and_nothing_is_written(self):
        before = self._transitions().count()
        response = self._override({f'received_qty_{self.line_a.pk}': '9.755',
                                   f'damaged_qty_{self.line_a.pk}': '0',
                                   'grn_on_behalf_reason': 'Engineer off site'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('more than 2 decimal places', _messages(response))
        self.line_a.refresh_from_db()
        self.assertIsNone(self.line_a.received_quantity)
        self.assertEqual(self._transitions().count(), before)


# ---------------------------------------------------------------------------
# vendor_order_create
# ---------------------------------------------------------------------------

class VendorOrderQuantityTests(RaiseFixture):

    def test_a_two_decimal_quantity_saves_exactly(self):
        response, _ = self.post(**{f'qty_{self.item_module.pk}': '10.25'})
        self.assertEqual(response.status_code, 302)
        line = VendorOrderLine.objects.get(boq_item=self.item_module)
        self.assertEqual(line.quantity, Decimal('10.25'))

    def test_three_decimals_is_refused_with_a_message(self):
        response, storage = self.post(**{f'qty_{self.item_module.pk}': '10.255'})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'more than 2 decimal places', status_code=400)
        self.assertEqual(VendorOrder.objects.count(), 0)
        storage.storage.from_.return_value.upload.assert_not_called()


# ---------------------------------------------------------------------------
# Template policy and the page shells
# ---------------------------------------------------------------------------

TEMPLATE_ROOT = os.path.join(settings.BASE_DIR, 'projects', 'templates')

# Any <input ...> whose type is number, including those built as JS string literals
# ('<input type="number" ' + ...), which is why the quote may be escaped or single.
NUMBER_INPUT = re.compile(r'<input\b[^>]*?type=\\?["\']number\\?["\'][^>]*>', re.S)

# project_detail.html is dead — no view renders it — and this session's MODE forbids
# editing it. Excluded by name so the exclusion is visible; see DEFERRED §G27.
EXCLUDED_TEMPLATES = {'project_detail.html'}

SHELLS = (
    os.path.join(TEMPLATE_ROOT, 'base.html'),
    os.path.join(TEMPLATE_ROOT, 'projects', 'admin', 'admin_base.html'),
    os.path.join(TEMPLATE_ROOT, 'projects', 'subadmin', 'subadmin_base.html'),
)
GUARD_PATH = 'projects/js/number_input_guard.js'
GUARD_SCRIPT = "<script src=\"{% static '" + GUARD_PATH + "' %}\"></script>"


class NumberInputTemplatePolicyTests(SimpleTestCase):

    def test_every_number_input_carries_an_explicit_step(self):
        """Without `step` the browser uses step=1, and a fractional value then fails
        the browser's own validation, which blocks the whole form."""
        missing, seen = [], 0
        for root, _, files in os.walk(TEMPLATE_ROOT):
            for name in files:
                if not name.endswith('.html') or name in EXCLUDED_TEMPLATES:
                    continue
                path = os.path.join(root, name)
                with open(path, encoding='utf-8') as handle:
                    source = handle.read()
                for match in NUMBER_INPUT.finditer(source):
                    seen += 1
                    if not re.search(r'\bstep=', match.group(0)):
                        line = source[:match.start()].count('\n') + 1
                        missing.append(f'{os.path.relpath(path, TEMPLATE_ROOT)}:{line}')
        self.assertGreater(seen, 20, 'the scan found too few inputs to be trusted')
        self.assertEqual(missing, [], 'number inputs with no step attribute')

    def test_the_quantity_inputs_use_step_any(self):
        expected = {
            'projects/boq_detail.html': ('boq_qty_', 'ord_qty_'),
            'projects/delivery_challan_create.html': ('line_item_qty_',),
            'projects/delivery_challan_detail.html': ('received_qty_',),
            'projects/vendor_order_form.html': ('name="qty_',),
        }
        for relative, names in expected.items():
            with open(os.path.join(TEMPLATE_ROOT, relative), encoding='utf-8') as handle:
                tags = NUMBER_INPUT.findall(handle.read())
            for name in names:
                matching = [tag for tag in tags if name in tag]
                with self.subTest(template=relative, field=name):
                    self.assertTrue(matching)
                    for tag in matching:
                        self.assertIn('step="any"', tag)

    def test_all_three_shells_include_the_guard_script(self):
        """Through {% static %}, so the URL follows STATIC_URL and any manifest storage
        instead of a hardcoded /static/ path — which is why each shell must load it."""
        for path in SHELLS:
            with self.subTest(shell=os.path.basename(path)):
                with open(path, encoding='utf-8') as handle:
                    source = handle.read()
                self.assertEqual(source.count(GUARD_SCRIPT), 1)
                self.assertIn('{% load static %}', source)
                self.assertLess(source.index('{% load static %}'), source.index(GUARD_SCRIPT))

    def test_the_static_finders_resolve_the_guard_script(self):
        """The path the shells name is one collectstatic will find and ship. A typo in
        the {% static %} argument would otherwise surface only as a 404 in the browser."""
        path = finders.find(GUARD_PATH)
        self.assertIsNotNone(path, f'{GUARD_PATH} is not found by the static finders')
        self.assertTrue(os.path.isfile(path))

    def test_the_guard_script_has_one_delegated_listener(self):
        path = finders.find(GUARD_PATH)
        with open(path, encoding='utf-8') as handle:
            source = handle.read()
        self.assertEqual(source.count('addEventListener'), 1)
        self.assertIn("document.addEventListener('wheel'", source)
        self.assertIn('.blur()', source)
        self.assertNotIn('preventDefault()', source.split('*/', 1)[1])
