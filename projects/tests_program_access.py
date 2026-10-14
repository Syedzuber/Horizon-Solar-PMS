"""
PM-2b — the Program creator fallback ends at handover (ruling PM-14).

views._can_access_program admitted Admin, CEO, anyone managing a live child site, and
the Program's creator — the last one for ever. Since PMs create Programs, a PM who
handed a whole tender over kept program_list, program_detail, program_edit,
opex_site_create, opex_site_bulk_upload and opex_site_bulk_template, and
opex_site_create would make them a new site's PM, restoring full authority.

PM-14: the creator fallback stops when BOTH are true — the creator holds a CLOSED
SitePmAssignment row on a site of the Program (soft-deleted sites included), AND
user_can_manage_program() is false for them. Everyone else is unchanged.

WHAT IS PINNED HERE — refusals first, because they are the point
-----------------------------------------------------------------
    - a creator after a full handover is refused on all six views, with the existing
      denial: absent from program_list (200), bare 404 on the five object views —
      the same bytes a PM who never touched the Program gets
    - a creator whose only closed row is on a soft-deleted site is refused
    - a creator whose sites were all handed over and then all soft-deleted is refused
    - a creator who never held a seat, still manages one site, was reassigned back,
      or is a coordinator only, is allowed as before
    - Admin and CEO are unchanged; the incoming PM is allowed
    - program_list issues at most ONE extra query for PM-14, however many Programs
    - Program.created_by is read-only in the Django admin (it is an access input now)

    python manage.py test projects.tests_program_access --settings=solarpms.test_settings
"""
from decimal import Decimal

from django.contrib.admin.sites import site as admin_site
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import Program, Project, SitePmAssignment
from .permissions import program_ids_handed_over_by, user_can_access_program
from .site_pm import change_site_pm

REASON = 'Tender moves to another PM'

# The five views that take a Program pk. program_list is asserted separately: its
# refusal is the Program's absence from a 200 page, not a 404.
OBJECT_VIEWS = ('program_detail', 'program_edit', 'opex_site_create',
                'opex_site_bulk_upload', 'opex_site_bulk_template')
ADMIN_PM_ONLY = ('program_edit', 'opex_site_create',
                 'opex_site_bulk_upload', 'opex_site_bulk_template')


def _profile(username, role):
    """A post_save signal creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title(), last_name='Test',
                                    email=f'{username}@example.com')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.save()
    return profile


def _client(profile):
    client = Client(SERVER_NAME='localhost')
    client.force_login(profile.user)
    return client


class ProgramAccessFixture(TestCase):
    """Users only. Every test builds its own Program, so each scenario's history is
    exactly the one it names."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = _profile('pa_admin', 'Admin')
        cls.ceo = _profile('pa_ceo', 'CEO')
        cls.creator = _profile('pa_creator', 'PM')
        cls.pm_in = _profile('pa_pm_in', 'PM')
        cls.stranger = _profile('pa_stranger', 'PM')   # never created or held anything

    def setUp(self):
        self._n = 0

    def _next(self):
        """A per-test serial for unique tender and site codes."""
        self._n += 1
        return self._n

    # -- builders ---------------------------------------------------------------------

    def _program(self, created_by=None):
        n = self._next()
        return Program.objects.create(
            name=f'Tender {n}', program_type='OPEX', client_name='Client', status='Active',
            short_tender_code=f'PA{n:03d}',
            created_by=(created_by or self.creator).user)

    def _site(self, program, pm):
        n = self._next()
        site =Project.objects.create(
            customer_name=f'Site {n}', project_id=f'PAS{n:04d}', program=program,
            customer_phone='9876543210', site_address='1 Sun Road', city='Bhopal',
            state='Madhya Pradesh', project_type='OPEX', dc_capacity_kw=Decimal('50.00'),
            status='Active')
        change_site_pm(site, pm, by=self.admin, expected_outgoing_pm=None)
        return site

    def _hand_over(self, site, from_pm, to_pm):
        change_site_pm(site, to_pm, by=self.admin, expected_outgoing_pm=from_pm,
                       reason=REASON)

    def _soft_delete(self, site):
        Project.objects.filter(pk=site.pk).update(is_deleted=True,
                                                  deleted_at=timezone.now())

    # -- assertions -------------------------------------------------------------------

    def _listed(self, profile, program):
        response = _client(profile).get(reverse('program_list'))
        self.assertEqual(response.status_code, 200)
        return program.pk in {p.pk for p in response.context['programs']}

    def assertRefusedEverywhere(self, profile, program):
        """The existing denial, exactly: absent from a 200 list, and on each object view
        the same bare 404 a PM with no connection to the Program gets — no redirect,
        no handover message."""
        self.assertFalse(self._listed(profile, program))
        for name in OBJECT_VIEWS:
            url = reverse(name, args=[program.pk])
            refused = _client(profile).get(url)
            baseline = _client(self.stranger).get(url)
            self.assertEqual(refused.status_code, 404, name)
            self.assertEqual(refused.content, baseline.content, name)
        self.assertFalse(user_can_access_program(profile.user, program))

    def assertAllowedEverywhere(self, profile, program):
        self.assertTrue(self._listed(profile, program))
        for name in OBJECT_VIEWS:
            response = _client(profile).get(reverse(name, args=[program.pk]))
            self.assertEqual(response.status_code, 200, name)
        self.assertTrue(user_can_access_program(profile.user, program))


class CreatorRefusedTests(ProgramAccessFixture):
    """The negative half — the reason this session exists."""

    def test_01_full_handover_refuses_creator_on_all_six_views(self):
        program = self._program()
        sites = [self._site(program, self.creator) for _ in range(3)]
        self.assertAllowedEverywhere(self.creator, program)   # before: a creator-PM

        for site in sites:
            self._hand_over(site, self.creator, self.pm_in)

        self.assertRefusedEverywhere(self.creator, program)

    def test_02_refused_post_creates_nothing(self):
        """A refused POST to opex_site_create is the same 404 and writes no site — the
        path that used to give a former creator a new seat."""
        program = self._program()
        site = self._site(program, self.creator)
        self._hand_over(site, self.creator, self.pm_in)
        before = Project.objects.count()

        response = _client(self.creator).post(
            reverse('opex_site_create', args=[program.pk]),
            {'site_code': 'PAZ999', 'site_name': 'Back door'})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(Project.objects.count(), before)
        self.assertFalse(SitePmAssignment.objects.filter(
            pm=self.creator, valid_to__isnull=True).exists())

    def test_03_refused_program_edit_post_changes_nothing(self):
        program = self._program()
        site = self._site(program, self.creator)
        self._hand_over(site, self.creator, self.pm_in)

        response = _client(self.creator).post(
            reverse('program_edit', args=[program.pk]), {'name': 'Renamed'})

        self.assertEqual(response.status_code, 404)
        program.refresh_from_db()
        self.assertNotEqual(program.name, 'Renamed')

    def test_04_only_closed_row_on_soft_deleted_site_refuses(self):
        """PM-14 (a) counts soft-deleted sites. The creator's one seat was handed over and
        the site later deleted; the live site was someone else's from the start."""
        program = self._program()
        held = self._site(program, self.creator)
        self._site(program, self.pm_in)
        self._hand_over(held, self.creator, self.pm_in)
        self._soft_delete(held)

        self.assertRefusedEverywhere(self.creator, program)

    def test_05_all_handed_over_then_all_soft_deleted_refuses(self):
        program = self._program()
        sites = [self._site(program, self.creator) for _ in range(2)]
        for site in sites:
            self._hand_over(site, self.creator, self.pm_in)
            self._soft_delete(site)

        self.assertRefusedEverywhere(self.creator, program)

    def test_06_handed_over_in_x_still_managing_y(self):
        """The rule is per Program: managing a site in Y gives nothing in X."""
        x = self._program()
        y = self._program()
        self._hand_over(self._site(x, self.creator), self.creator, self.pm_in)
        self._site(y, self.creator)

        self.assertRefusedEverywhere(self.creator, x)
        self.assertAllowedEverywhere(self.creator, y)


class CreatorAllowedTests(ProgramAccessFixture):
    """Every creator PM-14 does not name keeps the fallback exactly as before."""

    def test_01_creator_who_never_held_a_seat_is_allowed(self):
        program = self._program()
        self._site(program, self.pm_in)            # someone else's from the start
        self.assertAllowedEverywhere(self.creator, program)

    def test_02_creator_of_empty_program_is_allowed(self):
        """The window the fallback was written for."""
        self.assertAllowedEverywhere(self.creator, self._program())

    def test_03_partial_handover_still_managing_one_site_is_allowed(self):
        program = self._program()
        a, b, kept = (self._site(program, self.creator) for _ in range(3))
        self._hand_over(a, self.creator, self.pm_in)
        self._hand_over(b, self.creator, self.pm_in)
        self.assertAllowedEverywhere(self.creator, program)

    def test_04_handed_over_then_reassigned_back_is_allowed(self):
        program = self._program()
        a, b = (self._site(program, self.creator) for _ in range(2))
        self._hand_over(a, self.creator, self.pm_in)
        self._hand_over(b, self.creator, self.pm_in)
        self._hand_over(b, self.pm_in, self.creator)      # back again
        self.assertTrue(SitePmAssignment.objects.filter(
            pm=self.creator, valid_to__isnull=False).exists())
        self.assertAllowedEverywhere(self.creator, program)

    def test_05_coordinator_only_creator_is_allowed(self):
        """A coordinator never gets a history row, so PM-14 never names them; while
        coordinating they manage the Program anyway."""
        program = self._program()
        site = self._site(program, self.pm_in)
        site.coordinators.add(self.creator)
        self.assertAllowedEverywhere(self.creator, program)

        site.coordinators.remove(self.creator)
        self.assertAllowedEverywhere(self.creator, program)   # still no closed row


class OthersUnchangedTests(ProgramAccessFixture):

    def setUp(self):
        super().setUp()
        self.program = self._program()
        for site in [self._site(self.program, self.creator) for _ in range(2)]:
            self._hand_over(site, self.creator, self.pm_in)

    def test_01_admin_unchanged(self):
        self.assertAllowedEverywhere(self.admin, self.program)

    def test_02_ceo_unchanged(self):
        """CEO reads the list and detail; the four Admin/PM views stay role-gated 403."""
        self.assertTrue(self._listed(self.ceo, self.program))
        client = _client(self.ceo)
        self.assertEqual(
            client.get(reverse('program_detail', args=[self.program.pk])).status_code, 200)
        for name in ADMIN_PM_ONLY:
            self.assertEqual(
                client.get(reverse(name, args=[self.program.pk])).status_code, 403, name)
        self.assertTrue(user_can_access_program(self.ceo.user, self.program))

    def test_03_incoming_pm_allowed(self):
        self.assertAllowedEverywhere(self.pm_in, self.program)

    def test_04_non_creator_refusal_unchanged(self):
        """A PM with no tie to the Program: the same bare 404 as before PM-14."""
        for name in OBJECT_VIEWS:
            response = _client(self.stranger).get(reverse(name, args=[self.program.pk]))
            self.assertEqual(response.status_code, 404, name)
        self.assertFalse(self._listed(self.stranger, self.program))


class ProgramListQueryTests(ProgramAccessFixture):
    """PM-14 adds at most ONE query to program_list: the handed-over set, fetched once,
    and only when the PM created a listed Program."""

    def _history_queries(self, profile):
        with CaptureQueriesContext(connection) as ctx:
            _client(profile).get(reverse('program_list'))
        return sum('sitepmassignment' in q['sql'].lower() for q in ctx.captured_queries)

    def test_01_one_query_however_many_created_programs(self):
        for _ in range(3):
            program = self._program()
            self._hand_over(self._site(program, self.creator), self.creator, self.pm_in)
        self.assertEqual(self._history_queries(self.creator), 1)

        for _ in range(3):
            self._program()
        self.assertEqual(self._history_queries(self.creator), 1)

    def test_02_no_query_for_a_pm_who_created_nothing(self):
        self._site(self._program(), self.pm_in)
        self.assertEqual(self._history_queries(self.pm_in), 0)

    def test_03_handed_over_set_counts_soft_deleted_sites(self):
        program = self._program()
        site = self._site(program, self.creator)
        self._hand_over(site, self.creator, self.pm_in)
        self._soft_delete(site)
        self.assertEqual(program_ids_handed_over_by(self.creator), {program.pk})
        self.assertEqual(program_ids_handed_over_by(None), set())


class ProgramAdminCreatedByTests(ProgramAccessFixture):
    """Q3: created_by is an access input, so the Django admin shows it but cannot set it."""

    def test_01_change_form_does_not_offer_created_by(self):
        program = self._program()
        superuser = User.objects.create_superuser('pa_super', 'super@example.com', 'x')
        superuser.profile.role = 'Admin'
        superuser.profile.save()
        client = Client(SERVER_NAME='localhost')
        client.force_login(superuser)

        response = client.get(reverse('admin:projects_program_change', args=[program.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="created_by"')
        request = RequestFactory().get('/')
        request.user = superuser
        model_admin = admin_site._registry[Program]
        self.assertIn('created_by', model_admin.get_readonly_fields(request, program))
        self.assertNotIn('created_by', model_admin.get_form(request, program).base_fields)
