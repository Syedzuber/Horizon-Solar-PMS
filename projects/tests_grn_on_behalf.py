"""A receipt recorded by someone who was not the receiver. Session G2.

WHY THIS FILE EXISTS
--------------------
`override_grn` had two jobs and only ever admitted to one. On a line a site engineer had
already confirmed it corrects the quantities and preserves the original submitter — a
correction, and `tests_residential_baseline
.DeliveryGRNWorkflowTests.test_scm_overrides_a_grn_without_overwriting_the_original_engineer`
pins exactly that. On a line NOBODY had confirmed it did the same thing, which is not a
correction of anything: it wrote the quantities, left `grn_confirmed_by` NULL, and the
receipt rendered as recorded by nobody on both surfaces that display it. The G1 audit
verified that path end to end against a real database; no test covered it.

WHAT IS PINNED HERE
-------------------
  * the first-time branch names the ACTING user and marks the row, with a reason;
  * the correction branch is untouched — same confirmer, flag never set;
  * a submission carrying both gets one of each, in one request;
  * a missing reason refuses the WHOLE submission and writes nothing at all;
  * a missing reason on a pure correction is fine, because no line needs one;
  * the CHECK constraint refuses the pair directly, not only through the view;
  * the scope call added to `override_grn` did not shut supply chain out;
  * both display surfaces say who recorded it AND that they were not the receiver.

THE MECHANISM NAMES NO ROLE, AND NEITHER DOES THIS FILE'S SUBJECT. SCM is the only role
that can reach the override endpoint today, so SCM is the actor in these tests — but
every assertion is about "the acting user", never about supply chain in particular.
`UserProfile.is_warehouse_keeper` exists with no screen behind it yet; when a keeper can
receive alongside the engineer, these tests should need no edit beyond the actor.

WHAT IS DELIBERATELY NOT TESTED HERE. `confirm_grn` and its scoping predicate: the site
engineer rule is correct, is pinned by `tests_access_isolation.RoleGateScopingTests` and
by the baseline, and this session did not touch it. `recalculate_dc_status()` and
`sync_delivery_mirrors()` are likewise out of scope — the status rules are unchanged and
this file asserts the status only as evidence that a write did or did not happen.

Run with:
    python manage.py test projects --settings=solarpms.test_settings
"""
from datetime import date
from decimal import Decimal
from importlib import import_module

from django.db import IntegrityError, transaction
from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from .models import (
    DCLineItem, DeliveryChallan, Project, ProjectPhase, StatusTransition, Task,
    TaskTemplate, TaskTemplatePhase, TaskTemplateTask, UserProfile,
)
from .utils import resolve_residential_template


# The phrase both surfaces use for the same fact. Transcribed, not imported: a test that
# imports the string it is checking agrees with any wording the templates drift into.
ON_BEHALF_PHRASE = 'on behalf of the site team'

# The OPEX delivery mirror whose panel renders the second surface, and the DC category it
# reads. Same two constants tests_delivery_detail.py transcribes, for the same reason.
PANEL_TASK_NAME = 'Delivery — Solar Panels'
PANEL_CATEGORY = 'Solar Modules'


def _profile(username, role, first_name=None):
    """Create a user and set their profile role.

    signals.py creates the UserProfile on post_save with the model's DEFAULT role, so
    this UPDATES rather than creates — a second create would violate the OneToOne.
    """
    user = User.objects.create_user(
        username=username, password='x',
        first_name=first_name or username.title(), last_name='Test',
    )
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.save(update_fields=['role'])
    return profile


def _client_for(profile):
    client = Client()
    client.force_login(profile.user)
    return client


class GrnOnBehalfBase(TestCase):
    """One live project, a challan with two lines, and the three actors.

    TWO LINES IS THE POINT of this fixture and not a convenience. Half of what this file
    checks is that the two branches are decided PER LINE inside one submission, and a
    one-line challan would let a per-request implementation pass everything.

    The site engineer holds a task on this project, so `confirm_grn`'s scoping predicate
    admits them. That relationship is real, and is how the correction-branch fixtures get
    a genuinely SE-confirmed line rather than one written straight to the column.
    """

    @classmethod
    def setUpTestData(cls):
        cls.scm = _profile('g2_scm', 'SCM', first_name='Supply')
        cls.se = _profile('g2_se', 'Site Engineer', first_name='Ramesh')
        cls.pm = _profile('g2_pm', 'PM', first_name='Priya')

    def setUp(self):
        self.project = Project.objects.create(
            customer_name='G2 Customer',
            customer_phone='9876543210',
            site_address='1 Receipt Lane',
            city='Lucknow',
            project_type='Residential',
            status='Active',
            assigned_pm=self.pm,
        )
        # The relationship confirm_grn scopes on. One phase, one task, assigned to the SE.
        phase = ProjectPhase.objects.create(
            project=self.project, phase_name='Execution', phase_order=1,
        )
        Task.objects.create(
            phase=phase, task_name='Receive material', task_order=1,
            assigned_to=self.se, assigned_role='Site Engineer',
        )

        self.challan = DeliveryChallan.objects.create(
            project=self.project, dc_number='DC-G2-001', dc_date=date.today(),
            status=DeliveryChallan.EXPECTED, created_by=self.scm,
        )
        self.line_a = DCLineItem.objects.create(
            challan=self.challan, boq_category=PANEL_CATEGORY,
            item_description='Solar Module 540Wp',
            ordered_quantity=Decimal('10.00'), unit='Nos',
        )
        self.line_b = DCLineItem.objects.create(
            challan=self.challan, boq_category='Inverter',
            item_description='String Inverter 50kW',
            ordered_quantity=Decimal('4.00'), unit='Nos',
        )

    # -- helpers -------------------------------------------------------------

    def _override(self, payload, actor=None):
        return _client_for(actor or self.scm).post(
            reverse('override_grn', args=[self.project.project_id, self.challan.pk]),
            payload,
        )

    def _confirm_as_se(self, line, qty='10', damaged='0'):
        """Drive the real SE path, so a 'confirmed' line is one confirm_grn produced."""
        response = _client_for(self.se).post(
            reverse('confirm_grn', args=[self.project.project_id, self.challan.pk]),
            {f'received_qty_{line.pk}': qty, f'damaged_qty_{line.pk}': damaged},
        )
        self.assertEqual(response.status_code, 302,
                         'the site engineer holding a task here must be able to confirm')
        line.refresh_from_db()
        self.assertEqual(line.grn_confirmed_by, self.se)
        return line

    def _transitions(self):
        return StatusTransition.objects.filter(
            subject_type='delivery_challan', subject_id=self.challan.pk,
        )


# ---------------------------------------------------------------------------
# R2 — the two branches
# ---------------------------------------------------------------------------

class FirstTimeReceiptTests(GrnOnBehalfBase):
    """A line nobody has confirmed, recorded through the override endpoint."""

    def test_the_acting_user_is_recorded_and_the_row_is_marked_on_behalf(self):
        response = self._override({
            f'received_qty_{self.line_a.pk}': '10',
            f'damaged_qty_{self.line_a.pk}': '0',
            'grn_on_behalf_reason': 'Site engineer unreachable; verified over call',
        })
        self.assertEqual(response.status_code, 302)

        self.line_a.refresh_from_db()
        self.challan.refresh_from_db()

        # The acting user, NOT the absent engineer. The endpoint records who typed it.
        self.assertEqual(self.line_a.grn_confirmed_by, self.scm)
        self.assertNotEqual(self.line_a.grn_confirmed_by, self.se,
                            'the absent engineer must never be written here')
        self.assertTrue(self.line_a.grn_on_behalf)
        self.assertEqual(self.line_a.grn_on_behalf_reason,
                         'Site engineer unreachable; verified over call')
        # And the receipt itself landed.
        self.assertEqual(self.line_a.received_quantity, Decimal('10.00'))
        self.assertEqual(self.line_a.grn_date, date.today())
        self.assertEqual(self.challan.status, DeliveryChallan.RECEIVED)

    def test_the_reason_is_stripped_before_it_is_stored(self):
        """Whitespace around a real reason is trimmed, so the column never carries the
        form's padding and the constraint never sees a value the view thought was blank."""
        self._override({
            f'received_qty_{self.line_a.pk}': '10',
            f'damaged_qty_{self.line_a.pk}': '0',
            'grn_on_behalf_reason': '   Engineer on leave   ',
        })
        self.line_a.refresh_from_db()
        self.assertEqual(self.line_a.grn_on_behalf_reason, 'Engineer on leave')


class CorrectionTests(GrnOnBehalfBase):
    """A line an engineer already confirmed. Behaviour here is unchanged by G2."""

    def test_the_confirmer_is_unchanged_and_the_flag_stays_false(self):
        self._confirm_as_se(self.line_a, qty='8')

        response = self._override({
            f'received_qty_{self.line_a.pk}': '10',
            f'damaged_qty_{self.line_a.pk}': '0',
            'grn_on_behalf_reason': 'ignored — nothing here takes the first-time branch',
        })
        self.assertEqual(response.status_code, 302)

        self.line_a.refresh_from_db()
        self.assertEqual(self.line_a.received_quantity, Decimal('10.00'),
                         'the correction itself must still apply')
        self.assertEqual(self.line_a.grn_confirmed_by, self.se,
                         'the original submitter is preserved on a correction')
        self.assertFalse(self.line_a.grn_on_behalf,
                         'correcting a quantity does not make the receipt second-hand')
        self.assertEqual(self.line_a.grn_on_behalf_reason, '')

    def test_an_on_behalf_row_stays_marked_through_a_later_correction(self):
        """The flag records HOW the receipt was captured, which a later edit does not
        change. The second override takes the correction branch — grn_confirmed_by is
        set by then — so it must leave both columns exactly as they stand."""
        self._override({
            f'received_qty_{self.line_a.pk}': '6',
            f'damaged_qty_{self.line_a.pk}': '0',
            'grn_on_behalf_reason': 'Engineer travelling',
        })
        self._override({
            f'received_qty_{self.line_a.pk}': '10',
            f'damaged_qty_{self.line_a.pk}': '0',
        })
        self.line_a.refresh_from_db()
        self.assertEqual(self.line_a.received_quantity, Decimal('10.00'))
        self.assertEqual(self.line_a.grn_confirmed_by, self.scm)
        self.assertTrue(self.line_a.grn_on_behalf)
        self.assertEqual(self.line_a.grn_on_behalf_reason, 'Engineer travelling')


class MixedSubmissionTests(GrnOnBehalfBase):
    """One request, one unconfirmed line and one the engineer confirmed."""

    def test_the_flag_lands_on_the_unconfirmed_line_only(self):
        self._confirm_as_se(self.line_a, qty='10')   # line A is now a correction
        # line B has never been confirmed by anyone.

        response = self._override({
            f'received_qty_{self.line_a.pk}': '9',
            f'damaged_qty_{self.line_a.pk}': '0',
            f'received_qty_{self.line_b.pk}': '4',
            f'damaged_qty_{self.line_b.pk}': '0',
            'grn_on_behalf_reason': 'Engineer left site before the inverters arrived',
        })
        self.assertEqual(response.status_code, 302)

        self.line_a.refresh_from_db()
        self.line_b.refresh_from_db()

        # A — the correction. Untouched confirmer, no flag.
        self.assertEqual(self.line_a.received_quantity, Decimal('9.00'))
        self.assertEqual(self.line_a.grn_confirmed_by, self.se)
        self.assertFalse(self.line_a.grn_on_behalf)
        self.assertEqual(self.line_a.grn_on_behalf_reason, '')

        # B — the first-time receipt. Acting user, flagged, reason stored.
        self.assertEqual(self.line_b.received_quantity, Decimal('4.00'))
        self.assertEqual(self.line_b.grn_confirmed_by, self.scm)
        self.assertTrue(self.line_b.grn_on_behalf)
        self.assertEqual(self.line_b.grn_on_behalf_reason,
                         'Engineer left site before the inverters arrived')


# ---------------------------------------------------------------------------
# R3 — the mandatory reason, and its refusal
# ---------------------------------------------------------------------------

class BlankReasonRefusalTests(GrnOnBehalfBase):
    """A refusal that writes NOTHING. Not a partial write, not a status move."""

    def _assert_nothing_was_written(self, transitions_before):
        self.line_a.refresh_from_db()
        self.line_b.refresh_from_db()
        self.challan.refresh_from_db()
        for line in (self.line_a, self.line_b):
            self.assertIsNone(line.received_quantity)
            self.assertIsNone(line.grn_confirmed_by)
            self.assertIsNone(line.grn_date)
            self.assertFalse(line.grn_on_behalf)
            self.assertEqual(line.grn_on_behalf_reason, '')
        self.assertEqual(self.challan.status, DeliveryChallan.EXPECTED)
        self.assertEqual(self._transitions().count(), transitions_before,
                         'a refused submission must write no ledger row')

    def test_a_missing_reason_refuses_the_whole_submission(self):
        before = self._transitions().count()
        response = self._override({
            f'received_qty_{self.line_a.pk}': '10',
            f'damaged_qty_{self.line_a.pk}': '0',
        })
        self.assertEqual(response.status_code, 302, 'the refusal redirects back')
        self._assert_nothing_was_written(before)

    def test_a_whitespace_only_reason_refuses_the_whole_submission(self):
        before = self._transitions().count()
        self._override({
            f'received_qty_{self.line_a.pk}': '10',
            f'damaged_qty_{self.line_a.pk}': '0',
            'grn_on_behalf_reason': '    ',
        })
        self._assert_nothing_was_written(before)

    def test_the_refusal_says_why(self):
        response = self._override(
            {f'received_qty_{self.line_a.pk}': '10',
             f'damaged_qty_{self.line_a.pk}': '0'},
        )
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(
            any(ON_BEHALF_PHRASE.split(' of ')[0] in m or 'on behalf' in m
                for m in messages),
            f'the refusal must be readable; got {messages!r}',
        )

    def test_a_refused_mixed_submission_does_not_apply_the_correction_either(self):
        """THE WHOLE SUBMISSION, not the lines that happened to be fine. A correction
        that rides along with an unreasoned first-time receipt is refused with it, so
        the screen never half-applies what was typed."""
        self._confirm_as_se(self.line_a, qty='10')
        before = self._transitions().count()

        self._override({
            f'received_qty_{self.line_a.pk}': '9',    # correction, would be legal alone
            f'damaged_qty_{self.line_a.pk}': '0',
            f'received_qty_{self.line_b.pk}': '4',    # first-time, needs a reason
            f'damaged_qty_{self.line_b.pk}': '0',
        })

        self.line_a.refresh_from_db()
        self.line_b.refresh_from_db()
        self.assertEqual(self.line_a.received_quantity, Decimal('10.00'),
                         'the correction must NOT have been applied')
        self.assertIsNone(self.line_b.received_quantity)
        self.assertEqual(self._transitions().count(), before)

    def test_a_pure_correction_needs_no_reason(self):
        """No line takes the first-time branch, so the refusal is never reached. This is
        the shape the baseline's override test posts, and it must keep working."""
        self._confirm_as_se(self.line_a, qty='8')

        response = self._override({
            f'received_qty_{self.line_a.pk}': '10',
            f'damaged_qty_{self.line_a.pk}': '0',
        })
        self.assertEqual(response.status_code, 302)

        self.line_a.refresh_from_db()
        self.challan.refresh_from_db()
        self.assertEqual(self.line_a.received_quantity, Decimal('10.00'))
        self.assertEqual(self.line_a.grn_confirmed_by, self.se)
        self.assertFalse(self.line_a.grn_on_behalf)
        self.assertEqual(self.challan.status, DeliveryChallan.RECEIVED)

    def test_a_submission_with_no_parseable_line_needs_no_reason(self):
        """Blank and unparseable quantities are skipped, unchanged by G2 — so a
        submission that writes nothing takes no first-time branch and is not refused
        for want of a reason it does not need."""
        before = self._transitions().count()
        response = self._override({
            f'received_qty_{self.line_a.pk}': '',
            f'received_qty_{self.line_b.pk}': 'not a number',
        })
        self.assertEqual(response.status_code, 302)
        self.line_a.refresh_from_db()
        self.line_b.refresh_from_db()
        self.assertIsNone(self.line_a.received_quantity)
        self.assertIsNone(self.line_b.received_quantity)
        self.assertEqual(self._transitions().count(), before)


# ---------------------------------------------------------------------------
# R1 — the constraint, below the view
# ---------------------------------------------------------------------------

class ConstraintTests(GrnOnBehalfBase):
    """The database refuses the pair too, not only the endpoint."""

    def test_the_flag_without_a_reason_is_refused_by_the_database(self):
        line = self.line_a
        line.grn_on_behalf = True
        line.grn_on_behalf_reason = ''
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                line.save()

    def test_the_flag_with_a_reason_saves(self):
        """The other leg, so the test above cannot pass by refusing every save."""
        line = self.line_a
        line.grn_on_behalf = True
        line.grn_on_behalf_reason = 'Recorded from the warehouse'
        line.save()
        line.refresh_from_db()
        self.assertTrue(line.grn_on_behalf)

    def test_an_unflagged_row_may_carry_an_empty_reason(self):
        """Every row that existed before this migration is this shape. The constraint
        must admit them, which is why it is additive with no backfill."""
        line = self.line_b
        self.assertFalse(line.grn_on_behalf)
        self.assertEqual(line.grn_on_behalf_reason, '')
        line.received_quantity = Decimal('4.00')
        line.save()   # must not raise


# ---------------------------------------------------------------------------
# R4 — the scope call added to override_grn
# ---------------------------------------------------------------------------

class OverrideScopeTests(GrnOnBehalfBase):
    """`user_can_view_project` now guards override_grn, matching confirm_grn.

    THIS IS A SYMMETRY FIX, NOT A SCOPE CHANGE, and this class is the proof. SCM is in
    PORTFOLIO_VIEW_ROLES, so the helper returns True for a supply-chain user on every
    project — including one where they are not the PM and hold no task at all.
    """

    def test_supply_chain_still_succeeds_on_a_project_it_holds_no_task_on(self):
        self.assertNotEqual(self.project.assigned_pm, self.scm)
        self.assertFalse(
            self.project.phases.filter(tasks__assigned_to=self.scm).exists(),
            'the fixture must give the acting user no task here, or this proves nothing',
        )

        response = self._override({
            f'received_qty_{self.line_a.pk}': '10',
            f'damaged_qty_{self.line_a.pk}': '0',
            'grn_on_behalf_reason': 'Engineer unavailable',
        })
        self.assertEqual(response.status_code, 302)
        self.line_a.refresh_from_db()
        self.assertEqual(self.line_a.received_quantity, Decimal('10.00'))

    def test_a_challan_from_another_project_is_still_unreachable(self):
        """The cross-project guard is unchanged and still comes after the scope call."""
        other = Project.objects.create(
            customer_name='Other', customer_phone='9000000000',
            site_address='2 Elsewhere', city='Kanpur',
            project_type='Residential', status='Active', assigned_pm=self.pm,
        )
        other_challan = DeliveryChallan.objects.create(
            project=other, dc_number='DC-G2-OTHER', dc_date=date.today(),
            status=DeliveryChallan.EXPECTED, created_by=self.scm,
        )
        response = _client_for(self.scm).post(
            reverse('override_grn', args=[self.project.project_id, other_challan.pk]), {})
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# R5 — what the two surfaces say
# ---------------------------------------------------------------------------

class ChallanDetailDisplayTests(GrnOnBehalfBase):
    """The Confirmed By cell on the delivery challan detail page."""

    def _page(self, actor=None):
        response = _client_for(actor or self.scm).get(
            reverse('delivery_challan_detail',
                    args=[self.project.project_id, self.challan.pk]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _rows(self, actor=None):
        """Just the Line Items table.

        THE SLICE IS LOAD-BEARING, not tidiness. The whole page also carries the acting
        user's name in the navbar and the on-behalf phrase in the override modal's help
        text, so a bare assertIn/assertNotIn over the document would pass for the wrong
        reason in one direction and fail for the wrong reason in the other. What is
        being checked is the ROW.
        """
        html = self._page(actor)
        start = html.index('Line Items')
        return html[start:html.index('</table>', start)]

    def test_an_on_behalf_line_names_the_actor_and_says_it_was_on_behalf(self):
        self._override({
            f'received_qty_{self.line_a.pk}': '10',
            f'damaged_qty_{self.line_a.pk}': '0',
            'grn_on_behalf_reason': 'Engineer unreachable at handover',
        })
        rows = self._rows()
        self.assertIn('Supply Test', rows, 'the row must name who recorded it')
        self.assertIn(ON_BEHALF_PHRASE, rows)
        self.assertIn('Engineer unreachable at handover', rows,
                      'the stated reason must reach the row')

    def test_an_on_behalf_line_never_renders_an_empty_confirmed_by_cell(self):
        """D2 in one assertion. Before G2 this row rendered the em-dash placeholder,
        because grn_confirmed_by was NULL and the template's else branch fired."""
        self._override({
            f'received_qty_{self.line_a.pk}': '10',
            f'damaged_qty_{self.line_a.pk}': '0',
            'grn_on_behalf_reason': 'Engineer unreachable at handover',
        })
        self.line_a.refresh_from_db()
        self.assertIsNotNone(self.line_a.grn_confirmed_by)
        self.assertTrue(self.line_a.grn_on_behalf)

        # The Confirmed By cell of the receipt's own row, start to finish.
        rows = self._rows()
        cell_start = rows.index('Solar Module 540Wp')
        cell = rows[cell_start:]
        self.assertIn('Supply Test', cell)
        self.assertIn(ON_BEHALF_PHRASE, cell)

    def test_an_engineer_confirmed_line_names_the_engineer_and_nothing_more(self):
        """The phrase must not leak onto an ordinary receipt."""
        self._confirm_as_se(self.line_a, qty='10')
        rows = self._rows()
        self.assertIn('Ramesh Test', rows)
        self.assertNotIn(ON_BEHALF_PHRASE, rows,
                         'a receipt the engineer confirmed in person is not on behalf')

    def test_the_override_modal_offers_the_reason_input(self):
        html = self._page()
        self.assertIn('grn_on_behalf_reason', html,
                      'supply chain must have somewhere to type the reason')


class TaskPanelDisplayTests(TestCase):
    """The delivery panel on an OPEX mirror task — the second surface.

    A REALLY ACTIVATED OPEX SITE, following tests_delivery_detail.py: the mirror whose
    panel is rendered is the row `attach_opex_template()` produced, not a hand-made Task
    with `is_mirror` set. A hand-made row would prove the `if` works and nothing about
    whether the panel a PM actually opens carries the new phrase.

    `sync_delivery_mirrors()` is deliberately not named anywhere in this module — it has
    a closed list of two permitted callers asserted package-wide by
    tests_design_mirror_derivation.test_01b, and the GRN endpoint is one of them.
    """

    @classmethod
    def setUpTestData(cls):
        resolve_residential_template()
        module = import_module('projects.migrations.0075_seed_opex_template_v1')

        class _ConcreteApps:
            _MODELS = {
                'TaskTemplate': TaskTemplate, 'TaskTemplatePhase': TaskTemplatePhase,
                'TaskTemplateTask': TaskTemplateTask, 'Task': Task,
            }

            def get_model(self, app_label, model_name):
                assert app_label == 'projects'
                return self._MODELS[model_name]

        module.seed_opex_v1(_ConcreteApps(), None)
        cls.pm = _profile('g2p_pm', 'PM', first_name='Priya')
        cls.scm = _profile('g2p_scm', 'SCM', first_name='Supply')

    def setUp(self):
        self.site = Project.objects.create(
            customer_name='G2 Panel Site', customer_phone='9876543210',
            site_address='3 Mirror Road', city='Lucknow',
            project_type='OPEX', dc_capacity_kw=Decimal('100.00'),
            status='Draft', assigned_pm=self.pm,
        )
        response = _client_for(self.pm).post(
            reverse('opex_site_activate', args=[self.site.project_id]))
        self.assertEqual(response.status_code, 302, 'OPEX activation did not redirect')
        self.site.refresh_from_db()

        self.challan = DeliveryChallan.objects.create(
            project=self.site, dc_number='DC-G2-PANEL', dc_date=date.today(),
            status=DeliveryChallan.EXPECTED, created_by=self.scm,
        )
        self.line = DCLineItem.objects.create(
            challan=self.challan, boq_category=PANEL_CATEGORY,
            item_description='Solar Module 540Wp',
            ordered_quantity=Decimal('10.00'), unit='Nos',
        )

    def _mirror_task(self):
        task = Task.objects.filter(
            phase__project=self.site, task_name=PANEL_TASK_NAME).first()
        self.assertIsNotNone(task, 'the OPEX attach produced no delivery mirror')
        return task

    def test_the_read_path_carries_the_two_new_values_through(self):
        from .design_views import delivery_detail_for_task

        self._client_override('Engineer off site')
        detail = delivery_detail_for_task(self._mirror_task())
        self.assertIsNotNone(detail)
        line = detail['challans'][0]['lines'][0]
        self.assertEqual(line['grn_confirmed_by'], self.scm)
        self.assertTrue(line['grn_on_behalf'])
        self.assertEqual(line['grn_on_behalf_reason'], 'Engineer off site')
        # The shape is otherwise unchanged — the keys the panel already relied on.
        for key in ('item_description', 'unit', 'ordered', 'received', 'damaged',
                    'condition', 'grn_date', 'grn_confirmed_by'):
            self.assertIn(key, line)

    def test_the_panel_names_the_actor_and_says_it_was_on_behalf(self):
        self._client_override('Engineer off site')
        response = _client_for(self.pm).get(
            reverse('task_detail', args=[self.site.project_id, self._mirror_task().pk]))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('Supply Test', html)
        self.assertIn(ON_BEHALF_PHRASE, html)
        self.assertIn('Engineer off site', html)

    def _client_override(self, reason):
        response = _client_for(self.scm).post(
            reverse('override_grn', args=[self.site.project_id, self.challan.pk]),
            {f'received_qty_{self.line.pk}': '10',
             f'damaged_qty_{self.line.pk}': '0',
             'grn_on_behalf_reason': reason},
        )
        self.assertEqual(response.status_code, 302)
        self.line.refresh_from_db()
        self.assertTrue(self.line.grn_on_behalf)
