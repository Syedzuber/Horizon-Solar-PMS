"""
Tests for the Part 6 site-group helpers in projects/design_views.py.

WHY THIS FILE EXISTS — post_qc_pool() shipped broken and verification missed it.

`post_qc_pool()` originally excluded grouped sites with

    .exclude(project__group_memberships__removed_at__isnull=True)

which Django compiles to a NOT EXISTS over a LEFT OUTER JOIN. A project with no
membership rows still produces one phantom row whose `removed_at` is NULL, the condition
matches it, and the project is excluded. The pool therefore omitted exactly the sites it
exists to show — every released site that had never been grouped — and returned only the
sites that had once been grouped and removed.

Part 6's verification passed because the single site in the pool at that moment had a
removed membership, which is the one shape the broken query still returned. The bug was
found later, by seeding a tender whose released sites had never been grouped.

The tests below pin the pool's contract as four independent statements, so no single
fixture shape can hide a regression the way one site did before.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .models import (Program, Project, UserProfile, DesignAssignment, SiteGroup,
                     SiteGroupMembership, DESIGN_RELEASED, DESIGN_IN_DESIGN,
                     SITE_GROUP_DRAFT, SITE_GROUP_LOCKED,
                     GROUP_TYPE_PROCUREMENT, GROUP_TYPE_EXECUTION)
from .design_views import post_qc_pool, tender_release_completeness, _group_member_ids


def _make_user(username, role):
    user = User.objects.create_user(username=username, password='pw12345')
    profile = user.profile          # auto-created by the post_save signal
    profile.role = role
    profile.save(update_fields=['role'])
    return user, profile


class PostQCPoolTests(TestCase):
    """The pool is 'released, and in no group, oldest first'. Each clause is pinned."""

    def setUp(self):
        self.now = timezone.now()
        _, self.scm = _make_user('pool_scm', 'SCM')
        _, self.designer = _make_user('pool_designer', 'Design')
        self.program = Program.objects.create(
            program_type='OPEX', name='PoolTender', client_name='PoolClient',
            status='Active', short_tender_code='POOLT',
        )

    def _site(self, code, released_days_ago=None, status=DESIGN_RELEASED):
        site = Project(
            project_id=f'POOLT-{code}', customer_name='PoolClient',
            customer_phone='9876543210', site_address='1 Sun Rd', city='Delhi',
            project_type='OPEX', program=self.program, site_code=code,
            capacity_kw=Decimal('100.00'), status='Draft',
        )
        site.save()
        DesignAssignment.objects.create(
            project=site, assigned_to=self.designer, status=status,
            released_at=(self.now - timedelta(days=released_days_ago)
                         if released_days_ago is not None else None),
        )
        return site

    def _group(self, name, status=SITE_GROUP_DRAFT):
        return SiteGroup.objects.create(
            program=self.program, name=name, status=status, created_by=self.scm)

    # ── the clause that was broken ──────────────────────────────────────────────
    def test_released_site_that_was_never_grouped_is_in_the_pool(self):
        """THE REGRESSION. A site with zero membership rows must appear.

        This is the ordinary case — most released sites have never been in a group —
        and the original query returned none of them.
        """
        site = self._site('S01', released_days_ago=5)
        pool = post_qc_pool(self.program, self.now)
        self.assertEqual([a.project_id for a in pool], [site.pk],
                         'a released, never-grouped site must be in the post-QC pool')

    def test_pool_is_not_empty_when_no_membership_rows_exist_at_all(self):
        """Stronger form of the above: an empty SiteGroupMembership table must not
        empty the pool. The broken query returned [] for every site in this state."""
        self._site('S01', released_days_ago=3)
        self._site('S02', released_days_ago=9)
        self.assertEqual(SiteGroupMembership.objects.count(), 0)
        self.assertEqual(len(post_qc_pool(self.program, self.now)), 2)

    # ── the other three clauses ────────────────────────────────────────────────
    def test_site_with_a_live_procurement_membership_is_excluded(self):
        site = self._site('S01', released_days_ago=5)
        SiteGroupMembership.objects.create(
            group=self._group('G1'), project=site, added_by=self.scm)
        self.assertEqual(post_qc_pool(self.program, self.now), [])

    # ── D-1, prompt 1.1b: the pool asks "has SCM taken it", not "is it in a group" ──
    #
    # THESE TWO TESTS MANUFACTURE AN EXECUTION MEMBERSHIP DIRECTLY, and they have to.
    # Nothing in the product creates one — there is no execution-group screen — so the
    # suite would pass with `post_qc_pool()` narrowed or not. Green is not evidence
    # here; only a hand-built execution row is.
    def test_site_in_a_live_execution_group_still_appears_in_the_pool(self):
        """THE REGRESSION 1.1b EXISTS TO PREVENT.

        A released site that the PM has put in an execution batch has NOT been procured.
        Dropping it here would delete it from the only screen that tells SCM it is
        waiting — silently, and months from now at go-live rather than today.
        """
        site = self._site('S01', released_days_ago=5)
        execution_group = SiteGroup.objects.create(
            program=self.program, name='PM crew batch', status=SITE_GROUP_DRAFT,
            created_by=self.scm, group_type=GROUP_TYPE_EXECUTION)
        membership = SiteGroupMembership.objects.create(
            group=execution_group, project=site, added_by=self.scm)
        # The row really is what the test claims — save() copies the type from the group.
        self.assertEqual(membership.group_type, GROUP_TYPE_EXECUTION)
        self.assertIsNone(membership.removed_at)

        pool = post_qc_pool(self.program, self.now)
        self.assertEqual([a.project_id for a in pool], [site.pk],
                         'an execution membership is not a procurement membership — the '
                         'site is still waiting for SCM and must stay in the pool')

    def test_a_procurement_membership_still_excludes_a_site_that_also_has_an_execution_one(self):
        """The narrowing must not become a hole. Both memberships live at once (D-1);
        the procurement one is the one this screen answers to."""
        site = self._site('S01', released_days_ago=5)
        SiteGroupMembership.objects.create(
            group=self._group('G1'), project=site, added_by=self.scm)
        execution_group = SiteGroup.objects.create(
            program=self.program, name='PM crew batch', status=SITE_GROUP_DRAFT,
            created_by=self.scm, group_type=GROUP_TYPE_EXECUTION)
        SiteGroupMembership.objects.create(
            group=execution_group, project=site, added_by=self.scm)

        self.assertEqual(
            SiteGroupMembership.objects.filter(
                project=site, removed_at__isnull=True).count(), 2)
        self.assertEqual(post_qc_pool(self.program, self.now), [],
                         'SCM has taken this site — the execution membership beside it '
                         'changes nothing')

    def test_site_whose_membership_was_removed_is_back_in_the_pool(self):
        """Settled decision 6: a change request returns the site to the queue rather
        than losing it. This was the ONLY shape the broken query handled correctly."""
        site = self._site('S01', released_days_ago=5)
        SiteGroupMembership.objects.create(
            group=self._group('G1'), project=site, added_by=self.scm,
            removed_by=self.scm, removed_at=self.now - timedelta(days=1),
            removal_reason='PM change request')
        pool = post_qc_pool(self.program, self.now)
        self.assertEqual([a.project_id for a in pool], [site.pk])

    def test_a_locked_group_also_removes_a_site_from_the_pool(self):
        site = self._site('S01', released_days_ago=5)
        SiteGroupMembership.objects.create(
            group=self._group('G1', status=SITE_GROUP_LOCKED), project=site,
            added_by=self.scm)
        self.assertEqual(post_qc_pool(self.program, self.now), [])

    def test_unreleased_sites_are_never_in_the_pool(self):
        self._site('S01', released_days_ago=None, status=DESIGN_IN_DESIGN)
        released = self._site('S02', released_days_ago=4)
        pool = post_qc_pool(self.program, self.now)
        self.assertEqual([a.project_id for a in pool], [released.pk])

    def test_soft_deleted_sites_are_never_in_the_pool(self):
        site = self._site('S01', released_days_ago=4)
        Project.objects.filter(pk=site.pk).update(is_deleted=True)
        self.assertEqual(post_qc_pool(self.program, self.now), [])

    def test_another_tenders_sites_are_never_in_this_pool(self):
        self._site('S01', released_days_ago=4)
        other = Program.objects.create(
            program_type='OPEX', name='OtherTender', client_name='X',
            status='Active', short_tender_code='OTHER')
        self.assertEqual(len(post_qc_pool(self.program, self.now)), 1)
        self.assertEqual(post_qc_pool(other, self.now), [])

    # ── ordering and ageing ────────────────────────────────────────────────────
    def test_pool_is_ordered_oldest_release_first(self):
        """Sorted any other way, the site that has waited longest is buried — which is
        the whole failure mode the pool exists to surface."""
        newest = self._site('S01', released_days_ago=2)
        oldest = self._site('S02', released_days_ago=30)
        middle = self._site('S03', released_days_ago=15)
        self.assertEqual([a.project_id for a in post_qc_pool(self.program, self.now)],
                         [oldest.pk, middle.pk, newest.pk])

    def test_age_days_is_whole_days_since_release(self):
        self._site('S01', released_days_ago=17)
        pool = post_qc_pool(self.program, self.now)
        self.assertEqual(pool[0].age_days, 17)

    def test_age_days_is_none_when_released_at_is_missing(self):
        """`released_at` is nullable, so a hand-edited row must not crash the screen."""
        site = self._site('S01', released_days_ago=None)
        DesignAssignment.objects.filter(project=site).update(status=DESIGN_RELEASED)
        pool = post_qc_pool(self.program, self.now)
        self.assertEqual(len(pool), 1)
        self.assertIsNone(pool[0].age_days)


class CompletenessAndMembershipTests(TestCase):
    """The two figures the aggregate is qualified by."""

    def setUp(self):
        self.now = timezone.now()
        _, self.scm = _make_user('comp_scm', 'SCM')
        _, self.designer = _make_user('comp_designer', 'Design')
        self.program = Program.objects.create(
            program_type='OPEX', name='CompTender', client_name='C',
            status='Active', short_tender_code='COMPT')

    def _site(self, code, status):
        site = Project(project_id=f'COMPT-{code}', customer_name='C',
                       customer_phone='9876543210', site_address='1 Sun Rd', city='Delhi',
                       project_type='OPEX', program=self.program, site_code=code,
                       capacity_kw=Decimal('100.00'), status='Draft')
        site.save()
        DesignAssignment.objects.create(project=site, assigned_to=self.designer,
                                        status=status, released_at=self.now)
        return site

    def test_completeness_counts_released_over_total_sites(self):
        self._site('S01', DESIGN_RELEASED)
        self._site('S02', DESIGN_RELEASED)
        self._site('S03', DESIGN_IN_DESIGN)
        self.assertEqual(tender_release_completeness(self.program), (2, 3))

    def test_completeness_ignores_project_status(self):
        """Settled decision 9: OPEX sites are born Draft and nothing promotes them, so
        keying off Project.status would report 0 for every tender."""
        site = self._site('S01', DESIGN_RELEASED)
        self.assertEqual(site.status, 'Draft')
        self.assertEqual(tender_release_completeness(self.program), (1, 1))

    def test_group_member_ids_counts_active_memberships_only(self):
        a = self._site('S01', DESIGN_RELEASED)
        b = self._site('S02', DESIGN_RELEASED)
        group = SiteGroup.objects.create(program=self.program, name='G',
                                         status=SITE_GROUP_DRAFT, created_by=self.scm)
        SiteGroupMembership.objects.create(group=group, project=a, added_by=self.scm)
        SiteGroupMembership.objects.create(group=group, project=b, added_by=self.scm,
                                           removed_by=self.scm, removed_at=self.now,
                                           removal_reason='moved to next batch')
        self.assertEqual(_group_member_ids(group), [a.pk])
