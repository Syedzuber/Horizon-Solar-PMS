"""Typed dates must be plausible — forms.check_typed_date() at every entry point.

WHY THIS FILE EXISTS
--------------------
On 18 Sep 2026 production held three Task rows whose due_date was in the year 20 or
26. Every parser the views used — date.fromisoformat, parse_date, strptime and
forms.DateField — accepts '0026-09-17' as the year 26, and a browser date picker lets
a two-digit year through. The rule decided for it: a typed date is valid on or after
2020-01-01 and on or before five years from today, and anything else is refused with a
message naming that range.

There are fourteen entry points that store a typed date. Each one gets the same
battery (DateRuleCases): the year 26 AND the year 20 refused with nothing saved — the
year-26 case alone would pass a rule that only checked "more than N years ago" — the
two ends of the range accepted and one day past each refused, a malformed string and
an impossible day refused with a message rather than a 500, and 2021-01-01 (a
back-dated correction) accepted. Where a view edits an existing row, a row that
already holds a bad date can be corrected.

The two design views keep their own, stricter rule — a proposed date cannot be in the
past — so for them 2021-01-01 and 2020-01-01 pass the helper and are then refused by
THAT message, which is asserted instead of acceptance.

Run with:
    python manage.py test projects.tests_date_validation --settings=solarpms.test_settings
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.messages import constants as message_constants, get_messages
from django.core.management import call_command
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .forms import TYPED_DATE_FLOOR, check_typed_date, typed_date_ceiling
from .models import (
    DESIGN_AWAITING_ALLOCATION, DESIGN_IN_DESIGN,
    DeliveryChallan, DesignAssignment, DueDateChangeLog, DueDateCommitment, Issue,
    PaymentMilestone, PaymentRequest, Program, Project, ProjectPhase, Task, Vendor,
    VendorOrder,
)

YEAR_26 = '0026-09-17'
YEAR_20 = '0020-09-17'
MALFORMED = 'not-a-date'
IMPOSSIBLE = '2026-02-30'
BACKDATED = '2021-01-01'


def _profile(username, role, **flags):
    """A post_save signal creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    for field, value in flags.items():
        setattr(profile, field, value)
    profile.save()
    return profile


def _client(profile):
    client = Client(SERVER_NAME='localhost')
    client.force_login(profile.user)
    return client


def _ceiling():
    return typed_date_ceiling().isoformat()


def _past_ceiling():
    return (typed_date_ceiling() + timedelta(days=1)).isoformat()


class HelperTests(TestCase):
    """The helper itself, independent of any view."""

    def test_the_range_ends_are_inclusive(self):
        today = date(2026, 9, 18)
        self.assertEqual(check_typed_date('2020-01-01', today=today), (date(2020, 1, 1), None))
        self.assertEqual(check_typed_date('2031-09-18', today=today), (date(2031, 9, 18), None))
        self.assertIsNotNone(check_typed_date('2019-12-31', today=today)[1])
        self.assertIsNotNone(check_typed_date('2031-09-19', today=today)[1])

    def test_the_message_names_the_range_and_what_was_entered(self):
        _, error = check_typed_date(YEAR_26, today=date(2026, 9, 18))
        self.assertEqual(
            error,
            'Enter a date between 01 Jan 2020 and 18 Sep 2031. '
            '17 Sep 0026 is outside that range — check the year.')

    def test_an_impossible_day_is_a_message_not_an_exception(self):
        self.assertEqual(check_typed_date(IMPOSSIBLE),
                         (None, '"2026-02-30" is not a valid date. Pick a date from the calendar.'))

    def test_empty_input_is_not_an_error(self):
        for empty in (None, '', '   '):
            self.assertEqual(check_typed_date(empty), (None, None))

    def test_a_leap_day_ceiling_falls_back_to_28_february(self):
        self.assertEqual(typed_date_ceiling(date(2028, 2, 29)), date(2033, 2, 28))


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

class DateFixture(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.pm      = _profile('dv_pm',  'PM')
        cls.finance = _profile('dv_fin', 'Finance')
        cls.scm     = _profile('dv_scm', 'SCM')
        common = dict(customer_phone='9876543210', site_address='1 Sun Road',
                      city='Lucknow', state='Uttar Pradesh', project_type='Residential',
                      dc_capacity_kw=Decimal('5.00'), assigned_pm=cls.pm)
        cls.project = Project.objects.create(customer_name='Date Active', status='Active',
                                             cascade_scheduling=False, **common)
        cls.draft = Project.objects.create(customer_name='Date Draft', status='Draft',
                                           survey_date=date(2026, 10, 1), **common)
        cls.phase = ProjectPhase.objects.create(project=cls.project, phase_name='Phase 1',
                                                phase_order=1)
        cls.task = Task.objects.create(phase=cls.phase, task_name='Anchor', task_order=1,
                                       assigned_role=Task.PM, assigned_to=cls.pm)

    def setUp(self):
        self.task.refresh_from_db()
        self.project.refresh_from_db()
        self.draft.refresh_from_db()

    @staticmethod
    def text(response):
        """Every message queued on the request plus the body, for refusal assertions."""
        queued = ' '.join(str(m) for m in get_messages(response.wsgi_request))
        body = response.content.decode('utf-8', 'replace') if response.status_code == 200 else ''
        return f'{queued} {body}'


class DateRuleCases:
    """The battery every entry point runs. Subclasses define submit / snapshot / saved."""

    malformed_text = 'is not a valid date'
    accepts_past = True          # False for the two design views
    edits_existing = False       # True where the view can correct a stored bad date

    # Subclasses provide submit(value), snapshot(), assert_saved(value) and, where
    # edits_existing, seed_bad_row(). No stubs here: this class comes FIRST in each
    # test's bases, so a stub would shadow the fixture class that defines them.

    # -- refused -----------------------------------------------------------

    def _assert_refused(self, value, expect):
        before = self.snapshot()
        response = self.submit(value)
        self.assertNotEqual(response.status_code, 500)
        self.assertIn(expect, self.text(response))
        self.assertEqual(self.snapshot(), before, f'{value!r} was refused but something was saved')

    def test_year_26_is_refused(self):
        self._assert_refused(YEAR_26, 'check the year')

    def test_year_20_is_refused(self):
        self._assert_refused(YEAR_20, 'check the year')

    def test_the_day_before_the_floor_is_refused(self):
        self._assert_refused('2019-12-31', 'check the year')

    def test_one_day_past_the_ceiling_is_refused(self):
        self._assert_refused(_past_ceiling(), 'check the year')

    def test_the_refusal_names_the_range(self):
        self._assert_refused(YEAR_26, f'between 01 Jan 2020 and {typed_date_ceiling():%d %b %Y}')

    def test_a_malformed_string_is_refused_with_a_message(self):
        self._assert_refused(MALFORMED, self.malformed_text)

    def test_an_impossible_day_is_refused_with_a_message_not_a_500(self):
        self._assert_refused(IMPOSSIBLE, self.malformed_text)

    # -- accepted ----------------------------------------------------------

    def test_the_ceiling_itself_is_accepted(self):
        self.submit(_ceiling())
        self.assert_saved(typed_date_ceiling())

    def test_a_backdated_correction_is_accepted(self):
        if not self.accepts_past:
            self._assert_refused(BACKDATED, 'cannot be in the past')
            return
        self.submit(BACKDATED)
        self.assert_saved(date(2021, 1, 1))

    def test_the_floor_itself_is_accepted(self):
        if not self.accepts_past:
            self._assert_refused(TYPED_DATE_FLOOR.isoformat(), 'cannot be in the past')
            return
        self.submit(TYPED_DATE_FLOOR.isoformat())
        self.assert_saved(TYPED_DATE_FLOOR)

    def test_a_row_already_holding_a_bad_date_can_be_corrected(self):
        if not self.edits_existing:
            self.skipTest('this entry point only creates rows')
        self.seed_bad_row()
        good = timezone.localdate() + timedelta(days=30)
        self.submit(good.isoformat())
        self.assert_saved(good)


# ---------------------------------------------------------------------------
# #1 task_set_due_date — both branches, and the PM cascade
# ---------------------------------------------------------------------------

class TaskSetDueDatePMTests(DateRuleCases, DateFixture):
    edits_existing = True

    def submit(self, value):
        return _client(self.pm).post(
            reverse('task_set_due_date', args=[self.project.project_id, self.task.pk]),
            {'due_date': value})

    def snapshot(self):
        return Task.objects.get(pk=self.task.pk).due_date

    def assert_saved(self, value):
        self.assertEqual(self.snapshot(), value)

    def seed_bad_row(self):
        Task.objects.filter(pk=self.task.pk).update(due_date=date(26, 9, 17))


class TaskSetDueDateRoleOwnerTests(DateRuleCases, DateFixture):
    """The non-PM branch: a role owner on a project with cascade off."""
    edits_existing = True

    def setUp(self):
        super().setUp()
        Task.objects.filter(pk=self.task.pk).update(assigned_role=Task.FINANCE,
                                                    assigned_to=self.finance)

    def submit(self, value):
        return _client(self.finance).post(
            reverse('task_set_due_date', args=[self.project.project_id, self.task.pk]),
            {'due_date': value})

    def snapshot(self):
        return Task.objects.get(pk=self.task.pk).due_date

    def assert_saved(self, value):
        self.assertEqual(self.snapshot(), value)

    def seed_bad_row(self):
        Task.objects.filter(pk=self.task.pk).update(due_date=date(20, 9, 17))


class TaskSetDueDateCascadeTests(DateFixture):
    """A refused anchor must not ripple: recalculate_from_task writes the downstream
    tasks and DueDateChangeLog rows, so the check has to come before it."""

    def setUp(self):
        super().setUp()
        Project.objects.filter(pk=self.project.pk).update(cascade_scheduling=True)
        self.after = [
            Task.objects.create(phase=self.phase, task_name=f'Next {n}', task_order=n + 1,
                                assigned_role=Task.PM, duration_days=2,
                                due_date=date(2026, 12, n))
            for n in (1, 2)
        ]

    def _post(self, value):
        return _client(self.pm).post(
            reverse('task_set_due_date', args=[self.project.project_id, self.task.pk]),
            {'due_date': value})

    def _downstream(self):
        return [Task.objects.get(pk=t.pk).due_date for t in self.after]

    def test_a_valid_date_still_cascades(self):
        self._post('2026-11-02')
        self.assertEqual(Task.objects.get(pk=self.task.pk).due_date, date(2026, 11, 2))
        self.assertEqual(self._downstream(), [date(2026, 11, 4), date(2026, 11, 6)])
        self.assertEqual(DueDateChangeLog.objects.filter(task__phase=self.phase).count(), 3)

    def test_a_refused_date_leaves_every_downstream_task_and_the_log_untouched(self):
        for bad in (YEAR_26, YEAR_20, _past_ceiling(), MALFORMED):
            before = self._downstream()
            response = self._post(bad)
            self.assertNotEqual(response.status_code, 500)
            self.assertIsNone(Task.objects.get(pk=self.task.pk).due_date, bad)
            self.assertEqual(self._downstream(), before, bad)
            self.assertEqual(DueDateChangeLog.objects.count(), 0, bad)


# ---------------------------------------------------------------------------
# #2 _apply_task_status_change — Finance's inline date on the move to In Progress
# ---------------------------------------------------------------------------

class TaskStatusInlineDueDateTests(DateRuleCases, DateFixture):

    def setUp(self):
        super().setUp()
        Task.objects.filter(pk=self.task.pk).update(assigned_role=Task.FINANCE,
                                                    assigned_to=self.finance, due_date=None)

    def submit(self, value):
        return _client(self.finance).post(
            reverse('task_status_update', args=[self.project.project_id, self.task.pk]),
            {'status': Task.IN_PROGRESS, 'due_date': value})

    def snapshot(self):
        t = Task.objects.get(pk=self.task.pk)
        return (t.due_date, t.status)

    def assert_saved(self, value):
        self.assertEqual(self.snapshot(), (value, Task.IN_PROGRESS))

    def test_a_stored_bad_date_does_not_block_the_status_change(self):
        """The inline date only applies to a task with none; a task already holding a
        bad date moves on without being asked for one."""
        Task.objects.filter(pk=self.task.pk).update(due_date=date(26, 9, 17))
        _client(self.finance).post(
            reverse('task_status_update', args=[self.project.project_id, self.task.pk]),
            {'status': Task.IN_PROGRESS})
        self.assertEqual(Task.objects.get(pk=self.task.pk).status, Task.IN_PROGRESS)


# ---------------------------------------------------------------------------
# #3–#6 the ModelForm / Form views — inline field errors
# ---------------------------------------------------------------------------

class TaskAddTests(DateRuleCases, DateFixture):
    malformed_text = 'Enter a valid date'

    def submit(self, value):
        return _client(self.pm).post(
            reverse('task_add', args=[self.project.project_id]),
            {'phase': self.phase.pk, 'task_name': 'Added', 'assigned_role': Task.PM,
             'assigned_to': self.pm.pk, 'due_date': value})

    def snapshot(self):
        return Task.objects.filter(task_name='Added').count()

    def assert_saved(self, value):
        self.assertEqual(Task.objects.get(task_name='Added').due_date, value)


def _project_post(project, **dates):
    data = {'customer_name': project.customer_name, 'customer_phone': project.customer_phone,
            'site_address': project.site_address, 'city': project.city,
            'state': project.state, 'dc_capacity_kw': '5.00'}
    data.update(dates)
    return data


class ProjectCreateTests(DateRuleCases, DateFixture):
    """Both date fields on the create form go through the same clean_ hook."""
    malformed_text = 'Enter a valid date'
    field = 'survey_date'

    def submit(self, value):
        data = _project_post(self.draft, **{self.field: value})
        data.update(customer_name='Created By Test', project_type='Residential')
        return _client(self.pm).post(reverse('project_create'), data)

    def snapshot(self):
        return Project.objects.filter(customer_name='Created By Test').count()

    def assert_saved(self, value):
        self.assertEqual(getattr(Project.objects.get(customer_name='Created By Test'),
                                 self.field), value)


class ProjectCreateTargetDateTests(ProjectCreateTests):
    field = 'target_commissioning_date'


class ProjectEditTests(DateRuleCases, DateFixture):
    malformed_text = 'Enter a valid date'
    edits_existing = True
    field = 'survey_date'

    def submit(self, value):
        return _client(self.pm).post(
            reverse('project_edit', args=[self.draft.project_id]),
            _project_post(self.draft, **{self.field: value}))

    def snapshot(self):
        return getattr(Project.objects.get(pk=self.draft.pk), self.field)

    def assert_saved(self, value):
        self.assertEqual(self.snapshot(), value)

    def seed_bad_row(self):
        Project.objects.filter(pk=self.draft.pk).update(**{self.field: date(26, 9, 17)})


class ProjectEditTargetDateTests(ProjectEditTests):
    field = 'target_commissioning_date'


class ProjectFieldEditTests(DateRuleCases, DateFixture):
    malformed_text = 'Enter a valid date'
    edits_existing = True

    def submit(self, value):
        return _client(self.pm).post(
            reverse('project_field_edit', args=[self.project.project_id]),
            {'dc_capacity_kw': '5.00', 'target_commissioning_date': value},
            HTTP_HX_REQUEST='true')

    def snapshot(self):
        return Project.objects.get(pk=self.project.pk).target_commissioning_date

    def assert_saved(self, value):
        self.assertEqual(self.snapshot(), value)

    def seed_bad_row(self):
        Project.objects.filter(pk=self.project.pk).update(
            target_commissioning_date=date(26, 9, 17))

    def test_the_widget_carries_the_bounds(self):
        response = _client(self.pm).get(
            reverse('project_field_edit', args=[self.project.project_id]),
            HTTP_HX_REQUEST='true')
        self.assertContains(response, f'min="{TYPED_DATE_FLOOR.isoformat()}"')
        self.assertContains(response, f'max="{_ceiling()}"')


# ---------------------------------------------------------------------------
# #7 project_overview update_milestone (PM branch)
# ---------------------------------------------------------------------------

class MilestoneDueDateTests(DateRuleCases, DateFixture):
    edits_existing = True
    stored = date(2026, 10, 15)

    def setUp(self):
        super().setUp()
        self.milestone = PaymentMilestone.objects.create(
            project=self.project, milestone_name='M1', milestone_description='Survey',
            created_by=self.pm, due_date=self.stored)

    def submit(self, value):
        return _client(self.pm).post(
            reverse('project_overview', args=[self.project.project_id]),
            {'action': 'update_milestone', 'milestone_pk': self.milestone.pk,
             'milestone_description': 'Survey', 'amount': '', 'due_date': value})

    def snapshot(self):
        m = PaymentMilestone.objects.get(pk=self.milestone.pk)
        return (m.due_date, m.milestone_description)

    def assert_saved(self, value):
        self.assertEqual(PaymentMilestone.objects.get(pk=self.milestone.pk).due_date, value)

    def seed_bad_row(self):
        PaymentMilestone.objects.filter(pk=self.milestone.pk).update(due_date=date(26, 9, 17))

    def test_a_malformed_edit_keeps_the_stored_date_and_says_so(self):
        """This branch used to turn a malformed date into None and report 'updated'."""
        response = self.submit(MALFORMED)
        self.assertEqual(PaymentMilestone.objects.get(pk=self.milestone.pk).due_date, self.stored)
        levels = [(m.level, str(m)) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any(level == message_constants.ERROR for level, _ in levels), levels)
        self.assertNotIn('M1 updated.', [text for _, text in levels])

    def test_an_empty_date_still_clears_it(self):
        self.submit('')
        self.assertIsNone(PaymentMilestone.objects.get(pk=self.milestone.pk).due_date)


# ---------------------------------------------------------------------------
# #8 confirm_payment_request
# ---------------------------------------------------------------------------

class ConfirmPaymentDateTests(DateRuleCases, DateFixture):

    def setUp(self):
        super().setUp()
        vendor = Vendor.objects.create(name='Sunrise', contact_person='R', phone='9000000003')
        self.pr = PaymentRequest.objects.create(
            project=self.project, vendor=vendor, invoice_number='INV-1', invoice_document_name='i.pdf',
            invoice_document_url='http://x/i.pdf', invoice_document_path='p/i.pdf',
            amount=Decimal('1000.00'), requested_by=self.scm.user,
            status=PaymentRequest.APPROVED,
            vendor_order=VendorOrder.objects.create(   # O2: NOT NULL; fixture only
                vendor=vendor, project_type='Residential', created_by=self.scm))
        # O5: a payment date may not precede the day the request was raised. Raised on
        # the floor, so the battery's past dates still exercise the calendar range alone.
        PaymentRequest.objects.filter(pk=self.pr.pk).update(
            requested_date=timezone.make_aware(datetime(2020, 1, 1, 12, 0)))

    def submit(self, value):
        return _client(self.finance).post(
            reverse('confirm_payment_request', args=[self.project.project_id, self.pr.pk]),
            {'payment_date': value, 'payment_reference': 'UTR-1'})

    def test_the_ceiling_itself_is_accepted(self):
        """O5 NARROWS THIS ENTRY POINT: money cannot be paid in the future, so the
        ceiling that passes the calendar check is refused by mark_payment_paid()."""
        self._assert_refused(_ceiling(), 'cannot be in the future')

    def snapshot(self):
        pr = PaymentRequest.objects.get(pk=self.pr.pk)
        return (pr.status, pr.payment_date)

    def assert_saved(self, value):
        self.assertEqual(self.snapshot(), (PaymentRequest.CONFIRMED, value))


# ---------------------------------------------------------------------------
# #9–#11 the three issue views — refused, and the typed text survives
# ---------------------------------------------------------------------------

class IssueCases(DateRuleCases):
    title = 'Inverter tripping'
    description = 'Trips twice a day since commissioning'

    def submit(self, value):
        return _client(self.pm).post(self.url(), {
            'title': self.title, 'description': self.description, 'severity': 'High',
            'assigned_to': self.pm.pk, 'due_date': value})

    def snapshot(self):
        return Issue.objects.filter(title=self.title).count()

    def assert_saved(self, value):
        self.assertEqual(Issue.objects.get(title=self.title).due_date, value)

    def test_a_refused_issue_redisplays_the_typed_title_and_description_once(self):
        # One client throughout: the draft lives in that client's session.
        client = _client(self.pm)
        client.post(self.url(), {'title': self.title, 'description': self.description,
                                 'assigned_to': self.pm.pk, 'due_date': YEAR_26})
        self.assertEqual(self.snapshot(), 0)
        first = client.get(self.destination())
        self.assertContains(first, f'value="{self.title}"')
        self.assertContains(first, self.description)
        second = client.get(self.destination())
        self.assertNotContains(second, f'value="{self.title}"')

    def test_an_empty_date_still_raises_the_issue(self):
        self.submit('')
        self.assertIsNone(Issue.objects.get(title=self.title).due_date)


class ProjectIssueTests(IssueCases, DateFixture):
    def url(self):
        return reverse('create_project_issue', args=[self.project.project_id])

    def destination(self):
        return reverse('project_overview', args=[self.project.project_id])


class TaskIssueTests(IssueCases, DateFixture):
    def url(self):
        return reverse('create_task_issue', args=[self.project.project_id, self.task.pk])

    def destination(self):
        return reverse('task_detail', args=[self.project.project_id, self.task.pk])


class DeliveryIssueTests(IssueCases, DateFixture):
    def setUp(self):
        super().setUp()
        self.challan = DeliveryChallan.objects.create(
            project=self.project, dc_number='DC-V1', dc_date=date(2026, 9, 1),
            status=DeliveryChallan.EXPECTED, created_by=self.scm)

    def url(self):
        return reverse('create_delivery_issue', args=[self.project.project_id, self.challan.pk])

    def destination(self):
        return reverse('delivery_challan_detail', args=[self.project.project_id, self.challan.pk])


# ---------------------------------------------------------------------------
# #12 create_delivery_challan — both dates
# ---------------------------------------------------------------------------

class DeliveryChallanDcDateTests(DateRuleCases, DateFixture):
    field = 'dc_date'

    def submit(self, value):
        data = {'dc_number': 'DC-T1', 'dc_date': '2026-09-01', 'expected_delivery_date': '',
                'line_item_category_0': 'Solar Modules',
                'line_item_description_0': 'Solar Module 540Wp',
                'line_item_qty_0': '10', 'line_item_unit_0': 'Nos'}
        data[self.field] = value
        return _client(self.scm).post(
            reverse('create_delivery_challan', args=[self.project.project_id]), data)

    def snapshot(self):
        return DeliveryChallan.objects.filter(dc_number='DC-T1').count()

    def assert_saved(self, value):
        self.assertEqual(getattr(DeliveryChallan.objects.get(dc_number='DC-T1'), self.field),
                         value)


class DeliveryChallanExpectedDateTests(DeliveryChallanDcDateTests):
    field = 'expected_delivery_date'

    def test_an_empty_expected_date_is_still_allowed(self):
        self.submit('')
        self.assertIsNone(DeliveryChallan.objects.get(dc_number='DC-T1').expected_delivery_date)


# ---------------------------------------------------------------------------
# #13 / #14 design_due_date_propose / design_due_date_change
# ---------------------------------------------------------------------------

class DesignFixture(DateFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.head = _profile('dv_head', 'Design', is_design_head=True)
        cls.designer = _profile('dv_des', 'Design')
        cls.program = Program.objects.create(
            name='Date-Val', program_type='OPEX', client_name='DV', status='Active',
            short_tender_code='DV')
        cls.site = Project(
            project_id='DVSITE1', customer_name='DV', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX', program=cls.program,
            site_code='DVSITE1', dc_capacity_kw=Decimal('100.00'), status='Draft')
        cls.site.save()
        cls.assignment = DesignAssignment.objects.create(
            project=cls.site, status=DESIGN_AWAITING_ALLOCATION,
            survey_file_bucket='b', survey_file_path='DVSITE1/survey/x.pdf')

    def setUp(self):
        super().setUp()
        self.agreed = timezone.localdate() + timedelta(days=3)
        DueDateCommitment.objects.create(
            assignment=self.assignment, proposed_date=self.agreed,
            proposed_by=self.head, approved_by=self.head,
            approved_at=timezone.now(), is_current=True)
        DesignAssignment.objects.filter(pk=self.assignment.pk).update(
            assigned_to=self.designer, status=DESIGN_IN_DESIGN)

    def snapshot(self):
        return DueDateCommitment.objects.filter(assignment=self.assignment).count()

    def assert_saved(self, value):
        current = DueDateCommitment.objects.get(assignment=self.assignment, is_current=True)
        self.assertEqual(current.proposed_date, value)

    def seed_bad_row(self):
        DueDateCommitment.objects.filter(assignment=self.assignment).update(
            proposed_date=date(26, 9, 17))


class DesignProposeTests(DateRuleCases, DesignFixture):
    accepts_past = False
    edits_existing = True

    def submit(self, value):
        return _client(self.designer).post(
            reverse('design_due_date_propose', args=[self.site.project_id]),
            {'proposed_date': value, 'change_reason': 'needs longer'})

    def test_a_past_date_posted_directly_still_gets_the_past_date_message(self):
        """min=today is the template's; the server's own rule must still fire."""
        yesterday = (timezone.localdate() - timedelta(days=1)).isoformat()
        self._assert_refused(yesterday, 'The requested due date cannot be in the past.')


class DesignChangeTests(DateRuleCases, DesignFixture):
    accepts_past = False
    edits_existing = True

    def submit(self, value):
        return _client(self.head).post(
            reverse('design_due_date_change', args=[self.site.project_id]),
            {'proposed_date': value, 'change_reason': 'client asked'})

    def test_a_past_date_posted_directly_still_gets_the_past_date_message(self):
        yesterday = (timezone.localdate() - timedelta(days=1)).isoformat()
        self._assert_refused(yesterday, 'The new due date cannot be in the past.')


# ---------------------------------------------------------------------------
# Templates and the listing command
# ---------------------------------------------------------------------------

class DateInputBoundsTests(DateFixture):

    def test_the_task_row_date_inputs_carry_min_and_max(self):
        response = _client(self.pm).get(reverse('project_overview',
                                                 args=[self.project.project_id]))
        self.assertContains(response, f'min="{TYPED_DATE_FLOOR.isoformat()}" max="{_ceiling()}"')


class ListImplausibleDatesCommandTests(DateFixture):

    def test_it_lists_a_seeded_bad_row_and_writes_nothing(self):
        Task.objects.filter(pk=self.task.pk).update(due_date=date(26, 9, 17))
        from io import StringIO
        out = StringIO()
        with CaptureQueriesContext(connection) as ctx:
            call_command('list_implausible_dates', stdout=out)
        output = out.getvalue()
        self.assertIn('DB host:', output.splitlines()[0])
        self.assertIn(f'Range: 2020-01-01 .. {_ceiling()}', output)
        row = [line for line in output.splitlines() if line.startswith('Task ')]
        self.assertEqual(len(row), 1, output)
        self.assertIn(str(self.task.pk), row[0])
        self.assertIn(self.project.project_id, row[0])
        self.assertIn('0026-09-17', row[0])
        writes = [q['sql'] for q in ctx.captured_queries
                  if not q['sql'].lstrip().upper().startswith('SELECT')]
        self.assertEqual(writes, [], 'the listing command issued a non-SELECT query')
        self.assertGreater(len(ctx.captured_queries), 0)
