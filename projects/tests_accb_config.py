"""
B-3a verification — configured ACCBs: one configured BOQ line per ACCB.

WHAT THIS FILE PROVES
---------------------
  * B-3c: the switchgear list is the ruling's 11 options word for word.
  * The wording. accb_description() reproduces B-3c ruling 3's two examples word for word,
    and each rule behind them (1 vs N feeders, grouping, rating high to low, "with SPD"
    before "without SPD" for the same option).
  * The grouping identity. accb_config_key() ignores feeder order (and which of two
    identical O/G feeders carries the SPD tick) and changes with any change to type,
    option or O/G SPD. The SCM aggregate depends on exactly that.
  * Validation is server-side and refuses with NOTHING saved: an option not in the list,
    a swapped breaker/rating pair, SPD on an I/C feeder, a missing O/G SPD value.
  * An ACCB saved before B-3c draws, is refused on save and on Duplicate until its
    switchgear is re-entered, and can still be removed.
  * Ruling 6's markup: the remove confirmation exists only on a saved ACCB after Remove.
  * The query counts measured in the B-3c pre-flight.
  * The designer's add / edit / duplicate / remove each touch exactly one line and its
    configuration, Qty is always 1, and the BOQ's own write rule (author, no group lock,
    not submitted) applies.
  * EVERY OTHER WRITE PATH LEAVES ACCB LINES ALONE: the picker save that omits them, the
    spreadsheet upload, the reviewer's correction screen.
  * SCM keeps different configurations apart and changes no other item's total; the order
    raise lists ACCBs as orderable rows, one per configuration (B-3b; the raise itself is
    in tests_accb_order).
  * OPX-273 is locked on both catalogue screens and in the form.

RUNNER: like tests_boq_upload, this seeds catalogue codes in setUp, so it runs with
migrations disabled — `manage.py test projects --settings=solarpms.test_settings`. Migration
0149 creates OPX-273 on a migrated database; here setUp creates it.
"""
import json
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from .accb import (
    ACCB_CATALOGUE_LOCKED_MESSAGE, ACCB_CONFIGURED_CODE, ACCB_REENTER_MESSAGE,
    ACCB_REVIEWER_MESSAGE, ACCB_SWITCHGEAR, ACCB_UPLOAD_REFUSED_MESSAGE, accb_config_key,
    accb_description, accb_summary, stored_accb_config, validate_accb_config,
)
from .design_views import _boq_review_panel, aggregate_group_boq
from .forms import BOQItemMasterForm
from .models import (
    ACCBConfig, ActivityLog, BOQ, BOQCorrection, BOQItem, BOQItemMaster,
    DesignAssignment, DesignAttempt, Program, Project, SiteGroup, SiteGroupMembership,
    ATTEMPT_REASON_INITIAL, DESIGN_IN_DESIGN, DESIGN_IN_QC, SITE_GROUP_LOCKED,
    get_opex_boq_catalogue, get_opex_mandatory_items, opex_catalogue_category_order,
)
from .order_views import _parse_requirement, _picker_context, accb_amount_field
from .views import (
    BOQ_DOWNLOAD_ACCB_SHEET, BOQ_DOWNLOAD_COLUMNS, BOQ_DOWNLOAD_SHEET, _accb_create,
    accb_log_text,
)


def cfg(accb_type, incomers, outgoings):
    """A configuration dict — the shape accb.py takes (B-3c): I/C feeders from (breaker,
    rating) pairs, O/G feeders from (breaker, rating, spd)."""
    return {'type': accb_type,
            'in': [{'breaker': b, 'rating': r} for b, r in incomers],
            'out': [{'breaker': b, 'rating': r, 'spd': s} for b, r, s in outgoings]}


#: The full MCCB wording, as B-3c ruling 1 writes it.
MCCB = ('4P, 36kA, MCCB with S/C & O/L TM Based Extended Rotary Handle + Spreader '
        'Terminals')

#: The two examples of B-3c ruling 3, configuration and exact text.
EX_TYPE2 = cfg('Type-2', [('MCCB', 100), ('MCB', 63)], [('MCCB', 200, True)])
EX_TYPE2_TEXT = (
    'ACCB Type-2, 4P 415V. I/C Feeder 2 Nos.: 1 x 100A, 4P, 36kA, MCCB with S/C & O/L TM '
    'Based Extended Rotary Handle + Spreader Terminals; 1 x 63A, 4P, MCB. O/G Feeder 1 No.: '
    '1 x 200A, 4P, 36kA, MCCB with S/C & O/L TM Based Extended Rotary Handle + Spreader '
    'Terminals, with SPD.')
EX_TYPE1 = cfg('Type-1', [('MCB', 40), ('MCB', 25), ('MCB', 40)],
               [('MCCB', 125, True), ('MCCB', 125, False)])
EX_TYPE1_TEXT = (
    'ACCB Type-1, 4P 415V. I/C Feeder 3 Nos.: 2 x 40A, 4P, MCB; 1 x 25A, 4P, MCB. O/G Feeder '
    '2 Nos.: 1 x 125A, 4P, 36kA, MCCB with S/C & O/L TM Based Extended Rotary Handle + '
    'Spreader Terminals, with SPD; 1 x 125A, 4P, 36kA, MCCB with S/C & O/L TM Based Extended '
    'Rotary Handle + Spreader Terminals, without SPD.')

#: B-3c ruling 1, copied from the ruling's table by hand: the constant must equal it.
RULING_SWITCHGEAR = (
    ('MCB', 25, '25A, 4P, MCB'),
    ('MCB', 30, '30A, 4P, MCB'),
    ('MCB', 40, '40A, 4P, MCB'),
    ('MCB', 63, '63A, 4P, MCB'),
    ('MCCB', 100, '100A, 4P, 36kA, MCCB with S/C & O/L TM Based Extended Rotary Handle + Spreader Terminals'),
    ('MCCB', 125, '125A, 4P, 36kA, MCCB with S/C & O/L TM Based Extended Rotary Handle + Spreader Terminals'),
    ('MCCB', 200, '200A, 4P, 36kA, MCCB with S/C & O/L TM Based Extended Rotary Handle + Spreader Terminals'),
    ('MCCB', 250, '250A, 4P, 36kA, MCCB with S/C & O/L TM Based Extended Rotary Handle + Spreader Terminals'),
    ('MCCB', 320, '320A, 4P, 36kA, MCCB with S/C & O/L TM Based Extended Rotary Handle + Spreader Terminals'),
    ('MCCB', 400, '400A, 4P, 36kA, MCCB with S/C & O/L TM Based Extended Rotary Handle + Spreader Terminals'),
    ('MCCB', 630, '630A, 4P, 36kA, MCCB with S/C & O/L TM Based Extended Rotary Handle + Spreader Terminals'),
)


#: What B-3 wrote on the line of the legacy ACCB the tests seed (_seed_legacy_accb).
LEGACY_TEXT = ('ACCB Type-2, I/C Feeder 1 No. with MCCB 4P 415V 100A; O/G Feeder 1 No. with '
               'MCCB 4P 415V 150A, with SPD')


def _longest_config():
    """The longest description the rules allow (B-3c pre-flight P5): 20 I/C feeders using
    all 11 options, and 20 O/G feeders in 20 different (option, SPD) groups, MCCBs first."""
    incomers = [(b, r) for b, r, _t in ACCB_SWITCHGEAR] + [('MCCB', 630)] * 9
    groups = [(b, r, s) for b, r, _t in ACCB_SWITCHGEAR for s in (True, False)]
    outgoings = sorted(groups, key=lambda g: g[0] != 'MCCB')[:20]
    return cfg('Type-2', incomers, outgoings)


def _longest_key_config():
    """The longest grouping key: 20 + 20 feeders of the longest code, all with SPD."""
    return cfg('Type-2', [('MCCB', 630)] * 20, [('MCCB', 630, True)] * 20)


def accb_post_data(config):
    """The POST fields the card sends for a configuration (B-3c): one option code per
    feeder, and one '1'/'0' SPD value per O/G feeder."""
    return {'accb_type': config['type'],
            'in_option': [f'{f["breaker"]}-{f["rating"]}' for f in config['in']],
            'out_option': [f'{f["breaker"]}-{f["rating"]}' for f in config['out']],
            'out_spd': ['1' if f['spd'] else '0' for f in config['out']]}


# ===========================================================================
# The pure rules — wording, key, summary, validation
# ===========================================================================

class SwitchgearListTests(SimpleTestCase):

    def test_the_list_is_the_ruling_word_for_word(self):
        self.assertEqual(ACCB_SWITCHGEAR, RULING_SWITCHGEAR)

    def test_every_rating_belongs_to_one_breaker(self):
        # What lets the description and the key sort by rating alone.
        ratings = [rating for _b, rating, _t in ACCB_SWITCHGEAR]
        self.assertEqual(len(ratings), len(set(ratings)))


class DescriptionTests(SimpleTestCase):

    def test_ruling_example_1_word_for_word(self):
        self.assertEqual(accb_description(EX_TYPE2), EX_TYPE2_TEXT)

    def test_ruling_example_2_word_for_word(self):
        self.assertEqual(accb_description(EX_TYPE1), EX_TYPE1_TEXT)

    def test_one_feeder_is_1_No_and_several_are_N_Nos(self):
        text = accb_description(cfg('Type-1', [('MCB', 63)],
                                    [('MCCB', 100, True), ('MCCB', 200, False),
                                     ('MCB', 25, False)]))
        self.assertIn('I/C Feeder 1 No.: 1 x 63A, 4P, MCB.', text)
        self.assertIn('O/G Feeder 3 Nos.: ', text)

    def test_identical_options_are_grouped_and_sorted_by_rating_high_to_low(self):
        text = accb_description(cfg('Type-2',
                                    [('MCB', 25), ('MCCB', 400), ('MCB', 25), ('MCB', 63),
                                     ('MCCB', 400), ('MCB', 25)],
                                    [('MCB', 30, False)]))
        self.assertIn(f'I/C Feeder 6 Nos.: 2 x 400A, {MCCB}; 1 x 63A, 4P, MCB; '
                      f'3 x 25A, 4P, MCB. O/G', text)

    def test_og_groups_by_option_and_spd_with_spd_first_for_the_same_option(self):
        text = accb_description(cfg('Type-1', [('MCB', 40)],
                                    [('MCB', 30, False), ('MCCB', 250, False),
                                     ('MCB', 30, True), ('MCCB', 250, True),
                                     ('MCB', 30, False)]))
        self.assertTrue(text.endswith(
            f'O/G Feeder 5 Nos.: 1 x 250A, {MCCB}, with SPD; 1 x 250A, {MCCB}, without SPD; '
            f'1 x 30A, 4P, MCB, with SPD; 2 x 30A, 4P, MCB, without SPD.'))

    def test_the_longest_description_is_2446_characters(self):
        # Pre-flight P5: every column it is copied into automatically is unbounded text.
        self.assertEqual(len(accb_description(_longest_config())), 2446)


class ConfigKeyTests(SimpleTestCase):

    def test_ruling_example_keys(self):
        self.assertEqual(accb_config_key(EX_TYPE2), 'Type-2|IN:MCCB-100,MCB-63|OUT:MCCB-200+SPD')
        self.assertEqual(accb_config_key(EX_TYPE1),
                         'Type-1|IN:MCB-40,MCB-40,MCB-25|OUT:MCCB-125+SPD,MCCB-125')

    def test_feeder_order_does_not_change_the_key(self):
        shuffled = cfg('Type-1', [('MCB', 25), ('MCB', 40), ('MCB', 40)],
                       [('MCCB', 125, False), ('MCCB', 125, True)])
        self.assertEqual(accb_config_key(shuffled), accb_config_key(EX_TYPE1))
        self.assertEqual(accb_description(shuffled), EX_TYPE1_TEXT)

    def test_moving_an_spd_tick_between_identical_og_feeders_does_not_change_the_key(self):
        first = cfg('Type-1', [('MCB', 40)], [('MCCB', 200, True), ('MCCB', 200, False)])
        second = cfg('Type-1', [('MCB', 40)], [('MCCB', 200, False), ('MCCB', 200, True)])
        self.assertEqual(accb_config_key(first), accb_config_key(second))

    def test_any_change_to_type_option_or_an_og_spd_tick_changes_the_key(self):
        base = accb_config_key(EX_TYPE1)
        variants = {
            'type':       dict(EX_TYPE1, type='Type-2'),
            'I/C option': cfg('Type-1', [('MCB', 40), ('MCB', 30), ('MCB', 40)],
                              [('MCCB', 125, True), ('MCCB', 125, False)]),
            'O/G option': cfg('Type-1', [('MCB', 40), ('MCB', 25), ('MCB', 40)],
                              [('MCCB', 200, True), ('MCCB', 125, False)]),
            'SPD ticked': cfg('Type-1', [('MCB', 40), ('MCB', 25), ('MCB', 40)],
                              [('MCCB', 125, True), ('MCCB', 125, True)]),
            'SPD off':    cfg('Type-1', [('MCB', 40), ('MCB', 25), ('MCB', 40)],
                              [('MCCB', 125, False), ('MCCB', 125, False)]),
            'side':       cfg('Type-1', [('MCB', 40), ('MCB', 25)],
                              [('MCCB', 125, True), ('MCCB', 125, False), ('MCB', 40, False)]),
            'count':      cfg('Type-1', [('MCB', 40), ('MCB', 25), ('MCB', 40), ('MCB', 40)],
                              [('MCCB', 125, True), ('MCCB', 125, False)]),
        }
        for name, variant in variants.items():
            with self.subTest(change=name):
                self.assertNotEqual(accb_config_key(variant), base)

    def test_the_longest_key_fits_both_columns(self):
        from .models import VendorOrderLine
        longest = len(accb_config_key(_longest_key_config()))
        self.assertEqual(longest, 453)
        self.assertLessEqual(longest, ACCBConfig._meta.get_field('config_key').max_length)
        self.assertLessEqual(longest,
                             VendorOrderLine._meta.get_field('accb_config_key').max_length)


class SummaryAndLogTests(SimpleTestCase):

    def test_summary_wording(self):
        self.assertEqual(accb_summary(EX_TYPE2), 'Type-2 · I/C 2 · O/G 1 · SPD 1')
        self.assertEqual(accb_summary(EX_TYPE1), 'Type-1 · I/C 3 · O/G 2 · SPD 1')

    def test_a_stored_feeder_without_spd_counts_as_no_spd(self):
        legacy = {'type': 'Type-2', 'in': [{'breaker': 'MCCB', 'rating': 150}],
                  'out': [{'breaker': 'MCCB', 'rating': 150}]}
        self.assertEqual(accb_summary(legacy), 'Type-2 · I/C 1 · O/G 1 · SPD 0')

    def test_the_longest_log_line_fits_in_255(self):
        """Q6: no truncation in normal use. Built from the worst case of every part: a
        301-character name, a 30-character site code (Project.project_id's max), the
        largest ACCB number the column holds, and the longest change detail."""
        user = SimpleNamespace(get_full_name=lambda: 'N' * 301, username='u')
        profile = SimpleNamespace(user=user)
        project = SimpleNamespace(project_id='S' * Project._meta.get_field('project_id').max_length)
        longest = accb_summary(_longest_key_config())
        self.assertEqual(len(longest), 33)
        detail = f'{longest} → {longest}'
        text = accb_log_text(profile, 'changed', 32767, project, detail)
        self.assertLessEqual(len(text), ActivityLog._meta.get_field('action').max_length)


class ValidationTests(SimpleTestCase):

    def _ok(self, **overrides):
        args = {'accb_type': 'Type-2', 'in_options': ['MCCB-100'],
                'out_options': ['MCCB-200'], 'out_spds': ['1'], 'in_spds': []}
        args.update(overrides)
        return validate_accb_config(args['accb_type'], args['in_options'],
                                    args['out_options'], args['out_spds'], args['in_spds'])

    def test_a_valid_configuration_is_normalised(self):
        self.assertEqual(self._ok(in_options=[' MCCB-100 ', 'MCB-63']), EX_TYPE2)
        self.assertEqual(self._ok(out_spds=['0'])['out'][0]['spd'], False)

    def test_every_option_and_twenty_feeders_are_accepted(self):
        codes = [f'{b}-{r}' for b, r, _t in ACCB_SWITCHGEAR]
        config = self._ok(in_options=codes, out_options=codes, out_spds=['1', '0'] * 5 + ['1'])
        self.assertEqual([(f['breaker'], f['rating']) for f in config['out']],
                         [(b, r) for b, r, _t in ACCB_SWITCHGEAR])
        config = self._ok(in_options=['MCB-25'] * 20, out_options=['MCB-25'] * 20,
                          out_spds=['0'] * 20)
        self.assertEqual((len(config['in']), len(config['out'])), (20, 20))

    def test_each_rule_is_refused(self):
        cases = {
            'type':                 {'accb_type': 'Type-3'},
            'blank type':           {'accb_type': ''},
            'no I/C feeder':        {'in_options': []},
            '21 O/G feeders':       {'out_options': ['MCB-25'] * 21, 'out_spds': ['0'] * 21},
            '32 MCB not listed':    {'in_options': ['MCB-32']},
            '63 MCCB not listed':   {'in_options': ['MCCB-63']},
            '160 MCCB not listed':  {'out_options': ['MCCB-160']},
            'swapped MCCB 25':      {'in_options': ['MCCB-25']},
            'swapped MCB 100':      {'out_options': ['MCB-100']},
            'blank option':         {'in_options': ['']},
            'old breaker only':     {'in_options': ['MCCB']},
            'SPD on an I/C feeder': {'in_spds': ['1']},
            'I/C SPD of 0':         {'in_spds': ['0']},
            'missing O/G SPD':      {'out_options': ['MCCB-200', 'MCB-25'], 'out_spds': ['1']},
            'no O/G SPD at all':    {'out_spds': []},
            'O/G SPD not 1 or 0':   {'out_spds': ['on']},
            'O/G SPD blank':        {'out_spds': ['']},
        }
        for name, override in cases.items():
            with self.subTest(rule=name):
                with self.assertRaises(ValidationError):
                    self._ok(**override)

    def test_every_fault_is_named_at_once(self):
        with self.assertRaises(ValidationError) as caught:
            self._ok(accb_type='X', in_options=['MCB-32'], in_spds=['1'], out_spds=['maybe'])
        self.assertEqual(len(caught.exception.messages), 4)


class StoredConfigTests(SimpleTestCase):
    """B-3c: the re-check Duplicate and the B-4 copy run on a SAVED configuration."""

    def test_a_valid_stored_configuration_comes_back_unchanged(self):
        for config in (EX_TYPE1, EX_TYPE2):
            with self.subTest(type=config['type']):
                self.assertEqual(
                    stored_accb_config(config['type'], {'in': config['in'], 'out': config['out']}),
                    config)

    def test_an_old_format_configuration_is_refused(self):
        cases = {
            'typed rating':      {'in': [{'breaker': 'MCCB', 'rating': 150}],
                                  'out': [{'breaker': 'MCCB', 'rating': 200, 'spd': True}]},
            'no per-feeder SPD': {'in': [{'breaker': 'MCCB', 'rating': 100}],
                                  'out': [{'breaker': 'MCCB', 'rating': 200}]},
            'SPD on I/C':        {'in': [{'breaker': 'MCCB', 'rating': 100, 'spd': False}],
                                  'out': [{'breaker': 'MCCB', 'rating': 200, 'spd': True}]},
            'malformed':         {'in': ['x'], 'out': None},
            'not a dict':        None,
        }
        for name, feeders in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(ValidationError):
                    stored_accb_config('Type-2', feeders)


# ===========================================================================
# Shared fixtures for the database tests
# ===========================================================================

CATALOGUE = [
    # (code, description, unit, category, sort_order, is_mandatory)
    ('OPX-001', 'Solar PV Module', 'Nos', 'Module', 1, True),
    ('OPX-002', 'DCDB 5 In 5 Out', 'Nos', 'DCDB',   2, False),
    ('OPX-003', 'AC Cable 4 sqmm', 'Mtr', 'Cable',  3, False),
    ('OPX-039', 'ACDB Type-1 (For 12KW INV)', 'Nos', 'ACDB', 39, False),
    (ACCB_CONFIGURED_CODE, 'ACCB (configured)', 'Nos', 'ACCB', 273, False),
]


def _profile(username, role, **flags):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.capitalize())
    profile = user.profile
    profile.role = role
    profile.is_active = True
    for name, value in flags.items():
        setattr(profile, name, value)
    profile.save()
    return profile


class ACCBBase(TestCase):

    def setUp(self):
        BOQItemMaster.objects.bulk_create([
            BOQItemMaster(code=code, description=description, unit=unit, category=category,
                          project_type='OPEX', sort_order=sort_order,
                          is_mandatory=is_mandatory, is_active=True)
            for code, description, unit, category, sort_order, is_mandatory in CATALOGUE
        ])
        self.accb_master = BOQItemMaster.objects.get(code=ACCB_CONFIGURED_CODE)

        self.designer = _profile('desacc',  'Design')
        self.other    = _profile('desacc2', 'Design')
        self.qc       = _profile('qcacc',   'Design', is_design_qc=True)
        self.head     = _profile('headacc', 'Design', is_design_head=True)
        self.pm       = _profile('pmacc',   'PM')
        self.admin    = _profile('admacc',  'Admin')

        self.program = Program.objects.create(
            name='Test-ACCB', program_type='OPEX', client_name='ACCBClient',
            status='Active', short_tender_code='AC')
        self.site, self.assignment = self._opex_site('AC-A')

    # ── fixtures ────────────────────────────────────────────────────────────

    def _opex_site(self, code, status=DESIGN_IN_DESIGN):
        site = Project(
            project_id=code, customer_name='ACCBClient', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code=code,
            dc_capacity_kw=Decimal('100.00'), status='Draft',
            assigned_design=self.designer, assigned_pm=self.pm)
        site.save()
        assignment = DesignAssignment.objects.create(
            project=site, status=status, assigned_to=self.designer,
            survey_file_bucket='b', survey_file_path=f'{code}/survey/x.pdf')
        return site, assignment

    def _master(self, code):
        return BOQItemMaster.objects.get(code=code)

    def _boq(self, site=None):
        boq, _ = BOQ.objects.get_or_create(project=site or self.site)
        return boq

    def _line(self, code, quantity, site=None):
        master = self._master(code)
        return BOQItem.objects.create(
            boq=self._boq(site), item_master=master, serial_no=master.sort_order,
            category=master.category, description=master.description, uom=master.unit,
            boq_quantity=Decimal(str(quantity)), is_standard_item=True)

    def _seed_accb(self, config, site=None):
        """An ACCB written by the view's own create helper."""
        return _accb_create(self._boq(site), self.accb_master, config, self.designer)

    def _seed_legacy_accb(self, site=None):
        """An ACCB exactly as B-3 saved it, before the fixed switchgear list: a typed
        rating (150, not in the list), one SPD per ACCB, no per-feeder SPD, the old wording
        and the old key."""
        boq = self._boq(site)
        line = BOQItem.objects.create(
            boq=boq, item_master=self.accb_master, serial_no=273, category='ACCB',
            description=LEGACY_TEXT, uom='Nos', boq_quantity=Decimal('1'),
            is_standard_item=True)
        number = boq.next_accb_number
        BOQ.objects.filter(pk=boq.pk).update(next_accb_number=number + 1)
        return ACCBConfig.objects.create(
            boq=boq, boq_item=line, number=number, accb_type='Type-2', has_spd=True,
            feeders={'in': [{'breaker': 'MCCB', 'rating': 100}],
                     'out': [{'breaker': 'MCCB', 'rating': 150}]},
            config_key='Type-2|SPD|IN:MCCB-100|OUT:MCCB-150')

    def _design_lock(self, assignment=None):
        assignment = assignment or self.assignment
        DesignAttempt.objects.create(
            assignment=assignment, attempt_number=1,
            opened_reason=ATTEMPT_REASON_INITIAL, boq_submitted_at=timezone.now(),
            boq_submitted_by=assignment.assigned_to)
        assignment.current_attempt_number = 1
        assignment.save(update_fields=['current_attempt_number'])

    def _group_lock(self, site=None):
        group = SiteGroup.objects.create(program=self.program, name='G1',
                                         status=SITE_GROUP_LOCKED, created_by=self.pm)
        SiteGroupMembership.objects.create(group=group, project=site or self.site,
                                           added_by=self.pm)

    def _login(self, profile):
        self.assertTrue(self.client.login(username=profile.user.username, password='x'))

    def _accb_post(self, action, config=None, accb_id=None, site=None, extra=None):
        data = {'action': action}
        if accb_id is not None:
            data['accb_id'] = str(accb_id)
        if config is not None:
            data.update(accb_post_data(config))
        data.update(extra or {})
        return self.client.post(
            reverse('opex_boq_accb', kwargs={'project_id': (site or self.site).project_id}),
            data)

    def _accb_lines(self, site=None):
        return list(BOQItem.objects.filter(boq__project=site or self.site,
                                           accb_config__isnull=False)
                    .select_related('accb_config').order_by('accb_config__number'))


# ===========================================================================
# The designer's ACCB section
# ===========================================================================

class DesignerACCBTests(ACCBBase):

    def setUp(self):
        super().setUp()
        self._login(self.designer)

    def test_add_creates_exactly_one_line_and_configuration(self):
        response = self._accb_post('add', EX_TYPE2)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].endswith('#accb'))
        lines = self._accb_lines()
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(line.description, EX_TYPE2_TEXT)
        self.assertEqual(line.uom, 'Nos')
        self.assertEqual(line.boq_quantity, Decimal('1'))
        self.assertEqual(line.item_master, self.accb_master)
        self.assertEqual(line.accb_config.number, 1)
        self.assertEqual(line.accb_config.config_key, accb_config_key(EX_TYPE2))
        self.assertEqual(line.accb_config.as_config(), EX_TYPE2)
        self.assertTrue(line.accb_config.has_spd)
        self.assertEqual(BOQItem.objects.filter(boq__project=self.site).count(), 1)
        log = ActivityLog.objects.get(action_code='boq_accb_added')
        self.assertEqual(log.action,
                         'Desacc added ACCB 1 on AC-A (Type-2 · I/C 2 · O/G 1 · SPD 1)')

    def test_has_spd_is_derived_from_the_og_feeders(self):
        """B-3c answer 2: True when any O/G feeder has SPD, False when none has."""
        none = cfg('Type-1', [('MCB', 25)], [('MCB', 30, False), ('MCB', 40, False)])
        self._accb_post('add', none)
        self._accb_post('add', EX_TYPE1)
        self.assertEqual([l.accb_config.has_spd for l in self._accb_lines()], [False, True])
        # An edit that ticks one O/G SPD flips it; unticking every one flips it back.
        first = self._accb_lines()[0].accb_config
        self._accb_post('save', cfg('Type-1', [('MCB', 25)],
                                    [('MCB', 30, False), ('MCB', 40, True)]), accb_id=first.pk)
        first.refresh_from_db()
        self.assertTrue(first.has_spd)
        self._accb_post('save', none, accb_id=first.pk)
        first.refresh_from_db()
        self.assertFalse(first.has_spd)

    def test_both_approved_examples_become_two_lines_nos_qty_1(self):
        self._accb_post('add', EX_TYPE2)
        self._accb_post('add', EX_TYPE1)
        lines = self._accb_lines()
        self.assertEqual([l.description for l in lines], [EX_TYPE2_TEXT, EX_TYPE1_TEXT])
        self.assertEqual([l.accb_config.number for l in lines], [1, 2])
        self.assertTrue(all(l.uom == 'Nos' and l.boq_quantity == 1 for l in lines))

    def test_edit_updates_the_same_line(self):
        self._accb_post('add', EX_TYPE2)
        line = self._accb_lines()[0]
        changed = cfg('Type-2', [('MCCB', 125), ('MCB', 63)], [('MCCB', 200, True)])
        self._accb_post('save', changed, accb_id=line.accb_config.pk)
        lines = self._accb_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].pk, line.pk)
        self.assertEqual(lines[0].description, accb_description(changed))
        self.assertEqual(lines[0].accb_config.config_key, accb_config_key(changed))
        self.assertEqual(lines[0].boq_quantity, Decimal('1'))
        log = ActivityLog.objects.get(action_code='boq_accb_changed')
        self.assertEqual(log.action, 'Desacc changed ACCB 1 on AC-A '
                                     '(Type-2 · I/C 2 · O/G 1 · SPD 1; '
                                     'feeders changed)')

    def test_an_edit_that_changes_the_counts_logs_old_and_new_summary(self):
        self._accb_post('add', EX_TYPE2)
        accb_id = self._accb_lines()[0].accb_config.pk
        self._accb_post('save', EX_TYPE1, accb_id=accb_id)
        log = ActivityLog.objects.get(action_code='boq_accb_changed')
        self.assertIn('(Type-2 · I/C 2 · O/G 1 · SPD 1 → '
                      'Type-1 · I/C 3 · O/G 2 · SPD 1)', log.action)

    def test_a_save_that_changes_nothing_logs_nothing(self):
        self._accb_post('add', EX_TYPE2)
        accb_id = self._accb_lines()[0].accb_config.pk
        self._accb_post('save', EX_TYPE2, accb_id=accb_id)
        self.assertFalse(ActivityLog.objects.filter(action_code='boq_accb_changed').exists())

    def test_duplicate_creates_one_new_line_with_the_next_number(self):
        self._accb_post('add', EX_TYPE2)
        self._accb_post('add', EX_TYPE1)
        first = self._accb_lines()[0]
        self._accb_post('duplicate', accb_id=first.accb_config.pk)
        lines = self._accb_lines()
        self.assertEqual(len(lines), 3)
        copy = lines[2]
        self.assertEqual(copy.accb_config.number, 3)
        self.assertNotEqual(copy.pk, first.pk)
        self.assertEqual(copy.description, EX_TYPE2_TEXT)
        self.assertEqual(copy.accb_config.as_config(), EX_TYPE2)
        self.assertEqual(copy.boq_quantity, Decimal('1'))

    def test_duplicate_copies_every_option_and_spd_in_order(self):
        mixed = cfg('Type-1', [('MCB', 40), ('MCCB', 630), ('MCB', 25)],
                    [('MCCB', 125, False), ('MCB', 63, True), ('MCCB', 125, True)])
        self._accb_post('add', mixed)
        source = self._accb_lines()[0].accb_config
        self._accb_post('duplicate', accb_id=source.pk)
        copy = self._accb_lines()[1].accb_config
        self.assertEqual(copy.feeders, source.feeders)
        self.assertEqual(copy.as_config(), mixed)
        self.assertEqual((copy.config_key, copy.has_spd), (source.config_key, True))
        self.assertEqual(copy.boq_item.description, accb_description(mixed))

    def test_duplicate_of_an_old_format_accb_is_refused_and_creates_nothing(self):
        legacy = self._seed_legacy_accb()
        response = self._accb_post('duplicate', accb_id=legacy.pk)
        self.assertEqual(ACCBConfig.objects.count(), 1)
        self.assertEqual(BOQItem.objects.count(), 1)
        self.assertFalse(ActivityLog.objects.filter(action_code='boq_accb_added').exists())
        shown = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertEqual(shown, [f'ACCB {legacy.number} cannot be duplicated: '
                                 f'{ACCB_REENTER_MESSAGE}.'])

    def test_remove_deletes_the_line_and_its_configuration_and_numbers_stay(self):
        self._accb_post('add', EX_TYPE2)
        self._accb_post('add', EX_TYPE1)
        first = self._accb_lines()[0]
        self._accb_post('remove', accb_id=first.accb_config.pk)
        self.assertFalse(BOQItem.objects.filter(pk=first.pk).exists())
        self.assertFalse(ACCBConfig.objects.filter(pk=first.accb_config.pk).exists())
        self.assertEqual([l.accb_config.number for l in self._accb_lines()], [2])
        # Q5: numbers are kept; a new ACCB takes max+1, so the gap at 1 stays.
        self._accb_post('add', EX_TYPE2)
        self.assertEqual([l.accb_config.number for l in self._accb_lines()], [2, 3])
        self.assertTrue(ActivityLog.objects.filter(action_code='boq_accb_removed').exists())

    def test_quantity_is_always_1_whatever_is_posted(self):
        self._accb_post('add', EX_TYPE2, extra={'quantity': '5', 'boq_quantity': '5'})
        accb_id = self._accb_lines()[0].accb_config.pk
        self._accb_post('save', EX_TYPE1, accb_id=accb_id, extra={'quantity': '7'})
        self.assertEqual(self._accb_lines()[0].boq_quantity, Decimal('1'))

    def test_an_invalid_configuration_saves_nothing(self):
        """B-3c T5: each refused, nothing saved — not even the BOQ row."""
        bad = {
            'type':                 {'accb_type': 'Type-9'},
            'no I/C feeder':        {'in_option': []},
            '21 O/G feeders':       {'out_option': ['MCB-25'] * 21, 'out_spd': ['0'] * 21},
            '32 MCB not listed':    {'in_option': ['MCB-32', 'MCB-63']},
            '63 MCCB not listed':   {'in_option': ['MCCB-63', 'MCB-63']},
            '160 MCCB not listed':  {'out_option': ['MCCB-160']},
            'swapped MCCB 25':      {'in_option': ['MCCB-25', 'MCB-63']},
            'SPD on an I/C feeder': {'in_spd': ['1', '0']},
            'missing O/G SPD':      {'out_spd': []},
            'O/G SPD not 1 or 0':   {'out_spd': ['on']},
            # A page drawn before B-3c posts the old fields: no option is chosen.
            'old fields':           {'in_option': [], 'out_option': [], 'out_spd': [],
                                     'in_breaker': ['MCCB'], 'in_rating': ['100'],
                                     'out_breaker': ['MCCB'], 'out_rating': ['200'],
                                     'has_spd': '1'},
        }
        for name, override in bad.items():
            with self.subTest(rule=name):
                response = self._accb_post('add', EX_TYPE2, extra=override)
                self.assertEqual(response.status_code, 302)
                self.assertIn('Nothing was saved.',
                              [str(m) for m in get_messages(response.wsgi_request)])
                self.assertFalse(BOQ.objects.filter(project=self.site).exists())
                self.assertEqual(ACCBConfig.objects.count(), 0)
                self.assertEqual(BOQItem.objects.count(), 0)

    def test_an_invalid_edit_leaves_the_saved_accb_unchanged(self):
        self._accb_post('add', EX_TYPE2)
        line = self._accb_lines()[0]
        self._accb_post('save', dict(EX_TYPE2, type='Type-9'), accb_id=line.accb_config.pk)
        line.refresh_from_db()
        self.assertEqual(line.description, EX_TYPE2_TEXT)
        self.assertEqual(ACCBConfig.objects.get().as_config(), EX_TYPE2)

    def test_an_accb_from_another_site_is_a_404(self):
        other_site, _ = self._opex_site('AC-B')
        theirs = self._seed_accb(EX_TYPE2, site=other_site)
        self._accb_post('add', EX_TYPE1)
        response = self._accb_post('remove', accb_id=theirs.pk)
        self.assertEqual(response.status_code, 404)
        self.assertTrue(ACCBConfig.objects.filter(pk=theirs.pk).exists())

    def test_a_residential_project_404s(self):
        house = Project(project_id='HOUSE1', customer_name='House',
                        customer_phone='9876543211', site_address='2 Sun Rd', city='Delhi',
                        project_type='Residential', dc_capacity_kw=Decimal('10.00'),
                        status='Draft', assigned_design=self.designer, assigned_pm=self.pm)
        house.save()
        self.assertEqual(self._accb_post('add', EX_TYPE2, site=house).status_code, 404)

    def test_the_page_draws_the_section_and_loads_alpine_once(self):
        self._seed_accb(EX_TYPE2)
        response = self.client.get(reverse('opex_boq_entry',
                                           kwargs={'project_id': self.site.project_id}))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertEqual(html.count('alpinejs@'), 1)
        self.assertIn('id="accbData"', html)
        self.assertIn(reverse('opex_boq_accb', kwargs={'project_id': self.site.project_id}),
                      html)
        data = json.loads(html.split('id="accbData" type="application/json">', 1)[1]
                          .split('</script>', 1)[0])
        self.assertEqual(data['accbs'][0]['description'], EX_TYPE2_TEXT)
        self.assertNotIn(self.accb_master.pk,
                         [item['id'] for item in json.loads(response.context['catalogue_json'])])

    def _page(self):
        response = self.client.get(reverse('opex_boq_entry',
                                           kwargs={'project_id': self.site.project_id}))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        data = json.loads(html.split('id="accbData" type="application/json">', 1)[1]
                          .split('</script>', 1)[0])
        return html, data

    def test_each_feeder_offers_the_11_options_and_saved_ones_come_back_by_code(self):
        """B-3c: one dropdown of the fixed list; no ampere box, breaker select or card-level
        SPD; the saved ACCB's feeders come back as option codes with their SPD ticks."""
        self._seed_accb(EX_TYPE1)
        html, data = self._page()
        self.assertEqual([(o['code'], o['text']) for o in data['switchgear']],
                         [(f'{b}-{r}', t) for b, r, t in RULING_SWITCHGEAR])
        self.assertIn('<option value="">Choose switchgear…</option>', html)
        for breaker, rating, text in RULING_SWITCHGEAR:
            # Drawn by Django, so the & in "S/C & O/L" is escaped.
            self.assertIn(f'<option value="{breaker}-{rating}">'
                          f'{text.replace("&", "&amp;")}</option>', html)
        for gone in ('accbRatings', 'name="has_spd"', "'_breaker'", "'_rating'",
                     'rating_min', 'common_ratings'):
            self.assertNotIn(gone, html)
        self.assertIn('name="out_spd"', html)
        card = data['accbs'][0]
        self.assertEqual(card['in'], [{'option': 'MCB-40'}, {'option': 'MCB-25'},
                                      {'option': 'MCB-40'}])
        self.assertEqual(card['out'], [{'option': 'MCCB-125', 'spd': True},
                                       {'option': 'MCCB-125', 'spd': False}])
        self.assertFalse(card['legacy'])
        self.assertNotIn('spd', card)

    def test_the_remove_confirmation_is_drawn_only_for_a_saved_accb_after_remove(self):
        """B-3c ruling 6. No test runs the JavaScript; this pins the markup: the buttons and
        the confirmation are x-if blocks (x-show lost to Bootstrap's d-flex !important),
        and the confirmation needs a saved ACCB."""
        html, _data = self._page()
        self.assertIn('<template x-if="!card.confirmRemove">', html)
        self.assertIn('<template x-if="card.id && card.confirmRemove">', html)
        self.assertNotIn('x-show="card.confirmRemove"', html)
        self.assertNotIn('x-show="!card.confirmRemove"', html)

    def test_an_old_format_accb_shows_and_is_refused_until_re_entered(self):
        """B-3c T4: an ACCB saved before the fixed list draws (no crash) with every unlisted
        feeder blank and the card marked legacy; a save that leaves it as it was is
        refused; a save with the switchgear chosen fixes it."""
        legacy = self._seed_legacy_accb()
        html, data = self._page()
        card = data['accbs'][0]
        self.assertTrue(card['legacy'])
        self.assertEqual(card['in'], [{'option': 'MCCB-100'}])
        self.assertEqual(card['out'], [{'option': '', 'spd': False}])
        self.assertEqual(card['description'], LEGACY_TEXT)
        self.assertIn('Re-enter the switchgear', html)
        # What the card posts while the O/G dropdown is still blank.
        self._accb_post('save', accb_id=legacy.pk, extra={
            'accb_type': 'Type-2', 'in_option': ['MCCB-100'], 'out_option': [''],
            'out_spd': ['1']})
        legacy.refresh_from_db()
        self.assertEqual(legacy.boq_item.description, LEGACY_TEXT)
        self.assertEqual(legacy.config_key, 'Type-2|SPD|IN:MCCB-100|OUT:MCCB-150')
        self._accb_post('save', EX_TYPE2, accb_id=legacy.pk)
        legacy.refresh_from_db()
        legacy.boq_item.refresh_from_db()
        self.assertEqual(legacy.boq_item.description, EX_TYPE2_TEXT)
        self.assertEqual(legacy.as_config(), EX_TYPE2)
        self.assertEqual(legacy.config_key, accb_config_key(EX_TYPE2))
        _html, data = self._page()
        self.assertFalse(data['accbs'][0]['legacy'])

    def test_an_old_format_accb_can_still_be_removed(self):
        legacy = self._seed_legacy_accb()
        self._accb_post('remove', accb_id=legacy.pk)
        self.assertFalse(ACCBConfig.objects.exists())
        log = ActivityLog.objects.get(action_code='boq_accb_removed')
        self.assertIn('(Type-2 · I/C 1 · O/G 1 · SPD 0)', log.action)


class DesignerWriteRuleTests(ACCBBase):
    """P6: the BOQ's own write rule — author, no group lock, not submitted."""

    def setUp(self):
        super().setUp()
        self.accb = self._seed_accb(EX_TYPE2)

    def _assert_nothing_changed(self):
        self.assertEqual(ACCBConfig.objects.count(), 1)
        self.accb.boq_item.refresh_from_db()
        self.assertEqual(self.accb.boq_item.description, EX_TYPE2_TEXT)

    def test_refused_when_the_boq_is_submitted(self):
        self._design_lock()
        self._login(self.designer)
        for action, config in (('add', EX_TYPE1), ('save', EX_TYPE1),
                               ('duplicate', None), ('remove', None)):
            with self.subTest(action=action):
                self._accb_post(action, config, accb_id=self.accb.pk)
                self._assert_nothing_changed()

    def test_refused_under_a_group_lock(self):
        self._group_lock()
        self._login(self.designer)
        for action, config in (('add', EX_TYPE1), ('save', EX_TYPE1),
                               ('duplicate', None), ('remove', None)):
            with self.subTest(action=action):
                self._accb_post(action, config, accb_id=self.accb.pk)
                self._assert_nothing_changed()

    def test_refused_for_anyone_but_the_assigned_designer(self):
        for profile in (self.other, self.qc, self.head, self.pm, self.admin):
            with self.subTest(user=profile.user.username):
                self._login(profile)
                response = self._accb_post('remove', accb_id=self.accb.pk)
                self.assertEqual(response.status_code, 403)
                response = self._accb_post('add', EX_TYPE1)
                self.assertEqual(response.status_code, 403)
                self._assert_nothing_changed()

    def test_a_locked_page_shows_accbs_read_only(self):
        self._design_lock()
        self._login(self.designer)
        response = self.client.get(reverse('opex_boq_entry',
                                           kwargs={'project_id': self.site.project_id}))
        html = response.content.decode()
        # B-3c: the text holds '&', which the page escapes.
        self.assertIn(escape(EX_TYPE2_TEXT), html)
        self.assertNotIn(reverse('opex_boq_accb', kwargs={'project_id': self.site.project_id}),
                         html)


# ===========================================================================
# The other write paths leave ACCB lines alone
# ===========================================================================

class PickerSaveTests(ACCBBase):

    def setUp(self):
        super().setUp()
        self._login(self.designer)
        self.mandatory = self._line('OPX-001', 10)
        self.accb_a = self._seed_accb(EX_TYPE2)
        self.accb_b = self._seed_accb(EX_TYPE1)

    def _save(self, data):
        return self.client.post(reverse('opex_boq_entry',
                                        kwargs={'project_id': self.site.project_id}), data)

    def test_a_sheet_save_that_omits_the_accbs_leaves_them_intact(self):
        dcdb = self._master('OPX-002')
        response = self._save({'action': 'save_draft', 'item': [str(dcdb.pk)],
                               f'qty_{dcdb.pk}': '3'})
        self.assertEqual(response.status_code, 302)
        lines = self._accb_lines()
        self.assertEqual([l.pk for l in lines], [self.accb_a.boq_item_id, self.accb_b.boq_item_id])
        self.assertEqual([l.description for l in lines], [EX_TYPE2_TEXT, EX_TYPE1_TEXT])
        self.assertTrue(all(l.boq_quantity == 1 for l in lines))
        self.assertEqual([l.accb_config.as_config() for l in lines], [EX_TYPE2, EX_TYPE1])
        # The ordinary items still work as before: OPX-002 added, mandatory OPX-001 kept.
        self.assertEqual(BOQItem.objects.get(boq__project=self.site,
                                             item_master=dcdb).boq_quantity, Decimal('3'))
        self.assertTrue(BOQItem.objects.filter(pk=self.mandatory.pk).exists())

    def test_posting_the_accb_code_as_a_picker_item_adds_nothing(self):
        self._save({'action': 'save_draft', 'item': [str(self.accb_master.pk)],
                    f'qty_{self.accb_master.pk}': '9',
                    f'qty_row_{self.accb_a.boq_item_id}': '9',
                    'keep_row': [str(self.accb_a.boq_item_id)]})
        self.assertEqual(BOQItem.objects.filter(item_master=self.accb_master).count(), 2)
        self.assertTrue(all(l.boq_quantity == 1 for l in self._accb_lines()))


class UploadDownloadTests(ACCBBase):

    def setUp(self):
        super().setUp()
        self._login(self.designer)
        self._line('OPX-001', 10)
        self.accb = self._seed_accb(EX_TYPE2)

    def _file(self, rows):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = BOQ_DOWNLOAD_SHEET
        ws.append([header for header, _key in BOQ_DOWNLOAD_COLUMNS])
        for code, quantity in rows:
            ws.append([code, '', '', '', quantity])
        out = BytesIO()
        wb.save(out)
        return SimpleUploadedFile('boq.xlsx', out.getvalue())

    def _url(self):
        return reverse('opex_boq_upload', kwargs={'project_id': self.site.project_id})

    def test_a_file_carrying_the_accb_code_is_refused_with_the_message(self):
        response = self.client.post(self._url(), {
            'phase': 'preview', 'file': self._file([('OPX-001', 12), (ACCB_CONFIGURED_CODE, 5)])})
        self.assertEqual(response.context['stage'], 'errors')
        self.assertIn(f'Row 3: {ACCB_UPLOAD_REFUSED_MESSAGE}', response.context['errors'])
        self.assertEqual(self._accb_lines()[0].boq_quantity, Decimal('1'))

    def test_a_crafted_commit_carrying_the_accb_code_is_refused(self):
        response = self.client.post(self._url(), {
            'phase': 'commit',
            'rows_json': json.dumps([{'code': ACCB_CONFIGURED_CODE, 'quantity': '5'}])})
        self.assertEqual(response.context['stage'], 'errors')
        self.assertEqual(self._accb_lines()[0].boq_quantity, Decimal('1'))

    def test_an_ordinary_upload_leaves_the_accb_line_at_1(self):
        preview = self.client.post(self._url(), {
            'phase': 'preview', 'file': self._file([('OPX-001', 12), ('OPX-002', 4)])})
        self.assertEqual(preview.context['stage'], 'preview')
        self.client.post(self._url(), {'phase': 'commit',
                                       'rows_json': preview.context['rows_json']})
        line = self._accb_lines()[0]
        self.assertEqual(line.boq_quantity, Decimal('1'))
        self.assertEqual(line.description, EX_TYPE2_TEXT)
        self.assertEqual(BOQItem.objects.filter(boq__project=self.site).count(), 3)

    def test_the_download_carries_the_accbs_on_their_own_sheet(self):
        from openpyxl import load_workbook
        response = self.client.get(reverse('opex_boq_download',
                                           kwargs={'project_id': self.site.project_id}))
        wb = load_workbook(BytesIO(response.content))
        data_codes = [row[0] for row in wb[BOQ_DOWNLOAD_SHEET].iter_rows(min_row=2, values_only=True)]
        self.assertNotIn(ACCB_CONFIGURED_CODE, data_codes)
        accb_rows = list(wb[BOQ_DOWNLOAD_ACCB_SHEET].iter_rows(min_row=5, values_only=True))
        self.assertEqual(accb_rows, [(1, ACCB_CONFIGURED_CODE, EX_TYPE2_TEXT, 'Nos', 1)])


class ReviewerTests(ACCBBase):
    """P7: a reviewer sees ACCB lines read-only and cannot change them."""

    def setUp(self):
        super().setUp()
        self.site, self.assignment = self._opex_site('AC-QC', status=DESIGN_IN_QC)
        self._line('OPX-001', 10)
        self.accb = self._seed_accb(EX_TYPE2)
        self._design_lock()
        self._login(self.qc)
        self.url = reverse('boq_correct', kwargs={'project_id': self.site.project_id})

    def test_the_correction_screen_shows_accbs_with_edit_and_no_quantity_box(self):
        """DW-2b (DW-R3) supersedes B-3 P7's read-only rule: the ACCB gets Edit and Remove,
        which open the configurator pop-up (tests_dw2b_reviewer_accb). It still gets no
        quantity box."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        # B-3c: the text holds '&', which the page escapes.
        self.assertIn(escape(EX_TYPE2_TEXT), html)
        self.assertIn(f'data-accb-id="{self.accb.pk}" data-accb-intent="edit"', html)
        self.assertNotIn(f'name="item_id" value="{self.accb.boq_item_id}"', html)

    def test_a_crafted_quantity_change_is_refused(self):
        response = self.client.post(self.url, {'action': 'set_quantity',
                                               'item_id': str(self.accb.boq_item_id),
                                               'quantity': '4'}, follow=True)
        self.assertIn(ACCB_REVIEWER_MESSAGE,
                      [str(m) for m in response.context['messages']])
        self.assertEqual(self._accb_lines()[0].boq_quantity, Decimal('1'))
        self.assertFalse(BOQCorrection.objects.exists())

    def test_a_reviewer_cannot_add_a_bare_accb_line(self):
        self.client.post(self.url, {'action': 'add_line',
                                    'master_id': str(self.accb_master.pk), 'quantity': '1'})
        self.assertEqual(BOQItem.objects.filter(item_master=self.accb_master).count(), 1)

    def test_the_review_panel_carries_the_accbs(self):
        panel = _boq_review_panel(self.site.boq)
        self.assertEqual([row.pk for row in panel['boq_accb_rows']], [self.accb.boq_item_id])
        self.assertEqual(panel['boq_row_count'], 2)


# ===========================================================================
# SCM — kept apart by configuration, nothing else moves
# ===========================================================================

class SCMGroupingTests(ACCBBase):

    def setUp(self):
        super().setUp()
        self.site_b, _ = self._opex_site('AC-B')
        for site in (self.site, self.site_b):
            self._line('OPX-001', 10, site=site)
            self._line('OPX-002', 2, site=site)
        self.ids = [self.site.pk, self.site_b.pk]

    def test_same_configuration_in_a_different_feeder_order_is_one_row_count_2(self):
        """B-3c: the feeders in a different order AND the SPD tick on the other of two
        identical O/G feeders — still the same thing to buy."""
        reordered = cfg('Type-1', [('MCB', 25), ('MCB', 40), ('MCB', 40)],
                        [('MCCB', 125, False), ('MCCB', 125, True)])
        self._seed_accb(EX_TYPE1, site=self.site)
        self._seed_accb(reordered, site=self.site_b)
        rows = aggregate_group_boq(self.ids)['accb_lines']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['total_quantity'], Decimal('2'))
        self.assertEqual(rows[0]['site_count'], 2)
        self.assertEqual(rows[0]['description'], EX_TYPE1_TEXT)
        self.assertEqual(rows[0]['contributions'], [('AC-A', Decimal('1')), ('AC-B', Decimal('1'))])

    def test_a_different_configuration_gets_its_own_row(self):
        self._seed_accb(EX_TYPE1, site=self.site)
        self._seed_accb(EX_TYPE1, site=self.site)
        self._seed_accb(EX_TYPE2, site=self.site_b)
        rows = {row['description']: row for row in aggregate_group_boq(self.ids)['accb_lines']}
        self.assertEqual(set(rows), {EX_TYPE1_TEXT, EX_TYPE2_TEXT})
        self.assertEqual(rows[EX_TYPE1_TEXT]['total_quantity'], Decimal('2'))
        self.assertEqual(rows[EX_TYPE1_TEXT]['site_count'], 1)
        self.assertEqual(rows[EX_TYPE2_TEXT]['total_quantity'], Decimal('1'))

    def test_a_different_spd_pattern_gets_its_own_row(self):
        both_spd = cfg('Type-1', [('MCB', 40), ('MCB', 25), ('MCB', 40)],
                       [('MCCB', 125, True), ('MCCB', 125, True)])
        self._seed_accb(EX_TYPE1, site=self.site)
        self._seed_accb(both_spd, site=self.site_b)
        rows = {row['config_key']: row for row in aggregate_group_boq(self.ids)['accb_lines']}
        self.assertEqual(set(rows), {accb_config_key(EX_TYPE1), accb_config_key(both_spd)})
        self.assertEqual([row['total_quantity'] for row in rows.values()],
                         [Decimal('1'), Decimal('1')])
        self.assertEqual(rows[accb_config_key(both_spd)]['description'],
                         accb_description(both_spd))
        self.assertTrue(rows[accb_config_key(both_spd)]['description'].endswith(
            f'O/G Feeder 2 Nos.: 2 x 125A, {MCCB}, with SPD.'))

    def test_non_accb_totals_are_unchanged_by_adding_accbs(self):
        before = aggregate_group_boq(self.ids)
        self._seed_accb(EX_TYPE1, site=self.site)
        self._seed_accb(EX_TYPE2, site=self.site_b)
        after = aggregate_group_boq(self.ids)
        self.assertEqual(after['lines'], before['lines'])
        self.assertEqual(after['item_count'], before['item_count'])
        self.assertEqual(after['unlinked'], before['unlinked'])
        self.assertNotIn(self.accb_master.pk, [line['item_master'] for line in after['lines']])

    def test_the_order_raise_lists_accbs_as_orderable_rows(self):
        """B-3b (rewritten from B-3a's read-only test): one orderable row per
        configuration, under its own amount field. tests_accb_order covers the raise."""
        self._seed_accb(EX_TYPE1, site=self.site)
        self._seed_accb(EX_TYPE1, site=self.site_b)
        context = _picker_context(self.program, {})
        self.assertEqual(len(context['accb_requirement']), 1)
        row = context['accb_requirement'][0]
        self.assertEqual(json.loads(row['contrib']),
                         {str(self.site.pk): '1.00', str(self.site_b.pk): '1.00'})
        self.assertEqual(row['field'], accb_amount_field(accb_config_key(EX_TYPE1)))
        html = render_to_string('projects/partials/vendor_order_requirement.html', context)
        self.assertNotIn('ACCBs are not ordered from here yet', html)
        self.assertNotIn('voAccbCard', html)
        self.assertIn(f'name="{row["field"]}"', html)
        # Never amount_<OPX-273 pk>: that one field would be shared by every ACCB.
        self.assertNotIn(f'name="amount_{self.accb_master.pk}"', html)
        errors = []
        lines, priced = _parse_requirement(
            {row['field']: '5000', f'amount_{self.accb_master.pk}': '9999'}, self.ids, errors)
        self.assertEqual(errors, [])
        accb = [line for line in lines if line['accb_config_key']]
        self.assertEqual(len(accb), 1)
        self.assertEqual((accb[0]['item_master_id'], accb[0]['description'],
                          accb[0]['quantity'], accb[0]['amount'], accb[0]['accb_config_key']),
                         (self.accb_master.pk, EX_TYPE1_TEXT, Decimal('2'), Decimal('5000'),
                          accb_config_key(EX_TYPE1)))
        # The catalogue-row amount for OPX-273 is ignored; it is not how an ACCB is priced.
        self.assertEqual(priced, Decimal('5000'))
        self.assertEqual(sorted(line['code'] for line in lines if not line['accb_config_key']),
                         ['OPX-001', 'OPX-002'])

    def test_the_group_screen_draws_the_accb_rows(self):
        from .models import SITE_GROUP_DRAFT
        self._seed_accb(EX_TYPE1, site=self.site)
        scm = _profile('scmacc', 'SCM')
        group = SiteGroup.objects.create(program=self.program, name='G2',
                                         status=SITE_GROUP_DRAFT, created_by=scm)
        for site in (self.site, self.site_b):
            SiteGroupMembership.objects.create(group=group, project=site, added_by=scm)
        self._login(scm)
        response = self.client.get(reverse('site_group_detail', kwargs={'pk': group.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="groupAccbs"', response.content.decode())
        self.assertIn(escape(EX_TYPE1_TEXT), response.content.decode())


# ===========================================================================
# The catalogue: OPX-273 locked, everything else as before
# ===========================================================================

class CatalogueTests(ACCBBase):

    def test_the_picker_catalogue_leaves_out_only_the_accb_row(self):
        codes = [m.code for m in get_opex_boq_catalogue()]
        self.assertEqual(codes, ['OPX-001', 'OPX-002', 'OPX-003', 'OPX-039'])
        self.assertNotIn('ACCB', opex_catalogue_category_order())

    def test_the_mandatory_items_are_unchanged(self):
        self.assertEqual([m.code for m in get_opex_mandatory_items()], ['OPX-001'])

    def test_the_existing_acdb_item_still_works_in_the_picker(self):
        self._login(self.designer)
        acdb = self._master('OPX-039')
        self.client.post(reverse('opex_boq_entry', kwargs={'project_id': self.site.project_id}),
                         {'action': 'save_draft', 'item': [str(acdb.pk)], f'qty_{acdb.pk}': '2'})
        line = BOQItem.objects.get(boq__project=self.site, item_master=acdb)
        self.assertEqual((line.boq_quantity, line.description),
                         (Decimal('2'), 'ACDB Type-1 (For 12KW INV)'))

    def _assert_unchanged(self):
        self.accb_master.refresh_from_db()
        self.assertTrue(self.accb_master.is_active)
        self.assertFalse(self.accb_master.is_mandatory)
        self.assertEqual(self.accb_master.description, 'ACCB (configured)')

    def _form_data(self, **overrides):
        data = {'description': 'Edited', 'unit': 'Nos', 'category': 'ACCB',
                'is_active': 'on', 'sort_order': '273', 'project_type': 'OPEX'}
        data.update(overrides)
        return data

    def test_the_head_cannot_edit_it(self):
        self._login(self.head)
        url = reverse('design_boq_catalogue_edit', kwargs={'item_id': self.accb_master.pk})
        self.assertEqual(self.client.get(url).status_code, 302)
        response = self.client.post(url, self._form_data(is_mandatory='on'), follow=True)
        self.assertIn(ACCB_CATALOGUE_LOCKED_MESSAGE, [str(m) for m in response.context['messages']])
        self._assert_unchanged()

    def test_the_head_cannot_deactivate_it(self):
        self._login(self.head)
        self.client.post(reverse('design_boq_catalogue_toggle',
                                 kwargs={'item_id': self.accb_master.pk}))
        self._assert_unchanged()

    def test_the_admin_cannot_edit_it(self):
        self._login(self.admin)
        url = reverse('admin_boq_item_edit', kwargs={'item_id': self.accb_master.pk})
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.post(url, self._form_data(is_mandatory='on'))
        self._assert_unchanged()

    def test_the_admin_cannot_deactivate_it(self):
        self._login(self.admin)
        self.client.post(reverse('admin_boq_item_toggle', kwargs={'item_id': self.accb_master.pk}))
        self._assert_unchanged()

    def test_the_form_refuses_mandatory_on_it(self):
        form = BOQItemMasterForm(self._form_data(description='ACCB (configured)',
                                                 is_mandatory='on'),
                                 instance=self.accb_master)
        del form.fields['code']
        self.assertFalse(form.is_valid())
        self.assertIn(ACCB_CATALOGUE_LOCKED_MESSAGE, form.non_field_errors())
        self._assert_unchanged()

    def test_both_catalogue_lists_draw_it_locked(self):
        self._login(self.head)
        html = self.client.get(reverse('design_boq_catalogue')).content.decode()
        self.assertIn('Locked — used by configured ACCBs', html)
        self.assertNotIn(reverse('design_boq_catalogue_toggle',
                                 kwargs={'item_id': self.accb_master.pk}), html)
        self._login(self.admin)
        html = self.client.get(reverse('admin_boq_items')).content.decode()
        self.assertIn('Locked — used by configured ACCBs', html)
        self.assertNotIn(reverse('admin_boq_item_toggle',
                                 kwargs={'item_id': self.accb_master.pk}), html)

    def test_other_items_stay_editable(self):
        self._login(self.head)
        other = self._master('OPX-002')
        url = reverse('design_boq_catalogue_edit', kwargs={'item_id': other.pk})
        self.assertEqual(self.client.get(url).status_code, 200)


# ===========================================================================
# B-3a.1 — numbers are never reused; the group screen counts ACCBs
# ===========================================================================

class NumberCounterTests(ACCBBase):

    def setUp(self):
        super().setUp()
        self._login(self.designer)

    def _numbers(self):
        return [line.accb_config.number for line in self._accb_lines()]

    def test_a_removed_highest_number_is_not_reused(self):
        for _ in range(3):
            self._accb_post('add', EX_TYPE2)
        third = self._accb_lines()[2]
        self._accb_post('remove', accb_id=third.accb_config.pk)
        self.assertEqual(self._numbers(), [1, 2])
        self._accb_post('add', EX_TYPE1)
        self.assertEqual(self._numbers(), [1, 2, 4])
        self.assertEqual(BOQ.objects.get(project=self.site).next_accb_number, 5)

    def test_duplicate_takes_its_number_from_the_counter(self):
        self._accb_post('add', EX_TYPE2)
        self._accb_post('add', EX_TYPE1)
        lines = self._accb_lines()
        self._accb_post('remove', accb_id=lines[1].accb_config.pk)
        self._accb_post('duplicate', accb_id=lines[0].accb_config.pk)
        self.assertEqual(self._numbers(), [1, 3])

    def test_a_counter_behind_the_numbers_in_use_cannot_collide(self):
        self._accb_post('add', EX_TYPE2)
        self._accb_post('add', EX_TYPE2)
        BOQ.objects.filter(project=self.site).update(next_accb_number=1)
        self._accb_post('add', EX_TYPE1)
        self.assertEqual(self._numbers(), [1, 2, 3])
        self.assertEqual(BOQ.objects.get(project=self.site).next_accb_number, 4)

    def test_the_backfill_sets_max_plus_one_and_leaves_empty_boqs_at_1(self):
        import importlib
        from django.apps import apps
        backfill = importlib.import_module(
            'projects.migrations.0150_boq_next_accb_number').backfill_next_accb_number
        gapped = self._boq()
        for number in (1, 3):   # ACCB 2 was removed
            line = BOQItem.objects.create(boq=gapped, item_master=self.accb_master,
                                          serial_no=273, category='ACCB',
                                          description=EX_TYPE2_TEXT, uom='Nos',
                                          boq_quantity=Decimal('1'))
            ACCBConfig.objects.create(boq=gapped, boq_item=line, number=number,
                                      accb_type='Type-2', has_spd=True,
                                      feeders={'in': EX_TYPE2['in'], 'out': EX_TYPE2['out']},
                                      config_key=accb_config_key(EX_TYPE2))
        other_site, _ = self._opex_site('AC-EMPTY')
        empty = self._boq(other_site)
        BOQ.objects.filter(pk__in=[gapped.pk, empty.pk]).update(next_accb_number=1)
        backfill(apps, None)
        self.assertEqual(BOQ.objects.get(pk=gapped.pk).next_accb_number, 4)
        self.assertEqual(BOQ.objects.get(pk=empty.pk).next_accb_number, 1)


class GroupScreenCountTests(ACCBBase):

    def setUp(self):
        super().setUp()
        from .models import SITE_GROUP_DRAFT
        self.site_b, _ = self._opex_site('AC-B')
        self.scm = _profile('scmacc2', 'SCM')
        self.group = SiteGroup.objects.create(program=self.program, name='G3',
                                              status=SITE_GROUP_DRAFT, created_by=self.scm)
        for site in (self.site, self.site_b):
            SiteGroupMembership.objects.create(group=self.group, project=site,
                                               added_by=self.scm)
        self._login(self.scm)
        self.url = reverse('site_group_detail', kwargs={'pk': self.group.pk})

    def _page(self):
        import re
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return re.sub(r'\s+', ' ', response.content.decode())

    def test_nothing_at_all_says_nothing_to_aggregate(self):
        html = self._page()
        self.assertIn('id="groupNothingToAggregate"', html)
        self.assertIn('2 sites, 0 items </span>', html)
        self.assertNotIn('ACCB configuration', html)

    def test_catalogue_quantities_only_omit_the_accb_count(self):
        self._line('OPX-001', 10)
        html = self._page()
        self.assertIn('2 sites, 1 item </span>', html)
        self.assertNotIn('ACCB configuration', html)
        self.assertNotIn('id="groupNothingToAggregate"', html)

    def test_accbs_only_are_counted_and_not_called_nothing(self):
        self._seed_accb(EX_TYPE2)
        self._seed_accb(EX_TYPE2, site=self.site_b)
        html = self._page()
        self.assertIn('2 sites, 0 items, 1 ACCB configuration </span>', html)
        self.assertNotIn('id="groupNothingToAggregate"', html)
        self.assertIn('id="groupAccbsOnly"', html)

    def test_the_header_counts_each_configuration(self):
        self._line('OPX-001', 10)
        self._seed_accb(EX_TYPE2)
        self._seed_accb(EX_TYPE1, site=self.site_b)
        html = self._page()
        self.assertIn('2 sites, 1 item, 2 ACCB configurations </span>', html)

    def test_accbs_add_no_query_to_the_group_screen(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self._line('OPX-001', 10)
        self.client.get(self.url)   # warm the session
        with CaptureQueriesContext(connection) as without:
            self.client.get(self.url)
        self._seed_accb(EX_TYPE2)
        self._seed_accb(EX_TYPE1, site=self.site_b)
        with CaptureQueriesContext(connection) as with_accbs:
            self.client.get(self.url)
        self.assertEqual(len(with_accbs), len(without))


class QueryCountTests(ACCBBase):
    """B-3c: the counts measured before B-3c (pre-flight), unchanged by it. The group order
    raise page's 14 is pinned in tests_accb_order; the SCM group screen's "ACCBs add no
    query" is pinned in GroupScreenCountTests."""

    def setUp(self):
        super().setUp()
        self._login(self.designer)
        self._line('OPX-001', 10)
        self.url = reverse('opex_boq_entry', kwargs={'project_id': self.site.project_id})
        self.client.get(self.url)   # warm the session

    def _count(self, call):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as queries:
            call()
        return len(queries)

    def test_the_picker_page_is_19_queries_with_0_or_3_accbs(self):
        self.assertEqual(self._count(lambda: self.client.get(self.url)), 19)
        for config in (EX_TYPE1, EX_TYPE2, EX_TYPE1):
            self._seed_accb(config)
        self.assertEqual(self._count(lambda: self.client.get(self.url)), 19)

    def test_the_accb_save_post_is_17_queries(self):
        accb = self._seed_accb(EX_TYPE1)
        self.assertEqual(
            self._count(lambda: self._accb_post('save', EX_TYPE2, accb_id=accb.pk)), 17)

    def test_the_duplicate_post_is_19_queries(self):
        # The re-check added by B-3c is pure Python: no query.
        accb = self._seed_accb(EX_TYPE1)
        self._seed_accb(EX_TYPE2)
        self.assertEqual(self._count(lambda: self._accb_post('duplicate', accb_id=accb.pk)), 19)
