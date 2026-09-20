"""The consignments behind a delivery mirror task. Session T4.

WHY THIS FILE EXISTS
--------------------
The four OPEX delivery mirrors derive their status from `DCLineItem` rows, and until
this session that status was ALL a PM could see: a task reading "In Progress" with no
way to find out which challan was short, or whether anything had been raised at all.
`delivery_detail_for_task()` is the read side of the same subject, and this file pins
what it may say.

THE TWO FIGURES ARE NOT ONE FIGURE, and most of this file is about keeping them apart:

  * CONSIGNMENTS counts challans — fully received, partial, expected.
  * QUANTITY counts units of material, and ONLY within one unit. 400 Nos plus 1 Lot is
    not 401 of anything, so a category holding two units gets no aggregate at all.

AND THE QUANTITY FIGURE IS AGAINST WHAT WAS DISPATCHED, never against the site's
requirement. Nothing joins a `DCLineItem` to a `BOQItem` — B-18 is not built, and the
OPEX BOQ vocabulary (`Module`, `MMS`, `Conduit`, …) does not even share spellings with
`DCLineItem.CATEGORY_CHOICES` — so the site's need is not knowable here and must never
be implied. `LabellingTests` reads the rendered page for the words that say so, and for
the words that must not appear on it.

THE FIXTURE IS A REALLY ACTIVATED OPEX SITE, following tests_mirror_readonly.py: the
mirrors under test are the rows `attach_opex_template()` produced, not hand-made `Task`s
with the flag set. A hand-made row would prove the `if` works and nothing about whether
production carries the flag.

WHAT IS DELIBERATELY NOT CALLED HERE: `sync_delivery_mirrors()`. It has a closed list of
two permitted callers, asserted across the whole package by
`tests_design_mirror_derivation.test_01b`, and a test module naming it would break that
list. `AgreementTests` reaches the derivation the way production does instead, through
`recalculate_dc_status()`, which is one of the two.

Run with:
    python manage.py test projects --settings=solarpms.test_settings
"""
from datetime import date
from decimal import Decimal
from importlib import import_module

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from .design_views import delivery_detail_for_task
from .models import (
    DCLineItem, DeliveryChallan, Project, Task, TaskTemplate, TaskTemplatePhase,
    TaskTemplateTask, UserProfile, recalculate_dc_status,
)
from .utils import resolve_residential_template


# The task under test, and the category it reads. Transcribed rather than imported from
# DC_CATEGORY_TO_MIRROR_CODE, on the same argument tests_opex_template.py makes: a test
# that imports the mapping agrees with any mistake the mapping contains.
PANEL_TASK_NAME = 'Delivery — Solar Panels'
PANEL_CATEGORY = 'Solar Modules'
# A DIFFERENT bucket on the same site, used to prove the category filter is real.
OTHER_CATEGORY = 'Inverter'
# A mirror with no delivery source at all, and a non-mirror task a human owns.
NON_DELIVERY_MIRROR_NAME = 'COD'
CONTROL_TASK_NAME = 'Net Metering Approval'


class _ConcreteApps:
    """Stands in for the `apps` registry a RunPython function is handed."""

    _MODELS = {
        'TaskTemplate':      TaskTemplate,
        'TaskTemplatePhase': TaskTemplatePhase,
        'TaskTemplateTask':  TaskTemplateTask,
        'Task':              Task,
    }

    def get_model(self, app_label, model_name):
        assert app_label == 'projects'
        return self._MODELS[model_name]


def _seed_opex():
    module = import_module('projects.migrations.0075_seed_opex_template_v1')
    module.seed_opex_v1(_ConcreteApps(), None)


def _profile(username, role):
    """Create a user and give their profile `role`.

    signals.py creates the profile on post_save with the model's DEFAULT role, so this
    UPDATES rather than creates.
    """
    user = User.objects.create_user(
        username=username, password='x',
        first_name=username.title(), last_name='Test',
    )
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.save(update_fields=['role'])
    return profile


class DeliveryDetailFixture(TestCase):
    """One really activated OPEX site, its PM, and a challan builder."""

    @classmethod
    def setUpTestData(cls):
        resolve_residential_template()   # bootstraps RESIDENTIAL v1 on a virgin DB
        _seed_opex()
        cls.pm = _profile('t4_pm', 'PM')

    def setUp(self):
        self.site = Project.objects.create(
            customer_name='T4 Tender Site',
            customer_phone='9876543210',
            site_address='1 Consignment Road',
            city='Lucknow',
            project_type='OPEX',
            dc_capacity_kw=Decimal('100.00'),
            status='Draft',
            assigned_pm=self.pm,
        )
        client = Client()
        client.force_login(self.pm.user)
        response = client.post(
            reverse('opex_site_activate', args=[self.site.project_id]))
        self.assertEqual(response.status_code, 302, 'OPEX activation did not redirect')
        self.site.refresh_from_db()
        self.assertEqual(self.site.status, 'Active')

    # -- helpers -------------------------------------------------------------

    def _task(self, name=PANEL_TASK_NAME):
        task = Task.objects.filter(phase__project=self.site, task_name=name).first()
        self.assertIsNotNone(task, f'the OPEX attach produced no task named {name!r}')
        return task

    def _challan(self, dc_number, dc_date, lines, status=DeliveryChallan.EXPECTED):
        """One challan and its lines. `lines` are (category, description, unit,
        ordered, received, damaged) — received None meaning "GRN not done"."""
        challan = DeliveryChallan.objects.create(
            project=self.site, dc_number=dc_number, dc_date=dc_date, status=status,
        )
        for category, description, unit, ordered, received, damaged in lines:
            DCLineItem.objects.create(
                challan=challan,
                boq_category=category,
                item_description=description,
                unit=unit,
                ordered_quantity=Decimal(ordered),
                received_quantity=None if received is None else Decimal(received),
                damaged_quantity=damaged,
                grn_date=None if received is None else date(2026, 9, 10),
                grn_confirmed_by=None if received is None else self.pm,
            )
        return challan

    def _detail(self, name=PANEL_TASK_NAME):
        return delivery_detail_for_task(self._task(name))

    def _page(self, task=None, profile=None):
        task = task or self._task()
        client = Client()
        client.force_login((profile or self.pm).user)
        return client.get(
            reverse('task_detail', args=[self.site.project_id, task.pk]))


class SeveralChallansTests(DeliveryDetailFixture):
    """One category, three challans in three different states — H7's real shape.

    Nothing stops a site carrying several challans in one category: there is no
    uniqueness constraint on (project, category) or on dc_number, and the derivation
    has always read every line on every challan.
    """

    def setUp(self):
        super().setUp()
        # Raised OUT OF DATE ORDER on purpose: the rows must come back oldest-first by
        # dc_date, not in insertion or pk order, or the panel reads as a random list
        # rather than a delivery history.
        self._challan('DC-MIDDLE', date(2026, 9, 10), [
            (PANEL_CATEGORY, 'Mono PERC 540W', 'Nos', '100', '60', 0),
        ])
        self._challan('DC-LAST', date(2026, 9, 20), [
            (PANEL_CATEGORY, 'Mono PERC 540W', 'Nos', '50', None, 0),
        ])
        self._challan('DC-FIRST', date(2026, 9, 1), [
            (PANEL_CATEGORY, 'Mono PERC 540W', 'Nos', '300', '300', 0),
        ])
        # A challan in ANOTHER category on the SAME site. It must never appear in this
        # panel and must not move any of its numbers — the category is the only join
        # there is, so a lost filter is silent and this row is what makes it loud.
        self._challan('DC-INVERTER', date(2026, 9, 5), [
            (OTHER_CATEGORY, 'String inverter 60kW', 'Nos', '2', '2', 0),
        ])

    def test_01_one_row_per_challan_in_this_category_oldest_first(self):
        detail = self._detail()
        self.assertEqual(
            [row['dc_number'] for row in detail['challans']],
            ['DC-FIRST', 'DC-MIDDLE', 'DC-LAST'],
            'the panel is a delivery history and must read oldest-first by dc_date')

    def test_02_a_challan_in_another_category_is_not_in_this_panel(self):
        detail = self._detail()
        self.assertNotIn('DC-INVERTER', [row['dc_number'] for row in detail['challans']],
                         'the category filter is gone: an inverter challan is being '
                         'reported under Solar Panels')
        self.assertEqual(detail['consignments']['total'], 3)

    def test_03_the_three_states_are_counted_separately(self):
        self.assertEqual(
            self._detail()['consignments'],
            {'received': 1, 'partial': 1, 'expected': 1, 'total': 3},
            'fully received, partial and expected are three different answers and a '
            'PM acts differently on each')

    def test_04_each_row_carries_its_own_state(self):
        states = {row['dc_number']: row['state'] for row in self._detail()['challans']}
        self.assertEqual(states, {'DC-FIRST':  'received',
                                  'DC-MIDDLE': 'partial',
                                  'DC-LAST':   'expected'})

    def test_05_quantity_is_received_over_dispatched_within_the_one_unit(self):
        quantity = self._detail()['quantity']
        self.assertIsNotNone(quantity, 'one unit in the category, so the aggregate '
                                       'must be present')
        self.assertEqual(quantity['unit'], 'Nos')
        # 300 + 100 + 50 dispatched; 300 + 60 arrived; the unconfirmed 50 has not.
        self.assertEqual(quantity['ordered'], Decimal('450'))
        self.assertEqual(quantity['received'], Decimal('360'))
        self.assertEqual(
            quantity['ordered'] - quantity['received'], Decimal('90'),
            'the inverter challan has leaked into the Solar Panels quantities')

    def test_06_the_lines_of_each_challan_travel_with_it(self):
        first = self._detail()['challans'][0]
        self.assertEqual(len(first['lines']), 1)
        line = first['lines'][0]
        self.assertEqual(line['item_description'], 'Mono PERC 540W')
        self.assertEqual(line['unit'], 'Nos')
        self.assertEqual(line['ordered'], Decimal('300.00'))
        self.assertEqual(line['received'], Decimal('300.00'))
        self.assertEqual(line['grn_confirmed_by'], self.pm)
        self.assertIsNotNone(line['grn_date'])

    def test_07_the_page_renders_every_challan_number(self):
        response = self._page()
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        for dc_number in ('DC-FIRST', 'DC-MIDDLE', 'DC-LAST'):
            self.assertIn(dc_number, body)
        self.assertNotIn('DC-INVERTER', body,
                         'the Solar Panels page is showing an inverter challan')


class PartialAndDamagedTests(DeliveryDetailFixture):
    """Short and damaged material is reported, and does not read as received.

    A bucket reading "fully received" while material is missing is the failure this
    whole subject exists to avoid — a PM scans for it precisely to stop worrying about
    a category.
    """

    def test_01_a_short_line_makes_its_challan_partial(self):
        self._challan('DC-SHORT', date(2026, 9, 1), [
            (PANEL_CATEGORY, 'Mono PERC 540W', 'Nos', '100', '80', 0),
        ])
        detail = self._detail()
        self.assertEqual(detail['challans'][0]['state'], 'partial')
        self.assertEqual(detail['consignments'],
                         {'received': 0, 'partial': 1, 'expected': 0, 'total': 1})

    def test_02_a_full_but_damaged_line_is_not_fully_received(self):
        """The whole quantity arrived and some of it arrived broken.

        `_dc_item_severity()` calls that amber, `sync_delivery_mirrors()` refuses Done
        on it, and this panel must not contradict either by calling the challan
        received.
        """
        self._challan('DC-DAMAGED', date(2026, 9, 1), [
            (PANEL_CATEGORY, 'Mono PERC 540W', 'Nos', '100', '100', 7),
        ])
        detail = self._detail()
        self.assertEqual(detail['challans'][0]['state'], 'partial')
        self.assertEqual(detail['consignments']['received'], 0)

    def test_03_damaged_is_reported_beside_received_and_not_subtracted(self):
        """Both numbers are facts and the GRN screens show both.

        Netting them here would invent a third number no column holds.
        """
        self._challan('DC-DAMAGED', date(2026, 9, 1), [
            (PANEL_CATEGORY, 'Mono PERC 540W', 'Nos', '100', '100', 7),
        ])
        detail = self._detail()
        self.assertEqual(detail['quantity']['received'], Decimal('100'))
        self.assertEqual(detail['quantity']['damaged'], 7)
        self.assertEqual(detail['challans'][0]['lines'][0]['damaged'], 7)

    def test_04_an_unconfirmed_line_adds_nothing_to_received(self):
        self._challan('DC-WAITING', date(2026, 9, 1), [
            (PANEL_CATEGORY, 'Mono PERC 540W', 'Nos', '100', None, 0),
        ])
        detail = self._detail()
        self.assertEqual(detail['quantity']['received'], 0)
        self.assertEqual(detail['quantity']['ordered'], Decimal('100'))
        self.assertEqual(detail['challans'][0]['state'], 'expected')

    def test_05_one_good_line_does_not_make_the_whole_challan_received(self):
        """A challan carrying two lines of the same material, one complete and one
        short, is PARTIAL — the complete line does not speak for the challan.

        Written because a single-line fixture cannot tell "every line arrived" from
        "some line arrived": both read the same when there is only one. This is the
        case that separates them, and it is the ordinary shape of a real challan.
        """
        self._challan('DC-MIXED-LINES', date(2026, 9, 1), [
            (PANEL_CATEGORY, 'Mono PERC 540W', 'Nos', '100', '100', 0),
            (PANEL_CATEGORY, 'Mono PERC 545W', 'Nos', '100', '40', 0),
        ])
        detail = self._detail()
        self.assertEqual(detail['challans'][0]['state'], 'partial')
        self.assertEqual(detail['consignments'],
                         {'received': 0, 'partial': 1, 'expected': 0, 'total': 1})

    def test_06_a_confirmed_line_beside_an_unconfirmed_one_is_still_partial(self):
        """The same trap from the other side: half the challan has had no GRN."""
        self._challan('DC-HALF-GRN', date(2026, 9, 1), [
            (PANEL_CATEGORY, 'Mono PERC 540W', 'Nos', '100', '100', 0),
            (PANEL_CATEGORY, 'Mono PERC 545W', 'Nos', '100', None, 0),
        ])
        self.assertEqual(self._detail()['challans'][0]['state'], 'partial')

    def test_07_the_damaged_quantity_reaches_the_page(self):
        self._challan('DC-DAMAGED', date(2026, 9, 1), [
            (PANEL_CATEGORY, 'Mono PERC 540W', 'Nos', '100', '100', 7),
        ])
        self.assertIn('damaged', self._page().content.decode().lower())


class MixedUnitsTests(DeliveryDetailFixture):
    """Two units in one category: no aggregate at all, per-line figures instead."""

    def setUp(self):
        super().setUp()
        self._challan('DC-NOS', date(2026, 9, 1), [
            (PANEL_CATEGORY, 'Mono PERC 540W', 'Nos', '300', '300', 0),
        ])
        self._challan('DC-LOT', date(2026, 9, 5), [
            (PANEL_CATEGORY, 'Module spares kit', 'Lot', '2', '1', 0),
        ])

    def test_01_no_aggregate_is_offered(self):
        self.assertIsNone(
            self._detail()['quantity'],
            'two units in one category: 300 Nos and 2 Lot cannot be added, so there '
            'is no aggregate to show')

    def test_02_both_units_are_named_so_the_page_can_say_why(self):
        self.assertEqual(sorted(self._detail()['units']), ['Lot', 'Nos'])

    def test_03_the_per_line_figures_are_still_there(self):
        lines = [line for row in self._detail()['challans'] for line in row['lines']]
        self.assertEqual(
            sorted((line['unit'], line['ordered'], line['received']) for line in lines),
            sorted([('Lot', Decimal('2.00'), Decimal('1.00')),
                    ('Nos', Decimal('300.00'), Decimal('300.00'))]),
            'the aggregate is withheld, not the data')

    def test_04_consignments_are_unaffected_by_the_unit_split(self):
        """Challans are countable whatever they are measured in."""
        self.assertEqual(self._detail()['consignments'],
                         {'received': 1, 'partial': 1, 'expected': 0, 'total': 2})

    def test_05_the_page_says_why_there_is_no_total(self):
        body = self._page().content.decode()
        self.assertIn('Mixed units', body)
        self.assertIn('Lot', body)


class NoChallansTests(DeliveryDetailFixture):
    """Nothing raised yet — an answer, not a blank."""

    def test_01_the_helper_returns_empty_counts_rather_than_none(self):
        detail = self._detail()
        self.assertIsNotNone(detail, 'a delivery mirror with no challans is still a '
                                     'delivery mirror')
        self.assertEqual(detail['challans'], [])
        self.assertEqual(detail['consignments'],
                         {'received': 0, 'partial': 0, 'expected': 0, 'total': 0})
        self.assertIsNone(detail['quantity'])
        self.assertEqual(detail['units'], [])

    def test_02_the_empty_state_names_the_category(self):
        """R5. A blank panel reads as a broken page; the category is the thing the PM
        would otherwise have to infer from the task name."""
        response = self._page()
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('No delivery challan has been raised', body)
        self.assertIn(PANEL_CATEGORY, body)

    def test_03_a_site_with_no_challans_at_all_renders_every_delivery_mirror(self):
        for name in ('Delivery — Solar Panels', 'Delivery — Inverters',
                     'Delivery — BOS Kit', 'Delivery — MMS'):
            with self.subTest(task=name):
                self.assertEqual(self._page(self._task(name)).status_code, 200)


class NotADeliveryTaskTests(DeliveryDetailFixture):
    """The helper answers None for everything else, and the panel disappears with it."""

    def test_01_a_mirror_with_no_delivery_source_gets_no_panel(self):
        """COD is a mirror whose source object does not exist yet. An empty delivery
        panel on it would be a lie about what it is waiting for."""
        task = self._task(NON_DELIVERY_MIRROR_NAME)
        self.assertTrue(task.is_mirror, 'fixture wrong: COD is supposed to be a mirror')
        self.assertIsNone(delivery_detail_for_task(task))

    def test_02_a_human_owned_task_gets_no_panel(self):
        task = self._task(CONTROL_TASK_NAME)
        self.assertFalse(task.is_mirror, 'fixture wrong: the control task is a mirror')
        self.assertIsNone(delivery_detail_for_task(task))

    def test_03_a_task_added_by_hand_gets_no_panel_whatever_it_is_called(self):
        """There is no name matching in the helper and there must never be.

        A row called "Delivery_Solar panel lot-2" carries no `template_task`, so it is
        not one of the four buckets and nothing about the challans is its to report.
        """
        phase = self._task().phase
        hand_added = Task.objects.create(
            phase=phase, task_name='Delivery_Solar panel lot-2',
            task_order=99, assigned_role='SCM',
        )
        self.assertIsNone(hand_added.template_task_id)
        self.assertIsNone(delivery_detail_for_task(hand_added))

    def test_04_the_page_of_a_non_delivery_task_carries_no_panel(self):
        body = self._page(self._task(CONTROL_TASK_NAME)).content.decode()
        self.assertNotIn('deliveryDetailSection', body)
        self.assertNotIn('No delivery challan has been raised', body)

    def test_05_a_residential_task_is_untouched(self):
        """The predicate fails on project type without ever asking about it: the
        Residential template has no task carrying a delivery code."""
        for task in Task.objects.filter(phase__project__project_type='Residential')[:20]:
            self.assertIsNone(delivery_detail_for_task(task))


class PermissionTests(DeliveryDetailFixture):
    """R8: the panel is scoped by the check task_detail already makes."""

    def test_01_a_pm_who_does_not_own_the_site_is_refused_not_crashed(self):
        stranger = _profile('t4_other_pm', 'PM')
        self._challan('DC-FIRST', date(2026, 9, 1), [
            (PANEL_CATEGORY, 'Mono PERC 540W', 'Nos', '300', '300', 0),
        ])
        response = self._page(profile=stranger)
        self.assertEqual(response.status_code, 404,
                         'project scope must refuse, and must not 500')

    def test_02_the_owning_pm_sees_it(self):
        self._challan('DC-FIRST', date(2026, 9, 1), [
            (PANEL_CATEGORY, 'Mono PERC 540W', 'Nos', '300', '300', 0),
        ])
        self.assertEqual(self._page().status_code, 200)

    def test_03_a_portfolio_role_sees_it_too(self):
        """SCM is portfolio-wide in user_can_view_project, and raises the challans."""
        scm = _profile('t4_scm', 'SCM')
        self._challan('DC-FIRST', date(2026, 9, 1), [
            (PANEL_CATEGORY, 'Mono PERC 540W', 'Nos', '300', '300', 0),
        ])
        self.assertEqual(self._page(profile=scm).status_code, 200)


class LabellingTests(DeliveryDetailFixture):
    """R4, read off the rendered page.

    The quantity figure is received against what was DISPATCHED. There is no join from
    a delivery line to the site's BOQ, so the page cannot say what share of the site's
    requirement has arrived — and a "% delivered" label would say exactly that.
    """

    def setUp(self):
        super().setUp()
        self._challan('DC-FIRST', date(2026, 9, 1), [
            (PANEL_CATEGORY, 'Mono PERC 540W', 'Nos', '300', '150', 0),
        ])

    def test_01_the_page_says_dispatched(self):
        body = self._page().content.decode()
        self.assertIn('dispatched', body.lower())

    def test_02_the_page_says_what_the_figure_is_not(self):
        self.assertIn("not the site's requirement", self._page().content.decode())

    def test_03_the_page_never_calls_it_delivered_or_complete(self):
        body = self._page().content.decode().lower()
        for forbidden in ('% delivered', '% complete', 'percent delivered'):
            self.assertNotIn(forbidden, body,
                             f'the panel is claiming {forbidden!r}, which requires a '
                             f'BOQ denominator that does not exist (B-18)')


class AgreementTests(DeliveryDetailFixture):
    """R7: the panel reports the state; the derivation still decides it.

    Reached through `recalculate_dc_status()`, which is one of the two callers the
    derivation is allowed to have — this module must not name the derivation itself.
    """

    def test_01_all_lines_green_means_both_fully_received_and_Done(self):
        challan = self._challan('DC-FULL', date(2026, 9, 1), [
            (PANEL_CATEGORY, 'Mono PERC 540W', 'Nos', '300', '300', 0),
        ])
        recalculate_dc_status(challan)
        detail = self._detail()
        self.assertEqual(detail['consignments'],
                         {'received': 1, 'partial': 0, 'expected': 0, 'total': 1})
        self.assertEqual(self._task().status, Task.DONE,
                         'the panel says fully received while the badge does not say '
                         'Done — the two have diverged')

    def test_02_a_short_challan_holds_both_at_partial_and_In_Progress(self):
        challan = self._challan('DC-SHORT', date(2026, 9, 1), [
            (PANEL_CATEGORY, 'Mono PERC 540W', 'Nos', '300', '200', 0),
        ])
        recalculate_dc_status(challan)
        self.assertEqual(self._detail()['consignments']['received'], 0)
        self.assertEqual(self._task().status, Task.IN_PROGRESS)

    def test_03_nothing_raised_means_no_consignments_and_Not_Started(self):
        self.assertEqual(self._detail()['consignments']['total'], 0)
        self.assertEqual(self._task().status, Task.NOT_STARTED)


class ProjectOverviewRowTests(DeliveryDetailFixture):
    """R6: the same counts, compact, on the delivery mirror's row."""

    def _overview(self):
        client = Client()
        client.force_login(self.pm.user)
        return client.get(
            reverse('project_overview', args=[self.site.project_id]))

    def test_01_the_row_carries_the_consignment_count(self):
        self._challan('DC-FIRST', date(2026, 9, 1), [
            (PANEL_CATEGORY, 'Mono PERC 540W', 'Nos', '300', '300', 0),
        ])
        self._challan('DC-SECOND', date(2026, 9, 5), [
            (PANEL_CATEGORY, 'Mono PERC 540W', 'Nos', '100', None, 0),
        ])
        response = self._overview()
        self.assertEqual(response.status_code, 200)
        self.assertIn('1/2 consignments received', response.content.decode())

    def test_02_a_bucket_with_nothing_raised_says_so(self):
        self.assertIn('No consignments yet', self._overview().content.decode())

    def test_03_the_count_is_per_category_not_per_site(self):
        """Four delivery mirrors, one challan, one of them counts it."""
        self._challan('DC-INV', date(2026, 9, 1), [
            (OTHER_CATEGORY, 'String inverter 60kW', 'Nos', '2', '2', 0),
        ])
        body = self._overview().content.decode()
        self.assertEqual(body.count('1/1 consignment received'), 1,
                         'one challan in one category must count on exactly one row')
        self.assertEqual(body.count('No consignments yet'), 3)
