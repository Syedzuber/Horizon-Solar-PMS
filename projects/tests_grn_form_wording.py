"""
GRN-P2 — the Site Engineer GRN form's wording, its no-prices tick, and its empty boxes.

  W1/Q1  the SE tick is RENDERED VISIBLE; a page script hides it while the server's
         delivery-photo exemption would apply (progressive enhancement). The server rule
         (goods_receipts.plan_receipt_documents) is unchanged and still decides; its own
         tests are tests_grn_photo_rule and tests_receipt_attachments.
  W2     tick label "No prices or amounts are visible in these files."
  W3     help line naming SCM, PM and Finance.
  W4     a never-recorded line renders Received EMPTY with no placeholder, and Damaged
         EMPTY with placeholder "0"; a recorded line shows its stored values.
  W5     the SCM override form is unchanged.
  Q2     the shared refusal message names no label.

The script itself is not executed here (no browser in a test): these tests pin the markup
it needs and the values it mirrors from the server.
"""
import re
from html.parser import HTMLParser

from django.urls import reverse

from . import goods_receipts
from .goods_receipts import RECEIPT_DOCUMENT_PHOTO, RECEIPT_DOCUMENTS_NO_PRICES
from .models import DCLineItem
from .tests_grn_damage import GrnDamageBase

W2_LABEL = 'No prices or amounts are visible in these files.'
W3_HELP = ('Signed challan copy, LR/bilty or delivery photo. SCM, PM and Finance can see '
           "these files — don't attach anything showing prices.")
OLD_LABEL = ('These documents show no rates, values or amounts. Site Engineers and Finance '
             'will see them.')
OLD_HELP = ('Signed challan copy, LR/bilty or delivery photo. Site Engineers and Finance will '
            'see this file — no rates or amounts.')


class _Inputs(HTMLParser):
    """{name: attrs} for every <input>, and the attrs of each element carrying
    data-grn-no-prices."""

    def __init__(self):
        super().__init__()
        self.inputs, self.tick_wrappers = {}, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('name'):
            self.inputs[attrs['name']] = attrs
        if 'data-grn-no-prices' in attrs:
            self.tick_wrappers.append(attrs)


def _parse(html):
    parser = _Inputs()
    parser.feed(html)
    return parser


class GrnFormWordingBase(GrnDamageBase):

    def page(self, profile):
        self.login(profile)
        response = self.client.get(reverse('delivery_challan_detail',
                                           args=[self.site.project_id, self.dc.pk]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()


class TickTests(GrnFormWordingBase):

    def test_the_se_tick_is_rendered_visible_with_its_hook_and_w2_label(self):
        page = self.page(self.se)
        parsed = _parse(page)
        self.assertEqual(len(parsed.tick_wrappers), 1)          # SE form only
        self.assertNotIn('d-none', parsed.tick_wrappers[0].get('class', '').split())
        tick = parsed.inputs['receipt_docs_no_prices']
        self.assertEqual((tick['type'], tick['value'], tick['id']),
                         ('checkbox', '1', 'grnDocsNoPricesC'))
        self.assertIn(f'<label class="form-check-label small" for="grnDocsNoPricesC">{W2_LABEL}</label>',
                      page)
        self.assertNotIn(OLD_LABEL, page)

    def test_the_script_hook_is_present_and_mirrors_the_server(self):
        page = self.page(self.se)
        self.assertIn("document.querySelectorAll('[data-grn-no-prices]')", page)
        self.assertIn("tick.classList.toggle('d-none', exempt(docs));", page)
        self.assertIn("docs.addEventListener('change', refresh);", page)
        # The two values the script copies from the server must stay equal to it.
        self.assertIn(f"var PHOTO = '{RECEIPT_DOCUMENT_PHOTO}';", page)
        regex = re.search(r'var IMAGE_NAME = /(.+?)/i;', page).group(1)
        for ext in goods_receipts._IMAGE_EXTENSIONS:
            with self.subTest(ext=ext):
                self.assertRegex('photo' + ext.upper(), re.compile(regex, re.IGNORECASE))
        self.assertNotRegex('scan.pdf', re.compile(regex, re.IGNORECASE))

    def test_the_w3_help_line(self):
        page = self.page(self.se)
        self.assertIn(W3_HELP, page)
        self.assertNotIn(OLD_HELP, page)

    def test_the_scm_override_form_is_unchanged(self):
        page = self.page(self.scm)
        self.assertIn(f'<label class="form-check-label small" for="grnDocsNoPricesO">{OLD_LABEL}</label>',
                      page)
        self.assertIn(OLD_HELP, page)
        self.assertEqual(_parse(page).tick_wrappers, [])        # no hiding on the override
        self.assertEqual(_parse(page).inputs[f'received_qty_{self.inverter.pk}'].get('placeholder'), '0')

    def test_the_shared_refusal_names_no_label(self):
        self.assertEqual(RECEIPT_DOCUMENTS_NO_PRICES,
                         'Tick the box confirming no prices or amounts are visible, to attach '
                         'these documents.')


class EmptyBoxTests(GrnFormWordingBase):

    def boxes(self, line):
        inputs = _parse(self.page(self.se)).inputs
        return inputs[f'received_qty_{line.pk}'], inputs[f'damaged_qty_{line.pk}']

    def test_a_never_recorded_line_renders_empty_boxes(self):
        received, damaged = self.boxes(self.inverter)
        self.assertEqual(received.get('value'), '')
        self.assertNotIn('placeholder', received)               # no grey "0" either
        self.assertEqual(damaged.get('value'), '')
        self.assertEqual(damaged.get('placeholder'), '0')       # a placeholder, never a value
        self.assertEqual(damaged.get('data-grn-initial'), '0')  # the damage script's baseline

    def test_a_recorded_line_shows_its_stored_values(self):
        self.confirm({self.panels: ('10', '0', [])})
        self.panels.refresh_from_db()
        self.assertEqual(self.panels.received_quantity, 10)
        received, damaged = self.boxes(self.panels)
        self.assertEqual(received.get('value'), '10.00')
        self.assertEqual(damaged.get('value'), '0')             # stored 0 is real data
        received, damaged = self.boxes(self.inverter)           # still never recorded
        self.assertEqual((received.get('value'), damaged.get('value')), ('', ''))

    def test_submitting_the_empty_boxes_records_nothing(self):
        response = self.confirm({self.panels: ('', '', []), self.inverter: ('', '', [])})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(DCLineItem.objects.filter(challan=self.dc,
                                                   received_quantity__isnull=False).exists())
