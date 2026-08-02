"""
Part 11 verification — the OPEX BOQ catalogue, the picker, and the design lock.

WHY THIS FILE EXISTS
--------------------
Part 11 does three things that can each fail invisibly:

  * it SCOPES a table that Residential depends on. `get_standard_boq_items()` has fed
    every Residential BOQ since Part 0.5, and adding 207 rows to the table it reads would
    quietly put OPEX tender items on a rooftop house BOQ. The scoping is asserted from
    both directions — Residential gets exactly its own rows, and creating a Residential
    BOQ end to end produces exactly what it produced before.

  * it adds a LOCK that did not exist. Before Part 11 an OPEX BOQ stayed editable from
    the first draft until the Part 6 group lock — a designer could rewrite quantities
    while Design QC was reading them. Every point on the lock progression is exercised
    BY DIRECT POST rather than by asserting a button is absent: a hidden button is not a
    permission.

  * it makes reopening TOTAL. A rejection categorised `boq_quantity` may need a line that
    was never on the sheet, so "the designer can edit again" is not enough — test 12
    adds an item that did not previously exist and asserts it saved.

Numbered VERIFICATION comments map to the session brief's verification list.
"""
import importlib
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    Program, Project, UserProfile, BOQ, BOQItem, BOQItemMaster, Task, NotificationLog,
    DesignAssignment, DesignAttempt, ArkaSubmission, SiteGroup, SiteGroupMembership,
    SITE_GROUP_DRAFT, SITE_GROUP_LOCKED,
    DESIGN_IN_DESIGN, DESIGN_ARKA_SUBMITTED, DESIGN_ARTIFACTS_UPLOADED, DESIGN_IN_QC,
    DESIGN_AWAITING_HEAD_QC, DESIGN_RELEASED,
    ARKA_PENDING, ARKA_APPROVED,
    QC_PASSED, QC_FAILED,
    ATTEMPT_REASON_INITIAL, ATTEMPT_REASON_QC_FAILED, ATTEMPT_REASON_PM_CHANGE_REQUEST,
    ERR_BOQ_QUANTITY, ERR_DRAWING_INCOMPLETE,
    DESIGN_FILE_CAD_ZIP, DesignFile,
    get_standard_boq_items, get_opex_boq_catalogue, opex_catalogue_category_order,
)
from .permissions import project_boq_is_design_locked

# The migration's frozen literal IS the catalogue. Reading it here rather than
# re-parsing the spreadsheet means these tests assert the thing that actually ships, and
# the suite runs with MIGRATION_MODULES disabled so nothing else would have created it.
_MIGRATION = importlib.import_module(
    'projects.migrations.0057_boqitemmaster_project_type_opex_catalogue')
OPEX_BOQ_ITEMS = _MIGRATION.OPEX_BOQ_ITEMS
EXPECTED_CATEGORY_COUNTS = _MIGRATION.EXPECTED_CATEGORY_COUNTS
EXPECTED_UNITS = _MIGRATION.EXPECTED_UNITS

#: A stand-in for the 37 Part 0.5 rows. The count does not matter to anything Part 11
#: changed — what matters is that get_standard_boq_items() returns THESE and only these.
#:
#: THE LAST THREE ARE THE COLLIDING DESCRIPTIONS, and they are in this fixture on purpose.
#: `PVC Elbow 25MM`, `PVC Tee 25MM` and `Silver Spray Paint` exist in BOTH catalogues on
#: the real data (ITM-015/016/024 against OPX-131/132/193), and boq_detail's seeding builds
#: its master lookup as a dict KEYED BY DESCRIPTION. Unscoped, the OPEX row wins the key —
#: it sorts later — and a Residential BOQ line silently carries an item_master pointing
#: into the OPEX catalogue, which is the join Part 6 aggregation runs on. Nothing on screen
#: would look wrong. A fixture without a collision cannot catch it.
RESIDENTIAL_SEED = [
    ('Solar Modules', '595Wp Solar modules DCR', 'Nos'),
    ('Structure',     'Module Mounting Structure with STAAD report HDGI/GI', 'LOT'),
    ('Inverter',      '10 kW Grid-Tie Inverter Single Phase', 'Nos'),
    ('BOS',           'MC4 Connectors Male and Female', 'Nos'),
    ('BOS',           'Contingency', 'LS'),
    ('BOS',           'PVC Elbow 25MM', 'Nos'),          # also OPX-131
    ('BOS',           'PVC Tee 25MM', 'Nos'),            # also OPX-132
    ('BOS',           'Silver Spray Paint', 'Nos'),      # also OPX-193, unit Kg
]


def _profile(username, role, is_design_head=False, is_design_qc=False):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.is_design_head = is_design_head
    profile.is_design_qc = is_design_qc
    profile.save()
    return profile


class Part11Base(TestCase):
    """The real catalogue — 207 OPEX rows from the migration literal, plus a Residential
    template — and one OPEX site with a designer, a QC reviewer and a Head."""

    def setUp(self):
        BOQItemMaster.objects.bulk_create([
            BOQItemMaster(code=f'ITM-{i:03d}', description=description, unit=unit,
                          category=category, project_type='Residential',
                          is_active=True, sort_order=i)
            for i, (category, description, unit) in enumerate(RESIDENTIAL_SEED, start=1)
        ])
        BOQItemMaster.objects.bulk_create([
            BOQItemMaster(code=f'OPX-{i:03d}', description=description, unit=unit,
                          category=category, project_type='OPEX',
                          is_active=True, sort_order=i)
            for i, (category, description, unit) in enumerate(OPEX_BOQ_ITEMS, start=1)
        ])

        self.designer = _profile('des11',  'Design')
        self.other    = _profile('des11b', 'Design')
        self.qc       = _profile('qc11',   'Design', is_design_qc=True)
        self.head     = _profile('head11', 'Design', is_design_head=True)
        self.pm       = _profile('pm11',   'PM')
        self.scm      = _profile('scm11',  'SCM')

        self.program = Program.objects.create(
            name='Test-Part11', program_type='OPEX', client_name='P11Client',
            status='Active', short_tender_code='P11')

        self.site, self.assignment = self._opex_site('P11-A')

    # ── fixtures ────────────────────────────────────────────────────────────

    def _opex_site(self, code, designer=None):
        site = Project(
            project_id=code, customer_name='P11Client', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code=code,
            capacity_kw=Decimal('100.00'), status='Draft',
            assigned_design=designer or self.designer, assigned_pm=self.pm)
        site.save()
        assignment = DesignAssignment.objects.create(
            project=site, status=DESIGN_IN_DESIGN,
            assigned_to=designer or self.designer,
            survey_file_bucket='b', survey_file_path=f'{code}/survey/x.pdf')
        return site, assignment

    def _residential_site(self, code):
        site = Project(
            project_id=code, customer_name='House', customer_phone='9876543211',
            site_address='2 Sun Rd', city='Delhi', project_type='Residential',
            capacity_kw=Decimal('10.00'), status='Draft',
            assigned_design=self.designer, assigned_pm=self.pm)
        site.save()
        return site

    def _approved_arka(self, assignment):
        """An attempt carrying an Arka approved at BOTH gates — the state CAD and BOQ
        completion require. Written directly; the gates themselves are Part 9's tests."""
        attempt = DesignAttempt.objects.create(
            assignment=assignment, attempt_number=1,
            opened_reason=ATTEMPT_REASON_INITIAL)
        assignment.current_attempt_number = 1
        assignment.status = DESIGN_ARKA_SUBMITTED
        assignment.save()
        arka = ArkaSubmission.objects.create(
            attempt=attempt, version=1, capacity_kw=Decimal('120.00'),
            arka_link='https://example.com/arka', submitted_by=assignment.assigned_to,
            verdict=ARKA_APPROVED, head_verdict=ARKA_APPROVED, is_current=True)
        DesignFile.objects.create(
            attempt=attempt, kind=DESIGN_FILE_CAD_ZIP, version=1,
            bucket='b', path=f'{assignment.project.project_id}/cad/x.zip',
            original_filename='x.zip', size_bytes=10, content_type='application/zip',
            derived_from_arka=arka,      # NOT NULL — the Part 3 artifact pairing
            uploaded_by=assignment.assigned_to, is_current=True)
        return attempt

    def _login(self, profile):
        self.assertTrue(self.client.login(username=profile.user.username, password='x'))

    def _entry_url(self, site=None):
        return reverse('opex_boq_entry',
                       kwargs={'project_id': (site or self.site).project_id})

    def _master(self, code):
        return BOQItemMaster.objects.get(code=code)

    def _save_sheet(self, codes_and_quantities, site=None, keep_rows=(), action='save_draft'):
        """POST the picker form the way the browser does: one `item` per chosen master
        and a `qty_<pk>` beside it."""
        data = {'action': action, 'item': [], 'keep_row': [str(pk) for pk in keep_rows]}
        for code, quantity in codes_and_quantities:
            master = self._master(code)
            data['item'].append(str(master.pk))
            data[f'qty_{master.pk}'] = str(quantity)
        return self.client.post(self._entry_url(site), data)

    def _sheet_codes(self, site=None):
        project = site or self.site
        return sorted(
            row.item_master.code
            for row in BOQItem.objects.filter(boq__project=project)
            if row.item_master_id)


# ===========================================================================
# 1-4. The catalogue itself
# ===========================================================================

class CatalogueTests(Part11Base):

    def test_01_counts_by_project_type(self):
        """VERIFICATION 1 — 207 OPEX, and the Residential rows untouched beside them."""
        self.assertEqual(
            BOQItemMaster.objects.filter(project_type='OPEX').count(), 207)
        self.assertEqual(
            BOQItemMaster.objects.filter(project_type='Residential').count(),
            len(RESIDENTIAL_SEED))
        self.assertEqual(len(OPEX_BOQ_ITEMS), 207)

    def test_02_per_category_counts(self):
        """VERIFICATION 2 — every category count matches the brief exactly."""
        counts = {}
        for row in BOQItemMaster.objects.filter(project_type='OPEX'):
            counts[row.category] = counts.get(row.category, 0) + 1
        self.assertEqual(counts, EXPECTED_CATEGORY_COUNTS)

        # And the ORDER is spreadsheet order, derived rather than stored.
        self.assertEqual(opex_catalogue_category_order(),
                         list(EXPECTED_CATEGORY_COUNTS.keys()))

    def test_03_units_are_the_seven_normalised_values(self):
        """VERIFICATION 3 — nine source spellings collapsed to seven units."""
        units = set(BOQItemMaster.objects
                    .filter(project_type='OPEX')
                    .values_list('unit', flat=True))
        self.assertEqual(units, EXPECTED_UNITS)
        # The two normalisations specifically: no dotted spelling survives.
        self.assertNotIn('Nos.', units)
        self.assertNotIn('Mtr.', units)

    def test_04_duplicate_descriptions_are_eight_distinct_rows(self):
        """VERIFICATION 4 — the four lug descriptions exist twice, not deduplicated."""
        rows = list(BOQItemMaster.objects
                    .filter(project_type='OPEX',
                            description__in=['4Sqmm*Cu', '6Sqmm*Cu',
                                             '10Sqmm*Cu', '16Sqmm*Cu'])
                    .order_by('sort_order'))
        self.assertEqual(len(rows), 8)
        self.assertEqual(len({r.code for r in rows}), 8)          # distinct codes
        self.assertEqual([r.category for r in rows],
                         ['Pin Type Lug'] * 4 + ['Ring Type Lug'] * 4)
        # Verbatim — not prefixed, suffixed or disambiguated in the text itself.
        for row in rows:
            self.assertIn(row.description,
                          ['4Sqmm*Cu', '6Sqmm*Cu', '10Sqmm*Cu', '16Sqmm*Cu'])

    def test_04b_descriptions_are_whitespace_collapsed_and_otherwise_verbatim(self):
        """Settled decision 8 — collapsed, and nothing else changed."""
        for row in BOQItemMaster.objects.filter(project_type='OPEX'):
            self.assertNotIn('\n', row.description)
            self.assertNotIn('  ', row.description)
            self.assertEqual(row.description, row.description.strip())
        # A source typo is preserved: the catalogue must match what design wrote.
        self.assertTrue(BOQItemMaster.objects.filter(
            project_type='OPEX', description__contains='proctected').exists())


# ===========================================================================
# 5-6. Residential is untouched
# ===========================================================================

class ResidentialUnaffectedTests(Part11Base):

    def test_05_get_standard_boq_items_returns_residential_only(self):
        """VERIFICATION 5 — same shape, Residential rows only, OPEX absent."""
        items = get_standard_boq_items()
        self.assertEqual(len(items), len(RESIDENTIAL_SEED))
        self.assertEqual(sorted(items[0].keys()),
                         ['category', 'description', 'serial_no', 'uom'])
        self.assertEqual(
            [(i['category'], i['description'], i['uom']) for i in items],
            RESIDENTIAL_SEED)
        descriptions = {i['description'] for i in items}
        self.assertNotIn('Solar PV Module', descriptions)      # OPX-001
        self.assertNotIn('4Sqmm*Cu', descriptions)             # OPX-101

    def test_06_residential_boq_creation_is_unchanged(self):
        """VERIFICATION 6 — create one end to end and compare it to the template."""
        site = self._residential_site('HRP-RES-P11')
        self._login(self.designer)
        response = self.client.get(
            reverse('boq_detail', kwargs={'project_id': site.project_id}))
        self.assertEqual(response.status_code, 200)     # NOT redirected — this is not OPEX

        boq = BOQ.objects.get(project=site)
        rows = list(boq.items.order_by('serial_no'))
        self.assertEqual(len(rows), len(RESIDENTIAL_SEED))
        self.assertEqual(
            [(r.serial_no, r.category, r.description, r.uom) for r in rows],
            [(i['serial_no'], i['category'], i['description'], i['uom'])
             for i in get_standard_boq_items()])
        # Every row is catalogue-linked, and EVERY LINK POINTS AT A RESIDENTIAL MASTER.
        # This is the assertion that catches the description-collision regression: the
        # three colliding rows in RESIDENTIAL_SEED would otherwise link into the OPEX
        # catalogue and nothing on the page would look different.
        self.assertTrue(all(r.item_master_id for r in rows))
        self.assertTrue(all(r.item_master.project_type == 'Residential' for r in rows))
        for description in ('PVC Elbow 25MM', 'PVC Tee 25MM', 'Silver Spray Paint'):
            row = boq.items.get(description=description)
            self.assertTrue(row.item_master.code.startswith('ITM-'),
                            f'{description} linked to {row.item_master.code}')

    def test_06b_residential_boq_is_never_design_locked(self):
        """The lock is structurally False without a DesignAssignment, which is what makes
        it safe to AND into the shared boq_detail write gate."""
        site = self._residential_site('HRP-RES-P11B')
        self.assertFalse(project_boq_is_design_locked(site))

    def test_06c_picker_404s_on_a_residential_project(self):
        site = self._residential_site('HRP-RES-P11C')
        self._login(self.designer)
        response = self.client.get(
            reverse('opex_boq_entry', kwargs={'project_id': site.project_id}))
        self.assertEqual(response.status_code, 404)


# ===========================================================================
# 7-9. The picker
# ===========================================================================

class PickerTests(Part11Base):

    def test_07_catalogue_payload_supports_the_documented_searches(self):
        """VERIFICATION 7 — "inverter" and "6sqmm", against the real catalogue.

        THE BRIEF EXPECTS 15 FOR "inverter" AND THE TRUE ANSWER IS 18, because three items
        outside the Inverter category mention an inverter in their description: two
        Solar Meter + CT rows ("Below/Above 20KW Inverter CT is ...") and one BOS row
        ("Fasteners for Inverter/DCDB/ACDB Mounting"). 15 is the size of the Inverter
        CATEGORY, which is what the category dropdown returns.

        The search is deliberately NOT narrowed to make the number match. A designer
        typing "inverter" who is looking for the mounting fasteners should find them; a
        search that hid three genuine hits to satisfy a count would be a worse screen.
        Both numbers are pinned here so the distinction stays visible.
        """
        self._login(self.designer)
        response = self.client.get(self._entry_url())
        self.assertEqual(response.status_code, 200)
        catalogue = json.loads(response.context['catalogue_json'])
        self.assertEqual(len(catalogue), 207)

        # The picker matches on description, category and code — same predicate as the JS.
        def matches(query):
            q = query.lower()
            return [i for i in catalogue
                    if q in i['desc'].lower() or q in i['cat'].lower()
                    or q in i['code'].lower()]

        inverter = matches('inverter')
        self.assertEqual(len(inverter), 18)
        self.assertEqual(len([i for i in inverter if i['cat'] == 'Inverter']), 15)
        self.assertEqual(
            sorted({i['cat'] for i in inverter if i['cat'] != 'Inverter'}),
            ['BOS', 'Solar Meter + CT'])
        # The category dropdown — which is what returns exactly 15.
        self.assertEqual(len([i for i in catalogue if i['cat'] == 'Inverter']), 15)

        sixsqmm = matches('6sqmm')
        self.assertGreater(len({i['cat'] for i in sixsqmm}), 1)
        # Category is carried on every result, which is what disambiguates the duplicates.
        self.assertTrue(all(i['cat'] for i in sixsqmm))
        lugs = [i for i in sixsqmm if i['desc'] == '6Sqmm*Cu']
        self.assertEqual(sorted(i['cat'] for i in lugs),
                         ['Pin Type Lug', 'Ring Type Lug'])

    def test_07b_boq_starts_empty_and_a_get_writes_nothing(self):
        """The whole point of the picker: no pre-population, and no row created on GET."""
        self._login(self.designer)
        response = self.client.get(self._entry_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.context['added_json']), [])
        self.assertFalse(BOQ.objects.filter(project=self.site).exists())

    def test_07c_opex_designer_is_redirected_from_boq_detail(self):
        """boq_detail must not seed an OPEX site with the Residential template."""
        self._login(self.designer)
        response = self.client.get(
            reverse('boq_detail', kwargs={'project_id': self.site.project_id}))
        self.assertRedirects(response, self._entry_url(),
                             fetch_redirect_response=False)
        self.assertFalse(BOQ.objects.filter(project=self.site).exists())

    def test_07d_non_author_roles_still_read_boq_detail(self):
        """SCM and PM have no picker; the redirect must not catch them."""
        self._save_sheet_as_designer()
        for profile in (self.scm, self.pm):
            self.client.logout()
            self._login(profile)
            response = self.client.get(
                reverse('boq_detail', kwargs={'project_id': self.site.project_id}))
            self.assertEqual(response.status_code, 200, profile.user.username)

    def _save_sheet_as_designer(self):
        self._login(self.designer)
        self._save_sheet([('OPX-001', 40), ('OPX-008', 2)])
        self.client.logout()

    def test_08_add_six_items_save_draft_and_reload(self):
        """VERIFICATION 8 — six persist with their quantities intact."""
        self._login(self.designer)
        chosen = [('OPX-001', 40), ('OPX-008', 2), ('OPX-059', 250),
                  ('OPX-101', 24), ('OPX-105', 24), ('OPX-193', '1.5')]
        response = self._save_sheet(chosen)
        self.assertEqual(response.status_code, 302)

        rows = BOQItem.objects.filter(boq__project=self.site)
        self.assertEqual(rows.count(), 6)
        self.assertEqual(self._sheet_codes(),
                         ['OPX-001', 'OPX-008', 'OPX-059', 'OPX-101', 'OPX-105', 'OPX-193'])

        stored = {r.item_master.code: r for r in rows.select_related('item_master')}
        self.assertEqual(stored['OPX-001'].boq_quantity, Decimal('40'))
        self.assertEqual(stored['OPX-193'].boq_quantity, Decimal('1.5'))
        # Snapshot fields come from the catalogue row, including the real unit/category.
        self.assertEqual(stored['OPX-059'].uom, 'Meter')
        self.assertEqual(stored['OPX-101'].category, 'Pin Type Lug')
        self.assertEqual(stored['OPX-105'].category, 'Ring Type Lug')
        self.assertEqual(stored['OPX-101'].description, '4Sqmm*Cu')
        # serial_no comes from the catalogue's sort_order, not click order.
        self.assertEqual(stored['OPX-101'].serial_no, 101)

        # Reload: the six come back, and the picker excludes them from the catalogue side.
        response = self.client.get(self._entry_url())
        added = json.loads(response.context['added_json'])
        self.assertEqual(len(added), 6)
        self.assertEqual({a['qty'] for a in added} & {'40', '1.5'}, {'40', '1.5'})

    def test_08b_save_is_a_reconciliation_so_removal_deletes(self):
        self._login(self.designer)
        self._save_sheet([('OPX-001', 40), ('OPX-008', 2), ('OPX-059', 250)])
        self.assertEqual(len(self._sheet_codes()), 3)
        self._save_sheet([('OPX-001', 41)])            # two dropped from the POST
        self.assertEqual(self._sheet_codes(), ['OPX-001'])
        row = BOQItem.objects.get(boq__project=self.site)
        self.assertEqual(row.boq_quantity, Decimal('41'))

    def test_08c_a_forged_item_id_is_dropped_not_trusted(self):
        """Residential master pks and junk must not become OPEX BOQ rows."""
        self._login(self.designer)
        residential = BOQItemMaster.objects.filter(project_type='Residential').first()
        response = self.client.post(self._entry_url(), {
            'action': 'save_draft',
            'item': [str(residential.pk), '999999', 'abc',
                     str(self._master('OPX-001').pk)],
            f'qty_{self._master("OPX-001").pk}': '5',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._sheet_codes(), ['OPX-001'])

    def test_09_empty_category_indicator(self):
        """VERIFICATION 9 — untouched categories are reported, touched ones are not."""
        self._login(self.designer)
        response = self.client.get(self._entry_url())
        self.assertEqual(response.context['empty_categories'],
                         list(EXPECTED_CATEGORY_COUNTS.keys()))   # all 16, nothing added

        self._save_sheet([('OPX-001', 40), ('OPX-008', 2)])       # Module, Inverter
        response = self.client.get(self._entry_url())
        empties = response.context['empty_categories']
        self.assertNotIn('Module', empties)
        self.assertNotIn('Inverter', empties)
        self.assertIn('Earthing', empties)
        self.assertEqual(len(empties), 14)

    def test_09b_only_the_assigned_designer_may_write(self):
        """user_can_edit_project_boq is unchanged; the picker just applies it."""
        self._login(self.other)
        response = self._save_sheet([('OPX-001', 40)])
        self.assertEqual(response.status_code, 403)
        self.assertFalse(BOQ.objects.filter(project=self.site).exists())


# ===========================================================================
# 10-14. The lock progression
# ===========================================================================

class DesignLockTests(Part11Base):

    def setUp(self):
        super().setUp()
        self.attempt = self._approved_arka(self.assignment)
        self._login(self.designer)
        self._save_sheet([('OPX-001', 40), ('OPX-008', 2), ('OPX-059', 250),
                          ('OPX-101', 24), ('OPX-105', 24), ('OPX-193', '1.5')])

    def _mark_complete(self):
        return self.client.post(
            reverse('design_boq_complete',
                    kwargs={'project_id': self.site.project_id}), {})

    def _to_qc(self):
        """Move the site to `in_qc` the way design_qc_start would."""
        self.assignment.refresh_from_db()
        self.assignment.status = DESIGN_IN_QC
        self.assignment.save()
        self.attempt.refresh_from_db()
        self.attempt.qc_started_at = timezone.now()
        self.attempt.save(update_fields=['qc_started_at'])

    def test_10_mark_complete_freezes_the_boq(self):
        """VERIFICATION 10 — confirmed by direct POST, not by a missing button."""
        self.assertFalse(project_boq_is_design_locked(self.site))
        self._mark_complete()
        self.attempt.refresh_from_db()
        self.assertIsNotNone(self.attempt.boq_submitted_at)
        self.assertTrue(project_boq_is_design_locked(self.site))

        before = self._sheet_codes()
        response = self._save_sheet([('OPX-001', 999)])          # direct POST
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._sheet_codes(), before)            # nothing removed
        row = BOQItem.objects.get(boq__project=self.site,
                                  item_master__code='OPX-001')
        self.assertEqual(row.boq_quantity, Decimal('40'))        # nothing changed

        # And the older endpoints are shut too, so the picker is not the only door.
        response = self.client.post(
            reverse('boq_detail', kwargs={'project_id': self.site.project_id}),
            {'action': 'save_design', f'boq_qty_{row.pk}': '888'})
        self.assertEqual(response.status_code, 302)
        row.refresh_from_db()
        self.assertEqual(row.boq_quantity, Decimal('40'))

        response = self.client.post(
            reverse('boq_submit', kwargs={'project_id': self.site.project_id}), {})
        self.assertEqual(response.status_code, 403)

        # The screen says WHY, rather than rendering an inert form.
        response = self.client.get(self._entry_url())
        self.assertFalse(response.context['can_edit'])
        self.assertIn('design review', response.context['lock_reason'])

    def test_11_design_qc_sees_only_the_added_items(self):
        """VERIFICATION 11 — six rows on the review screen, never 207."""
        self._mark_complete()
        self._to_qc()
        self.client.logout()
        self._login(self.qc)
        response = self.client.get(
            reverse('design_qc_review', kwargs={'project_id': self.site.project_id}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['boq_row_count'], 6)
        self.assertEqual(response.context['boq_quantity_count'], 6)

        grouped = response.context['boq_by_category']
        self.assertEqual(sum(len(rows) for _c, rows in grouped), 6)
        # Grouped in CATALOGUE order, the same order the entry screen used.
        self.assertEqual([c for c, _rows in grouped],
                         ['Module', 'Inverter', 'DC Cable',
                          'Pin Type Lug', 'Ring Type Lug', 'BOS'])
        self.assertEqual(response.context['boq_off_catalogue'], [])

        # Read-only: no write path is offered, and the reviewer cannot take one anyway.
        response = self._save_sheet([('OPX-001', 1)])
        self.assertEqual(response.status_code, 403)

    def test_12_qc_rejection_reopens_the_full_picker(self):
        """VERIFICATION 12 — the designer adds an item that was NEVER on the sheet."""
        self._mark_complete()
        self._to_qc()
        self.client.logout()
        self._login(self.qc)
        response = self.client.post(
            reverse('design_qc_fail', kwargs={'project_id': self.site.project_id}),
            {'qc_remarks': 'Cable schedule short by one run.',
             'error_category': ERR_BOQ_QUANTITY,
             'redo_scope_submitted': '1', 'redo': ['boq']})
        self.assertEqual(response.status_code, 302)

        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.current_attempt_number, 2)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.qc_verdict, QC_FAILED)

        new_attempt = self.assignment.attempts.get(attempt_number=2)
        self.assertIsNone(new_attempt.boq_submitted_at)
        self.assertFalse(project_boq_is_design_locked(self.site))

        # THE REOPEN REQUIREMENT: add a line that did not exist before, not merely edit one.
        self.client.logout()
        self._login(self.designer)
        response = self.client.get(self._entry_url())
        self.assertTrue(response.context['can_edit'])
        self.assertEqual(response.context['lock_reason'], '')

        self._save_sheet([('OPX-001', 40), ('OPX-008', 2), ('OPX-059', 250),
                          ('OPX-101', 24), ('OPX-105', 24), ('OPX-193', '1.5'),
                          ('OPX-168', 60)])                       # NEW — Earthing
        self.assertIn('OPX-168', self._sheet_codes())
        self.assertEqual(len(self._sheet_codes()), 7)

    def test_12b_a_rejection_that_was_not_about_the_boq_leaves_it_frozen(self):
        """Part 9.1 scoping is respected rather than overruled — see the docstring on
        project_boq_is_design_locked."""
        self._mark_complete()
        self._to_qc()
        self.client.logout()
        self._login(self.qc)
        self.client.post(
            reverse('design_qc_fail', kwargs={'project_id': self.site.project_id}),
            {'qc_remarks': 'Section detail missing from sheet 3.',
             'error_category': ERR_DRAWING_INCOMPLETE,
             'redo_scope_submitted': '1', 'redo': ['cad']})

        new_attempt = self.assignment.attempts.get(attempt_number=2)
        self.assertIsNotNone(new_attempt.boq_submitted_at)     # carried forward
        self.assertTrue(project_boq_is_design_locked(self.site))

    def test_13_head_approval_locks_the_boq(self):
        """VERIFICATION 13 — confirmed by direct POST."""
        self._mark_complete()
        self._to_qc()
        self.client.logout()
        self._login(self.qc)
        self.client.post(
            reverse('design_qc_pass', kwargs={'project_id': self.site.project_id}), {})
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, DESIGN_AWAITING_HEAD_QC)

        self.client.logout()
        self._login(self.head)
        self.client.post(
            reverse('design_head_qc_pass', kwargs={'project_id': self.site.project_id}), {})
        self.assignment.refresh_from_db()
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.head_verdict, QC_PASSED)
        self.assertEqual(self.assignment.status, DESIGN_RELEASED)
        self.assertTrue(project_boq_is_design_locked(self.site))

        self.client.logout()
        self._login(self.designer)
        before = self._sheet_codes()
        self._save_sheet([('OPX-001', 777), ('OPX-168', 5)])
        self.assertEqual(self._sheet_codes(), before)
        self.assertEqual(
            BOQItem.objects.get(boq__project=self.site,
                                item_master__code='OPX-001').boq_quantity,
            Decimal('40'))

    def test_14_pm_change_request_reopens_with_the_full_picker(self):
        """VERIFICATION 14 — a new attempt clears the stamp, so the picker returns.

        PART 4.6 moved the attempt-opening from the raise to the Design Head's acceptance.
        The sheet must therefore STAY LOCKED while the request is pending — an untriaged
        request is not permission to start editing — and unlock only on acceptance. Both
        halves are asserted below.
        """
        from .models import DesignChangeRequest
        self._mark_complete()
        self._to_qc()
        self.assertTrue(project_boq_is_design_locked(self.site))

        self.client.logout()
        self._login(self.pm)
        response = self.client.post(
            reverse('design_change_request',
                    kwargs={'project_id': self.site.project_id}),
            {'reason': 'Client moved the array to the north shed.'})
        self.assertEqual(response.status_code, 302)

        # Pending: nothing has moved and the sheet is still locked.
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.current_attempt_number, 1)
        self.assertTrue(project_boq_is_design_locked(self.site))

        change = DesignChangeRequest.objects.get(attempt__assignment=self.assignment)
        self.client.logout()
        self._login(self.head)
        self.client.post(reverse('design_change_request_accept',
                                 kwargs={'pk': change.pk}))

        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.current_attempt_number, 2)
        new_attempt = self.assignment.attempts.get(attempt_number=2)
        self.assertEqual(new_attempt.opened_reason, ATTEMPT_REASON_PM_CHANGE_REQUEST)
        self.assertIsNone(new_attempt.boq_submitted_at)
        self.assertFalse(project_boq_is_design_locked(self.site))

        self.client.logout()
        self._login(self.designer)
        response = self.client.get(self._entry_url())
        self.assertTrue(response.context['can_edit'])
        self._save_sheet([('OPX-001', 40), ('OPX-170', 4)])       # new item, full picker
        self.assertEqual(self._sheet_codes(), ['OPX-001', 'OPX-170'])


# ===========================================================================
# 15-16. Part 6 aggregation, and what must not have changed
# ===========================================================================

class AggregationAndBlastRadiusTests(Part11Base):

    def test_15_group_aggregation_sums_across_sites_using_opex_items(self):
        """VERIFICATION 15 — the Part 6 join is item_master, and OPEX rows carry it."""
        from .design_views import aggregate_group_boq

        site_b, _assignment_b = self._opex_site('P11-B')
        self._login(self.designer)
        self._save_sheet([('OPX-001', 40), ('OPX-059', 250)])
        self._save_sheet([('OPX-001', 25), ('OPX-101', 12)], site=site_b)

        agg = aggregate_group_boq([self.site.pk, site_b.pk])
        lines = {row['item_master__code']: row for row in agg['lines']}

        self.assertEqual(lines['OPX-001']['total_quantity'], Decimal('65'))
        self.assertEqual(lines['OPX-001']['site_count'], 2)
        self.assertEqual(lines['OPX-001']['item_master__unit'], 'Nos')
        self.assertEqual(lines['OPX-059']['total_quantity'], Decimal('250'))
        self.assertEqual(lines['OPX-059']['site_count'], 1)

        per_site = dict(lines['OPX-001']['contributions'])
        self.assertEqual(per_site, {'P11-A': Decimal('40'), 'P11-B': Decimal('25')})
        self.assertEqual(agg['unlinked'], [])

    def test_15b_group_lock_still_beats_the_design_lock(self):
        """The procurement lock is not reversible and is not weakened by Part 11."""
        self._login(self.designer)
        self._save_sheet([('OPX-001', 40)])
        group = SiteGroup.objects.create(name='G1', program=self.program,
                                         status=SITE_GROUP_LOCKED,
                                         created_by=self.scm, locked_by=self.scm,
                                         locked_at=timezone.now())
        SiteGroupMembership.objects.create(group=group, project=self.site,
                                           added_by=self.scm)

        response = self.client.get(self._entry_url())
        self.assertFalse(response.context['can_edit'])
        self.assertIn('procurement group', response.context['lock_reason'])

        self._save_sheet([('OPX-001', 999)])
        self.assertEqual(
            BOQItem.objects.get(boq__project=self.site).boq_quantity, Decimal('40'))

    def test_16_nothing_outside_the_opex_boq_changed(self):
        """VERIFICATION 16 — no Task, no NotificationLog, no Residential row touched."""
        residential = self._residential_site('HRP-RES-P11D')
        self._login(self.designer)
        self.client.get(reverse('boq_detail',
                                kwargs={'project_id': residential.project_id}))
        residential_rows = set(
            BOQItem.objects.filter(boq__project=residential)
            .values_list('pk', 'description', 'boq_quantity'))
        masters_before = set(
            BOQItemMaster.objects.filter(project_type='Residential')
            .values_list('pk', 'code', 'description', 'unit', 'category', 'sort_order'))
        notifications_before = NotificationLog.objects.count()
        tasks_before = Task.objects.count()

        attempt = self._approved_arka(self.assignment)
        self._save_sheet([('OPX-001', 40), ('OPX-008', 2)])
        self.client.post(reverse('design_boq_complete',
                                 kwargs={'project_id': self.site.project_id}), {})

        self.assertEqual(
            set(BOQItem.objects.filter(boq__project=residential)
                .values_list('pk', 'description', 'boq_quantity')),
            residential_rows)
        self.assertEqual(
            set(BOQItemMaster.objects.filter(project_type='Residential')
                .values_list('pk', 'code', 'description', 'unit', 'category', 'sort_order')),
            masters_before)
        self.assertEqual(NotificationLog.objects.count(), notifications_before)
        self.assertEqual(Task.objects.count(), tasks_before)
        attempt.refresh_from_db()
        self.assertIsNotNone(attempt.boq_submitted_at)


# ===========================================================================
# Off-catalogue rows — the sites that already had a BOQ before Part 11
# ===========================================================================

class OffCatalogueRowTests(Part11Base):
    """OPEX sites created before Part 11 were seeded from the Residential template by the
    shared boq_detail. Those rows are real quantities on real sheets: they render, they
    are removable, and the picker never offers them back."""

    def setUp(self):
        super().setUp()
        boq = BOQ.objects.create(project=self.site)
        residential = BOQItemMaster.objects.filter(project_type='Residential').first()
        self.legacy = BOQItem.objects.create(
            boq=boq, item_master=residential, serial_no=1,
            category=residential.category, description=residential.description,
            uom=residential.unit, boq_quantity=Decimal('12'))
        self.adhoc = BOQItem.objects.create(
            boq=boq, item_master=None, serial_no=99, category='Other',
            description='Site-specific fabrication', uom='LOT',
            boq_quantity=Decimal('1'), is_standard_item=False)

    def test_off_catalogue_rows_render_and_are_not_offered_by_the_picker(self):
        self._login(self.designer)
        response = self.client.get(self._entry_url())
        off = response.context['added_off']
        self.assertEqual(sum(len(rows) for _c, rows in off), 2)
        self.assertEqual(response.context['added_off_count'], 2)
        self.assertEqual(json.loads(response.context['added_json']), [])
        codes = {i['code'] for i in json.loads(response.context['catalogue_json'])}
        self.assertTrue(all(c.startswith('OPX-') for c in codes))

    def test_off_catalogue_rows_survive_a_save_that_does_not_drop_them(self):
        """Kept rows stay, and an ABSENT quantity field leaves the number alone."""
        self._login(self.designer)
        self._save_sheet([('OPX-001', 40)],
                         keep_rows=[self.legacy.pk, self.adhoc.pk])
        self.assertEqual(BOQItem.objects.filter(boq__project=self.site).count(), 3)
        self.legacy.refresh_from_db()
        self.assertEqual(self.legacy.boq_quantity, Decimal('12'))

    def test_off_catalogue_quantity_is_editable_when_the_field_is_posted(self):
        """A posted field is applied; an empty one clears — the browser always posts it."""
        self._login(self.designer)
        master = self._master('OPX-001')
        self.client.post(self._entry_url(), {
            'action': 'save_draft',
            'item': [str(master.pk)], f'qty_{master.pk}': '40',
            'keep_row': [str(self.legacy.pk), str(self.adhoc.pk)],
            f'qty_row_{self.legacy.pk}': '18',
            f'qty_row_{self.adhoc.pk}': '',
        })
        self.legacy.refresh_from_db()
        self.adhoc.refresh_from_db()
        self.assertEqual(self.legacy.boq_quantity, Decimal('18'))
        self.assertIsNone(self.adhoc.boq_quantity)

    def test_off_catalogue_rows_are_removed_when_dropped(self):
        self._login(self.designer)
        self._save_sheet([('OPX-001', 40)], keep_rows=[self.legacy.pk])
        self.assertEqual(
            sorted(BOQItem.objects.filter(boq__project=self.site)
                   .values_list('description', flat=True)),
            sorted([self.legacy.description, 'Solar PV Module']))

    def test_review_panel_shows_off_catalogue_rows_separately(self):
        attempt = self._approved_arka(self.assignment)
        attempt.boq_submitted_at = timezone.now()
        attempt.boq_submitted_by = self.designer
        attempt.save()
        self.assignment.status = DESIGN_IN_QC
        self.assignment.save()

        self._login(self.qc)
        response = self.client.get(
            reverse('design_qc_review', kwargs={'project_id': self.site.project_id}))
        self.assertEqual(response.context['boq_row_count'], 2)
        self.assertEqual(response.context['boq_by_category'], [])
        self.assertEqual(sum(len(rows) for _c, rows
                             in response.context['boq_off_catalogue']), 2)
