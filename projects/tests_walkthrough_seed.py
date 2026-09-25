"""
seed_walkthrough / teardown_walkthrough — the synthetic walkthrough database.

What this pins, and why each earns a test:

  * the FOUR refusal checks, each on its own, and that a refusal writes nothing;
  * the one permitted non-.invalid address is admitted — and only that one;
  * a second run changes no row count (idempotent by area);
  * every seeded user is .invalid (bar the one), with both preferences off, and the
    master switches are off; nothing was sent on an external channel;
  * the states are what the doc claims — a released site carries its Arka, CAD, BOQ,
    attempts and ledger; every change-request verdict, payment status and challan status
    exists; no history runs backwards;
  * the teardown refuses (and deletes nothing) wherever exact removal would need a
    history row force-deleted or would touch a row the seed did not record — and where
    it may remove, it removes exactly the manifest and nothing else.

The suite runs without migrations (solarpms/test_settings.py), so the reference data an
empty MIGRATED database would hold — vendor categories, durations, both BOQ catalogues,
both task templates — is built here by calling those migrations' own RunPython
functions, so the seed meets the same rows it meets on a real walkthrough database.
"""
import io
import shutil
import tempfile
from collections import Counter
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from projects import models as m
from projects.management.commands import _walkthrough_support as support
from projects.utils import RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL

WALK_NAME = 'solarpms_walk_test'
NAME_PATCH = 'projects.management.commands._walkthrough_support.database_name'
HOST_PATCH = 'projects.management.commands._walkthrough_support.database_host'


def _build_migrated_reference_data():
    """What `migrate` leaves in an empty database, by the migrations' own functions."""
    for module, function in (
        ('0009_vendorcategory_data', 'seed_vendor_categories'),
        ('0034_task_duration_template', 'seed_duration_template'),
        ('0047_boqitemmaster_boqitem_item_master', 'seed_catalogue'),
        ('0057_boqitemmaster_project_type_opex_catalogue', 'scope_and_import'),
        ('0067_seed_residential_template_v1', 'seed_v1'),
        ('0075_seed_opex_template_v1', 'seed_opex_v1'),
    ):
        with patch('sys.stdout', new_callable=io.StringIO):
            getattr(import_module(f'projects.migrations.{module}'), function)(
                django_apps, None)


def _run(command, *args, manifest=None, name=WALK_NAME, **kwargs):
    out = io.StringIO()
    if manifest is not None:
        kwargs['manifest'] = str(manifest)
    with patch(NAME_PATCH, return_value=name):
        call_command(command, *args, stdout=out, stderr=io.StringIO(), **kwargs)
    return out.getvalue()


def _row_counts():
    return {f'{mdl._meta.label}': mdl.objects.count() for mdl in support.watched_models()}


class _TempManifestMixin:
    @classmethod
    def _temp_manifest(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix='walk-test-')
        return Path(cls._tmpdir) / 'manifest.json'

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(getattr(cls, '_tmpdir', ''), ignore_errors=True)


# ===========================================================================
# The refusal rule
# ===========================================================================
class RefusalTests(_TempManifestMixin, TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.manifest = cls._temp_manifest()

    def _refused(self, **kwargs):
        before = _row_counts()
        with self.assertRaises(CommandError) as ctx:
            _run('seed_walkthrough', manifest=self.manifest, **kwargs)
        self.assertEqual(_row_counts(), before, 'a refusal must write nothing')
        return str(ctx.exception)

    def test_check_1_a_remote_host_is_refused(self):
        with patch(HOST_PATCH, return_value='acela.proxy.rlwy.net'):
            message = self._refused()
        self.assertIn('check 1', message)

    def test_check_2_a_database_not_named_solarpms_walk_is_refused(self):
        for name in ('railway', 'solarpms_local'):
            with self.subTest(name=name):
                message = self._refused(name=name)
                self.assertIn('check 2', message)
                self.assertIn(name, message)

    def test_every_failed_check_is_named_not_just_the_first(self):
        with patch(HOST_PATCH, return_value='postgres.railway.internal'):
            message = self._refused(name='railway')
        self.assertIn('check 1', message)
        self.assertIn('check 2', message)

    def test_check_3_a_real_person_is_refused(self):
        User.objects.create_user('real.person', email='someone@horizonrenewablepower.com')
        self.assertIn('check 3', self._refused())

    def test_check_3_admits_exactly_the_one_permitted_address(self):
        User.objects.create_user('the.assignee', email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)
        User.objects.create_user('synthetic', email='x@walk.invalid')
        with patch(NAME_PATCH, return_value=WALK_NAME):
            self.assertEqual(support.refusal_reasons(), [])
        # ...and not a near miss of it.
        User.objects.create_user('near.miss', email='santosh@horizonrenewablepower.co')
        with patch(NAME_PATCH, return_value=WALK_NAME):
            reasons = support.refusal_reasons()
        self.assertEqual(len(reasons), 1)
        self.assertIn('check 3', reasons[0])

    def test_check_4_an_email_or_whatsapp_row_marked_sent_is_refused(self):
        profile = User.objects.create_user('synthetic', email='x@walk.invalid').profile
        for channel in ('email', 'whatsapp'):
            with self.subTest(channel=channel):
                row = m.NotificationLog.objects.create(
                    recipient=profile, channel=channel, status='sent', message='hello')
                self.assertIn('check 4', self._refused())
                row.delete()

    def test_check_4_ignores_in_app_and_skipped_rows(self):
        profile = User.objects.create_user('synthetic', email='x@walk.invalid').profile
        m.NotificationLog.objects.create(recipient=profile, channel='in_app',
                                         status='sent', message='in-app notice')
        m.NotificationLog.objects.create(recipient=profile, channel='email',
                                         status='skipped', message='switch off')
        with patch(NAME_PATCH, return_value=WALK_NAME):
            self.assertEqual(support.refusal_reasons(), [])

    def test_the_teardown_refuses_under_the_same_rule(self):
        with self.assertRaises(CommandError) as ctx:
            _run('teardown_walkthrough', manifest=self.manifest, name='railway')
        self.assertIn('check 2', str(ctx.exception))


# ===========================================================================
# The whole seed, once per class
# ===========================================================================
class SeededWalkthroughTests(_TempManifestMixin, TestCase):

    @classmethod
    def setUpTestData(cls):
        _build_migrated_reference_data()
        cls.manifest = cls._temp_manifest()
        cls.first_output = _run('seed_walkthrough', manifest=cls.manifest)

    # ---- idempotency --------------------------------------------------------
    def test_a_second_run_changes_no_row_count(self):
        before = _row_counts()
        output = _run('seed_walkthrough', manifest=self.manifest)
        self.assertEqual(_row_counts(), before)
        self.assertIn('already present and complete', output)

    def test_a_second_run_of_one_area_changes_nothing_either(self):
        before = _row_counts()
        _run('seed_walkthrough', only=['design'], manifest=self.manifest)
        self.assertEqual(_row_counts(), before)

    def test_the_first_line_names_the_database(self):
        self.assertTrue(self.first_output.startswith(f'[db] host='))
        self.assertIn(f'name={WALK_NAME}', self.first_output.splitlines()[0])

    def test_no_password_is_printed(self):
        from projects.management.commands.seed_walkthrough import WALK_PASSWORD
        self.assertNotIn(WALK_PASSWORD, self.first_output)

    # ---- people and notifications -------------------------------------------
    def test_every_seeded_email_is_invalid_except_the_one_permitted_address(self):
        emails = list(User.objects.filter(username__startswith='walk.')
                      .values_list('email', flat=True))
        self.assertEqual(len(emails), 16)
        real = [e for e in emails if not e.endswith('.invalid')]
        self.assertEqual(real, [RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL])

    def test_both_preferences_are_off_for_every_seeded_user(self):
        profiles = m.UserProfile.objects.filter(user__username__startswith='walk.')
        self.assertEqual(profiles.count(), 16)
        self.assertFalse(profiles.filter(email_notifications=True).exists())
        self.assertFalse(profiles.filter(whatsapp_notifications=True).exists())

    def test_both_master_switches_are_off(self):
        settings = m.SystemSettings.get()
        self.assertFalse(settings.email_enabled)
        self.assertFalse(settings.whatsapp_enabled)

    def test_nothing_was_sent_on_an_external_channel(self):
        external = m.NotificationLog.objects.filter(channel__in=('email', 'whatsapp'))
        self.assertTrue(external.exists(), 'the seed drives views that try to notify')
        self.assertEqual(set(external.values_list('status', flat=True)), {'skipped'})

    def test_the_capability_flags_landed_through_the_admin_screen(self):
        p = {x.user.username: x for x in m.UserProfile.objects.filter(
            user__username__startswith='walk.')}
        self.assertTrue(p['walk.designqc'].is_design_qc)
        self.assertTrue(p['walk.designhead'].is_design_head)
        self.assertTrue(p['walk.qaqc'].is_qaqc)
        self.assertTrue(p['walk.scm'].is_warehouse_keeper)
        self.assertTrue(p['walk.finance'].is_payment_approver)
        self.assertEqual(p['walk.designhead'].design_head_deputy, p['walk.designdeputy'])
        self.assertEqual(m.UserProfile.objects.filter(role='Admin').count(), 1)

    # ---- the states are what they claim ------------------------------------
    def test_a_released_site_carries_its_arka_cad_boq_attempts_and_ledger(self):
        a = m.DesignAssignment.objects.get(project__project_id='WALKP01')
        self.assertEqual(a.status, m.DESIGN_RELEASED)
        self.assertEqual(a.pm_approved_by.user.username, 'walk.pm')
        self.assertIsNotNone(a.released_at)
        attempt = a.attempts.get(attempt_number=1)
        self.assertEqual((attempt.qc_verdict, attempt.qc_reviewed_by.user.username),
                         (m.QC_PASSED, 'walk.designqc'))
        self.assertEqual((attempt.head_verdict, attempt.head_reviewed_by.user.username),
                         (m.QC_PASSED, 'walk.designhead'))
        self.assertIsNotNone(attempt.boq_submitted_at)
        arka = attempt.arka_submissions.get(is_current=True)
        self.assertEqual((arka.verdict, arka.head_verdict),
                         (m.ARKA_APPROVED, m.ARKA_APPROVED))
        self.assertNotEqual(arka.reviewed_by, arka.head_reviewed_by)
        cad = m.DesignFile.objects.get(attempt=attempt, kind=m.DESIGN_FILE_CAD_ZIP,
                                       is_current=True)
        self.assertEqual(cad.bucket, support.STUB_BUCKET)
        self.assertTrue(cad.original_filename.startswith(support.STUB_FILE_PREFIX))
        self.assertTrue(m.BOQItem.objects.filter(boq__project=a.project,
                                                 boq_quantity__gt=0).exists())
        ledger = list(m.StatusTransition.objects.filter(
            subject_type=m.SUBJECT_DESIGN_ASSIGNMENT, subject_id=a.pk).order_by('pk'))
        self.assertEqual(ledger[-1].to_status, m.DESIGN_RELEASED)
        self.assertEqual(ledger[-1].reason_code, m.REASON_DESIGN_PM_APPROVED)
        self.assertIn(m.REASON_DESIGN_HEAD_PASSED, [r.reason_code for r in ledger])
        self.assertEqual(ledger[0].to_status, m.DESIGN_AWAITING_ALLOCATION)

    def test_every_design_state_the_doc_lists_is_present(self):
        statuses = dict(m.DesignAssignment.objects.filter(
            project__project_id__startswith='WALKD').values_list('project__project_id',
                                                                  'status'))
        self.assertNotIn('WALKD01', statuses)   # no assignment: where a walk starts
        for code, status in {
            'WALKD02': m.DESIGN_AWAITING_ALLOCATION, 'WALKD03': m.DESIGN_IN_DESIGN,
            'WALKD04': m.DESIGN_ARKA_SUBMITTED, 'WALKD05': m.DESIGN_AWAITING_HEAD_ARKA,
            'WALKD06': m.DESIGN_ARKA_REJECTED, 'WALKD07': m.DESIGN_SURVEY_RETURNED,
            'WALKD08': m.DESIGN_ARKA_SUBMITTED, 'WALKD09': m.DESIGN_ARTIFACTS_UPLOADED,
            'WALKD10': m.DESIGN_IN_QC, 'WALKD11': m.DESIGN_AWAITING_HEAD_QC,
            'WALKD12': m.DESIGN_AWAITING_PM_APPROVAL, 'WALKD13': m.DESIGN_PM_REJECTED,
            'WALKD14': m.DESIGN_IN_DESIGN, 'WALKD15': m.DESIGN_RELEASED,
            'WALKD16': m.DESIGN_RELEASED,
        }.items():
            with self.subTest(code=code):
                self.assertEqual(statuses[code], status)
        self.assertEqual(m.DesignAssignment.objects.get(
            project__project_id='WALKD15').current_attempt_number, 2)

    def test_all_seven_change_request_verdicts_exist(self):
        verdicts = set(m.DesignChangeRequest.objects.values_list('verdict', flat=True))
        self.assertEqual(verdicts, {v for v, _label in m.CHANGE_REQUEST_VERDICT_CHOICES})

    def test_every_payment_status_exists_including_a_partial_approval(self):
        PR = m.PaymentRequest
        self.assertEqual(set(PR.objects.values_list('status', flat=True)),
                         {PR.PENDING_APPROVAL, PR.APPROVED, PR.ON_HOLD, PR.REJECTED,
                          PR.CONFIRMED})
        partial = PR.objects.filter(status=PR.APPROVED).exclude(approved_amount=None)
        self.assertTrue(any(p.approved_amount < p.amount for p in partial))
        paid = PR.objects.get(status=PR.CONFIRMED)
        self.assertNotEqual(paid.confirmed_by_id, paid.approved_by.user_id)

    def test_every_challan_status_exists_and_one_was_received_on_behalf(self):
        DC = m.DeliveryChallan
        self.assertEqual(set(DC.objects.values_list('status', flat=True)),
                         {DC.EXPECTED, DC.PARTIALLY_RECEIVED, DC.RECEIVED, DC.REJECTED})
        self.assertTrue(m.DCLineItem.objects.filter(grn_on_behalf=True).exists())

    def test_groups_pool_and_orders(self):
        self.assertEqual(set(m.SiteGroup.objects.values_list('status', flat=True)),
                         {m.SITE_GROUP_DRAFT, m.SITE_GROUP_LOCKED})
        self.assertTrue(m.SiteGroupMembership.objects.exclude(removed_at=None).exists())
        self.assertEqual(Counter(m.VendorOrder.objects.values_list('project_type', flat=True)),
                         Counter({'OPEX': 2, 'Residential': 1}))
        for doc in m.VendorOrderDocument.objects.all():
            self.assertEqual(doc.bucket, support.STUB_BUCKET)
            self.assertTrue(doc.file_name.startswith(support.STUB_FILE_PREFIX))

    def test_execution_states(self):
        tasks = {t.task_name: t for t in m.Task.objects.filter(
            phase__project__project_id='WALKE01')}
        civil = tasks['Civil Work and MMS Installation']
        self.assertEqual(civil.status, m.Task.DONE)
        self.assertNotEqual(civil.submitted_by_id, civil.approved_by_id)
        self.assertTrue(tasks['Module Installation'].is_awaiting_approval)
        self.assertEqual(tasks['Inverter Installation'].status, m.Task.BLOCKED)
        self.assertTrue(tasks['RMS Installation'].is_not_applicable)
        self.assertEqual(set(m.PunchPoint.objects.values_list('status', flat=True)),
                         {m.PunchPoint.OPEN, m.PunchPoint.WAIVED})
        self.assertEqual(set(m.Issue.objects.filter(project__project_id='WALKE01')
                             .values_list('status', flat=True)),
                         {m.Issue.OPEN, m.Issue.IN_PROGRESS, m.Issue.RESOLVED,
                          m.Issue.CLOSED})
        self.assertTrue(m.ChecklistItemCompletion.objects.filter(answer='no').exists())

    def test_residential_states(self):
        boqs = set(m.BOQ.objects.filter(project__project_type='Residential')
                   .values_list('status', flat=True))
        self.assertEqual(boqs, {'Submitted', 'Acknowledged', 'Revision Requested'})
        self.assertTrue(m.Project.objects.filter(project_type='Residential',
                                                 status='Draft').exists())
        self.assertEqual(set(m.PaymentMilestone.objects.values_list('status', flat=True)),
                         {'Pending', 'Invoiced', 'Received'})

    def test_no_history_runs_backwards_and_none_is_in_the_future(self):
        from django.utils import timezone
        rows = list(m.StatusTransition.objects.order_by('subject_type', 'subject_id', 'pk')
                    .values_list('subject_type', 'subject_id', 'occurred_at'))
        for previous, current in zip(rows, rows[1:]):
            if previous[:2] == current[:2]:
                self.assertLessEqual(previous[2], current[2], current[:2])
        self.assertFalse(m.StatusTransition.objects.filter(
            occurred_at__gt=timezone.now()).exists())

    # ---- the teardown on a full seed ----------------------------------------
    def test_teardown_refuses_every_area_with_history_and_deletes_nothing(self):
        before = _row_counts()
        dry = _run('teardown_walkthrough', manifest=self.manifest, dry_run=True)
        for area in ('design', 'changes', 'procurement', 'delivery', 'execution',
                     'residential'):
            self.assertRegex(dry, rf'{area}\s+REFUSED')
        self.assertIn('StatusTransition', dry)
        with self.assertRaises(CommandError) as ctx:
            _run('teardown_walkthrough', manifest=self.manifest)
        self.assertIn('DROP DATABASE', str(ctx.exception))
        self.assertEqual(_row_counts(), before)
        self.assertTrue(self.manifest.exists())


# ===========================================================================
# The teardown where exact removal IS possible
# ===========================================================================
class ExactTeardownTests(_TempManifestMixin, TestCase):
    """users + reference carry no history, so they can be removed — exactly."""

    @classmethod
    def setUpTestData(cls):
        _build_migrated_reference_data()
        # Rows that are NOT the seed's, and must survive its teardown.
        cls.outsider_user = User.objects.create_user('outsider', email='o@elsewhere.invalid')
        cls.outsider_vendor = m.Vendor.objects.create(
            name='Outsider Vendor', contact_person='Someone', phone='9999999999')
        cls.baseline = _row_counts()
        cls.manifest = cls._temp_manifest()
        _run('seed_walkthrough', only=['reference'], manifest=cls.manifest)

    def test_teardown_removes_exactly_the_seed_and_nothing_else(self):
        self.assertNotEqual(_row_counts(), self.baseline)
        dry = _run('teardown_walkthrough', manifest=self.manifest, dry_run=True)
        self.assertRegex(dry, r'users\s+removable')
        self.assertRegex(dry, r'reference\s+removable')
        self.assertNotEqual(_row_counts(), self.baseline, 'a dry run deletes nothing')

        _run('teardown_walkthrough', manifest=self.manifest)
        self.assertEqual(_row_counts(), self.baseline)
        self.assertTrue(User.objects.filter(pk=self.outsider_user.pk).exists())
        self.assertTrue(m.Vendor.objects.filter(pk=self.outsider_vendor.pk).exists())
        self.assertFalse(User.objects.filter(username__startswith='walk.').exists())
        self.assertFalse(self.manifest.exists(), 'an emptied manifest is consumed')

    def test_teardown_refuses_when_a_row_it_did_not_seed_depends_on_the_seed(self):
        # A walker's own row pointing at a seeded one: removing the seed would have to
        # delete it or null it, so the teardown refuses and deletes nothing.
        walker_made = m.StockLocation.objects.create(
            code='HAND-1', name='Made by hand',
            keeper=m.UserProfile.objects.get(user__username='walk.scm'))
        before = _row_counts()
        with self.assertRaises(CommandError):
            _run('teardown_walkthrough', manifest=self.manifest)
        self.assertEqual(_row_counts(), before)
        self.assertTrue(m.StockLocation.objects.filter(pk=walker_made.pk).exists())

    def test_removing_users_alone_while_reference_remains_is_refused(self):
        dry = _run('teardown_walkthrough', only=['users'], manifest=self.manifest,
                   dry_run=True)
        self.assertRegex(dry, r'users\s+REFUSED')
