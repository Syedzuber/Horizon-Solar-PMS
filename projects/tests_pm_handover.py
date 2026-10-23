"""
PM-2 — handing sites over: rulings PM-5, PM-6, PM-12, PM-13 and pre-flight Q1 to Q9.

WHAT IS PINNED HERE
-------------------
    - a bulk handover moves every chosen site, its open tasks and its pending bill PM
      steps, in one transaction; one notice to each PM, none per step (VERIFY 1)
    - a single-site Change PM does the same for N = 1 and no longer sends assign_project;
      a first assignment still does (VERIFY 2, Q8)
    - a seat that no longer holds the expected PM is refused and nothing moves (VERIFY 3)
    - one failing site fails the whole handover, and the page names it (VERIFY 4) —
      a refusal with its own message ("<code> failed:" only when it does not already
      start with the code, Q1), anything else (a real IntegrityError) as "unexpected
      error (logged)" with the traceback logged; a failing follow-up read (material
      steps, returning bills — inside the transaction, Q4) as "Nothing was handed over:
      unexpected error (logged)"; never a 500, on the bulk page and the single-site
      Change PM alike
    - a failed log line never loses the handover — its own savepoint (VERIFY 5, Q4)
    - material steps and bills waiting for changes are listed, not moved; SCM is told
      only when material steps remain (VERIFY 7)
    - a bill PM step still waiting on the Site Engineer moves silently and is announced
      to the incoming PM when the Site Engineer approves (VERIFY 8)
    - hand_over_pm_step() skips the SCM gate only: Admin, PM party, every other rule

Runs on SQLite and on Postgres. VERIFY 5 proves something only on Postgres, where a
failed statement inside a transaction refuses every later one until a rollback:
    python manage.py test projects.tests_pm_handover --settings=solarpms.test_settings
    python manage.py test projects.tests_pm_handover            # real settings: Postgres
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db import IntegrityError, connection
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .approvals import (
    ApprovalRefused, ContractorBill, apply_approval_decision, create_approval_request,
    hand_over_pm_step,
)
from .models import (
    ActivityLog, ApprovalStep, Program, Project, ProjectPhase, SiteGroup,
    SiteGroupMembership, SitePmAssignment, Task, Vendor, log_activity,
    APPROVAL_KIND_CONTRACTOR_BILL, APPROVAL_KIND_MATERIAL_PRE_ORDER, APPROVAL_PARTY_PM,
    APPROVAL_PARTY_SITE_ENGINEER, VENDOR_KIND_CONTRACTOR,
)
from .site_pm import SitePmChangeRefused, _failed_line, change_site_pm, hand_over_sites
from .tests_approvals import MODULE_LINE, SE_PHOTO

BUCKET = 'test-bills'
PDF = {'file_name': 'CB-1.pdf', 'bucket': BUCKET, 'path': 'site/bill/one.pdf',
       'file_size_kb': 100}
REASON = 'Nirankar moves to the IPGCL tenders'


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


def _name(profile):
    return profile.user.get_full_name()


def _pm_pair(notify_send):
    """T3c: the PM-6 pair as a mock of projects.notify.send_notification saw it.
    notify_now() calls it by keyword, once with ['in_app'] and once with ['email'] per
    PM. {recipient pk: [(channel, message, link)]}, handover labels only."""
    out = {}
    for call in notify_send.call_args_list:
        kwargs = call.kwargs
        if kwargs.get('template') in ('pm_handover_incoming', 'pm_handover_outgoing'):
            out.setdefault(kwargs['recipient'].pk, []).append(
                (kwargs['channels'][0], kwargs['message'], kwargs.get('link', '')))
    return out


def _db_error_on_call(n):
    """A change_site_pm stand-in that does the real work, then on its n-th call hits a
    real database error: an INSERT violating NOT NULL. The site's writes are already
    made when it fails, so the rollback has something to undo."""
    calls = []

    def run(*args, **kwargs):
        calls.append(1)
        result = change_site_pm(*args, **kwargs)
        if len(calls) == n:
            with connection.cursor() as cursor:
                cursor.execute('INSERT INTO projects_activitylog (action) VALUES (NULL)')
        return result
    return run


class HandoverFixture(TestCase):
    """One Program with three Active sites and one Cancelled site of pm_a, and one site
    of pm_c. On the sites: open and Done tasks; an active bill PM step (s1), a waiting one
    (s2), a bill waiting for changes (s1), a bill whose PM step SCM gave to pm_c (s1).
    Three material steps of pm_a: one naming s1, one naming the Program, one naming a
    group that holds s3, and one naming nothing."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = _profile('ho_admin', 'Admin')
        cls.pm_a = _profile('ho_pm_a', 'PM')
        cls.pm_b = _profile('ho_pm_b', 'PM')
        cls.pm_c = _profile('ho_pm_c', 'PM')
        cls.scm = _profile('ho_scm', 'SCM')
        cls.scm_2 = _profile('ho_scm_2', 'SCM')
        cls.se = _profile('ho_se', 'Site Engineer')
        cls.contractor = Vendor.objects.create(name='Civil Co', contact_person='R',
                                               phone='9000000011',
                                               kind=VENDOR_KIND_CONTRACTOR)
        cls.program = Program.objects.create(name='MPUVNL Test', program_type='OPEX',
                                             client_name='Client', status='Active',
                                             short_tender_code='HOT')

        cls.s1, cls.s2, cls.s3 = (cls._site(f'HOS00{i}', cls.pm_a) for i in (1, 2, 3))
        cls.cancelled = cls._site('HOS009', cls.pm_a)
        cls.other = cls._site('HOS010', cls.pm_c)
        Project.objects.filter(pk=cls.cancelled.pk).update(status='Cancelled')

        cls.open_tasks = {}
        for site in (cls.s1, cls.s2, cls.s3):
            phase = ProjectPhase.objects.create(project=site, phase_name='Execution',
                                                phase_order=1)
            cls.open_tasks[site.pk] = [
                Task.objects.create(phase=phase, task_name=f'Open {i}', task_order=i,
                                    assigned_to=cls.pm_a)
                for i in (1, 2)]
            Task.objects.create(phase=phase, task_name='Done', task_order=3,
                                assigned_to=cls.pm_a, status='Done',
                                completed_at=timezone.now())

        # Bills. bill_active: the Site Engineer has confirmed, the PM step is active.
        cls.bill_active = cls._bill(cls.s1, 'CB-1')
        apply_approval_decision(cls._step(cls.bill_active, APPROVAL_PARTY_SITE_ENGINEER),
                                'approved', cls.se, files=[dict(SE_PHOTO)])
        cls.bill_waiting = cls._bill(cls.s2, 'CB-2')           # PM step still waiting
        cls.bill_returning = cls._bill(cls.s1, 'CB-3')         # PM asks for changes
        apply_approval_decision(cls._step(cls.bill_returning, APPROVAL_PARTY_SITE_ENGINEER),
                                'approved', cls.se, files=[dict(SE_PHOTO)])
        apply_approval_decision(cls._step(cls.bill_returning, APPROVAL_PARTY_PM),
                                'changes_requested', cls.pm_a, note='Wrong bill date')
        cls.bill_third_pm = cls._bill(cls.s1, 'CB-4', pm=cls.pm_c)   # SCM chose pm_c

        # Material steps of pm_a — none of them moves.
        group = SiteGroup.objects.create(program=cls.program, name='Cluster East',
                                         created_by=cls.scm)
        SiteGroupMembership.objects.create(group=group, project=cls.s3, added_by=cls.scm)
        cls.mat_site = cls._material('Modules for HOS001', projects=[cls.s1])
        cls.mat_program = cls._material('Cables for the tender', programs=[cls.program])
        cls.mat_group = cls._material('Inverters for the east', site_groups=[group])
        cls.mat_none = cls._material('Spare fuses')

    # -- builders ---------------------------------------------------------------------

    @classmethod
    def _site(cls, code, pm):
        site = Project.objects.create(
            customer_name=f'Site {code}', project_id=code, program=cls.program,
            customer_phone='9876543210', site_address='1 Sun Road', city='Bhopal',
            state='Madhya Pradesh', project_type='OPEX', dc_capacity_kw=Decimal('50.00'),
            status='Active')
        change_site_pm(site, pm, by=cls.admin, expected_outgoing_pm=None)
        return site

    @classmethod
    def _bill(cls, site, number, pm=None):
        task = Task.objects.filter(phase__project=site).first()
        bill = ContractorBill(project=site, tasks=[task], amount='12500.00',
                              bill_number=number,
                              bill_date=timezone.localdate() - timedelta(days=3),
                              pdf=dict(PDF, path=f'site/bill/{number}.pdf'))
        with override_settings(SUPABASE_BILLS_BUCKET=BUCKET):
            return create_approval_request(
                kind=APPROVAL_KIND_CONTRACTOR_BILL, raised_by=cls.scm,
                title=f'Civil bill {number}', description='Foundation.',
                pm_assignee=pm or cls.pm_a, site_engineer_assignee=cls.se,
                vendor=cls.contractor, bill=bill)

    @classmethod
    def _material(cls, title, **scope):
        return create_approval_request(
            kind=APPROVAL_KIND_MATERIAL_PRE_ORDER, raised_by=cls.scm, title=title,
            description='Propose a make.', pm_assignee=cls.pm_a, material={},
            lines=[dict(MODULE_LINE)], **scope)

    @staticmethod
    def _step(approval, party):
        approval.refresh_from_db()
        return ApprovalStep.objects.get(request=approval, party=party,
                                        round=approval.current_round,
                                        verdict__in=['pending', 'approved'])

    def _live_pm_step(self, approval):
        approval.refresh_from_db()
        return (ApprovalStep.objects.filter(request=approval, party=APPROVAL_PARTY_PM,
                                            round=approval.current_round)
                .exclude(verdict='superseded').get())

    def _pm(self, site):
        return Project.objects.values_list('assigned_pm_id', flat=True).get(pk=site.pk)

    def _post_bulk(self, sites, reason=REASON, from_pm=None, to_pm=None):
        return _client(self.admin).post(reverse('admin_pm_handover'), {
            'program': self.program.pk, 'from_pm': (from_pm or self.pm_a).pk,
            'to_pm': (to_pm or self.pm_b).pk, 'site_ids': [s.pk for s in sites],
            'reason': reason})

    def _assert_nothing_moved(self):
        for site in (self.s1, self.s2, self.s3):
            self.assertEqual(self._pm(site), self.pm_a.pk, site.project_id)
            self.assertEqual(SitePmAssignment.objects.filter(site=site).count(), 1)
            for task in self.open_tasks[site.pk]:
                task.refresh_from_db()
                self.assertEqual(task.assigned_to_id, self.pm_a.pk)
        self.assertEqual(self._live_pm_step(self.bill_active).assignee_id, self.pm_a.pk)
        self.assertEqual(self._live_pm_step(self.bill_waiting).assignee_id, self.pm_a.pk)


# ---------------------------------------------------------------------------
# VERIFY 1 and 7 — the bulk handover
# ---------------------------------------------------------------------------

@mock.patch('projects.approval_notices.send_notification')
@mock.patch('projects.site_pm.send_notification')
class BulkHandoverTests(HandoverFixture):

    def _hand_over_all(self):
        with self.captureOnCommitCallbacks(execute=True):
            return self._post_bulk([self.s1, self.s2, self.s3])

    def test_every_site_task_and_bill_step_moves(self, _summary, _per_step):
        response = self._hand_over_all()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-testid="handover-result"')
        for site in (self.s1, self.s2, self.s3):
            self.assertEqual(self._pm(site), self.pm_b.pk)
            for task in self.open_tasks[site.pk]:
                task.refresh_from_db()
                self.assertEqual(task.assigned_to_id, self.pm_b.pk)
        self.assertEqual(Task.objects.filter(status='Done', assigned_to=self.pm_a).count(), 3)
        self.assertEqual(self._live_pm_step(self.bill_active).assignee_id, self.pm_b.pk)
        self.assertEqual(self._live_pm_step(self.bill_waiting).assignee_id, self.pm_b.pk)
        # Not the outgoing PM's step, and not a live step: neither moves.
        self.assertEqual(self._live_pm_step(self.bill_third_pm).assignee_id, self.pm_c.pk)
        self.assertEqual(self._pm(self.other), self.pm_c.pk)
        self.assertEqual(self._pm(self.cancelled), self.pm_a.pk)

    def test_the_superseded_step_records_the_admin_and_the_reason(self, _summary, _per_step):
        self._hand_over_all()
        old = ApprovalStep.objects.get(request=self.bill_active, party=APPROVAL_PARTY_PM,
                                       verdict='superseded')
        self.assertEqual((old.assignee_id, old.superseded_by_id, old.note),
                         (self.pm_a.pk, self.admin.pk, REASON))

    def test_one_notice_to_each_pm_and_none_per_step(self, summary, per_step):
        with mock.patch('projects.notify.send_notification') as pair:
            self._hand_over_all()
        per_step.assert_not_called()
        # T3c: the PM pair goes through notify_now() — one in-app and one email call per
        # PM — so it is read from notify's send_notification; `summary` sees SCM only.
        by_recipient = _pm_pair(pair)
        self.assertEqual(set(by_recipient), {self.pm_a.pk, self.pm_b.pk})
        incoming = by_recipient[self.pm_b.pk]
        self.assertEqual([c[0] for c in incoming], ['in_app', 'email'])
        self.assertIn('3 sites in MPUVNL Test', incoming[0][1])
        self.assertIn('6 open tasks and 2 bill approval steps moved to you', incoming[0][1])
        outgoing = by_recipient[self.pm_a.pk]
        self.assertEqual([c[0] for c in outgoing], ['in_app', 'email'])
        self.assertIn('4 material approval steps still name you', outgoing[0][1])
        self.assertNotIn(self.admin.pk, {c.args[0].pk for c in summary.call_args_list})

    def test_scm_gets_one_in_app_notice_each_when_material_steps_remain(self, summary, _ps):
        self._hand_over_all()
        scm_calls = [c for c in summary.call_args_list
                     if c.args[0].pk in (self.scm.pk, self.scm_2.pk)]
        self.assertEqual(sorted(c.args[0].pk for c in scm_calls),
                         sorted([self.scm.pk, self.scm_2.pk]))
        for call in scm_calls:
            self.assertEqual(call.kwargs['channels'], ['in_app'])
            self.assertIn('4 material approval steps still name', call.args[1])

    def test_no_scm_notice_when_no_material_step_remains(self, summary, _per_step):
        # pm_c holds HOS010 and no material step.
        with mock.patch('projects.notify.send_notification') as pair:
            with self.captureOnCommitCallbacks(execute=True):
                response = self._post_bulk([self.other], from_pm=self.pm_c)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._pm(self.other), self.pm_b.pk)
        # T3c: the PM pair is read from notify's send_notification, SCM from `summary`.
        recipients = set(_pm_pair(pair)) | {c.args[0].pk for c in summary.call_args_list}
        self.assertEqual(recipients, {self.pm_b.pk, self.pm_c.pk})

    def test_material_steps_are_listed_tagged_and_not_moved(self, _summary, _per_step):
        response = self._hand_over_all()
        self.assertContains(response, 'data-testid="handover-material"')
        for approval in (self.mat_site, self.mat_program, self.mat_group, self.mat_none):
            self.assertContains(response, f'#{approval.pk} {approval.title}')
            self.assertEqual(self._live_pm_step(approval).assignee_id, self.pm_a.pk)
        self.assertContains(response, 'names HOS001')
        self.assertContains(response, 'names MPUVNL Test')
        self.assertContains(response, 'group Cluster East holds a handed-over site')

    def test_a_bill_waiting_for_changes_is_listed(self, _summary, _per_step):
        response = self._hand_over_all()
        self.assertContains(response, 'data-testid="handover-returning-bills"')
        self.assertContains(response, f'#{self.bill_returning.pk} ')
        self.assertContains(
            response, f'On resubmit the PM step goes to {_name(self.pm_a)} unless SCM '
                      f'changes the approver.')

    def test_one_log_line_per_site_with_both_counts(self, _summary, _per_step):
        self._hand_over_all()
        line = ActivityLog.objects.filter(project=self.s1, action_code='pm_assigned') \
            .order_by('-pk').first()
        self.assertIn('2 open tasks moved, 1 bill approval step moved', line.action)

    def test_the_preview_offers_live_sites_and_skips_cancelled(self, _summary, _per_step):
        response = _client(self.admin).get(reverse('admin_pm_handover'), {
            'program': self.program.pk, 'from_pm': self.pm_a.pk, 'to_pm': self.pm_b.pk})
        self.assertContains(response, 'data-testid="handover-preview"')
        for site in (self.s1, self.s2, self.s3):
            self.assertContains(response, f'name="site_ids" value="{site.pk}"')
        self.assertNotContains(response, f'name="site_ids" value="{self.cancelled.pk}"')
        self.assertContains(response, 'data-testid="handover-skipped"')
        self.assertContains(response, "Select all of Ho_Pm_A Test's sites in MPUVNL Test")
        self.assertContains(response, 'data-tasks="2" data-steps="1"', count=2)
        self.assertContains(response, '(1 waiting on the Site Engineer)')
        self.assertContains(response, 'All <span class="hoN">0</span> move, or none do.')

    def test_only_admin_reaches_the_page(self, _summary, _per_step):
        for profile in (self.pm_a, self.scm, self.se):
            response = _client(profile).get(reverse('admin_pm_handover'))
            self.assertEqual(response.status_code, 403, profile.role)


# ---------------------------------------------------------------------------
# VERIFY 2 — the single-site Change PM is a one-site handover
# ---------------------------------------------------------------------------

@mock.patch('projects.views.send_notification')
@mock.patch('projects.approval_notices.send_notification')
@mock.patch('projects.site_pm.send_notification')
class SingleSiteTests(HandoverFixture):

    def _change(self, site, expected, to_pm=None):
        with self.captureOnCommitCallbacks(execute=True):
            return _client(self.admin).post(
                reverse('admin_assign_pm', args=[site.project_id]),
                {'pm_user_id': (to_pm or self.pm_b).pk, 'reason': REASON,
                 'expected_pm_id': expected.pk if expected else ''})

    def test_bill_steps_move_and_the_pm6_pair_replaces_assign_project(
            self, summary, per_step, assign_project):
        with mock.patch('projects.notify.send_notification') as pair:
            response = self._change(self.s1, self.pm_a)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._pm(self.s1), self.pm_b.pk)
        self.assertEqual(self._live_pm_step(self.bill_active).assignee_id, self.pm_b.pk)
        assign_project.assert_not_called()
        per_step.assert_not_called()
        # T3c: the PM pair goes through notify_now(), in-app then email per PM.
        by_recipient = _pm_pair(pair)
        self.assertEqual({pk: [c[0] for c in calls] for pk, calls in by_recipient.items()},
                         {self.pm_a.pk: ['in_app', 'email'],
                          self.pm_b.pk: ['in_app', 'email']})
        _channel, message, link = by_recipient[self.pm_b.pk][0]
        self.assertIn('handed over HOS001 to you', message)
        self.assertEqual(link, reverse('project_overview', args=['HOS001']))

    def test_the_message_lists_what_did_not_move(self, _summary, _per_step, _assign):
        response = self._change(self.s1, self.pm_a)
        text = ' '.join(str(m) for m in response.wsgi_request._messages)
        self.assertIn('2 open tasks moved, 1 bill approval step moved', text)
        self.assertIn('4 material approval steps still name', text)
        self.assertIn(f'#{self.mat_site.pk} Modules for HOS001 (names HOS001)', text)
        self.assertIn(f'Bill #{self.bill_returning.pk}', text)
        self.assertIn('On resubmit the PM step goes to', text)

    def test_a_first_assignment_still_sends_assign_project(self, summary, _ps, assign_project):
        empty = Project.objects.create(
            customer_name='Empty', project_id='HOS020', customer_phone='9876543210',
            site_address='1 Sun Road', city='Bhopal', state='Madhya Pradesh',
            project_type='OPEX', dc_capacity_kw=Decimal('5.00'), status='Draft')
        with mock.patch('projects.notify.send_notification') as pair:
            response = self._change(empty, None)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._pm(empty), self.pm_b.pk)
        self.assertEqual(assign_project.call_args.kwargs['template'], 'assign_project')
        summary.assert_not_called()
        self.assertEqual(_pm_pair(pair), {})   # T3c: nor through notify

    def test_the_modal_carries_the_shown_pm_and_the_bill_step_count(self, *_mocks):
        response = _client(self.admin).get(reverse('admin_project_list'))
        self.assertContains(response, 'name="expected_pm_id"', count=2)
        self.assertContains(response, 'data-bill-step-count="1"', count=2)   # HOS001, HOS002


# ---------------------------------------------------------------------------
# VERIFY 3 and 4 — stale previews and a failing site: nothing moves
# ---------------------------------------------------------------------------

@mock.patch('projects.views.send_notification')
@mock.patch('projects.approval_notices.send_notification')
@mock.patch('projects.site_pm.send_notification')
class AllOrNothingTests(HandoverFixture):

    def test_a_seat_that_changed_since_the_preview_is_refused(self, summary, per_step, _ap):
        # Somebody gives HOS002 to pm_c after the Admin loaded the preview.
        change_site_pm(self.s2, self.pm_c, by=self.admin, expected_outgoing_pm=self.pm_a,
                       reason='Meanwhile')
        before = SitePmAssignment.objects.count()
        with self.captureOnCommitCallbacks(execute=True):
            response = self._post_bulk([self.s1, self.s2, self.s3])
        self.assertContains(response, 'data-testid="handover-error"')
        self.assertContains(response, 'Nothing was handed over.')
        self.assertContains(response, f'Nothing was handed over. HOS002&#x27;s PM is now '
                                      f'{_name(self.pm_c)}, not {_name(self.pm_a)}')
        self.assertNotContains(response, 'HOS002 failed')   # Q1: the code is not repeated
        self.assertEqual(SitePmAssignment.objects.count(), before)
        self.assertEqual(self._pm(self.s1), self.pm_a.pk)     # HOS001 ran first: rolled back
        self.assertEqual(self._pm(self.s2), self.pm_c.pk)
        self.assertEqual(self._live_pm_step(self.bill_active).assignee_id, self.pm_a.pk)
        summary.assert_not_called()
        per_step.assert_not_called()

    def test_a_single_site_change_against_a_stale_form_is_refused(self, summary, _ps, _ap):
        change_site_pm(self.s1, self.pm_c, by=self.admin, expected_outgoing_pm=self.pm_a,
                       reason='Meanwhile')
        response = _client(self.admin).post(
            reverse('admin_assign_pm', args=['HOS001']),
            {'pm_user_id': self.pm_b.pk, 'reason': REASON, 'expected_pm_id': self.pm_a.pk})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._pm(self.s1), self.pm_c.pk)
        summary.assert_not_called()

    def test_an_assign_form_on_a_seat_filled_meanwhile_is_refused(self, *_mocks):
        with self.assertRaises(SitePmChangeRefused):
            change_site_pm(self.s1, self.pm_b, by=self.admin, expected_outgoing_pm=None)
        self.assertEqual(self._pm(self.s1), self.pm_a.pk)

    def test_one_failing_site_mid_bulk_moves_nothing_and_is_named(self, summary, _ps, _ap):
        # HOS009 is Cancelled; posted anyway (a tampered or stale form), it sorts after
        # HOS001..HOS003, so three sites are written before it fails.
        with self.captureOnCommitCallbacks(execute=True):
            response = self._post_bulk([self.s1, self.s2, self.s3, self.cancelled])
        self.assertContains(response,
                            'Nothing was handed over. HOS009 is Cancelled')
        self._assert_nothing_moved()
        summary.assert_not_called()

    def test_a_bill_step_the_rules_refuse_fails_the_whole_handover(self, summary, _ps, _ap):
        # The approval rules refuse the incoming PM on the first bill step (as the
        # eligibility or same-person rule would): the whole handover rolls back.
        with mock.patch('projects.approvals._check_assignees',
                        side_effect=ApprovalRefused('Ho_Pm_B Test cannot be named.')):
            with self.assertLogs('projects.site_pm', level='WARNING') as logs:
                with self.captureOnCommitCallbacks(execute=True):
                    response = self._post_bulk([self.s1, self.s2, self.s3])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response,
                            'Nothing was handed over. HOS001: bill approval')
        self._assert_nothing_moved()
        summary.assert_not_called()
        self.assertEqual(len(logs.records), 1)
        self.assertIn('HOS001 failed', logs.records[0].getMessage())
        self.assertIn('Ho_Pm_B Test cannot be named.', logs.records[0].getMessage())

    def test_an_unexpected_error_on_the_third_site_moves_nothing_and_is_named(
            self, summary, _ps, _ap):
        # A real database error (NOT NULL violation) after HOS001..HOS003 are all written:
        # not a refusal, so it used to escape as a 500. Run on Postgres too, where the
        # error also aborts the transaction until the rollback.
        with mock.patch('projects.site_pm.change_site_pm',
                        side_effect=_db_error_on_call(3)):
            with self.assertLogs('projects.site_pm', level='ERROR') as logs:
                with self.captureOnCommitCallbacks(execute=True):
                    response = self._post_bulk([self.s1, self.s2, self.s3])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-testid="handover-error"')
        self.assertContains(
            response, 'Nothing was handed over. HOS003 failed: unexpected error (logged)')
        self.assertNotContains(response, 'projects_activitylog')   # detail stays in the log
        self._assert_nothing_moved()
        summary.assert_not_called()
        self.assertEqual(len(logs.records), 1)
        self.assertIn('HOS003', logs.records[0].getMessage())
        self.assertIs(logs.records[0].exc_info[0], IntegrityError)

    def test_a_single_site_change_with_an_unexpected_error_is_named(self, summary, _ps, _ap):
        with mock.patch('projects.site_pm.change_site_pm',
                        side_effect=_db_error_on_call(1)):
            with self.assertLogs('projects.site_pm', level='ERROR') as logs:
                response = _client(self.admin).post(
                    reverse('admin_assign_pm', args=['HOS001']),
                    {'pm_user_id': self.pm_b.pk, 'reason': REASON,
                     'expected_pm_id': self.pm_a.pk})
        self.assertEqual(response.status_code, 302)
        text = ' '.join(str(m) for m in response.wsgi_request._messages)
        self.assertIn('Nothing was handed over. HOS001 failed: unexpected error (logged)',
                      text)
        self._assert_nothing_moved()
        summary.assert_not_called()
        self.assertIs(logs.records[0].exc_info[0], IntegrityError)
        self.assertIn('HOS001', logs.records[0].getMessage())

    def test_a_single_site_change_whose_bill_step_is_refused_is_named(self, summary, _ps, _ap):
        with mock.patch('projects.approvals._check_assignees',
                        side_effect=ApprovalRefused('Ho_Pm_B Test cannot be named.')):
            with self.assertLogs('projects.site_pm', level='WARNING') as logs:
                response = _client(self.admin).post(
                    reverse('admin_assign_pm', args=['HOS001']),
                    {'pm_user_id': self.pm_b.pk, 'reason': REASON,
                     'expected_pm_id': self.pm_a.pk})
        self.assertEqual(response.status_code, 302)
        text = ' '.join(str(m) for m in response.wsgi_request._messages)
        self.assertIn('Nothing was handed over. HOS001: bill approval', text)
        self._assert_nothing_moved()
        summary.assert_not_called()
        self.assertIn('HOS001 failed', logs.records[0].getMessage())

    # -- Q4: the follow-up reads run INSIDE the transaction ---------------------------

    def test_a_failing_material_read_moves_nothing(self, summary, _ps, _ap):
        # Fails inside hand_over_sites only (the view's preview has its own reference),
        # so the preview re-renders under the error.
        with mock.patch('projects.site_pm.material_steps_held_by',
                        side_effect=RuntimeError('material read broke')):
            with self.assertLogs('projects.site_pm', level='ERROR') as logs:
                with self.captureOnCommitCallbacks(execute=True):
                    response = self._post_bulk([self.s1, self.s2, self.s3])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Nothing was handed over: unexpected error (logged)')
        self.assertContains(response, 'data-testid="handover-preview"')
        self.assertNotContains(response, 'material read broke')
        self._assert_nothing_moved()
        summary.assert_not_called()
        self.assertIs(logs.records[0].exc_info[0], RuntimeError)

    def test_a_returning_bill_read_that_keeps_failing_still_renders(self, summary, _ps, _ap):
        # Fails in the handover AND in the preview reload: the page falls back to the
        # choose stage with the error, not a 500.
        broken = mock.Mock(side_effect=RuntimeError('returning read broke'))
        with mock.patch('projects.site_pm.returning_bills_for', broken), \
                mock.patch('projects.views.returning_bills_for', broken):
            with self.assertLogs('projects', level='ERROR') as logs:
                with self.captureOnCommitCallbacks(execute=True):
                    response = self._post_bulk([self.s1, self.s2, self.s3])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Nothing was handed over: unexpected error (logged)')
        self.assertContains(response, 'data-testid="handover-choose"')
        self.assertNotContains(response, 'data-testid="handover-preview"')
        self._assert_nothing_moved()
        summary.assert_not_called()
        self.assertEqual(sorted(r.name for r in logs.records),
                         ['projects.site_pm', 'projects.views'])

    def test_a_failing_read_on_a_single_site_change_moves_nothing(self, summary, _ps, _ap):
        with mock.patch('projects.site_pm.material_steps_held_by',
                        side_effect=RuntimeError('material read broke')):
            with self.assertLogs('projects.site_pm', level='ERROR'):
                response = _client(self.admin).post(
                    reverse('admin_assign_pm', args=['HOS001']),
                    {'pm_user_id': self.pm_b.pk, 'reason': REASON,
                     'expected_pm_id': self.pm_a.pk})
        self.assertEqual(response.status_code, 302)
        text = ' '.join(str(m) for m in response.wsgi_request._messages)
        self.assertIn('Nothing was handed over: unexpected error (logged)', text)
        self._assert_nothing_moved()
        summary.assert_not_called()

    def test_the_site_code_prefix_is_skipped_only_for_that_exact_code(self, *_mocks):
        site = Project(project_id='MB001')
        self.assertEqual(_failed_line(site, 'MB001 is Cancelled'), 'MB001 is Cancelled')
        self.assertEqual(_failed_line(site, "MB001's PM is now X"), "MB001's PM is now X")
        self.assertEqual(_failed_line(site, 'MB0010 is Cancelled'),
                         'MB001 failed: MB0010 is Cancelled')
        self.assertEqual(_failed_line(site, 'unexpected error (logged)'),
                         'MB001 failed: unexpected error (logged)')

    def test_reason_and_a_different_pm_are_required(self, *_mocks):
        with self.assertRaises(SitePmChangeRefused):
            hand_over_sites([self.s1], self.pm_a, self.pm_b, self.admin, '  ')
        with self.assertRaises(SitePmChangeRefused):
            hand_over_sites([self.s1], self.pm_a, self.pm_a, self.admin, REASON)
        self._assert_nothing_moved()


# ---------------------------------------------------------------------------
# VERIFY 5 — a failed log line never loses the handover (Q4)
# ---------------------------------------------------------------------------

@mock.patch('projects.site_pm.send_notification')
class LogFailureTests(HandoverFixture):

    def test_a_database_error_in_the_log_line_leaves_the_handover_committed(self, _notify):
        def failing_log(project, actor, action, **kwargs):
            # A real failing INSERT: action is NOT NULL. log_activity swallows the error;
            # on Postgres the statement has already aborted the transaction, which only
            # the savepoint around it in change_site_pm() can recover.
            log_activity(project, actor, None, **kwargs)

        logs_before = ActivityLog.objects.filter(action_code='pm_assigned').count()
        with mock.patch('projects.site_pm.log_activity', side_effect=failing_log):
            handover = hand_over_sites([self.s1, self.s2, self.s3], self.pm_a, self.pm_b,
                                       self.admin, REASON, program=self.program)
        self.assertEqual(handover.tasks_moved, 6)
        self.assertEqual(handover.bill_steps_moved, 2)
        for site in (self.s1, self.s2, self.s3):
            self.assertEqual(self._pm(site), self.pm_b.pk)
        self.assertEqual(ActivityLog.objects.filter(action_code='pm_assigned').count(),
                         logs_before)


# ---------------------------------------------------------------------------
# VERIFY 8 — a step still waiting on the Site Engineer
# ---------------------------------------------------------------------------

@mock.patch('projects.site_pm.send_notification')
class WaitingStepTests(HandoverFixture):

    def test_moved_silently_then_announced_when_the_site_engineer_approves(self, _summary):
        # T3c: the approval E1 is emailed, so it goes through notify_now() and is read
        # from notify's send_notification, filtered to its label (the handover's own PM
        # pair goes through the same function).
        def asked(calls):
            return {c.kwargs['recipient'].pk for c in calls.call_args_list
                    if c.kwargs.get('template') == 'approval_step_activated'}

        with mock.patch('projects.approval_notices.send_notification') as notices, \
                mock.patch('projects.notify.send_notification') as via_notify:
            with self.captureOnCommitCallbacks(execute=True):
                handover = hand_over_sites([self.s2], self.pm_a, self.pm_b, self.admin,
                                           REASON, program=self.program)
            self.assertEqual((handover.bill_steps_moved, handover.bill_steps_waiting), (1, 1))
            notices.assert_not_called()
            self.assertEqual(asked(via_notify), set())
            step = self._live_pm_step(self.bill_waiting)
            self.assertEqual(step.assignee_id, self.pm_b.pk)
            self.assertIsNone(step.activated_at)

            with self.captureOnCommitCallbacks(execute=True):
                apply_approval_decision(
                    self._step(self.bill_waiting, APPROVAL_PARTY_SITE_ENGINEER),
                    'approved', self.se, files=[dict(SE_PHOTO)])
        step = self._live_pm_step(self.bill_waiting)
        self.assertIsNotNone(step.activated_at)
        recipients = asked(via_notify)
        self.assertIn(self.pm_b.pk, recipients)
        self.assertNotIn(self.pm_a.pk, recipients)


# ---------------------------------------------------------------------------
# hand_over_pm_step() — the SCM gate is the only rule it skips
# ---------------------------------------------------------------------------

class HandOverPmStepTests(HandoverFixture):

    def test_only_an_admin_may_use_it(self):
        step = self._live_pm_step(self.bill_active)
        for actor in (self.scm, self.pm_a, None):
            with self.assertRaises(ApprovalRefused):
                hand_over_pm_step(step, self.pm_a, self.pm_b, actor, REASON)
        self.assertEqual(self._live_pm_step(self.bill_active).assignee_id, self.pm_a.pk)

    def test_only_a_pm_step_with_a_reason(self):
        se_step = self._step(self.bill_waiting, APPROVAL_PARTY_SITE_ENGINEER)
        with self.assertRaises(ApprovalRefused):
            hand_over_pm_step(se_step, self.se, self.pm_b, self.admin, REASON)
        with self.assertRaises(ApprovalRefused):
            hand_over_pm_step(self._live_pm_step(self.bill_active), self.pm_a, self.pm_b,
                              self.admin, '  ')

    def test_the_assignee_rules_still_hold(self):
        step = self._live_pm_step(self.bill_active)
        for not_a_pm in (self.se, self.scm):            # eligibility: an active PM only
            with self.assertRaises(ApprovalRefused):
                hand_over_pm_step(step, self.pm_a, not_a_pm, self.admin, REASON)
        self.pm_b.is_active = False
        self.pm_b.save()
        with self.assertRaises(ApprovalRefused):
            hand_over_pm_step(step, self.pm_a, self.pm_b, self.admin, REASON)
        self.assertEqual(self._live_pm_step(self.bill_active).assignee_id, self.pm_a.pk)

    def test_a_step_no_longer_held_by_the_outgoing_pm_is_left_alone(self):
        step = self._live_pm_step(self.bill_third_pm)    # pm_c's
        self.assertIsNone(hand_over_pm_step(step, self.pm_a, self.pm_b, self.admin, REASON))
        self.assertEqual(self._live_pm_step(self.bill_third_pm).assignee_id, self.pm_c.pk)
