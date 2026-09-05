"""
Checklist mechanism — the three structural mismatches, and witness capture.

WHY THIS FILE EXISTS
--------------------
The checklist machinery shipped (0.5, 2.4) with three assumptions baked into it that the
paper checklists it was built to replace do not share. Each is pinned here.

  1. A PHOTOGRAPH WAS ALWAYS MANDATORY. The completion view demanded a file unconditionally,
     because the first checklist anyone imagined was one where the evidence is a photograph.
     Most are not: "insulation resistance recorded?" is answered by a reading, and the
     photograph of a multimeter that the old rule forced was friction producing junk, not
     evidence. Now `Checklist.requires_photo`, per checklist, DEFAULT TRUE — pinned by
     `test_the_default_is_still_a_mandatory_photo` and the class below it, because a
     default that drifts to False silently un-mandates every checklist already authored.

  2. THERE WAS NO WAY TO SAY NO. One checkbox. An engineer standing in front of something
     that was not right could only leave the line blank, which reads identically to not
     having reached it — the finding disappeared into the same silence as the unvisited
     line. `answer` (yes / no / na) plus a MANDATORY remark on No is that fix, and
     `test_a_no_is_a_recorded_answer_not_an_unanswered_line` is the one that says the
     silence is actually gone.

  3. FORTY LINES, NO HEADINGS. A real commissioning sheet is grouped — Earthing, AC side,
     DC side — and rendering it flat is a thing the paper version never asked of anyone.
     `ChecklistItem.section` is presentation only and deliberately NOT part of R-8's
     snapshot; `test_renaming_a_section_leaves_the_answer_snapshot_alone` states that
     boundary as a test rather than as a comment.

And one recording gap: commissioning is WITNESSED, and checked_by/checked_at hold exactly
one authenticated person. `witness_names` records the others — as an assertion by the
completer, never as a signature. That distinction is only worth anything if the person
typing the names is told it, so `test_the_form_tells_the_completer_these_are_not_signatures`
pins the WORDING ON THE SCREEN, not just the column.

WHAT IS NOT TESTED HERE, BECAUSE THIS SESSION DOES NOT DO IT
    No real checklist content is seeded (that is a later session, waiting on answers about
    which items exist). No authoring UI for `section` or `requires_photo` beyond what the
    Django admin gives for free. No checklist gate on task completion (B-6). No signature
    capture of any kind — see the witness class docstring for why that is not what this is.

Run with:
    python manage.py test projects --settings=solarpms.test_settings
"""
from unittest.mock import MagicMock, patch

from django.apps import apps as django_apps
from django.db import IntegrityError, transaction
from django.test import RequestFactory
from django.urls import reverse

from .models import (
    Checklist, ChecklistItem, ChecklistItemCompletion, ChecklistTaskLink, Task,
    TemplateVersionLocked,
)
from .tests_checklist_snapshot import ChecklistSnapshotBase, _client_for, _photo
from .views import _checklist_context


class ChecklistMechanismBase(ChecklistSnapshotBase):
    """Reuses 0.5's fixture wholesale — one activated Residential project, one published
    checklist on one of its tasks — and adds the helpers this file needs on top.

    Sharing the fixture rather than rebuilding it is deliberate: these tests are about
    NEW behaviour on the SAME surface, and a second, subtly different project fixture is
    how two files end up disagreeing about what the surface is.
    """

    # -- helpers -------------------------------------------------------------

    def _publish_sectioned_checklist(self, name, pairs, task, requires_photo=True):
        """Author a draft with (section, label) pairs, publish it, and put it on `task`.

        Items are created in list order and given ascending `order`, so the fixture's
        order IS the rendered order and a test that cares about grouping can express it
        by writing the list in the order it expects to read back.

        RELINKS rather than adding a link: the base fixture already put a checklist on
        this task, and ChecklistTaskLink is unique per (task_name, project_type). One
        checklist per task is the rule being relied on, not one being worked around.
        """
        checklist = Checklist.objects.create(name=name, requires_photo=requires_photo)
        items = [
            ChecklistItem.objects.create(checklist=checklist, label=label,
                                         section=section, order=n)
            for n, (section, label) in enumerate(pairs, start=1)
        ]
        checklist.activate()
        self._relink(checklist, task)
        return checklist, items

    def _relink(self, checklist, task=None):
        """Point `task` at `checklist`, replacing whatever link it has."""
        task = task or self.task
        ChecklistTaskLink.objects.filter(
            task_name=task.task_name, project_type=self.project.project_type).delete()
        return ChecklistTaskLink.objects.create(
            checklist=checklist, task_name=task.task_name,
            project_type=self.project.project_type)

    def _post(self, item, data=None, photo=None, task=None, actor=None):
        """POST the completion form THROUGH THE REAL VIEW, with arbitrary fields.

        `_complete()` on the base class posts a photo and nothing else — the shape every
        caller had before `answer` existed — and is used here unchanged wherever the point
        is that the old shape still works. This one is for the new fields.
        """
        task    = task or self.task
        payload = dict(data or {})
        if photo is not None:
            payload['photo'] = photo
        with patch('projects.supabase_storage.get_supabase_client',
                   return_value=MagicMock()):
            return _client_for(actor or self.pm).post(
                reverse('checklist_item_complete',
                        args=[self.project.project_id, task.pk, item.pk]),
                payload)

    def _render_task(self, task=None):
        task = task or self.task
        return _client_for(self.pm).get(
            reverse('task_detail', args=[self.project.project_id, task.pk]))


# ---------------------------------------------------------------------------
# 1 — The photo requirement belongs to the checklist, and still defaults to on
# ---------------------------------------------------------------------------

class PhotoRequirementTests(ChecklistMechanismBase):

    def test_the_default_is_still_a_mandatory_photo(self):
        """THE LOAD-BEARING ASSERTION OF THE WHOLE FIELD. Every checklist that existed
        before this field, and every one authored without a thought about it, keeps the
        stricter rule. A default of False would quietly un-mandate the lot."""
        self.assertTrue(Checklist.objects.create(name='Fresh draft').requires_photo)
        self.assertTrue(self.checklist.requires_photo)

    def test_a_checklist_that_requires_a_photo_still_refuses_one_without(self):
        response = self._post(self.item, {'answer': 'yes'})
        self.assertEqual(ChecklistItemCompletion.objects.count(), 0)
        self.assertEqual(response.status_code, 302)

    def test_a_checklist_that_does_not_require_a_photo_accepts_a_completion_without_one(self):
        checklist, items = self._publish_sectioned_checklist(
            'Paperwork', [('', 'Sanction letter received?')], self.task,
            requires_photo=False)

        self._post(items[0], {'answer': 'yes'})

        completion = ChecklistItemCompletion.objects.get(item=items[0], task=self.task)
        self.assertTrue(completion.is_checked)
        self.assertEqual(completion.answer, ChecklistItemCompletion.YES)

    def test_a_completion_with_no_photo_stores_three_blank_photo_fields(self):
        """Not a missing photo — no photo. The three fields are blank together, the same
        way they are written together when there is one."""
        checklist, items = self._publish_sectioned_checklist(
            'Paperwork', [('', 'Sanction letter received?')], self.task,
            requires_photo=False)

        self._post(items[0], {'answer': 'yes'})

        completion = ChecklistItemCompletion.objects.get(item=items[0], task=self.task)
        self.assertEqual(completion.photo_file_name, '')
        self.assertEqual(completion.photo_url, '')
        self.assertEqual(completion.photo_supabase_path, '')

    def test_a_photo_is_still_accepted_where_it_is_not_required(self):
        """Optional means optional, not forbidden."""
        checklist, items = self._publish_sectioned_checklist(
            'Paperwork', [('', 'Sanction letter received?')], self.task,
            requires_photo=False)

        self._post(items[0], {'answer': 'yes'}, photo=_photo())

        completion = ChecklistItemCompletion.objects.get(item=items[0], task=self.task)
        self.assertTrue(completion.photo_url)
        self.assertTrue(completion.photo_supabase_path)

    def test_the_flag_is_per_checklist_and_not_a_global_switch(self):
        """Turning it off on one checklist must not reach any other. This is the whole
        reason it is a column on Checklist and not a setting."""
        relaxed, relaxed_items = self._publish_sectioned_checklist(
            'Relaxed', [('', 'A paperwork line')], self.task, requires_photo=False)
        self._post(relaxed_items[0], {'answer': 'yes'})
        self.assertEqual(ChecklistItemCompletion.objects.count(), 1)

        # Back to the strict fixture checklist: unchanged, still refuses.
        self._relink(self.checklist)
        self._post(self.item, {'answer': 'yes'})
        self.assertEqual(ChecklistItemCompletion.objects.count(), 1)

    def test_the_photo_input_is_not_offered_when_the_checklist_does_not_require_one(self):
        """The form asked for and the form accepted must agree — an input marked
        `required` for a rule the server does not apply is a dead end for the user."""
        checklist, _items = self._publish_sectioned_checklist(
            'Paperwork', [('', 'Sanction letter received?')], self.task,
            requires_photo=False)

        rendered = self._render_task()
        self.assertNotContains(rendered, 'name="photo"')
        self.assertContains(rendered, 'Record Answer')

    def test_the_photo_input_is_offered_when_it_is_required(self):
        rendered = self._render_task()
        self.assertContains(rendered, 'name="photo"')


# ---------------------------------------------------------------------------
# 2 — A real No, with a reason
# ---------------------------------------------------------------------------

class AnswerTests(ChecklistMechanismBase):

    def test_an_omitted_answer_is_yes(self):
        """THE MIGRATION, EXPRESSED AS A DEFAULT. Every caller written before this field —
        including the pinned tests in tests_checklist_snapshot — posts a photo and nothing
        else, and meant Yes by it."""
        self._complete(self.item)                      # the OLD post shape, unchanged
        completion = ChecklistItemCompletion.objects.get(item=self.item, task=self.task)
        self.assertEqual(completion.answer, ChecklistItemCompletion.YES)
        self.assertTrue(completion.is_checked)

    def test_an_explicit_yes_is_recorded_as_yes(self):
        self._post(self.item, {'answer': 'yes'}, photo=_photo())
        completion = ChecklistItemCompletion.objects.get(item=self.item, task=self.task)
        self.assertEqual(completion.answer, ChecklistItemCompletion.YES)

    def test_answering_no_without_a_remark_is_refused(self):
        response = self._post(self.item, {'answer': 'no'}, photo=_photo())
        self.assertEqual(ChecklistItemCompletion.objects.count(), 0)
        self.assertEqual(response.status_code, 302)

    def test_a_whitespace_only_remark_is_not_a_remark(self):
        self._post(self.item, {'answer': 'no', 'remarks': '   \n  '}, photo=_photo())
        self.assertEqual(ChecklistItemCompletion.objects.count(), 0)

    def test_answering_no_with_a_remark_succeeds_and_records_both(self):
        self._post(self.item,
                   {'answer': 'no', 'remarks': 'Reading 11 ohm on electrode 2.'},
                   photo=_photo())

        completion = ChecklistItemCompletion.objects.get(item=self.item, task=self.task)
        self.assertEqual(completion.answer, ChecklistItemCompletion.NO)
        self.assertEqual(completion.remarks, 'Reading 11 ohm on electrode 2.')
        self.assertTrue(completion.is_checked)

    def test_a_no_is_a_recorded_answer_not_an_unanswered_line(self):
        """DEFECT 2, stated directly. Before `answer` a failed check and an unreached one
        were the same row: is_checked False, nothing else. They are now distinguishable."""
        self._post(self.item, {'answer': 'no', 'remarks': 'Earth pit flooded.'},
                   photo=_photo())

        completion = ChecklistItemCompletion.objects.get(item=self.item, task=self.task)
        self.assertTrue(completion.is_checked)
        self.assertNotEqual(completion.answer, '')
        self.assertEqual(
            ChecklistItemCompletion.objects.filter(task=self.task, is_checked=True).count(), 1)

    def test_not_applicable_needs_no_remark(self):
        self._post(self.item, {'answer': 'na'}, photo=_photo())
        completion = ChecklistItemCompletion.objects.get(item=self.item, task=self.task)
        self.assertEqual(completion.answer, ChecklistItemCompletion.NA)
        self.assertEqual(completion.remarks, '')

    def test_a_yes_may_still_carry_a_remark(self):
        self._post(self.item, {'answer': 'yes', 'remarks': 'Measured 3.1 ohm.'},
                   photo=_photo())
        completion = ChecklistItemCompletion.objects.get(item=self.item, task=self.task)
        self.assertEqual(completion.remarks, 'Measured 3.1 ohm.')

    def test_an_answer_that_is_not_one_of_the_three_is_refused(self):
        self._post(self.item, {'answer': 'maybe'}, photo=_photo())
        self.assertEqual(ChecklistItemCompletion.objects.count(), 0)

    def test_the_photo_rule_does_not_bend_for_a_no(self):
        """A photo checklist wants the photograph of the thing that is wrong most of all."""
        self._post(self.item, {'answer': 'no', 'remarks': 'Conduit crushed.'})
        self.assertEqual(ChecklistItemCompletion.objects.count(), 0)

    def test_the_database_refuses_a_no_with_no_remark(self):
        """The view is one writer. A shell, a data migration and the next screen are not,
        and the constraint is what covers them."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ChecklistItemCompletion.objects.create(
                    item=self.item, task=self.task, is_checked=True,
                    answer=ChecklistItemCompletion.NO, remarks='')

    def test_the_database_allows_a_no_that_carries_its_remark(self):
        ChecklistItemCompletion.objects.create(
            item=self.item, task=self.task, is_checked=True,
            answer=ChecklistItemCompletion.NO, remarks='Because.')
        self.assertEqual(ChecklistItemCompletion.objects.count(), 1)

    def test_the_constraint_says_nothing_about_yes_na_or_unanswered(self):
        """Scoped to No on purpose: a blank remark is the normal case everywhere else."""
        second = (Task.objects.filter(phase__project=self.project)
                  .exclude(pk=self.task.pk).first())
        ChecklistItemCompletion.objects.create(item=self.item, task=self.task,
                                               answer=ChecklistItemCompletion.YES)
        ChecklistItemCompletion.objects.create(item=self.item, task=second,
                                               answer='')
        self.assertEqual(ChecklistItemCompletion.objects.count(), 2)

    def test_the_answer_is_rendered_on_a_completed_row(self):
        self._post(self.item, {'answer': 'no', 'remarks': 'Earth pit flooded.'},
                   photo=_photo())
        rendered = self._render_task()
        self.assertContains(rendered, 'Earth pit flooded.')
        self.assertContains(rendered, '✗ No')


# ---------------------------------------------------------------------------
# 3 — The backfill: a tick recorded before `answer` existed meant Yes
# ---------------------------------------------------------------------------

class AnswerBackfillTests(ChecklistMechanismBase):
    """Migration 0081's data step, exercised against the real model registry.

    Both live databases held ZERO completions when 0081 was written, so on them it moves
    nothing. That is exactly why it is tested here instead of trusted: the one operation
    in 0081 that could lose information is this one, and "the table was empty" is not a
    property of the code.
    """

    def test_a_pre_existing_tick_becomes_a_yes(self):
        from importlib import import_module
        migration = import_module(
            'projects.migrations.0081_checklist_answer_remarks_sections')

        # A row as it looked before `answer` existed: ticked, answer blank.
        completion = ChecklistItemCompletion.objects.create(
            item=self.item, task=self.task, is_checked=True, answer='')

        migration.backfill_answer_from_is_checked(django_apps, None)

        completion.refresh_from_db()
        self.assertEqual(completion.answer, ChecklistItemCompletion.YES)

    def test_an_unticked_row_stays_unanswered(self):
        from importlib import import_module
        migration = import_module(
            'projects.migrations.0081_checklist_answer_remarks_sections')

        completion = ChecklistItemCompletion.objects.create(
            item=self.item, task=self.task, is_checked=False, answer='')

        migration.backfill_answer_from_is_checked(django_apps, None)

        completion.refresh_from_db()
        self.assertEqual(completion.answer, '')

    def test_the_reverse_never_deletes_a_real_no(self):
        """Reversing a schema change must not destroy user input the forward pass never
        invented — it only clears the 'yes' it wrote."""
        from importlib import import_module
        migration = import_module(
            'projects.migrations.0081_checklist_answer_remarks_sections')

        second = (Task.objects.filter(phase__project=self.project)
                  .exclude(pk=self.task.pk).first())
        yes = ChecklistItemCompletion.objects.create(
            item=self.item, task=self.task, is_checked=True,
            answer=ChecklistItemCompletion.YES)
        no = ChecklistItemCompletion.objects.create(
            item=self.item, task=second, is_checked=True,
            answer=ChecklistItemCompletion.NO, remarks='Failed.')

        migration.unbackfill_answer(django_apps, None)

        yes.refresh_from_db()
        no.refresh_from_db()
        self.assertEqual(yes.answer, '')
        self.assertEqual(no.answer, ChecklistItemCompletion.NO)


# ---------------------------------------------------------------------------
# 4 — Sections: presentation, additive, and outside R-8
# ---------------------------------------------------------------------------

class SectionGroupingTests(ChecklistMechanismBase):

    def _context(self, task=None):
        """Call the real builder with a request carrying the PM, the way a view does."""
        request = RequestFactory().get('/')
        request.user = self.pm.user
        return _checklist_context(request, self.project, task or self.task)

    def test_items_with_no_section_form_one_ungrouped_block(self):
        """The pre-existing shape: nothing names a section, so nothing is grouped and the
        output is what it has always been."""
        context = self._context()
        self.assertEqual(len(context['checklist_sections']), 1)
        self.assertEqual(context['checklist_sections'][0]['name'], '')
        self.assertEqual(len(context['checklist_sections'][0]['rows']), 1)

    def test_items_group_under_their_headings_in_item_order(self):
        checklist, _items = self._publish_sectioned_checklist('Commissioning', [
            ('Earthing', 'Earth resistance < 5 ohm'),
            ('Earthing', 'Earth pits accessible'),
            ('DC side',  'String voltage recorded'),
            ('DC side',  'Fuses rated correctly'),
            ('AC side',  'ACDB breaker rating matches'),
        ], self.task)

        sections = self._context()['checklist_sections']
        self.assertEqual([s['name'] for s in sections], ['Earthing', 'DC side', 'AC side'])
        self.assertEqual([len(s['rows']) for s in sections], [2, 2, 1])

    def test_the_numbering_is_continuous_across_sections(self):
        """Item 5 is item 5 whichever heading it sits under — a per-section forloop
        counter would restart at 1 and renumber the sheet."""
        checklist, _items = self._publish_sectioned_checklist('Commissioning', [
            ('Earthing', 'One'), ('Earthing', 'Two'),
            ('DC side',  'Three'), ('DC side', 'Four'), ('AC side', 'Five'),
        ], self.task)

        sections = self._context()['checklist_sections']
        numbers = [row['number'] for section in sections for row in section['rows']]
        self.assertEqual(numbers, [1, 2, 3, 4, 5])

    def test_a_heading_that_reappears_later_renders_twice_in_place(self):
        """Grouping is consecutive, not a gather-by-name. Moving row 5 up under row 1's
        heading would silently reorder a sheet its author ordered deliberately."""
        checklist, _items = self._publish_sectioned_checklist('Commissioning', [
            ('Earthing', 'One'), ('DC side', 'Two'), ('Earthing', 'Three'),
        ], self.task)

        sections = self._context()['checklist_sections']
        self.assertEqual([s['name'] for s in sections], ['Earthing', 'DC side', 'Earthing'])
        self.assertEqual([row['number'] for s in sections for row in s['rows']], [1, 2, 3])

    def test_the_flat_rows_view_is_unchanged_and_still_carries_every_item(self):
        """`checklist_sections` is ADDITIVE. `checklist_rows` keeps its shape, its order
        and its contents, which is why neither caller of _checklist_context needed to
        change."""
        checklist, items = self._publish_sectioned_checklist('Commissioning', [
            ('Earthing', 'One'), ('DC side', 'Two'), ('AC side', 'Three'),
        ], self.task)

        context = self._context()
        self.assertEqual([row['item'].pk for row in context['checklist_rows']],
                         [it.pk for it in items])
        self.assertEqual([row['label'] for row in context['checklist_rows']],
                         ['One', 'Two', 'Three'])
        # Same row objects, reachable both ways.
        flat_from_sections = [row for s in context['checklist_sections'] for row in s['rows']]
        self.assertEqual(flat_from_sections, context['checklist_rows'])

    def test_section_headers_render_on_the_task_page(self):
        checklist, _items = self._publish_sectioned_checklist('Commissioning', [
            ('Earthing', 'Earth resistance under 5 ohm'),
            ('DC side',  'String voltage recorded'),
        ], self.task)

        rendered = self._render_task()
        self.assertContains(rendered, 'Earthing')
        self.assertContains(rendered, 'DC side')
        self.assertContains(rendered, 'Earth resistance under 5 ohm')
        self.assertContains(rendered, 'String voltage recorded')

    def test_an_ungrouped_checklist_renders_no_heading_row(self):
        """The zero-section case must look exactly like it did before sections existed."""
        rendered = self._render_task()
        self.assertContains(rendered, 'Earth resistance &lt; 5 ohm')
        self.assertNotContains(rendered, 'text-uppercase text-muted fw-semibold')

    def test_the_checklist_lists_its_sections_in_item_order(self):
        checklist, _items = self._publish_sectioned_checklist('Commissioning', [
            ('Earthing', 'One'), ('Earthing', 'Two'), ('DC side', 'Three'),
        ], self.task)
        self.assertEqual(checklist.sections, ['Earthing', 'DC side'])

    def test_a_section_is_content_and_is_frozen_once_the_version_is_live(self):
        """R-7 covers it for free, because it is a field on ChecklistItem. Stated as a
        test so nobody 'fixes' the heading on a live checklist and is surprised."""
        checklist, items = self._publish_sectioned_checklist(
            'Commissioning', [('Earthing', 'One')], self.task)
        items[0].section = 'Earth / bonding'
        with self.assertRaises(TemplateVersionLocked):
            items[0].save()

    def test_renaming_a_section_leaves_the_answer_snapshot_alone(self):
        """THE R-8 BOUNDARY. The section is not snapshotted and the label still is:
        re-grouping a display is not rewriting an answer."""
        checklist, items = self._publish_sectioned_checklist(
            'Commissioning', [('Earthing', 'Earth resistance under 5 ohm')], self.task)
        self._post(items[0], {'answer': 'yes'}, photo=_photo())

        # .update() bypasses the R-7 guard deliberately — the subject is the READ path.
        ChecklistItem.objects.filter(pk=items[0].pk).update(section='Earth / bonding')

        completion = ChecklistItemCompletion.objects.get(item=items[0], task=self.task)
        self.assertEqual(completion.item_text_snapshot, 'Earth resistance under 5 ohm')
        rendered = self._render_task()
        self.assertContains(rendered, 'Earth resistance under 5 ohm')
        # The heading follows the item, because it was never part of the record.
        self.assertContains(rendered, 'Earth / bonding')

    def test_rewording_a_label_under_a_section_still_renders_the_snapshot(self):
        """R-8's own guarantee, re-asserted inside a sectioned checklist so the two
        features are pinned together rather than one at a time."""
        checklist, items = self._publish_sectioned_checklist(
            'Commissioning', [('Earthing', 'Earth resistance under 5 ohm')], self.task)
        self._post(items[0], {'answer': 'yes'}, photo=_photo())

        ChecklistItem.objects.filter(pk=items[0].pk).update(
            label='Earth resistance under 5 ohm at all three electrodes')

        rendered = self._render_task()
        self.assertContains(rendered, 'Earth resistance under 5 ohm')
        self.assertNotContains(rendered, 'at all three electrodes')


# ---------------------------------------------------------------------------
# 5 — Witness capture, and what it explicitly is not
# ---------------------------------------------------------------------------

class WitnessCaptureTests(ChecklistMechanismBase):
    """THIS IS NOT A SIGNATURE AND THE TESTS SAY SO OUT LOUD.

    checked_by/checked_at record one authenticated person: whoever was logged in. A
    commissioning test has more people at the panel than that — the client's rep, the
    DISCOM inspector, the EPC's QA — and until now they left no trace at all.

    `witness_names` is where their names go. Nobody named in it logged in, typed anything
    or confirmed anything; the completer typed it. A name to ask "were you there?" beats
    no name, and it is not the countersigned document a real witnessed test certificate
    is. The distinction only survives if the person entering the names is TOLD it, which
    is why `test_the_form_tells_the_completer_these_are_not_signatures` pins the sentence
    on the screen and not merely the column in the database.
    """

    def test_witness_names_are_recorded_against_the_completion(self):
        self._post(self.item,
                   {'answer': 'yes', 'witness_names': 'R. Sharma (DISCOM), A. Iyer (client)'},
                   photo=_photo())
        completion = ChecklistItemCompletion.objects.get(item=self.item, task=self.task)
        self.assertEqual(completion.witness_names,
                         'R. Sharma (DISCOM), A. Iyer (client)')

    def test_witnesses_are_optional(self):
        self._post(self.item, {'answer': 'yes'}, photo=_photo())
        completion = ChecklistItemCompletion.objects.get(item=self.item, task=self.task)
        self.assertEqual(completion.witness_names, '')

    def test_the_completer_is_still_the_one_authenticated_name(self):
        """Witnesses sit BESIDE checked_by, they do not replace or dilute it."""
        self._post(self.item, {'answer': 'yes', 'witness_names': 'Someone else entirely'},
                   photo=_photo())
        completion = ChecklistItemCompletion.objects.get(item=self.item, task=self.task)
        self.assertEqual(completion.checked_by, self.pm.user)
        self.assertIsNotNone(completion.checked_at)

    def test_the_form_tells_the_completer_these_are_not_signatures(self):
        """THE WORDING IS THE FEATURE. A comment in models.py is read by developers; the
        person who could mistake this for a signature is the one typing into it."""
        rendered = self._render_task()
        self.assertContains(rendered, 'not verified signatures')
        self.assertContains(rendered, 'your statement')

    def test_a_recorded_witness_carries_the_same_caveat_to_whoever_reads_it(self):
        self._post(self.item, {'answer': 'yes', 'witness_names': 'R. Sharma (DISCOM)'},
                   photo=_photo())
        rendered = self._render_task()
        self.assertContains(rendered, 'R. Sharma (DISCOM)')
        self.assertContains(rendered, 'Witnessed by (stated)')
        self.assertContains(rendered, 'not verified signatures')
