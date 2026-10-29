"""The GRN line tables on the delivery challan page, laid out for a phone. Session DG-1.

WHY THIS FILE EXISTS
--------------------
The SE GRN form and the SCM Edit GRN modal scrolled sideways on a 360px phone. Below
992px each line is now a stacked, labelled block (CSS on the `grn-lines` class in the
page's extra_head). The layout is CSS only and is NOT tested here — no browser runs in
this suite. What is pinned is the markup the CSS relies on and the one thing a phone
layout must never do: render a second copy of the inputs. Two inputs with one name both
post, and _read_grn_lines reads whichever request.POST.get() returns.

  * each received_qty_/damaged_qty_/grn_notes_ name appears exactly once in the form
    that carries it, and exactly once in the page;
  * both tables carry the grn-lines class;
  * every GRN input carries its aria-label, received/damaged their inputmode, and the
    type/step/min they had before this session;
  * the SE page has no override form and the SCM page no confirm form.

The POST behaviour is unchanged and stays pinned by tests_residential_baseline,
tests_grn_on_behalf and tests_number_input.

Run with:
    python manage.py test projects.tests_grn_form_layout --settings=solarpms.test_settings
"""
import re
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from .models import (
    DCLineItem, DeliveryChallan, Project, ProjectPhase, Task, UserProfile,
)

INPUT_TAG = re.compile(r'<input\b[^>]*>', re.DOTALL)

# What each GRN input must carry, transcribed rather than read from the template so a
# drifted attribute fails here. `None` means the attribute must be absent.
EXPECTED = {
    'received_qty_': {'type': 'number', 'step': 'any', 'min': '0', 'inputmode': 'decimal',
                      'aria-label': 'Received quantity'},
    'damaged_qty_':  {'type': 'number', 'step': '1', 'min': '0', 'inputmode': 'numeric',
                      'aria-label': 'Damaged quantity'},
    'grn_notes_':    {'type': 'text', 'step': None, 'min': None, 'inputmode': None,
                      'aria-label': 'GRN notes'},
}


def _profile(username, role):
    """Create a user and set their profile role. signals.py creates the UserProfile on
    post_save, so this UPDATES rather than creates (a second create breaks the OneToOne)."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title(), last_name='Test')
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.save(update_fields=['role'])
    return profile


def _attr(tag, name):
    match = re.search(rf'\s{re.escape(name)}="([^"]*)"', tag)
    return match.group(1) if match else None


class GrnFormLayoutTests(TestCase):
    """One project, a challan with THREE lines (so a per-line duplicate cannot hide
    behind a one-line fixture), the SE who works on the site, and SCM."""

    @classmethod
    def setUpTestData(cls):
        cls.scm = _profile('dg1_scm', 'SCM')
        cls.se = _profile('dg1_se', 'Site Engineer')
        cls.pm = _profile('dg1_pm', 'PM')

    def setUp(self):
        self.project = Project.objects.create(
            customer_name='DG-1 Customer', customer_phone='9876543210',
            site_address='3 Layout Lane', city='Lucknow',
            project_type='Residential', status='Active', assigned_pm=self.pm,
        )
        # The task is what makes can_confirm_grn true for the SE.
        phase = ProjectPhase.objects.create(
            project=self.project, phase_name='Execution', phase_order=1)
        Task.objects.create(phase=phase, task_name='Receive material', task_order=1,
                            assigned_to=self.se, assigned_role='Site Engineer')
        self.challan = DeliveryChallan.objects.create(
            project=self.project, dc_number='DC-DG1-001', dc_date=date.today(),
            status=DeliveryChallan.EXPECTED, created_by=self.scm,
        )
        self.lines = [
            DCLineItem.objects.create(
                challan=self.challan, boq_category=category, item_description=description,
                ordered_quantity=Decimal(qty), unit='Nos')
            for category, description, qty in (
                ('Solar Modules', 'Solar Module 540Wp', '10.00'),
                ('Inverter', 'String Inverter 50kW', '1.00'),
                ('Structure', 'GI mounting structure', '4.00'),
            )
        ]

    # -- helpers -------------------------------------------------------------

    def _page(self, actor):
        client = Client()
        client.force_login(actor.user)
        response = client.get(reverse('delivery_challan_detail',
                                      args=[self.project.project_id, self.challan.pk]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _form(self, page, url_name):
        """The <form> posting to `url_name`, from its opening tag to its </form>."""
        action = self._action(url_name)
        self.assertEqual(page.count(action), 1, f'expected one {url_name} form')
        start = page.rindex('<form', 0, page.index(action))
        return page[start:page.index('</form>', start)]

    def _grn_inputs(self, html):
        """Every input whose name is a GRN line field, as (prefix, tag)."""
        found = []
        for tag in INPUT_TAG.findall(html):
            name = _attr(tag, 'name') or ''
            for prefix in EXPECTED:
                if name.startswith(prefix):
                    found.append((prefix, tag))
        return found

    def _assert_each_name_once(self, page, form):
        for line in self.lines:
            for prefix in EXPECTED:
                needle = f'name="{prefix}{line.pk}"'
                with self.subTest(field=needle):
                    self.assertEqual(form.count(needle), 1)
                    self.assertEqual(page.count(needle), 1)

    def _assert_attributes(self, form):
        inputs = self._grn_inputs(form)
        self.assertEqual(len(inputs), 3 * len(self.lines))
        for prefix, tag in inputs:
            for attribute, expected in EXPECTED[prefix].items():
                with self.subTest(field=_attr(tag, 'name'), attribute=attribute):
                    self.assertEqual(_attr(tag, attribute), expected)

    # -- the SE form ---------------------------------------------------------

    def test_se_form_renders_each_input_name_exactly_once(self):
        page = self._page(self.se)
        self._assert_each_name_once(page, self._form(page, 'confirm_grn'))

    def test_se_form_inputs_carry_their_labels_and_unchanged_attributes(self):
        page = self._page(self.se)
        self._assert_attributes(self._form(page, 'confirm_grn'))

    def test_se_form_table_carries_the_phone_class(self):
        form = self._form(self._page(self.se), 'confirm_grn')
        self.assertEqual(form.count('class="table table-sm grn-lines"'), 1)

    def _action(self, url_name):
        # The whole attribute, quotes included: confirm_grn's URL (.../grn/) is a prefix
        # of override_grn's (.../grn/override/), so a bare URL search matches both.
        return f'action="{reverse(url_name, args=[self.project.project_id, self.challan.pk])}"'

    def test_se_page_has_no_override_form(self):
        self.assertNotIn(self._action('override_grn'), self._page(self.se))

    # -- the SCM modal -------------------------------------------------------

    def test_scm_modal_renders_each_input_name_exactly_once(self):
        page = self._page(self.scm)
        self._assert_each_name_once(page, self._form(page, 'override_grn'))

    def test_scm_modal_inputs_carry_their_labels_and_unchanged_attributes(self):
        page = self._page(self.scm)
        self._assert_attributes(self._form(page, 'override_grn'))

    def test_scm_modal_table_carries_the_phone_class(self):
        form = self._form(self._page(self.scm), 'override_grn')
        self.assertEqual(form.count('class="table table-sm grn-lines"'), 1)

    def test_scm_page_has_no_confirm_form(self):
        self.assertNotIn(self._action('confirm_grn'), self._page(self.scm))

    # -- the style block -----------------------------------------------------

    def test_the_phone_layout_style_is_in_the_page_head(self):
        """The CSS lives in extra_head, so it is in <head>, scoped to grn-lines and
        switched on below 992px."""
        page = self._page(self.se)
        head = page[:page.index('</head>')]
        self.assertIn('@media (max-width: 991.98px)', head)
        self.assertIn('.grn-lines thead { display: none; }', head)
