"""
Checklist answer window — answers close at submission and at Done, and reopen on reject.

WHY THIS FILE EXISTS
--------------------
seed_opex_installation_checklists links checklists at TEMPLATE level, so every task on a
linked template task shows the checklist — including tasks already Done or awaiting
approval. On production a Done, approved task rendered 46 blank items that could still be
answered: nothing on the render or answer path looked at the task's status. Answering a
witnessed QMS checklist after it was handed over for approval must not be possible.

The rule is `permissions.checklist_answers_open(task)`:

    task.status != Done  and  not task.is_awaiting_approval

and it is enforced SERVER-SIDE. Every refusal below is asserted with a direct POST, never
only by the absence of a form, because a hidden form is not a gate.

WHAT IS PINNED
    Not Started and In Progress (unsubmitted) → accepted.
    Submitted, awaiting approval              → refused, PM included; 403, or the section
                                                re-rendered with the reason under HTMX.
    Rejected back to the engineer             → accepted again.
    Done, nothing answered                    → one factual line, no item table, no form.
    Done, some answered                       → answers shown read-only, no form, the
                                                rest badged "Not answered", and a POST on
                                                an unanswered item is refused.
    Closed, only No / NA answers              → rows shown; a No is an answer.
    Open, viewer who cannot answer            → "Pending", as it always was.
    HTMX re-render after a refused POST       → the same three rules as the full page.

THE RULE LIVES IN THE PARTIAL, AND THE FIXTURE IS SEED-SHAPED
    The notice first shipped as a branch in task_detail.html AROUND the partial include.
    The HTMX response includes the partial directly, so every re-render bypassed it; and
    the template change itself was left out of the commit that shipped the lock, so on
    production the Done task rendered 46 grey Pending rows while every test here passed
    against the working tree. The branch now sits inside _checklist.html, which both
    paths render. Section 6 builds its checklists the way seed_opex_installation_checklists
    does (a draft, items with and without sections, activate(), linked by template_task
    on a really activated OPEX site) so the card is tested against what production holds.

WHAT IS NOT HERE, BECAUSE THIS SESSION DOES NOT DO IT
    No gate on submit or approval for unanswered items — that is a separate decision.
    The card is never hidden; its visibility rule is untouched and pinned below.

STATE IS SET WITH .update(), NOT THROUGH THE STATUS AND APPROVAL VIEWS. The subject is the
checklist's reading of the task's fields, not how those fields get there — the approval
views have their own suites. The rejected-back fixture writes exactly the columns
task_reject() writes (submitted_by/submitted_at cleared, submission_remarks emptied,
approval_remarks set), so the predicate is tested against the real post-reject shape.

Run with:
    python manage.py test projects.tests_checklist_answer_window --settings=solarpms.test_settings
"""
from unittest.mock import MagicMock, patch

from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    Checklist, ChecklistItem, ChecklistItemCompletion, ChecklistTaskLink, Task,
)
from .permissions import checklist_answers_open
from .tests_checklist_snapshot import ChecklistSnapshotBase, _client_for, _photo
from .tests_checklist_task_link import LinkFixture
from .views import _checklist_context

NO_ANSWERS_NOTICE = 'No checklist answers were recorded for this task.'


class ChecklistAnswerWindowBase(ChecklistSnapshotBase):
    """0.5's fixture — one activated Residential project with a published checklist on
    'Pre Commissioning Check List' — plus helpers that put the task into each state."""

    # -- task states ---------------------------------------------------------

    def _set_status(self, status):
        Task.objects.filter(pk=self.task.pk).update(status=status)
        self.task.refresh_from_db()

    def _submit(self):
        """Submitted and not yet approved: the state task_submit_for_approval() leaves."""
        Task.objects.filter(pk=self.task.pk).update(
            status=Task.IN_PROGRESS,
            submitted_by=self.pm, submitted_at=timezone.now(),
            submission_remarks='Work complete.',
        )
        self.task.refresh_from_db()

    def _reject(self):
        """The columns task_reject() writes, and only those."""
        Task.objects.filter(pk=self.task.pk).update(
            submitted_by=None, submitted_at=None, submission_remarks='',
            approval_remarks='Torque marks missing.',
        )
        self.task.refresh_from_db()

    def _approve_and_close(self):
        now = timezone.now()
        Task.objects.filter(pk=self.task.pk).update(
            status=Task.DONE, approved_by=self.pm, approved_at=now, completed_at=now,
        )
        self.task.refresh_from_db()

    # -- requests ------------------------------------------------------------

    def _answer(self, item, hx=False):
        headers = {'HTTP_HX_REQUEST': 'true'} if hx else {}
        with patch('projects.supabase_storage.get_supabase_client',
                   return_value=MagicMock()):
            return _client_for(self.pm).post(
                reverse('checklist_item_complete',
                        args=[self.project.project_id, self.task.pk, item.pk]),
                {'answer': 'yes', 'photo': _photo()}, **headers,
            )

    def _page(self):
        return _client_for(self.pm).get(
            reverse('task_detail', args=[self.project.project_id, self.task.pk]))

    def _answer_url(self, item):
        return reverse('checklist_item_complete',
                       args=[self.project.project_id, self.task.pk, item.pk])

    def _answered(self, item):
        return ChecklistItemCompletion.objects.filter(
            item=item, task=self.task, is_checked=True).exists()

    def _relink_two_item_checklist(self):
        """Replace the fixture's one-item checklist with a two-item one, so a test can
        answer one item and leave the other blank. One checklist per (task_name,
        project_type) is the rule, so the old link goes first."""
        ChecklistTaskLink.objects.filter(task_name=self.task.task_name,
                                         project_type='Residential').delete()
        _checklist, items = self._publish_checklist(
            'Commissioning', ['Earth resistance < 5 ohm', 'Inverter display on'],
            task=self.task, code='commissioning-two')
        return items


# ---------------------------------------------------------------------------
# 1 — The predicate
# ---------------------------------------------------------------------------

class _FakeTask:
    DONE = Task.DONE

    def __init__(self, status, submitted_at=None, approved_at=None):
        self.status = status
        self.submitted_at = submitted_at
        self.approved_at = approved_at

    is_awaiting_approval = Task.is_awaiting_approval


class ChecklistAnswersOpenPredicateTests(SimpleTestCase):

    def test_not_started_is_open(self):
        self.assertTrue(checklist_answers_open(_FakeTask(Task.NOT_STARTED)))

    def test_in_progress_unsubmitted_is_open(self):
        self.assertTrue(checklist_answers_open(_FakeTask(Task.IN_PROGRESS)))

    def test_blocked_is_open(self):
        self.assertTrue(checklist_answers_open(_FakeTask(Task.BLOCKED)))

    def test_awaiting_approval_is_closed(self):
        self.assertFalse(checklist_answers_open(
            _FakeTask(Task.IN_PROGRESS, submitted_at=timezone.now())))

    def test_done_is_closed(self):
        self.assertFalse(checklist_answers_open(_FakeTask(Task.DONE)))

    def test_reopened_after_approval_is_open(self):
        """D2: Done → Blocked → In Progress keeps a stale approval. The predicate does
        not consult approved_at, so reopened work is checkable."""
        now = timezone.now()
        self.assertTrue(checklist_answers_open(
            _FakeTask(Task.IN_PROGRESS, submitted_at=now, approved_at=now)))


# ---------------------------------------------------------------------------
# 2 — Open states: answers accepted
# ---------------------------------------------------------------------------

class OpenTaskAcceptsAnswersTests(ChecklistAnswerWindowBase):

    def test_not_started_task_accepts_an_answer(self):
        self.assertEqual(self.task.status, Task.NOT_STARTED)
        response = self._answer(self.item)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self._answered(self.item))

    def test_in_progress_unsubmitted_task_accepts_an_answer(self):
        self._set_status(Task.IN_PROGRESS)
        response = self._answer(self.item)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self._answered(self.item))

    def test_rejected_back_task_accepts_answers_again(self):
        self._submit()
        self.assertEqual(self._answer(self.item).status_code, 403)
        self.assertFalse(self._answered(self.item))

        self._reject()
        response = self._answer(self.item)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self._answered(self.item))

    def test_rejected_back_task_renders_the_answer_form(self):
        self._submit()
        self._reject()
        self.assertContains(self._page(), self._answer_url(self.item))


# ---------------------------------------------------------------------------
# 3 — Submitted: refused by the server
# ---------------------------------------------------------------------------

class SubmittedTaskRefusesAnswersTests(ChecklistAnswerWindowBase):

    def test_a_direct_post_is_refused_with_403(self):
        """The PM is the actor: management authority does not reopen a closed task."""
        self._submit()
        response = self._answer(self.item)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ChecklistItemCompletion.objects.filter(task=self.task).exists())

    def test_an_htmx_post_is_refused_with_the_section_and_the_reason(self):
        """Same response kind as the permission refusal under HTMX: the section is
        re-rendered with the message inline. Not a 500, and nothing written."""
        self._submit()
        response = self._answer(self.item, hx=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Checklist answers are closed')
        self.assertFalse(self._answered(self.item))

    def test_the_page_offers_no_answer_form(self):
        self._submit()
        page = self._page()
        self.assertNotContains(page, self._answer_url(self.item))
        # Zero answers on a closed task: the notice, not blank rows.
        self.assertContains(page, NO_ANSWERS_NOTICE)


# ---------------------------------------------------------------------------
# 4 — Done: read-only, or one factual line
# ---------------------------------------------------------------------------

class DoneTaskChecklistCardTests(ChecklistAnswerWindowBase):

    def test_done_with_zero_answers_renders_the_notice_and_no_form(self):
        self._approve_and_close()
        page = self._page()
        self.assertContains(page, NO_ANSWERS_NOTICE)
        self.assertNotContains(page, self._answer_url(self.item))
        # No item table: the unanswered question is not rendered as a Pending row.
        self.assertNotContains(page, 'Earth resistance &lt; 5 ohm')
        # The card itself stays — visibility is unchanged.
        self.assertContains(page, 'id="checklistSection"')
        self.assertContains(page, self.checklist.name)

    def test_done_with_answers_renders_them_read_only(self):
        answered, blank = self._relink_two_item_checklist()
        self.assertEqual(self._answer(answered).status_code, 302)
        self._approve_and_close()

        page = self._page()
        self.assertNotContains(page, NO_ANSWERS_NOTICE)
        self.assertContains(page, 'Earth resistance &lt; 5 ohm')
        self.assertContains(page, '✓ Yes')
        self.assertNotContains(page, self._answer_url(answered))
        self.assertNotContains(page, self._answer_url(blank))
        # The witness-entry caveat belongs to the answer form and goes with it.
        self.assertNotContains(page, 'Witness names are recorded as')

    def test_done_task_refuses_a_direct_post_on_an_unanswered_item(self):
        answered, blank = self._relink_two_item_checklist()
        self._answer(answered)
        self._approve_and_close()

        response = self._answer(blank)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self._answered(blank))

    def test_done_without_approval_is_closed_too(self):
        """Residential and mirror tasks reach Done with no approval columns at all."""
        self._set_status(Task.DONE)
        self.assertEqual(self._answer(self.item).status_code, 403)
        self.assertContains(self._page(), NO_ANSWERS_NOTICE)


# ---------------------------------------------------------------------------
# 5 — The context flag follows the same rule
# ---------------------------------------------------------------------------

class CanCompleteItemsFollowsTheWindowTests(ChecklistAnswerWindowBase):

    def _context(self):
        return self._page().context

    def test_flag_is_true_while_open_and_false_once_submitted(self):
        self.assertTrue(self._context()['can_complete_items'])
        self._submit()
        context = self._context()
        self.assertFalse(context['can_complete_items'])
        self.assertFalse(context['checklist_answers_open'])

    def test_an_archived_checklist_still_hides_the_card(self):
        """Card visibility is untouched: no active checklist, no card — open or closed."""
        Checklist.objects.filter(pk=self.checklist.pk).update(status=Checklist.ARCHIVED)
        self._approve_and_close()
        page = self._page()
        self.assertNotContains(page, 'id="checklistSection"')
        self.assertNotContains(page, NO_ANSWERS_NOTICE)


# ---------------------------------------------------------------------------
# 6 — Seed-shaped checklists: the card as production holds it
# ---------------------------------------------------------------------------

NOT_ANSWERED = '>Not answered<'
PENDING      = '>Pending<'

# Two headings on the sectioned sheet, as Civil Work and MMS Installation has four; none
# on the flat one, as Module Installation has none. Labels and headings are distinct
# strings, so an assertion on one can never be satisfied by the other.
CIVIL_ITEMS = [
    ('Concrete Block (RCC/PCC)', 'Check concrete block dimensions as per drawing'),
    ('Concrete Block (RCC/PCC)', 'Check curing of concrete blocks'),
    ('Fixed type structure Installation', 'Check column verticality'),
    ('Fixed type structure Installation', 'Check rafter alignment'),
]
MODULE_ITEMS = [
    ('', 'Check PV Modules installed as per row layout'),
    ('', 'Check module clamps torqued'),
    ('', 'Check module cleaning done'),
]


def _seed_shaped_checklist(code, name, template_task, items):
    """Build a checklist EXACTLY as seed_opex_installation_checklists does: a draft with
    requires_photo off, the items bulk-created in order with their (possibly empty)
    section, activate(), then a link keyed by template_task alone -- the link's save()
    derives task_name and project_type from it. Nothing here is linked by name.

    If the seed's recipe changes, change this with it: the point of the helper is that
    the card is never again tested only against a shape production does not hold."""
    checklist = Checklist.objects.create(
        code=code, name=name, version_no=1,
        status=Checklist.DRAFT, requires_photo=False,
    )
    ChecklistItem.objects.bulk_create([
        ChecklistItem(checklist=checklist, label=label, section=section, order=n)
        for n, (section, label) in enumerate(items, start=1)
    ])
    checklist.activate()
    ChecklistTaskLink.objects.create(checklist=checklist, template_task=template_task)
    return checklist, list(checklist.items.all())


class SeedShapedChecklistBase(LinkFixture):
    """A really activated OPEX site (LinkFixture), with the Civil and Module tasks each
    carrying a seed-shaped checklist: Civil with sections, Module without."""

    def setUp(self):
        super().setUp()
        self.civil  = self._opex_task('CIVIL_WORK_AND_MMS_INSTALLATION')
        self.module = self._opex_task('MODULE_INSTALLATION')
        self.civil_checklist, self.civil_items = _seed_shaped_checklist(
            'OPEX-INST-CIVIL-MMS', 'Civil Work and MMS Installation',
            self.civil.template_task, CIVIL_ITEMS)
        self.module_checklist, self.module_items = _seed_shaped_checklist(
            'OPEX-INST-MODULE', 'Module Installation',
            self.module.template_task, MODULE_ITEMS)

    def _opex_task(self, code):
        return Task.objects.get(phase__project=self.site, template_task__code=code)

    # -- task states: the same columns ChecklistAnswerWindowBase writes ----------

    def _done(self, task):
        now = timezone.now()
        Task.objects.filter(pk=task.pk).update(
            status=Task.DONE, submitted_by=self.pm, submitted_at=now,
            approved_by=self.pm, approved_at=now, completed_at=now)
        task.refresh_from_db()

    def _submitted(self, task):
        Task.objects.filter(pk=task.pk).update(
            status=Task.IN_PROGRESS, submitted_by=self.pm, submitted_at=timezone.now(),
            submission_remarks='Work complete.')
        task.refresh_from_db()

    # -- requests ---------------------------------------------------------------

    def _url(self, task, item):
        return reverse('checklist_item_complete',
                       args=[self.site.project_id, task.pk, item.pk])

    def _answer(self, task, item, answer='yes', remarks='', hx=False):
        headers = {'HTTP_HX_REQUEST': 'true'} if hx else {}
        return _client_for(self.pm).post(
            self._url(task, item), {'answer': answer, 'remarks': remarks}, **headers)

    def _page(self, task):
        return _client_for(self.pm).get(
            reverse('task_detail', args=[self.site.project_id, task.pk]))

    # -- assertions ---------------------------------------------------------------

    def assertNoticeOnly(self, response, task, items):
        """The notice line and nothing of the item table: no rows, no headings, no
        badges, no form. The card's header and count are the full page's to assert."""
        self.assertContains(response, NO_ANSWERS_NOTICE, count=1)
        self.assertNotContains(response, '<table')
        for section, label in items:
            self.assertNotContains(response, label)
            if section:
                self.assertNotContains(response, section)
        self.assertNotContains(response, PENDING)
        self.assertNotContains(response, NOT_ANSWERED)
        for item in (self.civil_items if task == self.civil else self.module_items):
            self.assertNotContains(response, self._url(task, item))

    def assertCardHeader(self, response, checklist, count):
        self.assertContains(response, 'id="checklistSection"')
        self.assertContains(response, checklist.name)
        self.assertContains(
            response, f'<span class="badge bg-secondary ms-1">{count}</span>')


class SeedShapedClosedZeroAnswersTests(SeedShapedChecklistBase):

    def test_done_with_sections_shows_the_notice_only(self):
        self.assertFalse(ChecklistItemCompletion.objects.filter(task=self.civil).exists())
        self._done(self.civil)
        page = self._page(self.civil)
        self.assertNoticeOnly(page, self.civil, CIVIL_ITEMS)
        self.assertCardHeader(page, self.civil_checklist, len(CIVIL_ITEMS))

    def test_submitted_without_sections_shows_the_notice_only(self):
        self._submitted(self.module)
        page = self._page(self.module)
        self.assertNoticeOnly(page, self.module, MODULE_ITEMS)
        self.assertCardHeader(page, self.module_checklist, len(MODULE_ITEMS))

    def test_done_without_sections_shows_the_notice_only(self):
        self._done(self.module)
        self.assertNoticeOnly(self._page(self.module), self.module, MODULE_ITEMS)


class SeedShapedClosedPartialAnswersTests(SeedShapedChecklistBase):

    def test_answered_rows_read_only_and_the_rest_not_answered(self):
        self.assertEqual(self._answer(self.civil, self.civil_items[0]).status_code, 302)
        self._done(self.civil)

        page = self._page(self.civil)
        self.assertNotContains(page, NO_ANSWERS_NOTICE)
        self.assertContains(page, '✓ Yes', count=1)
        for _section, label in CIVIL_ITEMS:
            self.assertContains(page, label)
        # Section headings still group the read-only rows.
        self.assertContains(page, 'Concrete Block (RCC/PCC)')
        self.assertContains(page, NOT_ANSWERED, count=len(CIVIL_ITEMS) - 1)
        self.assertNotContains(page, PENDING)
        for item in self.civil_items:
            self.assertNotContains(page, self._url(self.civil, item))
        self.assertNotContains(page, 'Witness names are recorded as')

    def test_only_no_and_na_answers_are_answers(self):
        """is_checked is True for every recorded answer, No and NA included: a sheet
        answered entirely No is not a sheet with nothing recorded."""
        no, na, no_again = self.module_items
        self.assertEqual(
            self._answer(self.module, no, 'no', 'Clamp missing').status_code, 302)
        self.assertEqual(self._answer(self.module, na, 'na').status_code, 302)
        self.assertEqual(
            self._answer(self.module, no_again, 'no', 'Not cleaned').status_code, 302)
        self._done(self.module)

        page = self._page(self.module)
        self.assertNotContains(page, NO_ANSWERS_NOTICE)
        self.assertContains(page, '✗ No', count=2)
        self.assertContains(page, '<span class="badge bg-secondary">Not Applicable</span>',
                            count=1)
        self.assertNotContains(page, NOT_ANSWERED)
        self.assertNotContains(page, PENDING)
        for item in self.module_items:
            self.assertNotContains(page, self._url(self.module, item))

    def test_one_no_among_blanks_on_a_submitted_task(self):
        self.assertEqual(self._answer(
            self.civil, self.civil_items[2], 'no', 'Out of plumb').status_code, 302)
        self._submitted(self.civil)
        page = self._page(self.civil)
        self.assertNotContains(page, NO_ANSWERS_NOTICE)
        self.assertContains(page, '✗ No', count=1)
        self.assertContains(page, NOT_ANSWERED, count=len(CIVIL_ITEMS) - 1)


class SeedShapedOpenTaskTests(SeedShapedChecklistBase):

    def test_open_task_keeps_its_controls(self):
        page = self._page(self.civil)
        self.assertNotContains(page, NO_ANSWERS_NOTICE)
        self.assertNotContains(page, NOT_ANSWERED)
        for item in self.civil_items:
            self.assertContains(page, f'action="{self._url(self.civil, item)}"')
        self.assertContains(page, 'Concrete Block (RCC/PCC)')

    def test_open_task_with_one_answer_keeps_controls_on_the_rest(self):
        self.assertEqual(self._answer(self.module, self.module_items[0]).status_code, 302)
        page = self._page(self.module)
        self.assertContains(page, '✓ Yes', count=1)
        self.assertNotContains(page, NOT_ANSWERED)
        self.assertNotContains(page, NO_ANSWERS_NOTICE)
        for item in self.module_items[1:]:
            self.assertContains(page, f'action="{self._url(self.module, item)}"')

    def test_open_task_viewer_who_cannot_answer_sees_pending(self):
        """Open, zero answers, can_complete_items False (a viewer outside the task's
        role): the rows stay and say Pending -- not the notice, not Not answered.
        Rendered from the real context builder with only the permission flag turned
        off, because which role can view but not answer is not this file's subject."""
        request = RequestFactory().get('/')
        request.user = self.pm.user
        context = _checklist_context(request, self.site, self.module)
        self.assertTrue(context['checklist_answers_open'])
        context['can_complete_items'] = False
        html = render_to_string('projects/partials/_checklist.html', context)
        self.assertEqual(html.count(PENDING), len(MODULE_ITEMS))
        self.assertNotIn(NOT_ANSWERED, html)
        self.assertNotIn(NO_ANSWERS_NOTICE, html)
        self.assertNotIn('<form', html)


class SeedShapedHtmxRefusalTests(SeedShapedChecklistBase):
    """A refused POST under HTMX re-renders #checklistSection through
    _checklist_response.html. It must follow the full page's rules exactly."""

    def test_refused_post_on_a_closed_task_with_zero_answers_renders_the_notice(self):
        self._done(self.civil)
        response = self._answer(self.civil, self.civil_items[0], hx=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Checklist answers are closed')
        self.assertFalse(ChecklistItemCompletion.objects.filter(task=self.civil).exists())
        self.assertNoticeOnly(response, self.civil, CIVIL_ITEMS)
        # The out-of-band count badge still refreshes.
        self.assertContains(response, 'id="checklistCountBadge" hx-swap-oob="true"')

    def test_refused_post_without_sections_renders_the_notice(self):
        self._submitted(self.module)
        response = self._answer(self.module, self.module_items[0], hx=True)
        self.assertEqual(response.status_code, 200)
        self.assertNoticeOnly(response, self.module, MODULE_ITEMS)

    def test_refused_post_with_some_answers_renders_not_answered(self):
        self.assertEqual(self._answer(self.civil, self.civil_items[0]).status_code, 302)
        self._done(self.civil)
        response = self._answer(self.civil, self.civil_items[1], hx=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Checklist answers are closed')
        self.assertNotContains(response, NO_ANSWERS_NOTICE)
        self.assertContains(response, '✓ Yes', count=1)
        self.assertContains(response, NOT_ANSWERED, count=len(CIVIL_ITEMS) - 1)
        self.assertNotContains(response, PENDING)
        self.assertNotContains(response, '<form')

    def test_accepted_post_on_an_open_task_keeps_the_rest_answerable(self):
        response = self._answer(self.module, self.module_items[0], hx=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '✓ Yes', count=1)
        self.assertNotContains(response, NOT_ANSWERED)
        for item in self.module_items[1:]:
            self.assertContains(response, f'action="{self._url(self.module, item)}"')
