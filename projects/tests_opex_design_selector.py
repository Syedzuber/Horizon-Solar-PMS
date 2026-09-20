"""The header Design selector is not offered on OPEX (RESCO) sites.

WHY THE SELECTOR WENT AWAY. `Project.assigned_design` has two writers. On Residential
it is `project_activate()`, from the PM's own activation form, and the header selector
on the overview page edits it afterwards. On OPEX it is `_allocate_one()` in
design_views, which stamps the field at the one moment a designer is chosen so that it
cannot diverge from `DesignAssignment.assigned_to` — divergence locks the allocated
designer out of BOQ entry, because `user_can_edit_project_boq()` is W-narrow and the FK
alone carries the write gate. Migration 0051 repaired the rows that had already
diverged. The PM selector was a second writer into that field with none of that
context.

WHAT THIS SUITE PINS, and the distinction it exists to defend: the selector is withheld
and this view's `assign_design` POST is refused, while `_allocate_one()` is untouched.
Hiding a control is not the same as removing an authority, and a change that quietly
took the Head's allocation down with it would pass a test that only checked the header.
`AllocationStillWritesTheFieldTests` is that guard — it drives the real allocation
function on an OPEX site and asserts both the write AND the BOQ authority it confers.

WHAT THIS SUITE DOES NOT TEST. Nulling or migrating existing values: nothing does that,
by instruction. The 11 non-deleted OPEX sites carrying the FK on the local database all
agree with their allocation exactly, so there is no divergent data to repair and no
repair path to test — see EXECUTION_MODULE_DEFERRED.md §D for the standing exposure.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from .design_views import _allocate_one
from .models import (
    DesignAssignment, Project, UserProfile,
    DESIGN_AWAITING_ALLOCATION,
)
from .permissions import user_can_edit_project_boq


def _profile(username, role):
    """Create a user and give their profile `role`.

    signals.py already made a profile with the model's DEFAULT role by the time
    create_user() returns, so this UPDATES rather than creates — the same reason
    tests_opex_activation.py spells this out.
    """
    user = User.objects.create_user(
        username=username, password='x',
        first_name=username.title(), last_name='Test',
    )
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.save(update_fields=['role'])
    return profile


def _client_for(profile):
    client = Client()
    client.force_login(profile.user)
    return client


class DesignSelectorBase(TestCase):
    """One PM, two designers, and one site of each project_type.

    Every site is Active and PM-assigned, which is what `can_assign_design` required
    BEFORE this change — so a failure here is about project_type and nothing else.
    """

    @classmethod
    def setUpTestData(cls):
        cls.pm       = _profile('selpm',   'PM')
        cls.designer = _profile('seldes',  'Design')
        cls.other    = _profile('seldes2', 'Design')
        cls.head     = _profile('selhead', 'Design')

    def setUp(self):
        self.opex        = self._site('OPEX',        'Tender Site A')
        self.residential = self._site('Residential', 'House A')
        self.capex       = self._site('CAPEX',       'Factory A')

    def _site(self, project_type, customer_name):
        return Project.objects.create(
            customer_name=customer_name,
            customer_phone='9876543210',
            site_address='1 Sun Road',
            city='Lucknow',
            project_type=project_type,
            dc_capacity_kw=Decimal('100.00'),
            status='Active',
            assigned_pm=self.pm,
        )

    def _get(self, project):
        return _client_for(self.pm).get(
            reverse('project_overview', args=[project.project_id]))

    def _post_assign(self, project, designer_pk=''):
        response = _client_for(self.pm).post(
            reverse('project_overview', args=[project.project_id]),
            {'action': 'assign_design', 'assigned_design': str(designer_pk)},
        )
        project.refresh_from_db()
        return response


# ---------------------------------------------------------------------------
# 1 — the selector itself
# ---------------------------------------------------------------------------

class SelectorVisibilityTests(DesignSelectorBase):

    def test_opex_does_not_render_the_selector(self):
        response = self._get(self.opex)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['show_design_selector'])
        self.assertFalse(response.context['can_assign_design'])
        # The <select> and the hidden action that submits it are both gone.
        self.assertNotContains(response, 'name="assigned_design"')
        self.assertNotContains(response, 'value="assign_design"')

    def test_residential_still_renders_the_selector(self):
        response = self._get(self.residential)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['show_design_selector'])
        self.assertTrue(response.context['can_assign_design'])
        self.assertContains(response, 'name="assigned_design"')
        self.assertContains(response, 'value="assign_design"')

    def test_capex_still_renders_the_selector(self):
        response = self._get(self.capex)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['show_design_selector'])
        self.assertTrue(response.context['can_assign_design'])
        self.assertContains(response, 'name="assigned_design"')

    def test_opex_still_shows_an_already_assigned_designer_by_name(self):
        """Hiding the control does not hide the fact. Existing values are not nulled,
        so the site must still say who its designer is — otherwise the PM loses the
        information as well as the authority."""
        self.opex.assigned_design = self.designer
        self.opex.save(update_fields=['assigned_design'])
        response = self._get(self.opex)
        self.assertContains(response, self.designer.user.get_full_name())


# ---------------------------------------------------------------------------
# 2 — the POST behind it
# ---------------------------------------------------------------------------

class AssignDesignPostTests(DesignSelectorBase):

    def test_opex_post_is_refused_and_writes_nothing(self):
        """Hiding without refusing is not acceptable: a stale tab or a hand-rolled POST
        would otherwise still reach the write."""
        self.opex.assigned_design = self.designer
        self.opex.save(update_fields=['assigned_design'])

        response = self._post_assign(self.opex, self.other.pk)

        self.assertEqual(self.opex.assigned_design_id, self.designer.pk)   # unchanged
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any('RESCO' in m for m in messages), messages)
        self.assertTrue(any('No change was made' in m for m in messages), messages)

    def test_opex_post_cannot_clear_the_field_either(self):
        """The blank option is the same write by another name — an unassign would strip
        the allocated designer's BOQ authority just as surely as an overwrite."""
        self.opex.assigned_design = self.designer
        self.opex.save(update_fields=['assigned_design'])

        response = self._post_assign(self.opex, '')

        self.assertEqual(self.opex.assigned_design_id, self.designer.pk)
        # Refused out loud, not dropped: the blank option must reach the same branch.
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any('No change was made' in m for m in messages), messages)

    def test_residential_post_still_assigns(self):
        self._post_assign(self.residential, self.designer.pk)
        self.assertEqual(self.residential.assigned_design_id, self.designer.pk)

    def test_residential_post_still_unassigns(self):
        self.residential.assigned_design = self.designer
        self.residential.save(update_fields=['assigned_design'])
        self._post_assign(self.residential, '')
        self.assertIsNone(self.residential.assigned_design_id)

    def test_capex_post_still_assigns(self):
        self._post_assign(self.capex, self.designer.pk)
        self.assertEqual(self.capex.assigned_design_id, self.designer.pk)


# ---------------------------------------------------------------------------
# 3 — THE REGRESSION GUARD: the Head's allocation is untouched
# ---------------------------------------------------------------------------

class AllocationStillWritesTheFieldTests(DesignSelectorBase):
    """The refusal is scoped to project_overview's assign_design action ONLY.

    `_allocate_one()` writes `Project.assigned_design` directly on the model and does
    not route through that view, so it must keep working — and the authority that write
    confers must keep working with it. A refusal implemented one layer lower (on
    Project.save, in a signal, or in the permission helper) would pass section 2 above
    and fail here, which is the entire point of this class.
    """

    def setUp(self):
        super().setUp()
        self.assignment = DesignAssignment.objects.create(
            project=self.opex,
            status=DESIGN_AWAITING_ALLOCATION,
            survey_file_bucket='b',
            survey_file_path='OPEX/survey/x.pdf',   # satisfies survey_ready
        )

    def test_allocation_stamps_assigned_design_on_an_opex_site(self):
        self.assertIsNone(self.opex.assigned_design_id)

        _allocate_one(self.assignment, self.designer, self.head)

        self.opex.refresh_from_db()
        self.assertEqual(self.opex.assigned_design_id, self.designer.pk)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.assigned_to_id, self.designer.pk)

    def test_the_allocated_designer_can_still_edit_the_boq(self):
        """The reason the stamp matters. user_can_edit_project_boq() is W-narrow — the
        FK alone carries the write gate — so if allocation stopped writing it, the
        allocated designer would be locked out of the BOQ they were just handed."""
        self.assertFalse(user_can_edit_project_boq(self.designer.user, self.opex))

        _allocate_one(self.assignment, self.designer, self.head)

        self.opex.refresh_from_db()
        self.assertTrue(user_can_edit_project_boq(self.designer.user, self.opex))
        # Still nobody else's BOQ to write.
        self.assertFalse(user_can_edit_project_boq(self.other.user, self.opex))

    def test_reallocation_still_moves_the_field(self):
        """Reallocation is the repair path that replaces the selector for OPEX. If it
        stopped re-stamping, a corrected allocation would leave the OLD designer holding
        BOQ authority — a silent, unrepairable divergence."""
        _allocate_one(self.assignment, self.designer, self.head)
        self.assignment.refresh_from_db()

        _allocate_one(self.assignment, self.other, self.head)

        self.opex.refresh_from_db()
        self.assertEqual(self.opex.assigned_design_id, self.other.pk)
        self.assertTrue(user_can_edit_project_boq(self.other.user, self.opex))
        self.assertFalse(user_can_edit_project_boq(self.designer.user, self.opex))

    def test_the_view_refusal_does_not_block_a_later_allocation(self):
        """The two paths in one test, in the order a real site meets them: a PM tries
        the header POST and is refused, then the Head allocates and it works."""
        self._post_assign(self.opex, self.designer.pk)
        self.assertIsNone(self.opex.assigned_design_id)      # the refusal held

        _allocate_one(self.assignment, self.designer, self.head)

        self.opex.refresh_from_db()
        self.assertEqual(self.opex.assigned_design_id, self.designer.pk)
