"""
Prompt 1.1a — the schema half of D-1: `group_type` on SiteGroup and SiteGroupMembership.

WHAT THIS FILE PINS, AND WHY IT IS ONLY THIS MUCH.

1.1a widens the database's exclusivity key from "one live membership per project" to
"one live membership per project PER TYPE", and adds the two save() guards that keep the
denormalised copy honest. It changes NO behaviour: every consumer still assumes one
membership, and narrowing them is 1.1b's work with 1.1b's own review question.

So these tests are about the constraint and the guards, and nothing else. There is
deliberately nothing here about `active_group_membership()`, the design change-request
gate, `post_qc_pool()` or `project_boq_is_group_locked()` — those assertions need 1.1b's
code to be true, and writing them now would only pin today's behaviour as if it were the
intent.

ON ASSERTING AGAINST IntegrityError RATHER THAN THE CONSTRAINT NAME: the suite runs on
SQLite, whose message is 'UNIQUE constraint failed: <table>.<col>' and never names the
constraint. `tests_design_part46.test_02_...` asserts on the name and is the suite's one
standing failure because of exactly that. Do not copy it here.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from .models import (Program, Project, SiteGroup, SiteGroupMembership,
                     SiteGroupTypeImmutable, SITE_GROUP_DRAFT,
                     GROUP_TYPE_PROCUREMENT, GROUP_TYPE_EXECUTION)


def _make_user(username, role):
    user = User.objects.create_user(username=username, password='pw12345')
    profile = user.profile          # auto-created by the post_save signal
    profile.role = role
    profile.save(update_fields=['role'])
    return user, profile


class GroupTypeTestCase(TestCase):
    """Shared fixture: one tender, one site, and a group of each type."""

    def setUp(self):
        self.now = timezone.now()
        _, self.scm = _make_user('gt_scm', 'SCM')
        self.program = Program.objects.create(
            program_type='OPEX', name='GroupTypeTender', client_name='GTClient',
            status='Active', short_tender_code='GTT',
        )
        self.project = Project.objects.create(
            project_id='GTT-S01', customer_name='GTClient',
            customer_phone='9876543210', site_address='1 Sun Rd', city='Delhi',
            project_type='OPEX', program=self.program, site_code='S01',
            capacity_kw=Decimal('100.00'), status='Draft',
        )
        self.procurement_group = self._group('SCM batch', GROUP_TYPE_PROCUREMENT)
        self.execution_group = self._group('PM crew batch', GROUP_TYPE_EXECUTION)

    def _group(self, name, group_type):
        return SiteGroup.objects.create(
            program=self.program, name=name, status=SITE_GROUP_DRAFT,
            created_by=self.scm, group_type=group_type)

    def _join(self, group, project=None, **kwargs):
        return SiteGroupMembership.objects.create(
            group=group, project=project or self.project, added_by=self.scm, **kwargs)


class TwoLiveMembershipsTests(GroupTypeTestCase):
    """THE POINT OF THE WHOLE MIGRATION — one site, two live memberships, one of each
    type. This is the assertion the old `(project)`-only constraint made impossible."""

    def test_a_project_may_hold_one_live_membership_of_each_type(self):
        procurement = self._join(self.procurement_group)
        execution = self._join(self.execution_group)

        live = SiteGroupMembership.objects.filter(
            project=self.project, removed_at__isnull=True)
        self.assertEqual(live.count(), 2,
                         'a site must be able to sit in a procurement batch and an '
                         'execution batch at the same time (D-1)')
        self.assertEqual(
            sorted(live.values_list('group_type', flat=True)),
            [GROUP_TYPE_EXECUTION, GROUP_TYPE_PROCUREMENT])
        self.assertNotEqual(procurement.pk, execution.pk)

    def test_a_second_live_membership_of_the_same_type_is_refused(self):
        """The exclusivity that survives the widening. Asserted on the EXCEPTION TYPE —
        SQLite does not name the constraint in its message (see module docstring)."""
        self._join(self.procurement_group)
        second_procurement_group = self._group('SCM batch 2', GROUP_TYPE_PROCUREMENT)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._join(second_procurement_group)

    def test_a_second_live_execution_membership_is_refused_too(self):
        """The new type is not a loophole: it is exclusive on its own terms."""
        self._join(self.execution_group)
        second_execution_group = self._group('PM crew batch 2', GROUP_TYPE_EXECUTION)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._join(second_execution_group)

    def test_a_tombstoned_membership_does_not_block_a_new_one_of_the_same_type(self):
        """Removal stays soft, and the partial `condition=` is what keeps that true.
        Regrouping a site must not require deleting the history of where it was."""
        removed = self._join(self.procurement_group)
        SiteGroupMembership.objects.filter(pk=removed.pk).update(
            removed_at=self.now, removed_by=self.scm, removal_reason='regrouped')

        replacement_group = self._group('SCM batch 2', GROUP_TYPE_PROCUREMENT)
        replacement = self._join(replacement_group)

        self.assertEqual(replacement.group_type, GROUP_TYPE_PROCUREMENT)
        self.assertEqual(
            SiteGroupMembership.objects.filter(project=self.project).count(), 2,
            'the tombstone must still be there — history is why removal is soft')
        self.assertEqual(
            SiteGroupMembership.objects.filter(
                project=self.project, removed_at__isnull=True).count(), 1)


class MembershipGroupTypeIsCopiedFromTheGroupTests(GroupTypeTestCase):
    """The column is a copy taken at insert, never a caller's to set."""

    def test_insert_takes_group_type_from_the_group(self):
        membership = self._join(self.execution_group)
        membership.refresh_from_db()
        self.assertEqual(membership.group_type, GROUP_TYPE_EXECUTION)

    def test_insert_OVERWRITES_a_wrong_value_passed_by_a_caller(self):
        """A caller passing the wrong type must not be able to write it. If this ever
        stops holding, a site could hold two live memberships in two PROCUREMENT groups
        by mislabelling one of them, and the constraint would never see it."""
        membership = self._join(
            self.execution_group, group_type=GROUP_TYPE_PROCUREMENT)   # deliberately wrong

        self.assertEqual(membership.group_type, GROUP_TYPE_EXECUTION,
                         'save() must overwrite a caller-supplied group_type with the '
                         'value held by the group')
        membership.refresh_from_db()
        self.assertEqual(membership.group_type, GROUP_TYPE_EXECUTION,
                         'and the overwrite must be what reached the database')

    def test_the_default_is_procurement(self):
        """Every group and membership that existed before 1.1a is an SCM procurement
        batch; the migration's backfill and this default say the same thing."""
        group = SiteGroup.objects.create(
            program=self.program, name='Defaulted', status=SITE_GROUP_DRAFT,
            created_by=self.scm)
        self.assertEqual(group.group_type, GROUP_TYPE_PROCUREMENT)
        self.assertEqual(self._join(group).group_type, GROUP_TYPE_PROCUREMENT)


class GroupTypeIsImmutableTests(GroupTypeTestCase):
    """Neither row may change type after it exists. Both halves matter: the group's copy
    is what the memberships were stamped from, and the membership's copy is what the
    constraint is written over."""

    def test_membership_group_type_cannot_be_changed(self):
        membership = self._join(self.procurement_group)

        membership.group_type = GROUP_TYPE_EXECUTION
        with self.assertRaises(SiteGroupTypeImmutable):
            membership.save()

        membership.refresh_from_db()
        self.assertEqual(membership.group_type, GROUP_TYPE_PROCUREMENT,
                         'the refused change must not have reached the database')

    def test_membership_may_still_be_saved_when_group_type_is_untouched(self):
        """The guard must not make ordinary edits impossible — soft removal is a save()."""
        membership = self._join(self.procurement_group)
        membership.removed_at = self.now
        membership.removal_reason = 'left the batch'
        membership.save()

        membership.refresh_from_db()
        self.assertIsNotNone(membership.removed_at)
        self.assertEqual(membership.group_type, GROUP_TYPE_PROCUREMENT)

    def test_site_group_group_type_cannot_be_changed(self):
        group = self.procurement_group

        group.group_type = GROUP_TYPE_EXECUTION
        with self.assertRaises(SiteGroupTypeImmutable):
            group.save()

        group.refresh_from_db()
        self.assertEqual(group.group_type, GROUP_TYPE_PROCUREMENT,
                         'the refused change must not have reached the database')

    def test_site_group_may_still_be_saved_when_group_type_is_untouched(self):
        """Locking a group is a save(); the guard must not stand in its way."""
        group = self.procurement_group
        group.name = 'SCM batch, renamed'
        group.save()

        group.refresh_from_db()
        self.assertEqual(group.name, 'SCM batch, renamed')
        self.assertEqual(group.group_type, GROUP_TYPE_PROCUREMENT)


class ExistingFixturesAreAllProcurementTests(TestCase):
    """Task 3 item 7 — every membership the OTHER test modules create must still read
    'procurement'. Those fixtures pass no group_type at all, exactly as production code
    did before 1.1a, so this is the closest the suite can get to asserting that the
    migration's backfill claim about existing production rows is the right one."""

    def test_membership_created_the_pre_1_1a_way_reads_procurement(self):
        _, scm = _make_user('legacy_scm', 'SCM')
        program = Program.objects.create(
            program_type='OPEX', name='LegacyTender', client_name='LegacyClient',
            status='Active', short_tender_code='LEGT',
        )
        project = Project.objects.create(
            project_id='LEGT-S01', customer_name='LegacyClient',
            customer_phone='9876543210', site_address='2 Sun Rd', city='Delhi',
            project_type='OPEX', program=program, site_code='S01',
            capacity_kw=Decimal('100.00'), status='Draft',
        )
        # No group_type anywhere — the exact call shape every pre-existing fixture and
        # `design_views._add_sites()` use.
        group = SiteGroup.objects.create(
            program=program, name='Legacy batch', status=SITE_GROUP_DRAFT,
            created_by=scm)
        membership = SiteGroupMembership.objects.create(
            group=group, project=project, added_by=scm)

        self.assertEqual(group.group_type, GROUP_TYPE_PROCUREMENT)
        self.assertEqual(membership.group_type, GROUP_TYPE_PROCUREMENT)

    def test_no_membership_anywhere_in_the_database_is_anything_but_procurement(self):
        """Belt and braces: nothing may create an execution membership by accident."""
        self.assertFalse(
            SiteGroupMembership.objects.exclude(
                group_type=GROUP_TYPE_PROCUREMENT).exists())
