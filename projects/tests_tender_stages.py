"""
S3 — tender_stages: every live tender site in exactly one stage, and the CEO Tenders
"Site pipeline" section built from it.

THE RULE: S7 (activated_at set) beats everything; a live membership of a locked
procurement group (S6) beats every design stage; otherwise STATUS_TO_STAGE decides, and a
site with no DesignAssignment is S0. The helper runs in a fixed number of queries.

Real fixtures throughout: every assertion reads rows written to the test database.

    python manage.py test projects.tests_tender_stages --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import (
    DESIGN_ASSIGNMENT_STATUS_CHOICES, DESIGN_AWAITING_ALLOCATION,
    DESIGN_AWAITING_PM_APPROVAL, DESIGN_IN_DESIGN, DESIGN_IN_QC, DESIGN_PM_REJECTED,
    DESIGN_RELEASED, DESIGN_SURVEY_RETURNED, SITE_GROUP_DRAFT, SITE_GROUP_LOCKED,
    SUBJECT_DESIGN_ASSIGNMENT,
    DesignAssignment, DesignAttempt, Program, Project, ProjectPhase, SiteGroup,
    SiteGroupMembership, StatusTransition, Task, TaskTemplate, TaskTemplatePhase,
    TaskTemplateTask,
)
from .tender_stages import (
    STAGE_ACTIVATED, STAGE_AWAITING_PM, STAGE_IN_DESIGN, STAGE_IN_QC,
    STAGE_LOCKED_GROUP, STAGE_NO_SURVEY, STAGE_RELEASED, STAGE_SURVEY_ON_FILE, STAGES,
    STATUS_TO_STAGE, activated_progress, site_stages, stage_summary,
)
from .views import _kwp_coverage_text, _site_pipeline, tender_sites_qs


def _profile(username, role):
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile          # auto-created by the post_save signal
    profile.role = role
    profile.save(update_fields=['role'])
    return profile


def _at(days_ago):
    return timezone.now() - timedelta(days=days_ago)


class StageFixtures(TestCase):
    """One tender and helpers to put a site at any stage."""

    @classmethod
    def setUpTestData(cls):
        cls.tender = Program.objects.create(
            name='S3 Tender', program_type='OPEX', client_name='C',
            status='Active', short_tender_code='STG')

    @classmethod
    def _site(cls, project_id, kwp='10.00', status='Draft', **extra):
        if status in ('Active', 'In Progress', 'Commissioned'):
            extra.setdefault('activated_at', timezone.now())
        return Project.objects.create(
            project_id=project_id, customer_name=project_id, customer_phone='9876543210',
            site_address='1 Sun Road', city='Lucknow', project_type='OPEX',
            program=cls.tender, dc_capacity_kw=Decimal(kwp) if kwp is not None else None,
            status=status, target_commissioning_date=date.today() + timedelta(days=90),
            **extra)

    @staticmethod
    def _design(site, status, **fields):
        return DesignAssignment.objects.create(project=site, status=status, **fields)

    @classmethod
    def _group(cls, name, status=SITE_GROUP_LOCKED):
        return SiteGroup.objects.create(
            program=cls.tender, name=name, status=status,
            locked_at=_at(3) if status == SITE_GROUP_LOCKED else None)

    @classmethod
    def _one_site_per_stage(cls):
        """S0-S7, one site each. Returns {stage_key: site}."""
        sites = {}
        sites[STAGE_NO_SURVEY] = cls._site('STG-S0')

        s1 = cls._site('STG-S1')
        cls._design(s1, DESIGN_AWAITING_ALLOCATION, survey_link_added_at=_at(20))
        sites[STAGE_SURVEY_ON_FILE] = s1

        s2 = cls._site('STG-S2')
        cls._design(s2, DESIGN_IN_DESIGN, survey_link_added_at=_at(20), assigned_at=_at(15))
        sites[STAGE_IN_DESIGN] = s2

        s3 = cls._site('STG-S3')
        da3 = cls._design(s3, DESIGN_IN_QC, assigned_at=_at(15), current_attempt_number=1)
        DesignAttempt.objects.create(assignment=da3, attempt_number=1, qc_started_at=_at(4))
        sites[STAGE_IN_QC] = s3

        s4 = cls._site('STG-S4')
        da4 = cls._design(s4, DESIGN_AWAITING_PM_APPROVAL, assigned_at=_at(15))
        StatusTransition.objects.create(
            subject_type=SUBJECT_DESIGN_ASSIGNMENT, subject_id=da4.pk, project=s4,
            from_status='awaiting_head_qc', to_status=DESIGN_AWAITING_PM_APPROVAL,
            actor_role_code='Design', occurred_at=_at(2))
        sites[STAGE_AWAITING_PM] = s4

        s5 = cls._site('STG-S5')
        cls._design(s5, DESIGN_RELEASED, released_at=_at(10))
        sites[STAGE_RELEASED] = s5

        s6 = cls._site('STG-S6')
        cls._design(s6, DESIGN_RELEASED, released_at=_at(10))
        SiteGroupMembership.objects.create(group=cls._group('Locked batch'), project=s6)
        sites[STAGE_LOCKED_GROUP] = s6

        s7 = cls._site('STG-S7', status='Active')
        cls._design(s7, DESIGN_RELEASED, released_at=_at(10))
        sites[STAGE_ACTIVATED] = s7
        return sites


class StatusMappingTests(TestCase):

    def test_every_design_status_has_a_stage(self):
        """A status added to the choices without a stage fails here, not on the page."""
        missing = [value for value, _ in DESIGN_ASSIGNMENT_STATUS_CHOICES
                   if value not in STATUS_TO_STAGE]
        self.assertEqual(missing, [])

    def test_mapping_names_only_real_statuses_and_design_stages(self):
        self.assertEqual(set(STATUS_TO_STAGE),
                         {value for value, _ in DESIGN_ASSIGNMENT_STATUS_CHOICES})
        # Groups and activation are never read off the design status.
        self.assertFalse({STAGE_LOCKED_GROUP, STAGE_ACTIVATED} & set(STATUS_TO_STAGE.values()))

    def test_decided_placements(self):
        self.assertEqual(STATUS_TO_STAGE['qc_failed'], STAGE_IN_DESIGN)
        self.assertEqual(STATUS_TO_STAGE[DESIGN_PM_REJECTED], STAGE_IN_QC)
        self.assertEqual(STATUS_TO_STAGE[DESIGN_SURVEY_RETURNED], STAGE_NO_SURVEY)
        for status in ('allocated', 'due_date_proposed', 'awaiting_head_arka',
                       'arka_rejected', 'artifacts_uploaded'):
            self.assertEqual(STATUS_TO_STAGE[status], STAGE_IN_DESIGN, status)


class SiteStageTests(StageFixtures):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.by_stage = cls._one_site_per_stage()

    def _stage(self, site):
        return site_stages(Project.objects.filter(pk=site.pk))[site.pk]

    # a) one site per stage ---------------------------------------------------------

    def test_one_site_per_stage_lands_in_that_stage(self):
        stages = site_stages(tender_sites_qs())
        for stage_key, site in self.by_stage.items():
            self.assertEqual(stages[site.pk].stage_key, stage_key, site.project_id)
        self.assertEqual([s['sites'] for s in stage_summary(tender_sites_qs())], [1] * 8)

    # b) activation beats released and locked ---------------------------------------

    def test_activated_released_site_in_a_locked_group_is_s7(self):
        site = self._site('STG-ACT-LOCKED', status='In Progress')
        self._design(site, DESIGN_RELEASED, released_at=_at(10))
        SiteGroupMembership.objects.create(group=self._group('Batch B'), project=site)
        self.assertEqual(self._stage(site).stage_key, STAGE_ACTIVATED)

    def test_activated_site_with_design_still_open_is_s7(self):
        site = self._site('STG-ACT-OPEN', status='Active')
        self._design(site, DESIGN_IN_DESIGN, assigned_at=_at(5))
        self.assertEqual(self._stage(site).stage_key, STAGE_ACTIVATED)

    # c) no DesignAssignment -------------------------------------------------------

    def test_site_without_design_assignment_is_s0_with_no_date(self):
        info = self._stage(self.by_stage[STAGE_NO_SURVEY])
        self.assertEqual(info, (STAGE_NO_SURVEY, None, None))

    def test_held_site_is_s0_and_counted_as_held(self):
        site = self._site('STG-HOLD')
        self._design(site, DESIGN_SURVEY_RETURNED, survey_link_added_at=_at(20),
                     assigned_at=_at(15), survey_returned_at=_at(1))
        self.assertEqual(self._stage(site), (STAGE_NO_SURVEY, None, DESIGN_SURVEY_RETURNED))
        s0 = stage_summary(tender_sites_qs())[0]
        self.assertEqual((s0['sites'], s0['held']), (2, 1))

    # d) removed membership --------------------------------------------------------

    def test_removed_locked_membership_does_not_count(self):
        site = self._site('STG-REMOVED')
        self._design(site, DESIGN_RELEASED, released_at=_at(10))
        SiteGroupMembership.objects.create(group=self._group('Batch C'), project=site,
                                           removed_at=_at(1))
        self.assertEqual(self._stage(site).stage_key, STAGE_RELEASED)

    def test_draft_group_membership_does_not_count(self):
        site = self._site('STG-DRAFTGRP')
        self._design(site, DESIGN_RELEASED, released_at=_at(10))
        SiteGroupMembership.objects.create(
            group=self._group('Batch D', status=SITE_GROUP_DRAFT), project=site)
        self.assertEqual(self._stage(site).stage_key, STAGE_RELEASED)

    def test_locked_group_beats_a_reopened_design(self):
        """Decision 2a: the furthest stage wins, even when the design is not released."""
        site = self._site('STG-REOPENED')
        self._design(site, DESIGN_IN_DESIGN, assigned_at=_at(15))
        SiteGroupMembership.objects.create(group=self._group('Batch E'), project=site)
        self.assertEqual(self._stage(site).stage_key, STAGE_LOCKED_GROUP)

    # e) entered_at ----------------------------------------------------------------

    def test_s1_entered_at_is_the_earlier_survey_date(self):
        link_first = self._site('STG-LINK1')
        self._design(link_first, DESIGN_AWAITING_ALLOCATION,
                     survey_link_added_at=_at(9), survey_uploaded_at=_at(3))
        upload_first = self._site('STG-UPL1')
        self._design(upload_first, DESIGN_AWAITING_ALLOCATION,
                     survey_link_added_at=_at(2), survey_uploaded_at=_at(7))
        upload_only = self._site('STG-UPLONLY')
        self._design(upload_only, DESIGN_AWAITING_ALLOCATION, survey_uploaded_at=_at(5))

        stages = site_stages(tender_sites_qs())
        for site, expected in ((link_first, 'survey_link_added_at'),
                               (upload_first, 'survey_uploaded_at'),
                               (upload_only, 'survey_uploaded_at')):
            da = DesignAssignment.objects.get(project=site)
            self.assertEqual(stages[site.pk].entered_at, getattr(da, expected), site.project_id)

    def test_entered_at_per_stage(self):
        stages = site_stages(tender_sites_qs())
        get = lambda key: stages[self.by_stage[key].pk].entered_at
        da = lambda key: DesignAssignment.objects.get(project=self.by_stage[key])
        self.assertEqual(get(STAGE_IN_DESIGN), da(STAGE_IN_DESIGN).assigned_at)
        self.assertEqual(get(STAGE_IN_QC),
                         DesignAttempt.objects.get(assignment=da(STAGE_IN_QC)).qc_started_at)
        self.assertEqual(get(STAGE_AWAITING_PM),
                         StatusTransition.objects.get(subject_id=da(STAGE_AWAITING_PM).pk).occurred_at)
        self.assertEqual(get(STAGE_RELEASED), da(STAGE_RELEASED).released_at)
        self.assertEqual(get(STAGE_LOCKED_GROUP), SiteGroup.objects.get(name='Locked batch').locked_at)
        self.assertEqual(get(STAGE_ACTIVATED), self.by_stage[STAGE_ACTIVATED].activated_at)

    def test_pm_rejected_entered_at_comes_from_the_ledger(self):
        site = self._site('STG-PMREJ')
        da = self._design(site, DESIGN_PM_REJECTED, assigned_at=_at(15),
                          current_attempt_number=1)
        DesignAttempt.objects.create(assignment=da, attempt_number=1, qc_started_at=_at(8))
        rejected = StatusTransition.objects.create(
            subject_type=SUBJECT_DESIGN_ASSIGNMENT, subject_id=da.pk, project=site,
            from_status=DESIGN_AWAITING_PM_APPROVAL, to_status=DESIGN_PM_REJECTED,
            actor_role_code='PM', occurred_at=_at(1))
        self.assertEqual(self._stage(site), (STAGE_IN_QC, rejected.occurred_at, DESIGN_PM_REJECTED))

    # f) sums to the site count ----------------------------------------------------

    def test_stage_sum_equals_tender_site_count(self):
        # Outside tender_sites_qs(): must not be counted anywhere.
        self._site('STG-TEST', is_test=True)
        self._site('STG-CANC', status='Cancelled')
        self._site('STG-DEL', is_deleted=True)
        total = sum(s['sites'] for s in stage_summary(tender_sites_qs()))
        self.assertEqual(total, tender_sites_qs().count())
        self.assertEqual(total, 8)
        self.assertEqual(_site_pipeline(tender_sites_qs())['total'], total)


class QueryBudgetTests(StageFixtures):
    """g) 30 sites cost exactly what 3 sites cost."""

    def _populate(self, n, prefix):
        statuses = [None, DESIGN_AWAITING_ALLOCATION, DESIGN_IN_DESIGN, DESIGN_IN_QC,
                    DESIGN_AWAITING_PM_APPROVAL, DESIGN_RELEASED]
        group = self._group(f'{prefix} batch')
        for i in range(n):
            site = self._site(f'{prefix}-{i:02d}', status='Active' if i % 7 == 6 else 'Draft')
            status = statuses[i % len(statuses)]
            if status is not None:
                self._design(site, status, survey_link_added_at=_at(9), assigned_at=_at(8))
            if i % 5 == 4:
                SiteGroupMembership.objects.create(group=group, project=site)

    def _count(self, fn):
        with CaptureQueriesContext(connection) as ctx:
            fn(Project.objects.filter(project_id__startswith=self.prefix))
        return len(ctx)

    def _counts(self):
        return {fn.__name__: self._count(fn)
                for fn in (site_stages, stage_summary, activated_progress, _site_pipeline)}

    def test_query_count_does_not_grow_with_sites(self):
        self.prefix = 'QA'
        self._populate(3, 'QA')
        small = self._counts()
        self.prefix = 'QB'
        self._populate(30, 'QB')
        large = self._counts()
        self.assertEqual(small, large)
        self.assertEqual(large, {'site_stages': 1, 'stage_summary': 1,
                                 'activated_progress': 1, '_site_pipeline': 2})


class KwpCoverageTests(StageFixtures):
    """h) the kWp cell for all / none / some known."""

    def test_text(self):
        self.assertEqual(_kwp_coverage_text(Decimal('4745.00'), 3, 3), '4,745 kWp')
        self.assertEqual(_kwp_coverage_text(Decimal('0'), 0, 109), '— kWp (0 of 109 sites)')
        self.assertEqual(_kwp_coverage_text(Decimal('1200.00'), 40, 80), '1,200 kWp (40 of 80 sites)')
        self.assertEqual(_kwp_coverage_text(Decimal('0'), 0, 0), '—')

    def test_from_real_sites(self):
        # S1: both known. S2: one of two (a null and a zero are both unknown). S3: none.
        for pid, kwp in (('K1A', '100.00'), ('K1B', '50.50')):
            self._design(self._site(pid, kwp=kwp), DESIGN_AWAITING_ALLOCATION)
        for pid, kwp in (('K2A', '1200.00'), ('K2B', None), ('K2C', '0')):
            self._design(self._site(pid, kwp=kwp), DESIGN_IN_DESIGN)
        self._design(self._site('K3A', kwp=None), DESIGN_IN_QC)

        rows = {r['code']: r for r in _site_pipeline(tender_sites_qs())['stages']}
        self.assertEqual(rows['S1']['kwp'], '150.5 kWp')
        self.assertEqual(rows['S2']['kwp'], '1,200 kWp (1 of 3 sites)')
        self.assertEqual(rows['S3']['kwp'], '— kWp (0 of 1 site)')
        self.assertEqual(rows['S0']['kwp'], '—')


class ActivatedProgressTests(StageFixtures):
    """The rows under S7: delivery mirrors, installation-phase tasks, Commissioned."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        template = TaskTemplate.objects.create(code='OPEX', label='OPEX', project_type='OPEX')
        delivery = TaskTemplatePhase.objects.create(
            template=template, code='PROCUREMENT_DELIVERY', label='Delivery', sort_order=1)
        install = TaskTemplatePhase.objects.create(
            template=template, code='INSTALLATION', label='Installation', sort_order=2)
        cls.delivery_tt = [
            TaskTemplateTask.objects.create(phase=delivery, code=code, label=code,
                                            sort_order=i, is_mirror=True)
            for i, code in enumerate(('DELIVERY_SOLAR_PANELS', 'DELIVERY_INVERTERS',
                                      'DELIVERY_BOS_KIT', 'DELIVERY_MMS'))]
        cls.install_tt = [
            TaskTemplateTask.objects.create(phase=install, code=code, label=code, sort_order=i)
            for i, code in enumerate(('MODULE_INSTALLATION', 'INVERTER_INSTALLATION'))]

    def _build(self, site, delivered, installed, na_install=False):
        delivery = ProjectPhase.objects.create(project=site, phase_name='Delivery', phase_order=1)
        install = ProjectPhase.objects.create(project=site, phase_name='Installation', phase_order=2)
        for i, tt in enumerate(self.delivery_tt):
            Task.objects.create(phase=delivery, task_name=tt.label, task_order=i, is_mirror=True,
                                template_task=tt,
                                status=Task.DONE if i < delivered else Task.NOT_STARTED)
        for i, tt in enumerate(self.install_tt):
            Task.objects.create(phase=install, task_name=tt.label, task_order=i, template_task=tt,
                                status=Task.DONE if i < installed else Task.NOT_STARTED,
                                is_not_applicable=na_install and i >= installed)

    def test_counts(self):
        full = self._site('ACT-FULL', status='Commissioned')
        self._build(full, delivered=4, installed=2)
        partial = self._site('ACT-PART', status='Active')
        self._build(partial, delivered=3, installed=1)
        # The undone installation task is Not Applicable, so installation is complete.
        na = self._site('ACT-NA', status='Active')
        self._build(na, delivered=4, installed=1, na_install=True)
        # Activated with no tasks at all: no evidence, so complete for nothing.
        self._site('ACT-BARE', status='Active')
        # Not activated: done tasks here must not count.
        draft = self._site('ACT-DRAFT')
        self._build(draft, delivered=4, installed=2)

        self.assertEqual(activated_progress(tender_sites_qs()), {
            'activated': 4, 'material_delivered': 2,
            'installation_complete': 2, 'commissioned': 1})


class PipelineRenderTests(StageFixtures):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ceo = _profile('s3_ceo', 'CEO')
        cls._one_site_per_stage()
        held = cls._site('STG-HELD')
        cls._design(held, DESIGN_SURVEY_RETURNED, survey_link_added_at=_at(9))

    def test_rendered_under_tenders_only(self):
        client = Client(SERVER_NAME='localhost')
        client.force_login(self.ceo.user)
        tenders = client.get(reverse('dashboard_ceo'), {'context': 'tenders'})
        self.assertContains(tenders, 'Site pipeline')
        for _, label in STAGES:
            self.assertContains(tenders, label)
        self.assertContains(tenders, 'incl. 1 returned — survey inadequate')
        self.assertContains(tenders, 'All delivery mirror tasks Done — from recorded delivery challans')
        self.assertContains(tenders, 'Each site counted once, at its furthest stage. 9 sites total.')
        # The footer's N and the header's site count are one number.
        self.assertContains(tenders, '1 tender · 9 sites')
        # A multi-line template comment must not leak onto the page.
        self.assertNotContains(tenders, 'tender_stages.py decides which')

        for context in ('residential', None):
            params = {'context': context} if context else {}
            other = client.get(reverse('dashboard_ceo'), params)
            self.assertNotContains(other, 'Site pipeline')
            self.assertIsNone(other.context['site_pipeline'])

    def test_bar_widths(self):
        rows = _site_pipeline(tender_sites_qs())['stages']
        # S0 holds 2 sites (no assignment + held), every other stage 1.
        self.assertEqual([r['width'] for r in rows], [100, 50, 50, 50, 50, 50, 50, 50])

    def test_minimum_bar_width(self):
        for i in range(120):
            self._site(f'STG-BULK-{i:03d}')
        rows = _site_pipeline(tender_sites_qs())['stages']
        self.assertEqual(rows[0]['width'], 100)
        self.assertEqual(rows[1]['width'], 2)       # 1 of 122 rounds to 1; floor is 2
