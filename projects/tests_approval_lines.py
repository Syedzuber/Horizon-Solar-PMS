"""Material lines on approvals (28 Sep 2026, D-A28, D-A29, D-A30, D-A43).

What this file pins, and why each matters:

  * THE UNIT DECIDES, AND NOTHING IS ROUNDED. A count unit takes a whole number, a
    measured unit up to two places. 2.5 Nos and 2.505 KWp are REFUSED by the chokepoint
    (approvals._clean_lines), never stored as 3 and 2.51 — and three CHECKs refuse the
    same rows when the chokepoint is bypassed, because the chokepoint is not the only
    thing that can write a row.
  * AT LEAST ONE LINE, at raise and at resubmit — including the resubmit of a request
    raised before lines, which has none.
  * A RESUBMIT REPLACES THE SET, FOLLOWED BY ID. Edited lines keep their pk, so the
    change list names the line and the field that changed, with before and after;
    removed lines are deleted, and their round's snapshot still shows them.
  * EVERY OLD ROUND STILL RENDERS: snapshot schema 1 (with its boq_items) beside schema 2,
    and a request whose only material is the old request-wide make/spec/quantity.
  * THE FORM TOLERATES GAPS in line-<i>-<field> and redraws every line after a refusal.

Run with:
    python manage.py test projects.tests_approval_lines --settings=solarpms.test_settings
and under the real settings (Postgres, migrations applied — the CHECKs as 0106 made them):
    python manage.py test projects.tests_approval_lines
"""
from decimal import Decimal

from django.contrib import admin as django_admin
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.http import QueryDict
from django.test import Client, RequestFactory, SimpleTestCase
from django.urls import reverse

from . import approval_notices
from .approval_forms import parse_lines
from .approval_views import round_changes
from .approvals import (
    ApprovalRefused, apply_approval_decision, resubmit_approval_request, round_snapshot,
)
from .models import (
    ApprovalRequest, ApprovalRoundSnapshot, MaterialApprovalDetail, MaterialApprovalLine,
    APPROVAL_PARTY_PM, APPROVAL_STEP_CHANGES_REQUESTED,
)
from .tests_approval_notices import NoticeBase
from .tests_approvals import MODULE_LINE, ON_POSTGRES, ApprovalFixture
from .units import (
    COUNT_UNITS, MEASURED_UNITS, UNIT_CHOICES, UNIT_CODES, UNITS, format_quantity,
    unit_places,
)


def _line(**fields):
    return dict(MODULE_LINE, **fields)


class LinesFixture(ApprovalFixture):

    def lines_of(self, approval):
        return list(MaterialApprovalLine.objects.filter(detail__request=approval)
                    .order_by('position'))

    def changes_requested(self, approval):
        """The PM asks for changes; the request is then waiting for a resubmit."""
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.pm, note='Redo.')
        approval.refresh_from_db()
        return approval

    def as_posted(self, line):
        """A live line in the shape resubmit's `lines` takes, id included."""
        return {'id': line.pk, 'description': line.description, 'make': line.make,
                'specification': line.specification, 'quantity': str(line.quantity),
                'unit': line.unit}

    def make_legacy(self, approval, **legacy):
        """Turn `approval` into one raised before lines: no lines, the request-wide
        fields set, and a schema-1 round-1 snapshot carrying boq_items. Queryset writes
        go around the chokepoint and the snapshot's append-only save() on purpose."""
        legacy = legacy or {'proposed_make': 'Waaree', 'specification': '545 Wp mono',
                            'quantity_note': '120 modules'}
        MaterialApprovalLine.objects.filter(detail__request=approval).delete()
        MaterialApprovalDetail.objects.filter(request=approval).update(**legacy)
        snapshot = round_snapshot(approval, 1)
        material = snapshot['material']
        del material['lines']
        material.pop('legacy', None)
        material.update({'proposed_make': '', 'specification': '', 'quantity_note': '',
                         'boq_items': []}, **legacy)
        snapshot['schema'] = 1
        ApprovalRoundSnapshot.objects.filter(request=approval, round=1).update(
            snapshot=snapshot)
        return approval

    def client_for(self, profile):
        client = Client(SERVER_NAME='localhost')
        client.force_login(profile.user)
        return client


# ===========================================================================
# projects/units.py
# ===========================================================================

class UnitsTests(SimpleTestCase):

    def test_the_nine_units_and_their_kinds(self):
        self.assertEqual([(code, label, kind) for code, label, kind in UNITS], [
            ('Nos', 'Nos', 'count'), ('Meter', 'Meter', 'measured'), ('Set', 'Set', 'count'),
            ('Pkt', 'Packet', 'count'), ('Pair', 'Pair', 'count'), ('Lot', 'Lot', 'count'),
            ('KWp', 'KWp', 'measured'), ('Kg', 'Kg', 'measured'),
            ('LS', 'Lump sum', 'measured')])
        self.assertEqual(COUNT_UNITS, ('Nos', 'Set', 'Pkt', 'Pair', 'Lot'))
        self.assertEqual(MEASURED_UNITS, ('Meter', 'KWp', 'Kg', 'LS'))
        self.assertEqual(len(UNIT_CHOICES), 9)
        self.assertEqual(set(UNIT_CODES), set(COUNT_UNITS) | set(MEASURED_UNITS))

    def test_places_and_formatting(self):
        self.assertEqual((unit_places('Nos'), unit_places('KWp'), unit_places('Box')),
                         (0, 2, None))
        self.assertEqual(format_quantity(Decimal('120.00'), 'Nos'), '120')
        self.assertEqual(format_quantity(Decimal('2.5'), 'KWp'), '2.50')
        self.assertEqual(format_quantity('2.50', 'KWp'), '2.50')
        # Never rounded: a value that does not fit its unit is shown as stored.
        self.assertEqual(format_quantity(Decimal('2.50'), 'Nos'), '2.50')
        self.assertEqual(format_quantity('7', 'Box'), '7')


# ===========================================================================
# The chokepoint (approvals._clean_lines via create and resubmit)
# ===========================================================================

class ChokepointTests(LinesFixture):

    def assert_refused(self, message, **overrides):
        before = (ApprovalRequest.objects.count(), MaterialApprovalLine.objects.count())
        with self.assertRaises(ApprovalRefused) as caught:
            self.raise_material(**overrides)
        self.assertIn(message, str(caught.exception))
        self.assertEqual((ApprovalRequest.objects.count(),
                          MaterialApprovalLine.objects.count()), before)

    def test_a_fraction_of_a_count_unit_is_refused(self):
        self.assert_refused('Line 1 quantity (Nos): "2.5" must be a whole number.',
                            lines=[_line(quantity='2.5', unit='Nos')])

    def test_two_places_of_a_measured_unit_are_accepted_as_typed(self):
        approval = self.raise_material(lines=[_line(quantity='2.50', unit='KWp')])
        self.assertEqual([(l.quantity, l.unit) for l in self.lines_of(approval)],
                         [(Decimal('2.50'), 'KWp')])

    def test_a_third_place_is_refused_not_rounded(self):
        self.assert_refused('Line 1 quantity: "2.505" has more than 2 decimal places.',
                            lines=[_line(quantity='2.505', unit='KWp')])

    def test_zero_and_below_are_refused(self):
        for quantity in ('0', '0.00', '-1'):
            with self.subTest(quantity=quantity):
                with self.assertRaises(ApprovalRefused) as caught:
                    self.raise_material(lines=[_line(quantity=quantity, unit='Meter')])
                self.assertIn('Line 1 quantity', str(caught.exception))
        self.assertFalse(ApprovalRequest.objects.exists())

    def test_an_unknown_unit_is_refused(self):
        for unit in ('Box', '', 'nos'):
            with self.subTest(unit=unit):
                self.assert_refused('Line 1: choose a unit from the list.',
                                    lines=[_line(unit=unit)])

    def test_a_count_unit_takes_trailing_zeros(self):
        """"12.00" is exactly twelve: trailing zeros are not decimal places."""
        approval = self.raise_material(lines=[_line(quantity='12.00', unit='Set')])
        self.assertEqual(self.lines_of(approval)[0].quantity, Decimal('12'))

    def test_each_line_is_checked_and_named(self):
        self.assert_refused('Line 2: say what the material is.',
                            lines=[_line(), _line(description='   ')])
        self.assert_refused('Line 3 quantity (Pair): "1.5" must be a whole number.',
                            lines=[_line(), _line(), _line(quantity='1.5', unit='Pair')])
        self.assert_refused('Line 1 quantity (Nos): enter how many or how much.',
                            lines=[_line(quantity='')])

    def test_zero_lines_are_refused_at_raise(self):
        for lines in (None, []):
            with self.subTest(lines=lines):
                self.assert_refused('Add at least one material line.', lines=lines)

    def test_zero_lines_are_refused_at_resubmit(self):
        approval = self.changes_requested(self.raise_material())
        with self.assertRaises(ApprovalRefused) as caught:
            resubmit_approval_request(approval, self.scm, note='No lines.', lines=[])
        self.assertIn('Add at least one material line.', str(caught.exception))
        approval.refresh_from_db()
        self.assertEqual(approval.current_round, 1)
        self.assertEqual(len(self.lines_of(approval)), 1)

    def test_the_retired_material_fields_are_refused(self):
        for key in ('proposed_make', 'specification', 'quantity_note', 'boq_items'):
            with self.subTest(key=key):
                self.assert_refused('is not recorded on a request any more',
                                    material={key: 'x'})

    def test_a_contractor_bill_takes_no_lines(self):
        with self.assertRaises(ApprovalRefused) as caught:
            self.raise_bill(lines=[_line()])
        self.assertIn('no material lines', str(caught.exception))
        bill = self.raise_bill()
        apply_approval_decision(self.step(bill, 'site_engineer'),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.se, note='Redo.')
        with self.assertRaises(ApprovalRefused):
            resubmit_approval_request(bill, self.scm, note='x', lines=[_line()])

    def test_a_resubmit_may_not_name_another_requests_line(self):
        other = self.raise_material(title='Other')
        approval = self.changes_requested(self.raise_material())
        foreign = self.lines_of(other)[0]
        for lines in ([self.as_posted(foreign)],
                      [self.as_posted(self.lines_of(approval)[0])] * 2):   # named twice
            with self.subTest(lines=len(lines)):
                with self.assertRaises(ApprovalRefused) as caught:
                    resubmit_approval_request(approval, self.scm, note='x', lines=lines)
                self.assertIn('is not one of this request\'s lines', str(caught.exception))
        self.assertEqual(self.lines_of(other)[0].description, foreign.description)

    def test_a_create_may_not_name_a_line_id(self):
        existing = self.lines_of(self.raise_material())[0]
        self.assert_refused('is not one of this request\'s lines',
                            lines=[self.as_posted(existing)])


# ===========================================================================
# The database: each CHECK, the chokepoint bypassed
# ===========================================================================

class LineConstraintTests(LinesFixture):

    def setUp(self):
        self.detail = MaterialApprovalDetail.objects.get(request=self.raise_material())

    def assertViolates(self, name, **fields):
        values = dict(detail=self.detail, position=9, description='Bypass',
                      quantity=Decimal('1'), unit='Nos')
        values.update(fields)
        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                MaterialApprovalLine.objects.create(**values)
        # SQLite names a CHECK in its message but not a unique index; Postgres names both.
        if ON_POSTGRES or not name.startswith('uniq_'):
            self.assertIn(name, str(caught.exception))

    def test_quantity_must_be_positive(self):
        for quantity in (Decimal('0'), Decimal('-3')):
            with self.subTest(quantity=quantity):
                self.assertViolates('material_line_quantity_positive', quantity=quantity,
                                    unit='Meter')

    def test_unit_must_be_one_of_the_nine(self):
        """Choices are not enforced by the database; this CHECK is."""
        self.assertViolates('material_line_unit_known', unit='Box')

    def test_a_count_unit_holds_a_whole_number(self):
        for unit in COUNT_UNITS:
            with self.subTest(unit=unit):
                self.assertViolates('material_line_count_unit_whole',
                                    quantity=Decimal('2.50'), unit=unit)

    def test_a_measured_unit_holds_a_fraction(self):
        for unit in MEASURED_UNITS:
            MaterialApprovalLine.objects.create(
                detail=self.detail, position=10 + MEASURED_UNITS.index(unit),
                description='Fine', quantity=Decimal('2.50'), unit=unit)

    def test_a_position_is_used_once_per_request(self):
        self.assertViolates('uniq_material_line_position', position=1)


# ===========================================================================
# Snapshots: schema 2, and schema 1 still read
# ===========================================================================

class SnapshotTests(LinesFixture):

    def test_schema_2_holds_the_lines_and_only_non_empty_legacy(self):
        approval = self.raise_material(lines=[
            _line(), _line(description='DC cable', make='', specification='',
                           quantity='250.5', unit='Meter')])
        snap = round_snapshot(approval, 1)
        first, second = self.lines_of(approval)
        self.assertEqual(snap['schema'], 2)
        self.assertEqual(snap['material']['lines'], [
            {'id': first.pk, 'position': 1, 'description': 'Solar module 545 Wp',
             'make': 'Waaree', 'specification': '545 Wp mono PERC', 'quantity': '120',
             'unit': 'Nos'},
            {'id': second.pk, 'position': 2, 'description': 'DC cable', 'make': '',
             'specification': '', 'quantity': '250.50', 'unit': 'Meter'}])
        self.assertNotIn('legacy', snap['material'])
        self.assertNotIn('boq_items', snap['material'])
        self.assertNotIn('proposed_make', snap['material'])

        # A request that still carries a legacy field: that one, and only that one.
        MaterialApprovalDetail.objects.filter(request=approval).update(
            quantity_note='120 modules')
        approval = self.changes_requested(approval)
        resubmit_approval_request(approval, self.scm, note='Round two.')
        self.assertEqual(round_snapshot(approval, 2)['material']['legacy'],
                         {'quantity_note': '120 modules'})

    def test_a_schema_1_snapshot_with_boq_items_still_renders(self):
        approval = self.make_legacy(self.raise_material())
        self.assertEqual(round_snapshot(approval, 1)['schema'], 1)
        self.assertEqual(round_snapshot(approval, 1)['material']['boq_items'], [])
        page = self.client_for(self.scm).get(reverse('approval_detail', args=[approval.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Recorded before line items')
        self.assertContains(page, '120 modules')

        # A schema-1 BOQ pick, had one ever been made, is drawn under the same heading.
        snapshot = round_snapshot(approval, 1)
        snapshot['material']['boq_items'] = [{'id': 7, 'code': 'OPX-007',
                                              'description': 'Module', 'unit': 'Nos'}]
        ApprovalRoundSnapshot.objects.filter(request=approval, round=1).update(
            snapshot=snapshot)
        page = self.client_for(self.scm).get(reverse('approval_detail', args=[approval.pk]))
        self.assertContains(page, 'OPX-007 — Module')


# ===========================================================================
# round_changes across three rounds
# ===========================================================================

class RoundChangeTests(LinesFixture):

    def test_add_then_remove_then_change_one_field(self):
        approval = self.raise_material(lines=[
            _line(), _line(description='DC cable', make='Polycab', specification='4 sq mm',
                           quantity='250', unit='Meter')])
        module, cable = self.lines_of(approval)

        # Round 2: a line added.
        self.changes_requested(approval)
        resubmit_approval_request(approval, self.scm, note='Add inverters.', lines=[
            self.as_posted(module), self.as_posted(cable),
            _line(description='Inverter 50 kW', make='Sungrow', specification='',
                  quantity='2', unit='Nos')])
        inverter = self.lines_of(approval)[2]

        # Round 3: the cable removed, and one field of the module changed.
        self.changes_requested(approval)
        resubmit_approval_request(approval, self.scm, note='Drop cable, more modules.',
                                  lines=[dict(self.as_posted(module), quantity='130'),
                                         self.as_posted(inverter)])
        r1, r2, r3 = (round_snapshot(approval, n) for n in (1, 2, 3))

        self.assertEqual(round_changes(r1, r2), [
            {'label': 'Material lines',
             'added': ['Line 3 · Inverter 50 kW — 2 Nos (Sungrow)'], 'removed': []}])
        self.assertEqual(round_changes(r2, r3), [
            {'label': 'Material lines', 'added': [],
             'removed': ['DC cable (was line 2) — 250.00 Meter (Polycab)']},
            {'label': 'Line 1 · Solar module 545 Wp',
             'fields': [{'label': 'Quantity', 'old': '120', 'new': '130'}]}])

        # The kept lines kept their pks; the removed one is gone from the live set but
        # still in the rounds that saw it.
        self.assertEqual([l.pk for l in self.lines_of(approval)], [module.pk, inverter.pk])
        self.assertFalse(MaterialApprovalLine.objects.filter(pk=cable.pk).exists())
        self.assertEqual([l['id'] for l in r2['material']['lines']],
                         [module.pk, cable.pk, inverter.pk])

        page = self.client_for(self.scm).get(reverse('approval_detail', args=[approval.pk]))
        self.assertContains(page, '+ Added: Line 3 · Inverter 50 kW — 2 Nos (Sungrow)')
        self.assertContains(page, '− Removed: DC cable (was line 2) — 250.00 Meter (Polycab)')
        self.assertContains(page, 'Quantity: <span class="text-muted">120</span> → 130',
                            html=False)

    def test_a_removed_line_and_the_new_line_taking_its_number_are_told_apart(self):
        """Remove line 2 of 3 and add a line: the old line 3 moves up to 2 and the new
        line becomes 3. The change list names each by its description; the moved line,
        which nobody edited, is not reported at all."""
        approval = self.changes_requested(self.raise_material(lines=[
            _line(), _line(description='DC cable', make='', specification='',
                           quantity='250', unit='Meter'),
            _line(description='Clamp', make='', specification='', quantity='40',
                  unit='Pkt')]))
        module, cable, clamp = self.lines_of(approval)
        resubmit_approval_request(approval, self.scm, note='Cable out, earthing in.', lines=[
            self.as_posted(module), self.as_posted(clamp),
            _line(description='Earthing strip', make='', specification='', quantity='10',
                  unit='Meter')])
        self.assertEqual([(l.pk, l.position, l.description) for l in self.lines_of(approval)],
                         [(module.pk, 1, 'Solar module 545 Wp'), (clamp.pk, 2, 'Clamp'),
                          (self.lines_of(approval)[2].pk, 3, 'Earthing strip')])
        changes = round_changes(round_snapshot(approval, 1), round_snapshot(approval, 2))
        self.assertEqual(changes, [{'label': 'Material lines',
                                    'added': ['Line 3 · Earthing strip — 10.00 Meter'],
                                    'removed': ['DC cable (was line 2) — 250.00 Meter']}])
        page = self.client_for(self.scm).get(reverse('approval_detail', args=[approval.pk]))
        self.assertContains(page, '+ Added: Line 3 · Earthing strip — 10.00 Meter')
        self.assertContains(page, '− Removed: DC cable (was line 2) — 250.00 Meter')
        self.assertNotContains(page, 'Clamp (was line')

    def test_a_unit_change_is_not_also_a_quantity_change(self):
        approval = self.changes_requested(self.raise_material())
        module = self.lines_of(approval)[0]
        resubmit_approval_request(approval, self.scm, note='Metres.',
                                  lines=[dict(self.as_posted(module), unit='Meter')])
        self.assertEqual(round_changes(round_snapshot(approval, 1),
                                       round_snapshot(approval, 2)),
                         [{'label': 'Line 1 · Solar module 545 Wp',
                           'fields': [{'label': 'Unit', 'old': 'Nos', 'new': 'Meter'}]}])

    def test_an_unchanged_resubmit_reports_nothing(self):
        approval = self.changes_requested(self.raise_material())
        resubmit_approval_request(approval, self.scm, note='Same.',
                                  lines=[self.as_posted(self.lines_of(approval)[0])])
        self.assertEqual(round_changes(round_snapshot(approval, 1),
                                       round_snapshot(approval, 2)), [])

    def test_renumbering_after_a_removal_is_not_a_change(self):
        """Removing line 1 moves line 2 to position 1 without anyone editing it."""
        approval = self.changes_requested(self.raise_material(
            lines=[_line(), _line(description='Cable', quantity='5', unit='Meter')]))
        module, cable = self.lines_of(approval)
        resubmit_approval_request(approval, self.scm, note='Cable only.',
                                  lines=[self.as_posted(cable)])
        self.assertEqual([(l.pk, l.position) for l in self.lines_of(approval)],
                         [(cable.pk, 1)])
        self.assertEqual(round_changes(round_snapshot(approval, 1),
                                       round_snapshot(approval, 2)),
                         [{'label': 'Material lines', 'added': [],
                           'removed': ['Solar module 545 Wp (was line 1) — 120 Nos (Waaree)']}])


# ===========================================================================
# Requests raised before lines
# ===========================================================================

class LegacyTests(LinesFixture):

    def test_a_legacy_only_request_renders(self):
        approval = self.make_legacy(self.raise_material())
        client = self.client_for(self.scm)
        page = client.get(reverse('approval_detail', args=[approval.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Recorded before line items', count=2)   # card + round 1
        for text in ('Waaree', '545 Wp mono', '120 modules'):
            self.assertContains(page, text)
        self.assertNotContains(page, '<th class="fw-normal">Material</th>', html=False)

        self.changes_requested(approval)
        form = client.get(reverse('approval_resubmit', args=[approval.pk]))
        self.assertContains(form, 'Recorded before line items')
        self.assertContains(form, 'name="line-0-description"')    # one empty row
        self.assertNotContains(form, 'name="line-1-description"')

    def test_its_resubmit_requires_at_least_one_line(self):
        approval = self.changes_requested(self.make_legacy(self.raise_material()))
        for lines in (None, []):
            with self.subTest(lines=lines):
                with self.assertRaises(ApprovalRefused) as caught:
                    resubmit_approval_request(approval, self.scm, note='x', lines=lines)
                self.assertIn('at least one', str(caught.exception))
        resubmit_approval_request(approval, self.scm, note='Now as lines.',
                                  lines=[_line(), _line(description='Clamp', quantity='40',
                                                        unit='Pkt')])
        changes = round_changes(round_snapshot(approval, 1), round_snapshot(approval, 2))
        self.assertEqual(changes, [{'label': 'Quantity recorded as lines', 'removed': [],
                                    'added': ['Line 1 · Solar module 545 Wp — 120 Nos (Waaree)',
                                              'Line 2 · Clamp — 40 Packet (Waaree)']}])
        # The legacy fields are untouched, and still recorded in the new round.
        self.assertEqual(round_snapshot(approval, 2)['material']['legacy']['quantity_note'],
                         '120 modules')


# ===========================================================================
# The screens
# ===========================================================================

class FormTests(LinesFixture):

    def create_url(self, client):
        return client.get(reverse('approval_create'))['Location']

    def base_form(self):
        return {'title': 'Modules', 'description': 'For the tender.',
                'pm_assignee': self.pm.pk}

    @staticmethod
    def row(index, **fields):
        values = dict(MODULE_LINE, id='')
        values.update(fields)
        return {f'line-{index}-{field}': value for field, value in values.items()}

    def test_parse_lines_orders_by_index_as_a_number_and_skips_blank_rows(self):
        post = QueryDict(mutable=True)
        for index, description in ((10, 'Ten'), (2, 'Two'), (9, 'Nine')):
            post.update(self.row(index, description=description))
        post.update(self.row(4, description='', make='', specification='', quantity=''))
        post.update({'line-x-description': 'junk', 'line-3-colour': 'red'})
        lines = parse_lines(post)
        self.assertEqual([l['description'] for l in lines], ['Two', 'Nine', 'Ten'])
        self.assertEqual(lines[0], dict(MODULE_LINE, id=None, description='Two'))

    def test_gapped_indices_raise_the_lines_in_order(self):
        client = self.client_for(self.scm)
        data = dict(self.base_form(), **self.row(0, description='Module'),
                    **self.row(3, description='Cable', quantity='12.5', unit='Meter'),
                    **self.row(10, description='Clamp', quantity='40', unit='Pkt'))
        client.post(self.create_url(client), data)
        approval = ApprovalRequest.objects.get()
        self.assertEqual([(l.position, l.description, l.quantity, l.unit)
                          for l in self.lines_of(approval)],
                         [(1, 'Module', Decimal('120'), 'Nos'),
                          (2, 'Cable', Decimal('12.50'), 'Meter'),
                          (3, 'Clamp', Decimal('40'), 'Pkt')])

    def test_a_third_place_redraws_the_form_with_every_line_intact(self):
        client = self.client_for(self.scm)
        url = self.create_url(client)
        data = dict(self.base_form(), **self.row(0, description='Module row'),
                    **self.row(5, description='Tracker row', make='Nextracker',
                               quantity='2.505', unit='KWp'))
        response = client.post(url, data)
        self.assertContains(response, 'Line 2 quantity: &quot;2.505&quot; has more than 2 '
                                      'decimal places.', status_code=400)
        for text in ('value="Module row"', 'value="Tracker row"', 'value="Nextracker"',
                     'value="2.505"', 'value="120"',
                     '<option value="KWp" selected>KWp</option>',
                     '<option value="Nos" data-count selected>Nos</option>',
                     'name="line-1-quantity"'):          # gaps closed up on redraw
            self.assertContains(response, text, status_code=400)
        self.assertFalse(ApprovalRequest.objects.exists())
        self.assertIn('?key=', url)                       # the duplicate guard is intact

    def test_the_quantity_inputs_are_step_any(self):
        """step="any" on the drawn input (Prompt N's policy); the script narrows it per
        unit, and the server stays the rule."""
        client = self.client_for(self.scm)
        page = client.get(self.create_url(client)).content.decode()
        self.assertIn('type="number" step="any" min="0.01" inputmode="decimal" '
                      'name="line-0-quantity"', page)
        self.assertIn("qty.step = isCount ? '1' : 'any';", page)

    def test_resubmit_removes_edits_and_adds_and_history_names_each(self):
        approval = self.raise_material(lines=[
            _line(), _line(description='DC cable', make='', specification='', quantity='250',
                           unit='Meter'),
            _line(description='Clamp', make='', specification='', quantity='40', unit='Pkt')])
        module, cable, clamp = self.lines_of(approval)
        self.changes_requested(approval)
        client = self.client_for(self.scm)
        data = {'revise': '1', 'title': approval.title, 'description': approval.description,
                'vendor': '', 'note': 'Cable out, clamps up, add inverter.',
                **self.row(0, id=module.pk),
                **self.row(2, id=clamp.pk, description='Clamp', make='', specification='',
                           quantity='60', unit='Pkt'),
                **self.row(7, description='Inverter', make='Sungrow', specification='',
                           quantity='2', unit='Nos')}
        response = client.post(reverse('approval_resubmit', args=[approval.pk]), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual([(l.pk, l.position, l.quantity) for l in self.lines_of(approval)[:2]],
                         [(module.pk, 1, Decimal('120')), (clamp.pk, 2, Decimal('60'))])
        page = client.get(reverse('approval_detail', args=[approval.pk]))
        self.assertContains(page, '+ Added: Line 3 · Inverter — 2 Nos (Sungrow)')
        self.assertContains(page, '− Removed: DC cable (was line 2) — 250.00 Meter')
        self.assertContains(page, 'Line 2 · Clamp (was line 3)')     # moved up and edited
        self.assertContains(page, 'Quantity: <span class="text-muted">40</span> → 60',
                            html=False)

    def test_the_detail_page_draws_the_lines_table(self):
        approval = self.raise_material(lines=[_line(), _line(
            description='Tracker', make='', specification='Single axis', quantity='1.5',
            unit='KWp')])
        page = self.client_for(self.pm).get(reverse('approval_detail', args=[approval.pk]))
        self.assertContains(page, '120 Nos', count=2)             # request card + round 1
        self.assertContains(page, '1.50 KWp', count=2)
        self.assertContains(page, 'Make: Waaree')
        self.assertNotContains(page, 'Recorded before line items')


# ===========================================================================
# Email summary and admin
# ===========================================================================

class EmailAndAdminTests(LinesFixture):

    def test_the_email_summarises_the_lines(self):
        approval = self.raise_material(lines=[
            _line(description='Solar module 540W'), _line(description='Cable', unit='Meter'),
            _line(description='Clamp')])
        self.assertEqual(approval_notices.lines_summary(approval),
                         '120 Nos — Solar module 540W, +2 more')
        html = approval_notices.email_html(approval, 'Approval needed', 'Please decide.')
        self.assertIn('Material:</span> 120 Nos — Solar module 540W, +2 more', html)

        single = self.raise_material(lines=[_line(quantity='2.5', unit='KWp')])
        self.assertEqual(approval_notices.lines_summary(single),
                         '2.50 KWp — Solar module 545 Wp')
        bill = self.raise_bill()
        self.assertEqual(approval_notices.lines_summary(bill), '')
        self.assertNotIn('Material:', approval_notices.email_html(bill, 's', 'b'))

    def test_the_admin_shows_lines_read_only(self):
        request = RequestFactory().get('/')
        request.user = User.objects.create_superuser('al_admin', 'a@example.com', 'pw')
        model_admin = django_admin.site._registry[MaterialApprovalDetail]
        inline = model_admin.get_inline_instances(request, None)
        self.assertEqual([type(i).model for i in inline], [MaterialApprovalLine])
        self.assertFalse(inline[0].has_add_permission(request, None))
        self.assertFalse(inline[0].has_change_permission(request, None))
        self.assertFalse(inline[0].has_delete_permission(request, None))
        self.assertNotIn(MaterialApprovalLine, django_admin.site._registry)


class EmailTextPartTests(NoticeBase):
    """The line summary is in BOTH parts of the email; the bell text stays one sentence.
    NoticeBase records every email with its text and HTML parts (no HTTP)."""

    def test_the_summary_is_in_the_text_part_and_the_html_part(self):
        _, sent = self.create_material(design=False, lines=[
            _line(description='Solar module 540W'), _line(description='Cable', unit='Meter'),
            _line(description='Clamp')])
        summary = '120 Nos — Solar module 540W, +2 more'
        mail = [m for m in self.mail if m['to'] == 'ap_pm@example.com']
        self.assertEqual(len(mail), 1)
        self.assertIn(f'\n\nMaterial: {summary}', mail[0]['text'])
        self.assertIn(f'Material:</span> {summary}', mail[0]['html'])
        # The bell is the notice alone, and the PM still hears once, on both channels.
        self.assertNotIn('Material:', self.note_for(self.pm))
        self.assertEqual(sent['ap_pm'], (approval_notices.T_ACTIVATED, ('email', 'in_app')))
        self.assertTrue(mail[0]['text'].startswith(self.note_for(self.pm)))

    def test_a_request_without_lines_sends_no_material_line(self):
        # Changed deliberately in 4a-2 (ruling 4): a contractor bill's email now carries
        # its own one-line summary, under "Bill", after the notice — still never a
        # "Material:" line, and the bell is still the notice alone.
        self.act(self.raise_bill)
        mail = [m for m in self.mail if m['to'] == 'ap_se@example.com']
        self.assertEqual(len(mail), 1)
        self.assertNotIn('Material:', mail[0]['text'])
        self.assertNotIn('Material:', mail[0]['html'])
        self.assertTrue(mail[0]['text'].startswith(f'{self.note_for(self.se)}\n\nBill: '))
        self.assertNotIn('Bill:', self.note_for(self.se))
