"""
B-0 — catalogue hygiene: safe lengths, units from the one shared list, no unit change
after use, and before/after values in the catalogue edit log.

What these tests pin:
- the catalogue unit is a dropdown of projects/units.py UNITS, and category / unit fit a
  site BOQ line's columns (so a catalogue value can no longer crash a BOQ save);
- an old spelling ('No.', 'Mtr', 'LOT') opens as its list code and saves as it, without
  tripping the unit lock;
- an item already on a BOQ line cannot change its unit (ruling 12), on both screens;
  description edits still work;
- every catalogue add, edit, retire and reactivate logs only the changed fields, old → new;
- normalise_catalogue_units: dry run writes nothing; --apply rewrites only mapped
  spellings, logs each, skips OPX-038, never touches BOQ lines, and is idempotent.
"""
from decimal import Decimal
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from .forms import UNIT_LOCKED_MESSAGE, BOQItemMasterForm
from .models import BOQ, ActivityLog, BOQItem, BOQItemMaster, Project
from .units import UNIT_CODES

LINE_CATEGORY_MAX = BOQItem._meta.get_field('category').max_length
LINE_UOM_MAX = BOQItem._meta.get_field('uom').max_length


def _profile(username, role, is_design_head=False):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.is_design_head = is_design_head
    profile.save()
    return profile


def _post_data(item=None, **overrides):
    """A complete edit POST for `item` (or a blank create POST), with overrides."""
    data = {
        'description':  item.description if item else 'New item',
        'unit':         item.unit if item else 'Nos',
        'category':     item.category if item else 'BOS',
        'sort_order':   str(item.sort_order) if item else '1',
        'is_active':    'on',
    }
    if item:
        # Both edit views delete `code`, so they ignore this; a form built directly in a
        # test still has the field and needs it.
        data['code'] = item.code
        if not item.is_active:
            data.pop('is_active')
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


class CatalogueBase(TestCase):
    def setUp(self):
        self.admin = _profile('admin_b0', 'Admin')
        self.head = _profile('head_b0', 'Design', is_design_head=True)
        self.pm = _profile('pm_b0', 'PM')
        self.site = Project(
            project_id='B0-SITE', customer_name='B0 Client', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX', site_code='B0SITE',
            dc_capacity_kw=Decimal('100.00'), status='Draft', assigned_pm=self.pm)
        self.site.save()
        self.boq = BOQ.objects.create(project=self.site)

    def _item(self, code, unit, project_type='OPEX', description=None, category='BOS'):
        return BOQItemMaster.objects.create(
            code=code, project_type=project_type, unit=unit, category=category,
            description=description or f'{code} description', sort_order=1)

    def _use(self, item, uom=None):
        """Put `item` on a BOQ line, copying category and unit as the line paths do."""
        return BOQItem.objects.create(
            boq=self.boq, item_master=item, serial_no=item.sort_order,
            category=item.category, description=item.description,
            uom=uom or item.unit, boq_quantity=Decimal('3'))

    def _login(self, profile):
        self.assertTrue(self.client.login(username=profile.user.username, password='x'))

    def _logs(self, item):
        return list(ActivityLog.objects
                    .filter(entity_type='BOQItemMaster', entity_id=item.pk)
                    .order_by('pk').values_list('action', flat=True))

    def _head_edit(self, item, **overrides):
        self._login(self.head)
        return self.client.post(reverse('design_boq_catalogue_edit', args=[item.pk]),
                                _post_data(item, **overrides))


# ===========================================================================
# T1 — the unit list and the lengths
# ===========================================================================

class FormRuleTests(CatalogueBase):

    def _create_form(self, **overrides):
        data = _post_data(code='OPX-900', project_type='OPEX', **overrides)
        return BOQItemMasterForm(data)

    def test_the_unit_is_a_dropdown_of_the_shared_list(self):
        form = BOQItemMasterForm()
        offered = [value for value, _label in form.fields['unit'].choices if value]
        self.assertEqual(offered, list(UNIT_CODES))

    def test_only_list_codes_are_accepted(self):
        self.assertTrue(self._create_form(unit='KWp').is_valid())
        for value in ('Mtr', 'No.', 'Each', 'Kilometres1', ''):
            form = self._create_form(unit=value)
            self.assertFalse(form.is_valid(), value)
            self.assertEqual(form.errors['unit'], ['Choose a unit from the list.'], value)

    def test_every_list_code_fits_a_boq_line(self):
        for code in UNIT_CODES:
            self.assertLessEqual(len(code), LINE_UOM_MAX, code)

    def test_a_category_longer_than_a_boq_line_holds_is_refused(self):
        self.assertTrue(self._create_form(category='C' * LINE_CATEGORY_MAX).is_valid())
        form = self._create_form(category='C' * (LINE_CATEGORY_MAX + 1))
        self.assertFalse(form.is_valid())
        self.assertIn(f'at most {LINE_CATEGORY_MAX} characters', form.errors['category'][0])

    def test_a_boq_save_at_the_maximum_lengths_succeeds(self):
        """The longest values the form accepts fit the line columns the line paths copy
        them into. SQLite does not enforce varchar lengths, so the column validators are
        run explicitly; the same values are proved on Postgres in the B-0 report."""
        longest_unit = max(UNIT_CODES, key=len)
        form = self._create_form(category='C' * LINE_CATEGORY_MAX, unit=longest_unit)
        self.assertTrue(form.is_valid(), form.errors)
        item = form.save()
        line = self._use(item)
        BOQItem._meta.get_field('category').run_validators(line.category)
        BOQItem._meta.get_field('uom').run_validators(line.uom)
        line.refresh_from_db()
        self.assertEqual((line.category, line.uom), ('C' * LINE_CATEGORY_MAX, longest_unit))

    def test_an_old_spelling_opens_as_its_list_code(self):
        for stored, shown in (('No.', 'Nos'), ('No', 'Nos'), ('Mtr', 'Meter'),
                              ('LOT', 'Lot'), ('KWp', 'KWp')):
            item = self._item(f'OPX-{stored.replace(".", "D")}', stored)
            self.assertEqual(BOQItemMasterForm(instance=item).initial['unit'], shown, stored)

    def test_a_unit_with_no_mapping_opens_unselected_and_must_be_set(self):
        item = self._item('OPX-901', 'Each')
        self.assertEqual(BOQItemMasterForm(instance=item).initial['unit'], '')
        form = BOQItemMasterForm(_post_data(item, unit='', project_type='OPEX'),
                                 instance=item)
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors['unit'], ['Choose a unit from the list.'])
        form = BOQItemMasterForm(_post_data(item, unit='Set', project_type='OPEX'),
                                 instance=item)
        self.assertTrue(form.is_valid(), form.errors)


# ===========================================================================
# T2 — no unit change after use (ruling 12)
# ===========================================================================

class UnitLockTests(CatalogueBase):

    def test_a_real_unit_change_on_an_item_in_use_is_refused(self):
        item = self._item('OPX-910', 'Nos')
        self._use(item)
        response = self._head_edit(item, unit='Set')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "its unit can&#x27;t change")
        item.refresh_from_db()
        self.assertEqual(item.unit, 'Nos')

    def test_a_unit_change_on_an_unused_item_is_allowed(self):
        item = self._item('OPX-911', 'Nos')
        response = self._head_edit(item, unit='Set')
        self.assertRedirects(response, reverse('design_boq_catalogue'),
                             fetch_redirect_response=False)
        item.refresh_from_db()
        self.assertEqual(item.unit, 'Set')

    def test_a_description_edit_on_an_item_in_use_is_allowed(self):
        item = self._item('OPX-912', 'Nos')
        self._use(item)
        response = self._head_edit(item, description='Renamed item')
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertEqual((item.description, item.unit), ('Renamed item', 'Nos'))

    def test_a_spelling_variant_in_use_saves_mapped_without_tripping_the_lock(self):
        item = self._item('OPX-913', 'No.')
        line = self._use(item)
        response = self._head_edit(item, unit='Nos', description='Lug 25 sqmm')
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.unit, 'Nos')
        line.refresh_from_db()
        self.assertEqual(line.uom, 'No.')   # the line keeps its own text

    def test_a_spelling_variant_in_use_still_cannot_become_another_unit(self):
        item = self._item('OPX-914', 'Mtr')
        self._use(item)
        form = BOQItemMasterForm(_post_data(item, unit='Set', project_type='OPEX'),
                                 instance=item)
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors['unit'], [UNIT_LOCKED_MESSAGE])

    def test_an_unmapped_unit_in_use_is_refused_with_the_retire_message(self):
        item = self._item('OPX-915', 'Each')
        self._use(item)
        form = BOQItemMasterForm(_post_data(item, unit='Nos', project_type='OPEX'),
                                 instance=item)
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors['unit'], [UNIT_LOCKED_MESSAGE])

    def test_a_line_on_a_deleted_site_still_counts_as_in_use(self):
        item = self._item('OPX-916', 'Nos')
        self._use(item)
        Project.objects.filter(pk=self.site.pk).update(is_deleted=True)
        form = BOQItemMasterForm(_post_data(item, unit='Set', project_type='OPEX'),
                                 instance=item)
        self.assertFalse(form.is_valid())

    def test_the_admin_screen_refuses_too(self):
        item = self._item('ITM-900', 'Nos', project_type='Residential')
        self._use(item)
        self._login(self.admin)
        response = self.client.post(
            reverse('admin_boq_item_edit', args=[item.pk]),
            _post_data(item, unit='Kg', project_type='Residential'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "its unit can&#x27;t change")
        item.refresh_from_db()
        self.assertEqual(item.unit, 'Nos')


# ===========================================================================
# T3 — before / after in the log
# ===========================================================================

class EditLogTests(CatalogueBase):

    def test_an_edit_logs_only_the_changed_fields_old_to_new(self):
        item = self._item('OPX-920', 'Nos', description='Old text', category='BOS')
        self._head_edit(item, description='New text', category='MMS')
        log = ActivityLog.objects.get(entity_type='BOQItemMaster', entity_id=item.pk)
        self.assertEqual(log.action,
                         "Updated RESCO BOQ catalogue item 'OPX-920': "
                         "description: 'Old text' → 'New text'; category: 'BOS' → 'MMS'")
        self.assertEqual(log.action_code, 'design_boq_item_updated')
        self.assertEqual(log.actor, self.head)

    def test_a_spelling_fix_is_logged_as_a_unit_change_of_text(self):
        item = self._item('OPX-921', 'No.')
        self._head_edit(item, unit='Nos')
        self.assertEqual(self._logs(item),
                         ["Updated RESCO BOQ catalogue item 'OPX-921': unit: No. → Nos"])

    def test_a_save_that_changes_nothing_says_so(self):
        item = self._item('OPX-922', 'Nos')
        self._head_edit(item)
        self.assertEqual(self._logs(item),
                         ["Updated RESCO BOQ catalogue item 'OPX-922': no fields changed"])

    def test_retire_and_reactivate_log_the_active_flag(self):
        item = self._item('OPX-923', 'Nos')
        self._login(self.head)
        url = reverse('design_boq_catalogue_toggle', args=[item.pk])
        self.client.post(url)
        self.client.post(url)
        self.assertEqual(self._logs(item), [
            "Deactivated RESCO BOQ catalogue item 'OPX-923': active: yes → no",
            "Activated RESCO BOQ catalogue item 'OPX-923': active: no → yes",
        ])
        codes = list(ActivityLog.objects.filter(entity_type='BOQItemMaster',
                                                entity_id=item.pk)
                     .order_by('pk').values_list('action_code', flat=True))
        self.assertEqual(codes, ['design_boq_item_deactivated', 'design_boq_item_activated'])

    def test_a_create_lists_every_field(self):
        self._login(self.head)
        self.client.post(reverse('design_boq_catalogue_create'),
                         _post_data(code='OPX-924', unit='Pair', description='MC4 pair'))
        item = BOQItemMaster.objects.get(code='OPX-924')
        self.assertEqual(self._logs(item), [
            "Created RESCO BOQ catalogue item 'OPX-924': description: → 'MC4 pair'; "
            "unit: → Pair; category: → 'BOS'; type: → RESCO; active: → yes; "
            "mandatory: → no; sort order: → 1"])

    def test_the_admin_screen_logs_before_and_after_with_no_action_code(self):
        item = self._item('ITM-901', 'Mtr', project_type='Residential')
        self._login(self.admin)
        self.client.post(reverse('admin_boq_item_edit', args=[item.pk]),
                         _post_data(item, unit='Meter', project_type='Residential',
                                    sort_order='4'))
        log = ActivityLog.objects.get(entity_type='BOQItemMaster', entity_id=item.pk)
        self.assertEqual(log.action, "Updated BOQ catalogue item 'ITM-901': "
                                     "unit: Mtr → Meter; sort order: 1 → 4")
        self.assertEqual(log.action_code, '')

    def test_long_descriptions_are_shortened_and_split_into_their_own_entry(self):
        old, new = 'A' * 200, 'B' * 200
        item = self._item('OPX-925', 'Nos', description=old)
        self._head_edit(item, description=new, category='MMS')
        logs = self._logs(item)
        self.assertEqual(len(logs), 2)
        for text in logs:
            self.assertLessEqual(len(text), ActivityLog._meta.get_field('action').max_length)
        self.assertEqual(logs[0], "Updated RESCO BOQ catalogue item 'OPX-925': "
                                  f"description: '{'A' * 94}…' → '{'B' * 94}…'")
        self.assertEqual(logs[1], "Updated RESCO BOQ catalogue item 'OPX-925': "
                                  "category: 'BOS' → 'MMS'")


# ===========================================================================
# T4 — normalise_catalogue_units
# ===========================================================================

class NormaliseCommandTests(CatalogueBase):

    def setUp(self):
        super().setUp()
        self.dotted = self._item('OPX-209', 'No.')
        self.bare = self._item('OPX-930', 'No')
        self.metre = self._item('ITM-008', 'Mtr', project_type='Residential')
        self.lot = self._item('ITM-003', 'LOT', project_type='Residential')
        self.fine = self._item('OPX-931', 'KWp')
        self.tin_shade = self._item('OPX-038', 'No')
        self.unknown = self._item('OPX-932', 'Roll')
        self.line = self._use(self.dotted)
        self.tin_line = self._use(self.tin_shade, uom='KWp')

    def _run(self, *args):
        out = StringIO()
        call_command('normalise_catalogue_units', *args, stdout=out)
        return out.getvalue()

    def _units(self):
        return dict(BOQItemMaster.objects.values_list('code', 'unit'))

    def test_the_dry_run_writes_nothing(self):
        before = self._units()
        output = self._run()
        self.assertEqual(self._units(), before)
        self.assertFalse(ActivityLog.objects.filter(entity_type='BOQItemMaster').exists())
        self.assertIn('DRY RUN', output)
        self.assertIn('OPX-209    No. → Nos   (1 BOQ lines)', output)
        self.assertIn('4 would change, 1 skipped, 1 with no mapping', output)

    def test_apply_rewrites_only_mapped_spellings_and_logs_each(self):
        output = self._run('--apply')
        self.assertEqual(self._units(), {
            'OPX-209': 'Nos', 'OPX-930': 'Nos', 'ITM-008': 'Meter', 'ITM-003': 'Lot',
            'OPX-931': 'KWp',      # already on the list
            'OPX-038': 'No',       # skipped: ruling 11 open
            'OPX-932': 'Roll',     # no mapping: listed, untouched
        })
        self.assertIn('SKIPPED  OPX-038', output)
        self.assertIn("NO MAPPING  OPX-932    'Roll'", output)
        self.assertEqual(self._logs(self.dotted), [
            "normalise_catalogue_units (system) updated RESCO BOQ catalogue item "
            "'OPX-209': unit: No. → Nos"])
        self.assertEqual(self._logs(self.lot), [
            "normalise_catalogue_units (system) updated BOQ catalogue item "
            "'ITM-003': unit: LOT → Lot"])
        logs = ActivityLog.objects.filter(entity_type='BOQItemMaster')
        self.assertEqual(logs.count(), 4)
        self.assertFalse(logs.exclude(actor=None).exists())

    def test_apply_never_touches_boq_lines(self):
        self._run('--apply')
        self.line.refresh_from_db()
        self.tin_line.refresh_from_db()
        self.assertEqual((self.line.uom, self.tin_line.uom), ('No.', 'KWp'))

    def test_a_second_run_changes_nothing(self):
        self._run('--apply')
        units, log_count = self._units(), ActivityLog.objects.count()
        output = self._run('--apply')
        self.assertEqual(self._units(), units)
        self.assertEqual(ActivityLog.objects.count(), log_count)
        self.assertIn('0 changed, 1 skipped, 1 with no mapping', output)
