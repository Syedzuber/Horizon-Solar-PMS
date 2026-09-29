"""Contractor bills 4a-1 — vendor kind, bill detail, rules and private PDF storage.

What this file pins, and why each matters:

  * A BILL IS REFUSED WHOLE. Every refusal create_approval_request() gives a bill leaves
    no request, detail, task link, step, snapshot or ledger row behind.
  * A VALID BILL IS ITS DETAIL. One site, its tasks, the Site Engineer asked first and
    the PM waiting, the scope set to that one site, and ledger rows carrying the site
    (D-A32) — while a material approval's rows still carry none.
  * WARNINGS ARE PURE AND NEVER BLOCK. Each bill_rules function fires, and stays quiet,
    exactly where the rulings say; the chokepoint never asks them.
  * STORAGE FAILS CLOSED. With SUPABASE_BILLS_BUCKET empty an upload is refused with a
    message before any network call, no link is minted, and every approvals page still
    renders.
  * OLD SNAPSHOTS STAND. Schema 1 and 2 rounds render as before; only a bill is schema 3.

Run with:
    python manage.py test projects.tests_contractor_bills --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from . import bill_storage
from .approvals import (
    ApprovalRefused, ContractorBill, apply_approval_decision, create_approval_request,
    resubmit_approval_request, round_snapshot, withdraw_approval_request,
)
from .bill_rules import (
    COUNTING_BILL_STATUSES, incomplete_task_warnings, other_bill_warnings, pm_warnings,
    repeated_bill_number_warnings, site_engineer_choices, site_engineer_warnings,
    task_complete_for_bill,
)
from .design_storage import DesignStorageError
from .forms import VendorForm
from .models import (
    ApprovalRequest, ApprovalRoundSnapshot, ApprovalStep, ContractorBillDetail,
    ContractorBillTask, Program, Project, ProjectPhase, SiteGroup, StatusTransition, Task,
    Vendor, VendorCategory,
    APPROVAL_APPROVED, APPROVAL_CHANGES_REQUESTED, APPROVAL_KIND_CONTRACTOR_BILL,
    APPROVAL_KIND_MATERIAL_PRE_ORDER, APPROVAL_OPEN, APPROVAL_REJECTED, APPROVAL_WITHDRAWN,
    APPROVAL_STEP_APPROVED, APPROVAL_STEP_CHANGES_REQUESTED, APPROVAL_STEP_REJECTED,
    SUBJECT_APPROVAL_REQUEST, VENDOR_KIND_CONTRACTOR, VENDOR_KIND_SUPPLIER,
)
from .tests_approvals import MODULE_LINE, SE_PHOTO

BUCKET = 'test-bills'
PDF = {'file_name': 'CB-7.pdf', 'bucket': BUCKET, 'path': 'site/bill/abc.pdf',
       'file_size_kb': 120}


def _profile(username, role, **flags):
    """A post_save signal creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title())
    profile = user.profile
    profile.role = role
    profile.is_active = True
    for field, value in flags.items():
        setattr(profile, field, value)
    profile.save()
    return profile


def _site(name, project_type='Residential', status='Active', pm=None, **extra):
    return Project.objects.create(
        customer_name=name, status=status, customer_phone='9876543210',
        site_address='1 Sun Road', city='Lucknow', state='Uttar Pradesh',
        project_type=project_type, dc_capacity_kw=Decimal('5.00'), assigned_pm=pm, **extra)


def _task(project, name, **fields):
    phase = (ProjectPhase.objects.filter(project=project).first()
             or ProjectPhase.objects.create(project=project, phase_name='Installation',
                                            phase_order=1))
    order = Task.objects.filter(phase=phase).count() + 1
    return Task.objects.create(phase=phase, task_name=name, task_order=order, **fields)


class BillFixture(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.scm  = _profile('cb_scm', 'SCM')
        cls.pm   = _profile('cb_pm', 'PM')
        cls.pm_b = _profile('cb_pm_b', 'PM')
        cls.se   = _profile('cb_se', 'Site Engineer')
        cls.se_b = _profile('cb_se_b', 'Site Engineer')
        cls.contractor = Vendor.objects.create(name='Civil Co', contact_person='R',
                                               phone='9000000011',
                                               kind=VENDOR_KIND_CONTRACTOR)
        cls.supplier = Vendor.objects.create(name='Module Co', contact_person='R',
                                             phone='9000000012')
        cls.site = _site('Bill Site', pm=cls.pm)
        cls.foundation = _task(cls.site, 'Foundation', assigned_to=cls.se)
        cls.wiring = _task(cls.site, 'Wiring', location_label='Block A')
        cls.other_site = _site('Other Site')
        cls.elsewhere = _task(cls.other_site, 'Elsewhere')

    def bill(self, **fields):
        values = dict(project=self.site, tasks=[self.foundation, self.wiring],
                      amount='12500.00', bill_number='CB-7',
                      bill_date=timezone.localdate() - timedelta(days=3), pdf=dict(PDF))
        values.update(fields)
        return ContractorBill(**values)

    def raise_bill(self, bill=None, **overrides):
        kwargs = dict(kind=APPROVAL_KIND_CONTRACTOR_BILL, raised_by=self.scm,
                      title='Civil works bill', description='Foundation, block A.',
                      pm_assignee=self.pm, site_engineer_assignee=self.se,
                      vendor=self.contractor, bill=bill or self.bill())
        kwargs.update(overrides)
        with self.settings(SUPABASE_BILLS_BUCKET=BUCKET):
            return create_approval_request(**kwargs)

    def raise_material(self, **overrides):
        kwargs = dict(kind=APPROVAL_KIND_MATERIAL_PRE_ORDER, raised_by=self.scm,
                      title='Module make', description='Propose Waaree 545 Wp.',
                      pm_assignee=self.pm, material={}, lines=[dict(MODULE_LINE)])
        kwargs.update(overrides)
        return create_approval_request(**kwargs)

    def step(self, approval, party):
        approval.refresh_from_db()
        return ApprovalStep.objects.get(request=approval, party=party,
                                        round=approval.current_round,
                                        verdict__in=['pending', 'approved',
                                                     'changes_requested', 'rejected'])

    def ledger(self, approval):
        return list(StatusTransition.objects
                    .filter(subject_type=SUBJECT_APPROVAL_REQUEST, subject_id=approval.pk)
                    .order_by('pk'))

    def client_for(self, profile):
        client = Client(SERVER_NAME='localhost')
        client.force_login(profile.user)
        return client


# ===========================================================================
# Refusals — each writes nothing
# ===========================================================================

class RefusalTests(BillFixture):

    def counts(self):
        return [model.objects.count() for model in (
            ApprovalRequest, ContractorBillDetail, ContractorBillTask, ApprovalStep,
            ApprovalRoundSnapshot, StatusTransition)]

    def assert_refused(self, fragment, bill=None, **overrides):
        before = self.counts()
        with self.assertRaises(ApprovalRefused) as caught:
            self.raise_bill(bill=bill, **overrides)
        self.assertIn(fragment, str(caught.exception))
        self.assertEqual(self.counts(), before, 'a refused bill wrote something')

    def test_a_bill_without_its_details(self):
        before = self.counts()
        with self.assertRaises(ApprovalRefused) as caught, \
                self.settings(SUPABASE_BILLS_BUCKET=BUCKET):
            create_approval_request(
                kind=APPROVAL_KIND_CONTRACTOR_BILL, raised_by=self.scm, title='t',
                description='d', pm_assignee=self.pm, site_engineer_assignee=self.se,
                vendor=self.contractor)
        self.assertIn("Enter the contractor bill's details.", str(caught.exception))
        self.assertEqual(self.counts(), before)

    def test_a_material_request_carrying_bill_details(self):
        before = self.counts()
        with self.assertRaises(ApprovalRefused) as caught:
            self.raise_material(bill=self.bill())
        self.assertIn('Only a contractor bill carries bill details.', str(caught.exception))
        self.assertEqual(self.counts(), before)

    def test_the_vendor(self):
        self.assert_refused('Module Co is recorded as a supplier, not a contractor. '
                            "Only a contractor's bill can be raised.",
                            vendor=self.supplier)
        Vendor.objects.filter(pk=self.contractor.pk).update(is_active=False)
        self.contractor.refresh_from_db()
        self.assert_refused('Civil Co is inactive.', vendor=self.contractor)

    def test_a_vendor_that_is_both_may_bill(self):
        both = Vendor.objects.create(name='Both Co', contact_person='R',
                                     phone='9000000013', kind='both')
        self.assertIsNotNone(self.raise_bill(vendor=both).bill_detail)

    def test_the_site(self):
        self.assert_refused('Choose the site this bill is for.',
                            bill=self.bill(project=None))
        gone = _site('Gone', is_deleted=True)
        self.assert_refused('That site has been deleted.', bill=self.bill(project=gone))
        draft = _site('Drafty', status='Draft')
        self.assert_refused(f'{draft.project_id} is Draft; a bill cannot be raised '
                            f'against it.', bill=self.bill(project=draft, tasks=[]))

    def test_every_status_but_draft_may_be_billed(self):
        for status in ('Active', 'In Progress', 'Commissioned', 'On Hold', 'Cancelled'):
            with self.subTest(status=status):
                site = _site(f'S {status}', status=status)
                task = _task(site, 'Work')
                bill = self.raise_bill(bill=self.bill(project=site, tasks=[task],
                                                      bill_number=f'N-{status}'))
                self.assertEqual(bill.bill_detail.project, site)

    def test_any_other_scope(self):
        program = Program.objects.create(name='Tender', program_type='OPEX',
                                         short_tender_code='TND26', client_name='C')
        group = SiteGroup.objects.create(name='G', program=program)
        message = 'A contractor bill is about its one site'
        self.assert_refused(message, programs=[program])
        self.assert_refused(message, site_groups=[group])
        self.assert_refused(message, projects=[self.other_site])
        self.assert_refused(message, projects=[self.site, self.other_site])

    def test_the_tasks(self):
        self.assert_refused('Choose at least one task this bill covers.',
                            bill=self.bill(tasks=[]))
        self.assert_refused(f"'Elsewhere' is not a task on {self.site.project_id}.",
                            bill=self.bill(tasks=[self.foundation, self.elsewhere]))
        mirror = _task(self.site, 'Delivery', is_mirror=True)
        self.assert_refused("'Delivery' is a mirror task", bill=self.bill(tasks=[mirror]))
        ghost = _task(self.site, 'Ghost')
        Task.objects.filter(pk=ghost.pk).delete()
        self.assert_refused('A task you chose no longer exists. Choose again.',
                            bill=self.bill(tasks=[ghost]))

    def test_the_amount(self):
        self.assert_refused('Enter the bill amount.', bill=self.bill(amount=''))
        self.assert_refused('The bill amount: "12,500" is not a number.',
                            bill=self.bill(amount='12,500'))
        self.assert_refused('The bill amount', bill=self.bill(amount='100.005'))
        self.assert_refused('The bill amount', bill=self.bill(amount='99999999999'))
        for amount in ('0', '-5', Decimal('0.00')):
            with self.subTest(amount=amount):
                self.assert_refused('The bill amount must be more than zero.',
                                    bill=self.bill(amount=amount))

    def test_the_bill_number(self):
        self.assert_refused("Enter the contractor's bill number.",
                            bill=self.bill(bill_number='   '))
        self.assert_refused('The bill number must be 100 characters or fewer.',
                            bill=self.bill(bill_number='N' * 101))

    def test_the_bill_date(self):
        self.assert_refused('Enter the bill date.', bill=self.bill(bill_date=None))
        self.assert_refused('Bill date: "2026-02-30" is not a valid date.',
                            bill=self.bill(bill_date='2026-02-30'))
        self.assert_refused('Bill date: Enter a date between 01 Jan 2020',
                            bill=self.bill(bill_date=date(2019, 12, 31)))
        self.assert_refused('The bill date cannot be in the future.',
                            bill=self.bill(bill_date=timezone.localdate()
                                           + timedelta(days=1)))

    def test_the_pdf(self):
        self.assert_refused("Attach the contractor's bill as a PDF.",
                            bill=self.bill(pdf=None))
        self.assert_refused("Attach the contractor's bill as a PDF.",
                            bill=self.bill(pdf=dict(PDF, path='')))
        self.assert_refused('The bill must be a PDF.',
                            bill=self.bill(pdf=dict(PDF, file_name='bill.jpg')))
        self.assert_refused('The bill PDF must be stored privately',
                            bill=self.bill(pdf=dict(PDF, bucket='solarpms-files')))

    def test_with_bill_storage_off_every_bill_is_refused(self):
        before = self.counts()
        with self.assertRaises(ApprovalRefused) as caught, \
                self.settings(SUPABASE_BILLS_BUCKET=''):
            create_approval_request(
                kind=APPROVAL_KIND_CONTRACTOR_BILL, raised_by=self.scm, title='t',
                description='d', pm_assignee=self.pm, site_engineer_assignee=self.se,
                vendor=self.contractor, bill=self.bill(pdf=dict(PDF, bucket='')))
        self.assertIn("Attach the contractor's bill as a PDF.", str(caught.exception))
        with self.assertRaises(ApprovalRefused) as caught, \
                self.settings(SUPABASE_BILLS_BUCKET=''):
            create_approval_request(
                kind=APPROVAL_KIND_CONTRACTOR_BILL, raised_by=self.scm, title='t',
                description='d', pm_assignee=self.pm, site_engineer_assignee=self.se,
                vendor=self.contractor, bill=self.bill())
        self.assertIn('The bill PDF must be stored privately', str(caught.exception))
        self.assertEqual(self.counts(), before)

    def test_the_site_engineer(self):
        self.assert_refused('Choose the Site Engineer who will confirm the work.',
                            site_engineer_assignee=None)
        self.assert_refused('cannot be named as the Site Engineer approver',
                            site_engineer_assignee=self.pm_b)
        self.se_b.is_active = False
        self.se_b.save(update_fields=['is_active'])
        self.assert_refused('cannot be named as the Site Engineer approver',
                            site_engineer_assignee=self.se_b)


# ===========================================================================
# A valid bill
# ===========================================================================

class ValidBillTests(BillFixture):

    def test_the_detail_the_links_and_the_steps(self):
        bill = self.raise_bill()
        detail = bill.bill_detail
        self.assertEqual((detail.project, detail.amount, detail.bill_number, detail.bill_date),
                         (self.site, Decimal('12500.00'), 'CB-7',
                          timezone.localdate() - timedelta(days=3)))
        self.assertEqual((detail.pdf_file_name, detail.pdf_bucket, detail.pdf_path,
                          detail.pdf_size_kb), ('CB-7.pdf', BUCKET, 'site/bill/abc.pdf', 120))
        self.assertEqual([link.task for link in detail.task_links.order_by('pk')],
                         [self.foundation, self.wiring])
        se, pm = self.step(bill, 'site_engineer'), self.step(bill, 'pm')
        self.assertEqual((se.assignee, se.sequence, se.verdict), (self.se, 1, 'pending'))
        self.assertIsNotNone(se.activated_at)
        self.assertEqual((pm.assignee, pm.sequence, pm.verdict), (self.pm, 2, 'pending'))
        self.assertIsNone(pm.activated_at)
        self.assertEqual(list(bill.projects.all()), [self.site])
        self.assertFalse(bill.programs.exists())
        self.assertFalse(bill.site_groups.exists())

    def test_the_scope_may_be_passed_as_the_site_itself_and_a_repeated_task_is_one_link(self):
        bill = self.raise_bill(bill=self.bill(tasks=[self.foundation, self.foundation]),
                               projects=[self.site])
        self.assertEqual(bill.bill_detail.task_links.count(), 1)
        self.assertEqual(list(bill.projects.all()), [self.site])

    def test_the_bill_ledger_rows_carry_the_site(self):
        bill = self.raise_bill()
        apply_approval_decision(self.step(bill, 'site_engineer'), APPROVAL_STEP_APPROVED,
                                self.se, files=[dict(SE_PHOTO)])
        apply_approval_decision(self.step(bill, 'pm'), APPROVAL_STEP_APPROVED, self.pm)
        rows = self.ledger(bill)
        self.assertEqual([row.to_status for row in rows], [APPROVAL_OPEN, APPROVAL_APPROVED])
        self.assertEqual({row.project_id for row in rows}, {self.site.pk})

    def test_a_bill_sent_back_and_withdrawn_keeps_the_site_on_every_row(self):
        bill = self.raise_bill()
        apply_approval_decision(self.step(bill, 'site_engineer'),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.se, note='Redo.')
        withdraw_approval_request(bill, self.scm, note='Contractor re-issuing.')
        self.assertEqual({row.project_id for row in self.ledger(bill)}, {self.site.pk})

    def test_a_material_approvals_rows_still_carry_no_project(self):
        approval = self.raise_material(projects=[self.site])
        apply_approval_decision(self.step(approval, 'pm'),
                                APPROVAL_STEP_CHANGES_REQUESTED, self.pm, note='Other make.')
        resubmit_approval_request(approval, self.scm, note='Changed make.')
        apply_approval_decision(self.step(approval, 'pm'), APPROVAL_STEP_REJECTED,
                                self.pm, note='No.')
        other = self.raise_material(title='Second')
        withdraw_approval_request(other, self.scm, note='Not needed.')
        rows = self.ledger(approval) + self.ledger(other)
        self.assertEqual(len(rows), 6)     # open, changes, open, rejected; open, withdrawn
        self.assertEqual({row.project_id for row in rows}, {None})

    def test_a_material_request_needs_no_extra_query_for_its_ledger_row(self):
        from .utils import _approval_request_project
        approval = self.raise_material()
        with self.assertNumQueries(0):
            self.assertIsNone(_approval_request_project(approval))

    def test_the_bill_snapshot_is_schema_3(self):
        bill = self.raise_bill()
        snap = round_snapshot(bill, 1)
        self.assertEqual(snap['schema'], 3)
        self.assertIsNone(snap['material'])
        self.assertEqual(snap['bill'], {
            'project': {'id': self.site.pk, 'project_id': self.site.project_id,
                        'customer_name': 'Bill Site'},
            'amount': '12500.00', 'bill_number': 'CB-7',
            'bill_date': (timezone.localdate() - timedelta(days=3)).isoformat(),
            'pdf': {'file_name': 'CB-7.pdf', 'bucket': BUCKET, 'path': 'site/bill/abc.pdf',
                    'size_kb': 120},
            'tasks': [{'id': self.foundation.pk, 'task_name': 'Foundation',
                       'location_label': '', 'phase_name': 'Installation'},
                      {'id': self.wiring.pk, 'task_name': 'Wiring',
                       'location_label': 'Block A', 'phase_name': 'Installation'}],
        })
        self.assertEqual([s['party'] for s in snap['steps']], ['site_engineer', 'pm'])

    def test_a_material_snapshot_stays_schema_2_with_no_bill_key(self):
        snap = round_snapshot(self.raise_material(), 1)
        self.assertEqual(snap['schema'], 2)
        self.assertNotIn('bill', snap)


# ===========================================================================
# Database rules
# ===========================================================================

class ConstraintTests(BillFixture):

    def assertViolates(self, name, write):
        with self.assertRaises(IntegrityError) as caught, transaction.atomic():
            write()
        self.assertIn(name, str(caught.exception))

    def test_the_detail_checks(self):
        bill = self.raise_bill()
        rows = ContractorBillDetail.objects.filter(pk=bill.bill_detail.pk)
        self.assertViolates('contractor_bill_amount_positive', lambda: rows.update(amount=0))
        self.assertViolates('contractor_bill_number_required',
                            lambda: rows.update(bill_number=''))
        self.assertViolates('contractor_bill_pdf_required', lambda: rows.update(pdf_path=''))

    def test_one_link_per_task_per_bill(self):
        detail = self.raise_bill().bill_detail
        with self.assertRaises(IntegrityError), transaction.atomic():
            ContractorBillTask.objects.create(detail=detail, task=self.foundation)

    def test_the_vendor_kind_is_one_of_three(self):
        self.assertViolates('vendor_kind_known',
                            lambda: Vendor.objects.filter(pk=self.supplier.pk)
                            .update(kind='subcontractor'))


# ===========================================================================
# D-A33 — the completion rule
# ===========================================================================

class CompletionTests(BillFixture):

    def test_residential_and_capex_are_complete_when_done(self):
        for project_type in ('Residential', 'CAPEX'):
            with self.subTest(project_type=project_type):
                self.assertTrue(task_complete_for_bill(Task(status='Done'), project_type))
                self.assertFalse(task_complete_for_bill(Task(status='In Progress'),
                                                        project_type))

    def test_opex_needs_done_and_approved(self):
        now = timezone.now()
        self.assertTrue(task_complete_for_bill(Task(status='Done', approved_at=now), 'OPEX'))
        self.assertFalse(task_complete_for_bill(Task(status='Done'), 'OPEX'))
        self.assertFalse(task_complete_for_bill(
            Task(status='In Progress', approved_at=now), 'OPEX'))

    def test_a_reopened_opex_task_recorded_as_is(self):
        """RECORDED, NOT FIXED (Q11): a reopen (Done -> Blocked) leaves approved_at set.
        While reopened the task is not complete; taken back to Done it reads complete on
        the old approval, because nothing cleared the stale stamp."""
        task = Task(status='Done', approved_at=timezone.now())
        task.status = 'Blocked'                    # reopened; approved_at kept
        self.assertFalse(task_complete_for_bill(task, 'OPEX'))
        task.status = 'Done'                       # back to Done with no fresh approval
        self.assertTrue(task_complete_for_bill(task, 'OPEX'))


# ===========================================================================
# The warnings — pure, never blocks
# ===========================================================================

class WarningTests(BillFixture):

    def test_incomplete_tasks(self):
        residential = [Task(task_name='Pour', status='Done'),
                       Task(task_name='Wire', status='In Progress', location_label='Block A'),
                       Task(task_name='Paint', status='Not Started', is_not_applicable=True)]
        self.assertEqual(incomplete_task_warnings(self.site, residential), [
            "'Wire — Block A' is not complete — it is In Progress.",
            "'Paint' is marked Not Applicable.",
        ])
        opex = _site('Resco', project_type='OPEX')
        tasks = [Task(task_name='Mount', status='Done'),
                 Task(task_name='Earth', status='Done', approved_at=timezone.now())]
        self.assertEqual(incomplete_task_warnings(opex, tasks),
                         ["'Mount' is Done but not yet approved."])
        self.assertEqual(incomplete_task_warnings(self.site, residential[:1]), [])

    def test_a_task_on_another_bill_counts_only_while_that_bill_is_in_play(self):
        self.assertEqual(COUNTING_BILL_STATUSES,
                         {APPROVAL_OPEN, APPROVAL_CHANGES_REQUESTED, APPROVAL_APPROVED})
        first = self.raise_bill()
        message = ("'Foundation' is also on bill CB-7 — \"Civil works bill\" (open).")
        self.assertIn(message, other_bill_warnings([self.foundation]))
        self.assertEqual(other_bill_warnings([self.foundation], exclude=first), [])
        for status in (APPROVAL_CHANGES_REQUESTED, APPROVAL_APPROVED):
            ApprovalRequest.objects.filter(pk=first.pk).update(
                status=status, closed_at=timezone.now() if status == APPROVAL_APPROVED
                else None)
            self.assertEqual(len(other_bill_warnings([self.foundation])), 1, status)
        for status in (APPROVAL_REJECTED, APPROVAL_WITHDRAWN):
            ApprovalRequest.objects.filter(pk=first.pk).update(
                status=status, closed_at=timezone.now(), withdrawal_note='x')
            self.assertEqual(other_bill_warnings([self.foundation]), [], status)
        self.assertEqual(other_bill_warnings([self.elsewhere]), [])

    def test_a_repeated_bill_number_from_the_same_contractor(self):
        first = self.raise_bill()
        self.assertEqual(repeated_bill_number_warnings(self.contractor, ' cb-7 '),
                         ['Civil Co already has bill number CB-7 on "Civil works bill" '
                          '(open).'])
        self.assertEqual(repeated_bill_number_warnings(self.contractor, 'CB-8'), [])
        self.assertEqual(repeated_bill_number_warnings(self.supplier, 'CB-7'), [])
        self.assertEqual(repeated_bill_number_warnings(self.contractor, '  '), [])
        self.assertEqual(repeated_bill_number_warnings(self.contractor, 'CB-7',
                                                       exclude=first), [])
        withdraw_approval_request(first, self.scm, note='Re-issued.')
        self.assertEqual(repeated_bill_number_warnings(self.contractor, 'CB-7'), [])

    def test_a_site_engineer_holding_no_task_on_the_site(self):
        self.assertEqual(site_engineer_warnings(self.site, self.se), [])
        self.assertEqual(site_engineer_warnings(self.site, self.se_b),
                         [f'Cb_Se_B holds no task on {self.site.project_id}.'])

    def test_a_pm_who_is_not_the_sites_assigned_pm(self):
        self.assertEqual(pm_warnings(self.site, self.pm), [])
        self.assertEqual(pm_warnings(self.site, self.pm_b),
                         [f'Cb_Pm_B is not the assigned PM on {self.site.project_id}.'])
        self.assertEqual(pm_warnings(self.other_site, self.pm),
                         [f'Cb_Pm is not the assigned PM on {self.other_site.project_id}.'])

    def test_site_engineers_on_the_site_come_first(self):
        gone = _profile('cb_se_gone', 'Site Engineer', is_active=False)
        with self.assertNumQueries(2):
            choices = site_engineer_choices(self.site)
        self.assertEqual(choices, [(self.se, True), (self.se_b, False)])
        self.assertNotIn(gone, [p for p, _ in choices])

    def test_the_chokepoint_raises_a_bill_every_warning_would_mention(self):
        """Warnings never block: a Site Engineer and a PM off the site, an unfinished
        task, and a repeated number still make a bill."""
        self.raise_bill()
        again = self.raise_bill(site_engineer_assignee=self.se_b, pm_assignee=self.pm_b)
        self.assertEqual(again.bill_detail.bill_number, 'CB-7')


# ===========================================================================
# Private storage
# ===========================================================================

def _pdf(name='bill.pdf', body=b'%PDF-1.7\n...', content_type='application/pdf'):
    return SimpleUploadedFile(name, body, content_type=content_type)


class StorageTests(BillFixture):

    def test_with_the_setting_empty_an_upload_is_refused_before_any_call(self):
        with self.settings(SUPABASE_BILLS_BUCKET=''), \
                mock.patch.object(bill_storage, '_client') as client:
            with self.assertRaises(bill_storage.BillStorageError) as caught:
                bill_storage.upload_bill_pdf(_pdf(), self.site)
        self.assertEqual(str(caught.exception), bill_storage.BILL_STORAGE_OFF)
        self.assertEqual(str(caught.exception),
                         'Bill PDFs cannot be stored right now: the private bills bucket '
                         'is not configured. Nothing was saved.')
        client.assert_not_called()

    def test_with_the_setting_empty_no_link_is_minted(self):
        with self.settings(SUPABASE_BILLS_BUCKET=''), \
                mock.patch.object(bill_storage, 'get_design_file_url') as sign:
            self.assertIsNone(bill_storage.bill_pdf_url(BUCKET, 'a/b.pdf'))
        sign.assert_not_called()

    def test_a_link_is_signed_only_in_the_bills_bucket_for_900_seconds(self):
        with self.settings(SUPABASE_BILLS_BUCKET=BUCKET), \
                mock.patch.object(bill_storage, 'get_design_file_url',
                                  return_value='https://signed') as sign:
            self.assertEqual(bill_storage.bill_pdf_url(BUCKET, 'a/b.pdf'), 'https://signed')
            sign.assert_called_once_with(BUCKET, 'a/b.pdf', 900)
            with self.assertLogs('projects.bill_storage', 'WARNING'):
                self.assertIsNone(bill_storage.bill_pdf_url('solarpms-files', 'a/b.pdf'))
            self.assertIsNone(bill_storage.bill_pdf_url(BUCKET, ''))
            self.assertEqual(sign.call_count, 1)

    def test_a_signing_failure_reads_as_unavailable(self):
        with self.settings(SUPABASE_BILLS_BUCKET=BUCKET), \
                mock.patch.object(bill_storage, 'get_design_file_url',
                                  side_effect=DesignStorageError('down')), \
                self.assertLogs('projects.bill_storage', 'ERROR'):
            self.assertIsNone(bill_storage.bill_pdf_url(BUCKET, 'a/b.pdf'))

    def test_only_a_real_pdf_is_accepted(self):
        cases = [
            (_pdf(name='bill.jpg'), 'The bill must be a PDF.'),
            (_pdf(body=b''), 'The bill PDF is empty.'),
            (_pdf(content_type='image/png'), 'The bill must be a PDF.'),
            (_pdf(body=b'<html>not a pdf'), 'That file is not a PDF'),
        ]
        for upload, message in cases:
            with self.subTest(message=message):
                with self.assertRaises(bill_storage.BillStorageError) as caught:
                    bill_storage.validate_bill_pdf(upload)
                self.assertIn(message, str(caught.exception))
        bill_storage.validate_bill_pdf(_pdf(content_type='application/octet-stream'))
        big = _pdf()
        big.size = bill_storage.BILL_PDF_MAX_BYTES + 1
        with self.assertRaises(bill_storage.BillStorageError) as caught:
            bill_storage.validate_bill_pdf(big)
        self.assertIn('the limit is 20 MB', str(caught.exception))

    def test_an_upload_stores_under_the_site_and_returns_the_chokepoints_pdf(self):
        with self.settings(SUPABASE_BILLS_BUCKET=BUCKET), \
                mock.patch.object(bill_storage, '_client') as client:
            stored = bill_storage.upload_bill_pdf(_pdf(name='CB 7.pdf'), self.site)
        self.assertEqual((stored['file_name'], stored['bucket'], stored['file_size_kb']),
                         ('CB 7.pdf', BUCKET, 1))
        self.assertRegex(stored['path'], rf'^{self.site.project_id}/bill/[0-9a-f]{{32}}\.pdf$')
        client.return_value.storage.from_.assert_called_once_with(BUCKET)
        bill = self.raise_bill(bill=self.bill(pdf=stored))
        self.assertEqual(bill.bill_detail.pdf_path, stored['path'])

    def test_a_recorded_pdf_is_never_discarded(self):
        bill = self.raise_bill()
        with self.settings(SUPABASE_BILLS_BUCKET=BUCKET), \
                mock.patch.object(bill_storage, '_client') as client:
            self.assertFalse(bill_storage.discard_unrecorded_bill_pdf(
                {'bucket': BUCKET, 'path': bill.bill_detail.pdf_path}))
            self.assertFalse(bill_storage.discard_unrecorded_bill_pdf(
                {'bucket': 'solarpms-files', 'path': 'x.pdf'}))
            client.assert_not_called()
            self.assertTrue(bill_storage.discard_unrecorded_bill_pdf(
                {'bucket': BUCKET, 'path': 'site/bill/orphan.pdf'}))
        client.return_value.storage.from_.return_value.remove.assert_called_once_with(
            ['site/bill/orphan.pdf'])


class PagesWithStorageOffTests(BillFixture):
    """With the bills bucket unset, every approvals page, and the vendor master, renders.
    Since 4a-2 the raise page draws ?kind=contractor_bill and says bills cannot be stored
    yet — pinned by tests_approval_views.PreDispatchRaiseTests
    .test_contractor_bill_draws_the_bill_form_and_unknown_kinds_go_back."""

    def test_every_approvals_page_renders(self):
        with self.settings(SUPABASE_BILLS_BUCKET=BUCKET):
            bill = self.raise_bill()
        material = self.raise_material()
        client = self.client_for(self.scm)
        with self.settings(SUPABASE_BILLS_BUCKET=''):
            for url in (reverse('approval_list'), reverse('approval_aging'),
                        reverse('approval_detail', args=[bill.pk]),
                        reverse('approval_detail', args=[material.pk]),
                        reverse('approval_create') + '?kind=material_pre_order'):
                with self.subTest(url=url):
                    response = client.get(url, follow=True)
                    self.assertEqual(response.status_code, 200)

    def test_the_vendor_master_renders_the_kind(self):
        admin = _profile('cb_admin', 'Admin')
        client = self.client_for(admin)
        with self.settings(SUPABASE_BILLS_BUCKET=''):
            listing = client.get(reverse('vendor_list'))
            self.assertContains(listing, '<th>Kind</th>', html=True)
            self.assertContains(listing, 'Contractor')
            form = client.get(reverse('vendor_edit', args=[self.contractor.pk]))
            self.assertContains(form, 'name="kind"')


class SnapshotSchemaTests(BillFixture):
    """Rounds written before this session still draw as they did."""

    def test_schema_2_and_schema_1_rounds_render(self):
        approval = self.raise_material()
        client = self.client_for(self.scm)
        url = reverse('approval_detail', args=[approval.pk])
        self.assertContains(client.get(url), 'Solar module 545 Wp')
        # Rewrite round 1 as a schema-1 snapshot — queryset update, around the
        # append-only save() on purpose, as tests_approval_lines does.
        snapshot = round_snapshot(approval, 1)
        material = snapshot['material']
        material.pop('lines')
        material.update({'proposed_make': 'Waaree', 'specification': '545 Wp mono',
                         'quantity_note': '120 modules', 'boq_items': []})
        snapshot['schema'] = 1
        ApprovalRoundSnapshot.objects.filter(request=approval, round=1).update(
            snapshot=snapshot)
        self.assertContains(client.get(url), '120 modules')


# ===========================================================================
# The vendor master form (D-A14, Q2)
# ===========================================================================

class VendorFormTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.category = VendorCategory.objects.create(name='Civil')

    def data(self, **fields):
        values = {'name': 'Form Co', 'contact_person': 'R', 'phone': '9000000020',
                  'email': '', 'address': '', 'gst_number': '', 'msme_number': '',
                  'categories': [self.category.pk]}
        values.update(fields)
        return values

    def test_a_blank_kind_is_a_supplier_on_add(self):
        form = VendorForm(self.data())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().kind, VENDOR_KIND_SUPPLIER)

    def test_a_blank_kind_leaves_it_unchanged_on_edit(self):
        vendor = VendorForm(self.data(kind='contractor')).save()
        self.assertEqual(vendor.kind, VENDOR_KIND_CONTRACTOR)
        form = VendorForm(self.data(), instance=vendor)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().kind, VENDOR_KIND_CONTRACTOR)
        form = VendorForm(self.data(kind='both'), instance=vendor)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().kind, 'both')

    def test_an_unknown_kind_is_refused(self):
        self.assertFalse(VendorForm(self.data(kind='subcontractor')).is_valid())
