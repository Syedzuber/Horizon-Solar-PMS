"""
StatusTransition — the state ledger. Prompt 0.3.

WHY THIS FILE EXISTS
--------------------
`ActivityLog` records that things happened. It does not record how they got there:
no from-status, no to-status, no reason, and no actor-role-at-the-time anywhere in
the codebase before this session. Stage dwell time between two arbitrary states was
unanswerable, and so was "who moved this, and why".

This file asserts the four properties that make the ledger worth having, because
each of them is quietly destroyable by a later edit that looks like an improvement:

  1. THE ROLE IS A COPY, NOT A JOIN. A user who changes role next year must not
     rewrite last year's history. Someone will eventually notice `actor_role_code`
     duplicates `actor.role` and "normalise" it away.
  2. THE TABLE IS APPEND-ONLY (R-4). A correction is a new row.
  3. THE HELPER RAISES, IT NEVER SWALLOWS. `log_activity()` catches bare
     `Exception`; the two are allowed to fail differently and must (R-3). Someone
     will eventually "harmonise" them.
  4. THE ROW AND ITS STATUS CHANGE COMMIT TOGETHER. A transition with no status
     change is a history of something that never happened; a status change with no
     transition is a gap indistinguishable from "we never instrumented this path".

Everything after that is coverage: one real status change, through one real view,
per instrumented subject type — plus the dwell-time query that was the point.

WHAT IS NOT COVERED HERE, AND WHY
---------------------------------
`DesignAssignment` / `DesignAttempt` (fourteen statuses) and `PaymentRequest` are
NOT instrumented — see docs/execution-model.md §13. Their absence from this file is
deliberate and documented, not an oversight.

Run with:
    python manage.py test projects --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import transaction
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    BOQ, BOQItem, BOQItemMaster, DCLineItem, DeliveryChallan, Issue,
    PaymentMilestone, Project, StatusTransition, Task, UserProfile,
    ACTOR_ROLE_SYSTEM, AppendOnlyViolation, REASON_BLOCKED, REASON_CREATED,
    REASON_GRN_CONFIRMED, REASON_MILESTONE_SYNC, SUBJECT_BOQ,
    SUBJECT_DELIVERY_CHALLAN, SUBJECT_ISSUE, SUBJECT_PAYMENT_MILESTONE,
    SUBJECT_PROJECT, SUBJECT_TASK,
)
from .utils import (
    RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL, assign_tasks_to, record_transition,
)


def _profile(username, role, email=''):
    """Create a User and set its auto-created UserProfile's role.

    A post_save signal on User creates the UserProfile, so we fetch and mutate
    rather than creating a second one. Same helper shape as the baseline suite.
    """
    user = User.objects.create_user(username=username, password='pw12345', email=email)
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.save()
    return profile


def _client_for(profile):
    client = Client()
    client.force_login(profile.user)
    return client


class LedgerFixture(TestCase):
    """One activated Residential project with a full cast.

    Activation runs THROUGH THE REAL VIEW, which means the fixture itself already
    exercises the Project → Active instrumentation before a single test body runs.
    """

    def setUp(self):
        # Activation raises and rolls back without this account.
        self.finance = _profile('st_fin', 'Finance',
                                email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)
        self.pm      = _profile('st_pm',     'PM')
        self.se      = _profile('st_se',     'Site Engineer')
        self.design  = _profile('st_design', 'Design')
        self.scm     = _profile('st_scm',    'SCM')

        # Migrations are disabled under test_settings, so the catalogue data
        # migration never runs and get_standard_boq_items() would raise on an empty
        # catalogue. Three rows are enough to author and submit a BOQ for real.
        for order, (code, desc, cat, unit) in enumerate([
            ('ITM-001', 'Solar Module 540Wp',        'Solar Modules', 'Nos'),
            ('ITM-002', 'Module Mounting Structure', 'Structure',     'Nos'),
            ('ITM-003', 'String Inverter 5kW',       'Inverter',      'Nos'),
        ], start=1):
            BOQItemMaster.objects.create(
                code=code, description=desc, category=cat, unit=unit,
                project_type='Residential', is_active=True, sort_order=order,
            )

        self.project = Project.objects.create(
            customer_name='Ledger Residence',
            customer_phone='9876543210',
            site_address='1 Sun Road',
            city='Lucknow',
            project_type='Residential',
            capacity_kw=Decimal('5.00'),
            contract_value=Decimal('300000.00'),
            status='Draft',
            assigned_pm=self.pm,
            target_commissioning_date=date.today() + timedelta(days=90),
        )
        response = _client_for(self.pm).post(
            reverse('project_activate', args=[self.project.project_id]),
            {'assigned_design_id': self.design.pk},
        )
        self.assertEqual(response.status_code, 302, 'activation did not redirect')
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, 'Active')

        # Activation deliberately leaves every SE task unassigned — hand this
        # project's engineer their real tasks, as a PM does through task_assign.
        assign_tasks_to(
            Task.objects.filter(phase__project=self.project,
                                assigned_role=Task.SITE_ENGINEER),
            self.se,
        )

    # -- helpers -------------------------------------------------------------

    def _task(self, task_name):
        return Task.objects.get(phase__project=self.project, task_name=task_name)

    def _rows(self, subject_type, subject):
        """This subject's ledger, oldest first — the shape every read here uses."""
        return list(StatusTransition.objects.filter(
            subject_type=subject_type, subject_id=subject.pk,
        ).order_by('occurred_at', 'pk'))

    def _make_dc(self, dc_number='DC-001', ordered=Decimal('10.00')):
        challan = DeliveryChallan.objects.create(
            project=self.project, dc_number=dc_number, dc_date=date.today(),
            status=DeliveryChallan.EXPECTED, created_by=self.scm,
        )
        DCLineItem.objects.create(
            challan=challan, boq_category='Solar Modules',
            item_description='Solar Module 540Wp', ordered_quantity=ordered, unit='Nos',
        )
        return challan

    def _seed_boq(self, quantity=Decimal('10.00')):
        boq = BOQ.objects.create(project=self.project)
        for index, master in enumerate(BOQItemMaster.objects.filter(
                project_type='Residential').order_by('sort_order'), start=1):
            BOQItem.objects.create(
                boq=boq, serial_no=index, category=master.category,
                description=master.description, uom=master.unit,
                item_master=master, boq_quantity=quantity,
            )
        return boq


# ---------------------------------------------------------------------------
# 1 — The helper's contract
# ---------------------------------------------------------------------------

class RecordTransitionContractTests(LedgerFixture):
    """What record_transition() writes, and what it refuses to do."""

    def test_it_writes_a_row_with_every_expected_field(self):
        task = self._task('MMS Installation')
        row = record_transition(
            task, to_status=Task.IN_PROGRESS, from_status=Task.NOT_STARTED,
            actor=self.se, reason_code=REASON_BLOCKED, remark='scaffolding late',
        )

        row.refresh_from_db()
        self.assertEqual(row.subject_type, SUBJECT_TASK)
        self.assertEqual(row.subject_id, task.pk)
        self.assertEqual(row.from_status, Task.NOT_STARTED)
        self.assertEqual(row.to_status, Task.IN_PROGRESS)
        self.assertEqual(row.actor, self.se)
        self.assertEqual(row.actor_role_code, 'Site Engineer')
        self.assertEqual(row.reason_code, REASON_BLOCKED)
        self.assertEqual(row.remark, 'scaffolding late')
        self.assertIsNone(row.client_uuid)
        self.assertIsNotNone(row.occurred_at)

    def test_subject_type_is_derived_from_the_model_not_from_the_caller(self):
        """Six models, six subject types, and no string ever passed in. A typo in a
        caller-supplied string would be indistinguishable from a subject type that
        genuinely has no rows yet."""
        pairs = [
            (self.project,                                    SUBJECT_PROJECT),
            (self._task('MMS Installation'),                  SUBJECT_TASK),
            (self._seed_boq(),                                SUBJECT_BOQ),
            (self._make_dc(),                                 SUBJECT_DELIVERY_CHALLAN),
            (self.project.milestones.get(milestone_name='M1'), SUBJECT_PAYMENT_MILESTONE),
        ]
        for subject, expected in pairs:
            with self.subTest(subject=type(subject).__name__):
                row = record_transition(subject, to_status='X', actor=self.pm)
                self.assertEqual(row.subject_type, expected)

    def test_the_project_is_resolved_for_every_subject_type(self):
        """Denormalised so one project's whole history is one query, not a six-way
        union. Task is the odd one — it has no project FK and goes via its phase."""
        for subject in (self._task('MMS Installation'), self._seed_boq(),
                        self._make_dc(),
                        self.project.milestones.get(milestone_name='M1')):
            with self.subTest(subject=type(subject).__name__):
                row = record_transition(subject, to_status='X', actor=self.pm)
                self.assertEqual(row.project, self.project)

    def test_a_missing_actor_records_the_system_role_rather_than_blank(self):
        """The Zoho webhook creates projects with no request user at all.
        actor_role_code is required, so 'nobody' needs a spelling of its own."""
        row = record_transition(self.project, to_status='Active', actor=None)
        self.assertIsNone(row.actor)
        self.assertEqual(row.actor_role_code, ACTOR_ROLE_SYSTEM)

    def test_an_unregistered_subject_type_is_refused(self):
        with self.assertRaises(ValueError):
            record_transition(self.pm, to_status='Anything', actor=self.pm)

    def test_to_status_is_required(self):
        with self.assertRaises(ValueError):
            record_transition(self.project, to_status='', actor=self.pm)


class ActorRoleIsACopyTests(LedgerFixture):
    """The single most destroyable property in this table."""

    def test_changing_the_users_role_afterwards_does_not_rewrite_history(self):
        task = self._task('MMS Installation')
        row = record_transition(task, to_status=Task.IN_PROGRESS,
                                from_status=Task.NOT_STARTED, actor=self.se)
        self.assertEqual(row.actor_role_code, 'Site Engineer')

        # The engineer is promoted. Everything they did as an engineer must still
        # read as having been done by an engineer.
        self.se.role = 'PM'
        self.se.save(update_fields=['role'])

        row.refresh_from_db()
        self.assertEqual(
            row.actor_role_code, 'Site Engineer',
            'actor_role_code is a COPY taken at write time. If this now says "PM", '
            'somebody has replaced the copy with a join to actor.role and every '
            'historical row in the system now lies about who did what.',
        )
        # The FK still points at the live user — the two answer different questions.
        self.assertEqual(row.actor, self.se)
        self.assertEqual(row.actor.role, 'PM')

    def test_a_deleted_user_does_not_erase_the_row(self):
        """SET_NULL, matching ActivityLog.actor. The role survives the user."""
        temp = _profile('st_temp', 'Site Engineer')
        row = record_transition(self.project, to_status='Active', actor=temp)
        temp_user = temp.user
        temp_user.delete()          # cascades to the UserProfile

        row.refresh_from_db()
        self.assertIsNone(row.actor)
        self.assertEqual(row.actor_role_code, 'Site Engineer')


# ---------------------------------------------------------------------------
# 2 — Append-only (R-4)
# ---------------------------------------------------------------------------

class AppendOnlyTests(LedgerFixture):

    def test_saving_an_existing_row_raises(self):
        row = record_transition(self.project, to_status='Active', actor=self.pm)
        row.remark = 'actually it was something else'
        with self.assertRaises(AppendOnlyViolation):
            row.save()

        row.refresh_from_db()
        self.assertEqual(row.remark, '')

    def test_deleting_a_row_raises(self):
        row = record_transition(self.project, to_status='Active', actor=self.pm)
        with self.assertRaises(AppendOnlyViolation):
            row.delete()
        self.assertTrue(StatusTransition.objects.filter(pk=row.pk).exists())

    def test_a_correction_is_a_new_row(self):
        """R-4's positive half: the table is not read-only, it is append-only."""
        task = self._task('MMS Installation')
        record_transition(task, to_status=Task.DONE, from_status=Task.NOT_STARTED,
                          actor=self.se)
        record_transition(task, to_status=Task.IN_PROGRESS, from_status=Task.DONE,
                          actor=self.pm, remark='marked Done in error')

        rows = self._rows(SUBJECT_TASK, task)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1].to_status, Task.IN_PROGRESS)
        self.assertEqual(rows[-1].remark, 'marked Done in error')

    def test_the_model_is_not_registered_in_django_admin(self):
        """The admin's change form is an UPDATE, which would walk straight past
        save()'s guard on a page built for exactly that purpose."""
        from django.contrib import admin
        self.assertNotIn(StatusTransition, admin.site._registry)


# ---------------------------------------------------------------------------
# 3 — It raises. It never swallows. (R-2, R-3)
# ---------------------------------------------------------------------------

class ExceptionsPropagateTests(LedgerFixture):
    """The whole difference from log_activity(), which catches bare Exception."""

    def test_a_database_failure_inside_the_helper_propagates(self):
        with patch.object(StatusTransition, 'save',
                          side_effect=RuntimeError('database is on fire')):
            with self.assertRaises(RuntimeError):
                record_transition(self.project, to_status='Active', actor=self.pm)

    def test_log_activity_swallows_where_record_transition_does_not(self):
        """Asserted side by side on purpose. The feed and the ledger are different
        things (R-3) and are ALLOWED to fail differently. If this test starts
        failing because log_activity() now raises, that is a deliberate change to
        ActivityLog and belongs in its own session — but if it fails because
        record_transition() stopped raising, the ledger has silently become lossy."""
        from .models import ActivityLog, log_activity

        with patch.object(ActivityLog.objects, 'create',
                          side_effect=RuntimeError('database is on fire')):
            log_activity(self.project, self.pm, 'anything')   # swallowed, no raise

        with patch.object(StatusTransition, 'save',
                          side_effect=RuntimeError('database is on fire')):
            with self.assertRaises(RuntimeError):
                record_transition(self.project, to_status='Active', actor=self.pm)


# ---------------------------------------------------------------------------
# 4 — The atomic guarantee: the row and its status change commit together
# ---------------------------------------------------------------------------

class AtomicWithTheStatusChangeTests(LedgerFixture):
    """The most important tests in this file.

    A transition row without its status change is a history of something that never
    happened. A status change without its row is a gap indistinguishable from "we
    never instrumented this path". Neither may survive a failure.
    """

    def test_a_failure_after_the_row_rolls_the_row_back_with_the_status(self):
        task = self._task('MMS Installation')

        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                Task.objects.filter(pk=task.pk).update(status=Task.DONE)
                record_transition(task, to_status=Task.DONE,
                                  from_status=Task.NOT_STARTED, actor=self.se)
                raise RuntimeError('something downstream blew up')

        task.refresh_from_db()
        self.assertEqual(task.status, Task.NOT_STARTED)
        self.assertEqual(self._rows(SUBJECT_TASK, task), [],
                         'an orphan transition row survived a rolled-back status change')

    def test_a_ledger_failure_in_a_real_view_leaves_the_status_unchanged(self):
        """Driven through the real endpoint. If record_transition() cannot write,
        the task must NOT move — a status change nobody can account for is exactly
        what this table exists to prevent."""
        task = self._task('MMS Installation')
        Task.objects.filter(pk=task.pk).update(due_date=date.today() + timedelta(days=3))
        url = reverse('task_status_update', args=[self.project.project_id, task.pk])

        with patch('projects.views.record_transition',
                   side_effect=RuntimeError('ledger unavailable')):
            with self.assertRaises(RuntimeError):
                _client_for(self.se).post(url, {'status': Task.IN_PROGRESS})

        task.refresh_from_db()
        self.assertEqual(task.status, Task.NOT_STARTED)
        self.assertEqual(self._rows(SUBJECT_TASK, task), [])

    def test_the_happy_path_through_the_same_view_writes_both(self):
        """The control for the test above — same view, nothing patched."""
        task = self._task('MMS Installation')
        Task.objects.filter(pk=task.pk).update(due_date=date.today() + timedelta(days=3))

        _client_for(self.se).post(
            reverse('task_status_update', args=[self.project.project_id, task.pk]),
            {'status': Task.IN_PROGRESS},
        )
        task.refresh_from_db()
        self.assertEqual(task.status, Task.IN_PROGRESS)
        self.assertEqual(len(self._rows(SUBJECT_TASK, task)), 1)


# ---------------------------------------------------------------------------
# 5 — Idempotency on client_uuid (R-14)
# ---------------------------------------------------------------------------

class ClientUuidReplayTests(LedgerFixture):
    """A queued offline submission from a site engineer will be replayed. A repeat
    is IGNORED — not duplicated, and not an error, because an error would make the
    phone retry forever."""

    def test_a_repeated_client_uuid_writes_one_row_and_does_not_raise(self):
        task = self._task('MMS Installation')
        key = 'b6b0e0e0-0000-4000-8000-000000000001'

        first = record_transition(task, to_status=Task.DONE,
                                  from_status=Task.NOT_STARTED, actor=self.se,
                                  client_uuid=key)
        second = record_transition(task, to_status=Task.DONE,
                                   from_status=Task.NOT_STARTED, actor=self.se,
                                   client_uuid=key)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            StatusTransition.objects.filter(client_uuid=key).count(), 1)

    def test_the_replay_is_ignored_even_when_the_payload_differs(self):
        """The key is the identity. A phone that re-sends with a mangled body must
        not be able to append a second, different history."""
        task = self._task('MMS Installation')
        key = 'b6b0e0e0-0000-4000-8000-000000000002'

        first = record_transition(task, to_status=Task.DONE, actor=self.se,
                                  client_uuid=key, remark='done')
        second = record_transition(task, to_status=Task.BLOCKED, actor=self.pm,
                                   client_uuid=key, remark='no it is not')

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.to_status, Task.DONE)
        self.assertEqual(second.remark, 'done')

    def test_rows_without_a_key_are_never_deduplicated_against_each_other(self):
        """client_uuid is nullable and NULLs are distinct — an unlimited number of
        keyless rows must coexist under the unique index."""
        task = self._task('MMS Installation')
        for _ in range(3):
            record_transition(task, to_status=Task.IN_PROGRESS, actor=self.se)
        self.assertEqual(len(self._rows(SUBJECT_TASK, task)), 3)

    def test_a_replay_does_not_poison_the_callers_transaction(self):
        """The nested atomic() inside the helper is a SAVEPOINT. Without it the
        duplicate-key IntegrityError would abort the OUTER transaction, so
        recovering from a replay would roll back the status change it belongs to."""
        task = self._task('MMS Installation')
        key = 'b6b0e0e0-0000-4000-8000-000000000003'
        record_transition(task, to_status=Task.DONE, actor=self.se, client_uuid=key)

        with transaction.atomic():
            Task.objects.filter(pk=task.pk).update(status=Task.DONE)
            record_transition(task, to_status=Task.DONE, actor=self.se,
                              client_uuid=key)
            # The outer transaction must still be usable after the swallowed
            # duplicate — this write is the proof.
            Task.objects.filter(pk=task.pk).update(completed_at=timezone.now())

        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)
        self.assertIsNotNone(task.completed_at)
        self.assertEqual(StatusTransition.objects.filter(client_uuid=key).count(), 1)


# ---------------------------------------------------------------------------
# 6 — One real status change, through one real view, per subject type
# ---------------------------------------------------------------------------

class InstrumentedSubjectTypeTests(LedgerFixture):
    """Six subject types, six endpoints, six from/to assertions.

    These are the tests that catch an instrumented call site being dropped in a
    later refactor — the failure mode that leaves the ledger silently partial.
    """

    def test_project_activation(self):
        """Written by the fixture's own activation POST, before any test body ran."""
        rows = self._rows(SUBJECT_PROJECT, self.project)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].from_status, 'Draft')
        self.assertEqual(rows[0].to_status, 'Active')
        self.assertEqual(rows[0].actor, self.pm)
        self.assertEqual(rows[0].actor_role_code, 'PM')

    def test_project_creation(self):
        """The other end of the only two states Project.status ever reaches today
        (B-3: there is no closure workflow). Creation has no from-status."""
        response = _client_for(self.pm).post(reverse('project_create'), {
            'customer_name': 'Second Residence',
            'customer_phone': '9876543211',
            'site_address': '2 Sun Road',
            'city': 'Lucknow',
            'state': 'Uttar Pradesh',
            'project_type': 'Residential',
            'capacity_kw': '5.00',
            'contract_value': '300000.00',
            'target_commissioning_date': (date.today() + timedelta(days=90)).isoformat(),
        })
        self.assertEqual(response.status_code, 302)

        created = Project.objects.get(customer_name='Second Residence')
        rows = self._rows(SUBJECT_PROJECT, created)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].from_status, '')
        self.assertEqual(rows[0].to_status, 'Draft')
        self.assertEqual(rows[0].reason_code, REASON_CREATED)

    def test_task_status_update(self):
        task = self._task('MMS Installation')
        Task.objects.filter(pk=task.pk).update(due_date=date.today() + timedelta(days=3))
        url = reverse('task_status_update', args=[self.project.project_id, task.pk])
        client = _client_for(self.se)

        client.post(url, {'status': Task.IN_PROGRESS})
        client.post(url, {'status': Task.DONE})

        rows = self._rows(SUBJECT_TASK, task)
        self.assertEqual([(r.from_status, r.to_status) for r in rows], [
            (Task.NOT_STARTED, Task.IN_PROGRESS),
            (Task.IN_PROGRESS, Task.DONE),
        ])
        self.assertEqual({r.actor for r in rows}, {self.se})
        self.assertEqual({r.project for r in rows}, {self.project})

    def test_task_blocked_captures_the_hold_reason(self):
        """The one task path that collects any free text today. R-9 wants a remark
        on every transition; this is the only place the UI already asks for one."""
        task = self._task('MMS Installation')
        _client_for(self.se).post(
            reverse('task_status_update', args=[self.project.project_id, task.pk]),
            {'status': Task.BLOCKED, 'block_issue_title': 'Crane unavailable'},
        )

        rows = self._rows(SUBJECT_TASK, task)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].to_status, Task.BLOCKED)
        self.assertEqual(rows[0].reason_code, REASON_BLOCKED)
        self.assertEqual(rows[0].remark, 'Crane unavailable')

        # The blocking Issue that the same POST creates gets its own opening row.
        issue = Issue.objects.get(task=task)
        issue_rows = self._rows(SUBJECT_ISSUE, issue)
        self.assertEqual(len(issue_rows), 1)
        self.assertEqual(issue_rows[0].from_status, '')
        self.assertEqual(issue_rows[0].to_status, Issue.OPEN)

    def test_task_detail_status_update_is_instrumented_too(self):
        """The second of the two task status endpoints. Both write to the same
        ledger; instrumenting one and not the other would make dwell time depend on
        which screen the engineer happened to be on."""
        task = self._task('MMS Installation')
        Task.objects.filter(pk=task.pk).update(due_date=date.today() + timedelta(days=3))

        _client_for(self.se).post(
            reverse('task_detail_status_update',
                    args=[self.project.project_id, task.pk]),
            {'status': Task.IN_PROGRESS},
        )
        rows = self._rows(SUBJECT_TASK, task)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].from_status, rows[0].to_status),
                         (Task.NOT_STARTED, Task.IN_PROGRESS))

    def test_boq_submit_and_acknowledge(self):
        boq = self._seed_boq()
        _client_for(self.design).post(
            reverse('boq_submit', args=[self.project.project_id]))
        _client_for(self.scm).post(
            reverse('boq_acknowledge', args=[self.project.project_id]))

        rows = self._rows(SUBJECT_BOQ, boq)
        self.assertEqual([(r.from_status, r.to_status) for r in rows], [
            ('Draft',     'Submitted'),
            ('Submitted', 'Acknowledged'),
        ])
        self.assertEqual(rows[0].actor, self.design)
        self.assertEqual(rows[1].actor, self.scm)
        self.assertEqual(rows[1].actor_role_code, 'SCM')

    def test_boq_revision_request_records_the_pms_stated_reason(self):
        boq = self._seed_boq()
        boq.status = 'Submitted'
        boq.save(update_fields=['status'])

        _client_for(self.pm).post(
            reverse('boq_request_revision', args=[self.project.project_id]),
            {'reason': 'Inverter rating is wrong'},
        )
        rows = self._rows(SUBJECT_BOQ, boq)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].from_status, rows[0].to_status),
                         ('Submitted', 'Revision Requested'))
        self.assertEqual(rows[0].remark, 'Inverter rating is wrong')

    def test_delivery_challan_creation_and_grn(self):
        """Two rows: the challan opening at Expected, and the GRN moving it. Without
        the first, 'how long did this delivery take' has no start."""
        challan = self._make_dc()
        item = challan.line_items.first()

        _client_for(self.se).post(
            reverse('confirm_grn', args=[self.project.project_id, challan.pk]),
            {f'received_qty_{item.pk}': '10', f'damaged_qty_{item.pk}': '0'},
        )

        rows = self._rows(SUBJECT_DELIVERY_CHALLAN, challan)
        # The fixture builds the challan directly, so only the GRN row exists here.
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].from_status, rows[0].to_status),
                         (DeliveryChallan.EXPECTED, DeliveryChallan.RECEIVED))
        self.assertEqual(rows[0].actor, self.se)
        self.assertEqual(rows[0].reason_code, REASON_GRN_CONFIRMED)

    def test_a_recalculation_that_changes_nothing_writes_no_row(self):
        """An idempotent recalculation is not a transition. Recording it as one
        would litter the delivery history with dwell times of zero."""
        from .models import recalculate_dc_status

        challan = self._make_dc()
        recalculate_dc_status(challan, actor=self.se)   # still Expected
        self.assertEqual(self._rows(SUBJECT_DELIVERY_CHALLAN, challan), [])

    def test_delivery_challan_created_through_the_view_opens_its_ledger(self):
        _client_for(self.scm).post(
            reverse('create_delivery_challan', args=[self.project.project_id]),
            {'dc_number': 'DC-VIEW-1', 'dc_date': date.today().isoformat(),
             'line_item_category_0': 'Solar Modules',
             'line_item_description_0': 'Solar Module 540Wp',
             'line_item_qty_0': '10', 'line_item_unit_0': 'Nos'},
        )
        challan = DeliveryChallan.objects.get(dc_number='DC-VIEW-1')
        rows = self._rows(SUBJECT_DELIVERY_CHALLAN, challan)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].from_status, '')
        self.assertEqual(rows[0].to_status, DeliveryChallan.EXPECTED)
        self.assertEqual(rows[0].reason_code, REASON_CREATED)

    def test_issue_open_in_progress_resolved_closed(self):
        client = _client_for(self.pm)
        client.post(
            reverse('create_project_issue', args=[self.project.project_id]),
            {'title': 'Inverter scratched', 'description': 'noted at GRN',
             'severity': Issue.HIGH, 'assigned_to': str(self.se.pk)},
        )
        issue = Issue.objects.get(project=self.project, title='Inverter scratched')

        client.post(reverse('update_issue_status', args=[issue.pk]))
        client.post(reverse('resolve_issue', args=[issue.pk]),
                    {'resolution_note': 'Replacement despatched.'})
        client.post(reverse('close_issue', args=[issue.pk]))

        rows = self._rows(SUBJECT_ISSUE, issue)
        self.assertEqual([(r.from_status, r.to_status) for r in rows], [
            ('',                 Issue.OPEN),
            (Issue.OPEN,         Issue.IN_PROGRESS),
            (Issue.IN_PROGRESS,  Issue.RESOLVED),
            (Issue.RESOLVED,     Issue.CLOSED),
        ])

    def test_a_reopen_leaves_the_resolution_readable_in_the_ledger(self):
        """reopen_issue CLEARS resolution_note on the Issue row. The ledger keeps
        what was claimed — R-4 earning its keep."""
        client = _client_for(self.pm)
        client.post(
            reverse('create_project_issue', args=[self.project.project_id]),
            {'title': 'Cable short', 'description': 'x', 'severity': Issue.HIGH,
             'assigned_to': str(self.se.pk)},
        )
        issue = Issue.objects.get(project=self.project, title='Cable short')
        client.post(reverse('update_issue_status', args=[issue.pk]))
        client.post(reverse('resolve_issue', args=[issue.pk]),
                    {'resolution_note': 'Re-pulled the run.'})
        client.post(reverse('reopen_issue', args=[issue.pk]))

        issue.refresh_from_db()
        self.assertEqual(issue.status, Issue.OPEN)
        self.assertEqual(issue.resolution_note, '')     # wiped off the Issue

        rows = self._rows(SUBJECT_ISSUE, issue)
        self.assertEqual(rows[-1].to_status, Issue.OPEN)
        self.assertEqual(
            [r.remark for r in rows if r.to_status == Issue.RESOLVED],
            ['Re-pulled the run.'],
            'the resolution claim must survive the reopen that erased it',
        )

    def test_payment_milestone_invoiced_then_received(self):
        m1 = self.project.milestones.get(milestone_name='M1')
        PaymentMilestone.objects.filter(pk=m1.pk).update(amount=Decimal('100000.00'))
        client = _client_for(self.finance)

        client.post(reverse('milestone_invoice',
                            args=[self.project.project_id, m1.pk]))
        client.post(reverse('milestone_receive',
                            args=[self.project.project_id, m1.pk]),
                    {'amount_received': '100000'})

        rows = self._rows(SUBJECT_PAYMENT_MILESTONE, m1)
        self.assertEqual([(r.from_status, r.to_status) for r in rows], [
            ('Pending',  'Invoiced'),
            ('Invoiced', 'Received'),
        ])
        self.assertEqual({r.actor_role_code for r in rows}, {'Finance'})

    def test_the_task_to_milestone_sync_writes_the_milestones_row(self):
        """Completing a Finance confirmation task flips its milestone. The milestone
        moved, so the milestone gets a row — with the reason that says a human did
        not touch it directly."""
        m3 = self.project.milestones.get(milestone_name='M3')
        task = self._task('100% Payment Confirmation')
        Task.objects.filter(pk=task.pk).update(due_date=date.today())

        _client_for(self.finance).post(
            reverse('task_status_update', args=[self.project.project_id, task.pk]),
            {'status': Task.DONE, 'amount_received': '100000'},
        )
        m3.refresh_from_db()
        self.assertEqual(m3.status, 'Received')

        rows = self._rows(SUBJECT_PAYMENT_MILESTONE, m3)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].from_status, rows[0].to_status),
                         ('Pending', 'Received'))
        self.assertEqual(rows[0].reason_code, REASON_MILESTONE_SYNC)

    def test_the_milestone_to_task_sync_writes_the_tasks_row(self):
        """The same sync in the other direction — Finance marking M3 Received
        completes the confirmation task. Instrumenting only one direction would make
        the ledger's answer depend on which screen was used."""
        m3 = self.project.milestones.get(milestone_name='M3')
        PaymentMilestone.objects.filter(pk=m3.pk).update(
            amount=Decimal('100000.00'), status='Invoiced')
        task = self._task('100% Payment Confirmation')

        _client_for(self.finance).post(
            reverse('milestone_receive', args=[self.project.project_id, m3.pk]),
            {'amount_received': '100000'},
        )
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)

        rows = self._rows(SUBJECT_TASK, task)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].from_status, rows[0].to_status),
                         (Task.NOT_STARTED, Task.DONE))
        self.assertEqual(rows[0].reason_code, REASON_MILESTONE_SYNC)


# ---------------------------------------------------------------------------
# 7 — The question that was unanswerable before this table existed
# ---------------------------------------------------------------------------

class DwellTimeTests(LedgerFixture):
    """§6 of docs/execution-model.md: "dwell time in Blocked is partly answerable
    today; dwell time between two arbitrary states is not." This is the fix."""

    @staticmethod
    def _dwell(rows, status):
        """Time spent in `status`, computed FROM THE LEDGER ALONE.

        Entry is the row that moved INTO the status; exit is the next row after it.
        No Task field is read — that is the point: Task has completed_at and
        blocked_since and nothing else, so 'In Progress' has never had an answer.
        """
        for index, row in enumerate(rows):
            if row.to_status == status and index + 1 < len(rows):
                return rows[index + 1].occurred_at - row.occurred_at
        return None

    def test_time_in_the_middle_state_is_computable_from_the_ledger_alone(self):
        task = self._task('MMS Installation')
        start = timezone.now() - timedelta(hours=10)

        # Three states, explicit timestamps — a replayed offline submission carries
        # its own occurred_at for exactly this reason.
        record_transition(task, to_status=Task.NOT_STARTED, actor=self.se,
                          occurred_at=start)
        record_transition(task, to_status=Task.IN_PROGRESS,
                          from_status=Task.NOT_STARTED, actor=self.se,
                          occurred_at=start + timedelta(hours=2))
        record_transition(task, to_status=Task.DONE, from_status=Task.IN_PROGRESS,
                          actor=self.se, occurred_at=start + timedelta(hours=5))

        rows = self._rows(SUBJECT_TASK, task)
        self.assertEqual(self._dwell(rows, Task.IN_PROGRESS), timedelta(hours=3))
        self.assertEqual(self._dwell(rows, Task.NOT_STARTED), timedelta(hours=2))
        self.assertIsNone(self._dwell(rows, Task.DONE),
                          'the terminal state has no exit yet, and must not fake one')

    def test_the_same_computation_works_on_rows_the_real_views_wrote(self):
        """The arithmetic above is only worth anything if the endpoints produce rows
        of the same shape. They do — this walks the real ladder and asserts the
        query returns a real, ordered, non-negative interval."""
        task = self._task('MMS Installation')
        Task.objects.filter(pk=task.pk).update(due_date=date.today() + timedelta(days=3))
        url = reverse('task_status_update', args=[self.project.project_id, task.pk])
        client = _client_for(self.se)

        client.post(url, {'status': Task.IN_PROGRESS})
        client.post(url, {'status': Task.DONE})

        rows = self._rows(SUBJECT_TASK, task)
        self.assertEqual(len(rows), 2)
        dwell = self._dwell(rows, Task.IN_PROGRESS)
        self.assertIsNotNone(dwell, 'In Progress dwell time is unanswerable')
        self.assertGreaterEqual(dwell, timedelta(0))

    def test_one_projects_whole_timeline_is_a_single_query(self):
        """What the (project, occurred_at) index is for. Before this table, the same
        question meant a union across six unrelated models with no shared shape."""
        task = self._task('MMS Installation')
        Task.objects.filter(pk=task.pk).update(due_date=date.today() + timedelta(days=3))
        _client_for(self.se).post(
            reverse('task_status_update', args=[self.project.project_id, task.pk]),
            {'status': Task.IN_PROGRESS},
        )
        boq = self._seed_boq()
        _client_for(self.design).post(
            reverse('boq_submit', args=[self.project.project_id]))

        timeline = StatusTransition.objects.filter(
            project=self.project).order_by('occurred_at', 'pk')
        subject_types = {row.subject_type for row in timeline}
        self.assertEqual(subject_types,
                         {SUBJECT_PROJECT, SUBJECT_TASK, SUBJECT_BOQ})
        self.assertGreaterEqual(len(timeline), 3)
