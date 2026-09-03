"""
Prompt 1.2a — the execution capability flags (R-15) and `StockLocation` (B-14).

WHAT THIS FILE IS ACTUALLY FOR, BECAUSE IT IS NOT WHAT IT LOOKS LIKE.

Three boolean columns that default False and one table with no writer look like nothing
worth testing. The thing under test is not the columns; it is the DECISION they encode.

R-15 says QA/QC, HSE and warehouse keeping are FLAGS, NOT ROLE_CHOICES VALUES, because a
new role string costs its holder every `Task.assigned_role` match — the lesson Part 6.5b
paid for when 'Design Head' was added as a role and taken back out again. The way that
decision gets quietly undone is not someone deleting a flag; it is someone deciding a
flag ought to "do something" and wiring it into a permission path. So the load-bearing
assertion here is a NEGATIVE one: setting a capability flag changes the user's role not
at all and grants authority nowhere. If `test_setting_every_flag_grants_no_authority`
ever needs relaxing, the thing being relaxed is R-15.

NOTHING READS THESE FLAGS YET. Consumers arrive with 2.2 (is_hse), 2.3 (is_qaqc) and 4.1
(is_warehouse_keeper), and each brings its own permission helper with it (R-12). There is
deliberately no `user_is_keeper_of()` to test.

`StockLocation`'s tests are about the two things B-14 settled that a schema alone does
not say out loud: a warehouse outlives its keeper, and a keeper may hold more than one.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase

from .models import Program, Project, StockLocation, UserProfile
from .permissions import user_can_manage_project


CAPABILITY_FLAGS = ('is_qaqc', 'is_hse', 'is_warehouse_keeper')


def _make_user(username, role=''):
    user = User.objects.create_user(username=username, password='pw12345')
    profile = user.profile          # auto-created by the post_save signal
    profile.role = role
    profile.save(update_fields=['role'])
    return user, profile


class CapabilityFlagDefaultsTests(TestCase):
    """A flag nobody has set must be off, on every profile the product creates."""

    def test_all_three_flags_default_to_false(self):
        _, profile = _make_user('cf_new')

        for flag in CAPABILITY_FLAGS:
            with self.subTest(flag=flag):
                self.assertFalse(getattr(profile, flag),
                                 f'{flag} must default False — a capability nobody '
                                 f'granted is a capability nobody has')

    def test_the_defaults_survive_a_round_trip_through_the_database(self):
        """`default=False` on the field and `NOT NULL DEFAULT false` in the column are
        two different claims. This asserts the second one."""
        _, profile = _make_user('cf_roundtrip')
        profile.refresh_from_db()

        for flag in CAPABILITY_FLAGS:
            with self.subTest(flag=flag):
                self.assertIs(getattr(profile, flag), False)

    def test_the_flags_are_independent_of_each_other(self):
        """One person may hold any combination — a warehouse keeper who is also the HSE
        signatory is a normal thing on a three-warehouse operation."""
        _, profile = _make_user('cf_combo')
        profile.is_hse = True
        profile.is_warehouse_keeper = True
        profile.save(update_fields=['is_hse', 'is_warehouse_keeper'])

        profile.refresh_from_db()
        self.assertTrue(profile.is_hse)
        self.assertTrue(profile.is_warehouse_keeper)
        self.assertFalse(profile.is_qaqc, 'setting two flags must not set the third')


class CapabilityFlagsGrantNothingTests(TestCase):
    """THE POINT OF THE WHOLE SESSION, ASSERTED AS AN ABSENCE.

    A capability flag is a fact recorded about a person, not an authority. Until its
    consumer ships, setting one must change nothing a user can do — and after its
    consumer ships, it must still not change anything OUTSIDE that consumer. These tests
    are what stands between R-15 and a future session that decides a flag should imply
    "and also a bit of PM authority, surely".
    """

    def setUp(self):
        self.owner_user, self.owner = _make_user('cf_pm', 'PM')
        self.other_user, self.other = _make_user('cf_other', 'Site Engineer')
        self.program = Program.objects.create(
            program_type='OPEX', name='CapFlagTender', client_name='CFClient',
            status='Active', short_tender_code='CFT',
        )
        self.project = Project.objects.create(
            project_id='CFT-S01', customer_name='CFClient',
            customer_phone='9876543210', site_address='1 Sun Rd', city='Delhi',
            project_type='OPEX', program=self.program, site_code='S01',
            capacity_kw=Decimal('100.00'), status='Draft',
            assigned_pm=self.owner,
        )

    def test_the_fixture_itself_is_honest(self):
        """If the PM could not manage his own project, the negative assertions below
        would pass for the wrong reason."""
        self.assertTrue(user_can_manage_project(self.owner_user, self.project))
        self.assertFalse(user_can_manage_project(self.other_user, self.project))

    def test_setting_every_flag_grants_no_authority(self):
        """All three flags on a user with no claim to this project. He must still have
        no claim to it. A flag is not a back door into `user_can_manage_project()`."""
        for flag in CAPABILITY_FLAGS:
            setattr(self.other, flag, True)
        self.other.save(update_fields=list(CAPABILITY_FLAGS))

        self.assertFalse(
            user_can_manage_project(self.other_user, self.project),
            'a capability flag must grant NOTHING on its own — see R-15 and the '
            'class docstring before changing this')

    def test_each_flag_alone_grants_no_authority(self):
        """Asserted one at a time as well as together, so a future wiring of exactly one
        flag into a permission path cannot hide behind the combined case."""
        for flag in CAPABILITY_FLAGS:
            with self.subTest(flag=flag):
                UserProfile.objects.filter(pk=self.other.pk).update(
                    **{f: (f == flag) for f in CAPABILITY_FLAGS})
                self.other.refresh_from_db()

                self.assertTrue(getattr(self.other, flag), 'fixture sanity')
                self.assertFalse(
                    user_can_manage_project(self.other_user, self.project),
                    f'{flag} alone must grant no project authority')

    def test_setting_a_flag_does_not_change_the_role(self):
        """The whole reason these are flags: the holder KEEPS the role he had, and with
        it every `Task.assigned_role` match that role gives him."""
        before = self.other.role
        self.other.is_qaqc = True
        self.other.save(update_fields=['is_qaqc'])
        self.other.refresh_from_db()

        self.assertEqual(self.other.role, before)
        self.assertEqual(self.other.role, 'Site Engineer')

    def test_the_flags_are_not_role_choices_values(self):
        """The literal thing R-15 forbids. If any of these names becomes a ROLE_CHOICES
        value, this fails and the failure names the decision."""
        role_values = {value for value, _ in UserProfile.ROLE_CHOICES}

        for name in ('QA/QC', 'QAQC', 'HSE', 'Warehouse Keeper', 'Warehouse'):
            with self.subTest(candidate=name):
                self.assertNotIn(
                    name, role_values,
                    'R-15: these are capability flags, not roles. A new ROLE_CHOICES '
                    'value costs its holder every Task.assigned_role match — see the '
                    'ROLE_CHOICES comment in models.py.')

    def test_a_flag_holder_keeps_his_own_project_authority(self):
        """The mirror of the negative case. Flags must not TAKE anything away either —
        the PM who also signs off HSE is still the PM."""
        self.owner.is_hse = True
        self.owner.save(update_fields=['is_hse'])

        self.assertTrue(user_can_manage_project(self.owner_user, self.project))


class StockLocationTests(TestCase):
    """B-14: warehouses are rows, and a keeper's authority follows the warehouse."""

    def setUp(self):
        _, self.keeper = _make_user('sl_keeper', 'SCM')
        _, self.other_keeper = _make_user('sl_keeper_2', 'SCM')

    def _location(self, code, **kwargs):
        return StockLocation.objects.create(
            name=kwargs.pop('name', f'Warehouse {code}'), code=code, **kwargs)

    def test_a_warehouse_can_exist_with_no_keeper(self):
        """A new building is entered before anyone is put in charge of it, and a
        building whose keeper left still has stock in it."""
        location = self._location('HYD-1')

        self.assertIsNone(location.keeper)
        self.assertTrue(location.is_active)

    def test_a_keeper_can_be_assigned(self):
        location = self._location('HYD-1', keeper=self.keeper)
        location.refresh_from_db()

        self.assertEqual(location.keeper, self.keeper)
        self.assertIn(location, self.keeper.keeper_of.all())

    def test_deleting_the_keepers_profile_does_not_delete_the_warehouse(self):
        """SET_NULL, and this is why it matters: CASCADE here would delete a BUILDING —
        and everything a later part hangs off it — because a person left the company."""
        location = self._location('HYD-1', keeper=self.keeper)

        self.keeper.delete()

        location.refresh_from_db()
        self.assertIsNone(location.keeper,
                          'the warehouse must outlive its keeper, with nobody in '
                          'charge of it until an admin names a replacement')
        self.assertTrue(StockLocation.objects.filter(pk=location.pk).exists())

    def test_two_warehouses_may_share_one_keeper(self):
        """One keeper per warehouse is the rule; one warehouse per keeper is NOT. On a
        three-warehouse operation with someone on leave, this will happen."""
        first = self._location('HYD-1', keeper=self.keeper)
        second = self._location('HYD-2', keeper=self.keeper)

        self.assertEqual(first.keeper, second.keeper)
        self.assertEqual(self.keeper.keeper_of.count(), 2)

    def test_code_is_unique(self):
        self._location('HYD-1')

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._location('HYD-1', name='A different building, same code')

    def test_a_second_warehouse_may_have_a_different_keeper(self):
        """The sanity case beside the shared-keeper one: keepers are per row, not global."""
        first = self._location('HYD-1', keeper=self.keeper)
        second = self._location('MUM-1', keeper=self.other_keeper)

        self.assertNotEqual(first.keeper, second.keeper)

    def test_deactivation_is_the_only_retirement_there_is(self):
        """NO `is_deleted` ON THIS MODEL, DELIBERATELY. This codebase has no custom
        managers, so every soft-deleted model is one more filter every future queryset
        must remember. If someone adds `is_deleted` here, this test says why not."""
        self.assertFalse(hasattr(StockLocation, 'is_deleted'))

        location = self._location('HYD-1')
        location.is_active = False
        location.save(update_fields=['is_active'])

        location.refresh_from_db()
        self.assertFalse(location.is_active)
        self.assertTrue(StockLocation.objects.filter(pk=location.pk).exists(),
                        'a closed warehouse keeps its history — it is not removed')

    def test_nothing_is_seeded(self):
        """THE COUNT MUST NOT BE STRUCTURAL (B-14). Horizon runs three warehouses today;
        that is data the product owner enters, not a migration and not a constant. A
        seeded row here would be the first step back towards a hardcoded three."""
        self.assertEqual(StockLocation.objects.count(), 0,
                         'no migration may seed a warehouse')
