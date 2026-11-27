"""
DW-3 verification — Remove a calculated line (an override to 0 with a note); Clear BOQ at
the top of the picker.

WHAT THIS FILE PROVES
---------------------
  * ONE PATH (R2). A designer's Remove is an override to 0 with its note: the line stays at
    0 with its override, a recalculation record is written, no BOQCorrection. It survives
    two picker saves, an unchanged DW-7 upload and a reviewer correction elsewhere. Restore
    (the existing clear) puts the calculated figure back.
  * R5. A line calculated from it follows, and the pop-up listed it beforehand: on the
    designer's path it is removed (D1), on the reviewer's it is set to 0 (answer 3).
  * REVIEWERS (B-2e, DW-2a). QC may Remove in QC's turn ("by QC"); QC is refused once QC
    passed the package (awaiting_head_qc); the Head may.
  * R3 / answer 7. An empty note is refused and nothing is saved: beside the Remove form on
    the correction screen; in the pop-up (script) on the picker, the server backstop too.
  * R6. Refused on an ordered line, on a mandatory item and on an item with no line.
  * B-2b.1. The picker's Remove is disabled while the sheet has unsaved changes.
  * R4. "Removed (0) by <who>: note" on the picker, correction screen, QC/Head panel and,
    for Design users, the BOQ view; "Removed (0)" alone for SCM and the PM and on the site
    card (answer 10); the download's Calculated column reads "Removed: note". An override to
    any other figure keeps "Overridden".
  * Design groups leave the removed line out (no 0 row, no new warning); DW-1 unchanged.
  * R7. Clear BOQ sits in the top bar for the designer in the edit window, nowhere else.
  * Queries: the markers and the pop-up's list cost no query on any screen; the Remove POST
    is pinned.

RUNNER: `manage.py test projects.tests_dw3_remove_calculated --settings=solarpms.test_settings`.
"""
import io
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.template.loader import render_to_string
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .design_boq_group_views import consolidate
from .models import (
    BOQ, BOQCorrection, BOQLineOverride, BOQRecalculation, DesignAttempt,
    ATTEMPT_REASON_INITIAL, DESIGN_AWAITING_HEAD_QC, DESIGN_IN_QC,
    FORMULA_SOURCE_OVERRIDE_SET, OVERRIDE_SET_AS_DESIGNER, OVERRIDE_SET_AS_HEAD,
    OVERRIDE_SET_AS_QC, boq_minimum_content_gaps,
)
from .site_drawings_views import _boq_rows
from .tests_boq_formula_apply import _profile
from .tests_boq_reviewer_override import ReviewerOverrideBase
from .tests_boq_formula_display import DisplayBase
from .views import (BOQ_REMOVE_MANDATORY_MESSAGE, BOQ_REMOVE_NO_LINE_MESSAGE,
                    BOQ_REMOVE_NOTE_REQUIRED_MESSAGE, BOQ_REMOVE_ORDERED_MESSAGE)

D = Decimal
NOTE = 'no logger on this roof'
DESIGNER_MARK = 'Removed (0) by the designer (Fxdes)'

#: The Remove POSTs, measured at the DW-3 build on this file's fixture (no redirect
#: followed): the endpoint's gate, the rules and the lock, the line, the override write, the
#: engine over the sheet (the chained FX-RS485 deleted on the designer's path, D1; set to 0
#: on the reviewer's, B-2e Q2), the records and the activity entry. Each includes the one
#: query R6 adds, the order-line exists() inside formula_line_blocked(), run only on a set to
#: 0 of a line with no ordered quantity (QueryTests.test_r6_adds_one_query_to_a_set_to_zero).
REMOVE_POSTS = {'designer': 31, 'qc': 31}


def _n(call):
    with CaptureQueriesContext(connection) as ctx:
        response = call()
    return len(ctx), response


class DesignerBase(DisplayBase):
    """The designer's site, in design, with the three rules applied by a picker save:

        FX-LA1 2, FX-INV1 12  ->  FX-EARTH 10, FX-LOG 2, FX-RS485 10 (= 5 m x FX-LOG)
    """

    def setUp(self):
        super().setUp()
        self._rules()
        self.scm = _profile('fxscm', 'SCM')
        self._login(self.designer)
        self._picker({'FX-LA1': 2, 'FX-INV1': 12})
        self.logger = self._lines('FX-LOG')[0]

    def _remove(self, line=None, note=NOTE):
        return self._override('set', line or self.logger, quantity='0', note=note)

    def _restore(self, line=None):
        return self._override('clear', line or self.logger)

    def _removed_state(self):
        """(FX-LOG quantity, its override's note, FX-RS485 quantity or 'absent')."""
        line = self._lines('FX-LOG')[0]
        override = BOQLineOverride.objects.filter(line=line).first()
        return (line.boq_quantity, override.note if override else None, self._qty('FX-RS485'))

    def _to_review(self):
        """The designer's site handed in and with QC: the reviewer's correction window."""
        DesignAttempt.objects.create(
            assignment=self.assignment, attempt_number=1, opened_reason=ATTEMPT_REASON_INITIAL,
            boq_submitted_at=timezone.now(), boq_submitted_by=self.designer)
        self.assignment.current_attempt_number = 1
        self.assignment.status = DESIGN_IN_QC
        self.assignment.save(update_fields=['current_attempt_number', 'status'])


# ===========================================================================
# T1 / T2 — the designer's Remove and Restore
# ===========================================================================

class DesignerRemoveTests(DesignerBase):

    def test_remove_is_an_override_to_zero_with_its_note(self):
        response = self._remove()
        self.assertIn("FX-LOG removed. The line stays at 0 and the rule won't change it. "
                      "Use Restore to put the calculated figure back.", self._messages(response))
        self.assertEqual(self._removed_state(), (D(0), NOTE, 'absent'))
        override = BOQLineOverride.objects.get(line=self.logger)
        self.assertEqual((override.set_by, override.set_as), (self.designer,
                                                             OVERRIDE_SET_AS_DESIGNER))
        own = BOQRecalculation.objects.get(item_code='FX-LOG', source=FORMULA_SOURCE_OVERRIDE_SET)
        self.assertEqual((own.quantity_before, own.quantity_after, own.source),
                         (D(2), D(0), FORMULA_SOURCE_OVERRIDE_SET))
        self.assertFalse(BOQCorrection.objects.exists())

    def test_it_survives_two_saves_an_unchanged_upload_and_a_reviewer_correction(self):
        self._remove()
        for _ in range(2):
            self._picker({'FX-LA1': 2, 'FX-INV1': 12})
            self.assertEqual(self._removed_state(), (D(0), NOTE, 'absent'))

        # The DW-7 download, uploaded unchanged: DW-7b refuses the confirm, nothing moves.
        sheet = self.client.get(reverse('opex_boq_download', args=[self.site.project_id]))
        url = reverse('opex_boq_upload', args=[self.site.project_id])
        preview = self.client.post(url, {'phase': 'preview', 'file': SimpleUploadedFile(
            'boq.xlsx', sheet.content)})
        self.assertTrue(preview.context['no_changes'])
        confirm = self.client.post(url, {'phase': 'commit',
                                         'rows_json': preview.context['rows_json']}, follow=True)
        self.assertIn('This file changes nothing. Nothing was saved.', self._messages(confirm))
        self.assertEqual(self._removed_state(), (D(0), NOTE, 'absent'))

        # A reviewer corrects a driver of the removed line: the engine runs over it.
        self._to_review()
        self._login(self.qc)
        inverters = self._lines('FX-INV1')[0]
        self._correct(self.site, 'set_quantity', item_id=str(inverters.pk), quantity='22')
        self.assertEqual(self._qty('FX-INV1'), D(22))
        self.assertEqual(self._removed_state(), (D(0), NOTE, 'absent'))

    def test_restore_puts_the_calculated_figure_back(self):
        self._remove()
        self._restore()
        self.assertEqual(self._removed_state(), (D(2), None, D(10)))

    def test_the_popup_listed_the_chained_line_and_it_followed(self):
        before = self._added(self._picker_get())[self.items['FX-LOG'].pk]
        self.assertEqual(before['follow'], ['FX-RS485 10 → 0, removed from the BOQ'])
        self.assertNotIn('also', before)          # nothing stale on this sheet
        self.assertNotIn('follow', self._added(self._picker_get())[self.items['FX-EARTH'].pk])
        self._remove()
        self.assertEqual(self._qty('FX-RS485'), 'absent')

    def test_the_popup_counts_stale_lines_apart_from_what_follows(self):
        # FX-EARTH stored 7 where its rule gives 10: any save brings it up to date.
        earth = self._lines('FX-EARTH')[0]
        earth.boq_quantity = D(7)
        earth.save(update_fields=['boq_quantity'])
        row = self._added(self._picker_get())[self.items['FX-LOG'].pk]
        self.assertEqual((row['follow'], row['also']),
                         (['FX-RS485 10 → 0, removed from the BOQ'], 1))
        page = self._picker_get().content.decode()
        self.assertIn("'Saving also brings ' + calc.also + ' other calculated line'", page)

    def test_an_empty_note_is_refused_and_nothing_is_saved(self):
        response = self._remove(note='  ')
        self.assertIn('An override needs a note saying why the calculated figure is wrong '
                      'for this site.', self._messages(response))
        self.assertIn('Nothing was saved.', self._messages(response))
        self.assertEqual(self._removed_state(), (D(2), None, D(10)))
        # Answer 7: the pop-up answers it beside its button; its note box is not `required`.
        page = self._picker_get().content.decode()
        self.assertIn('id="boqRmError" hidden', page)
        textarea = page[page.index('id="boqRmNote"'):]
        self.assertNotIn('required', textarea[:textarea.index('>')])
        self.assertIn('if (!rmNote.value.trim()) {', page)


# ===========================================================================
# Reviewers: QC in QC's turn, the Head, and DW-2a
# ===========================================================================

class ReviewerRemoveTests(ReviewerOverrideBase):

    def _remove(self, line=None, note=NOTE):
        return self._correct(self.review, 'override_set', line_id=str((line or self.logger).pk),
                             quantity='0', note=note, remove='1')

    def test_qc_removes_in_qcs_turn_and_is_named(self):
        self._login(self.qc)
        self._remove()
        # B-2e Q2: the reviewer's path never deletes, so the chained line is set to 0.
        self.assertEqual((self._qty('FX-LOG', self.review), self._qty('FX-RS485', self.review)),
                         (D(0), D(0)))
        override = BOQLineOverride.objects.get()
        self.assertEqual((override.set_as, override.note), (OVERRIDE_SET_AS_QC, NOTE))
        self.assertFalse(BOQCorrection.objects.exists())
        page = self.client.get(reverse('boq_correct', args=[self.review.project_id]))
        self.assertContains(page, f'Removed (0) by QC (Fxqc): {NOTE}')
        self.assertContains(page, '>Restore</button>')

    def test_qc_is_refused_once_qc_passed_it_and_the_head_may(self):
        self._at(DESIGN_AWAITING_HEAD_QC)
        self._login(self.qc)
        self._remove()
        self.assertEqual(self._qty('FX-LOG', self.review), D(2))
        self.assertFalse(BOQLineOverride.objects.exists())
        self._login(self.head)
        self._remove()
        self.assertEqual(BOQLineOverride.objects.get().set_as, OVERRIDE_SET_AS_HEAD)
        page = self.client.get(reverse('boq_correct', args=[self.review.project_id]))
        self.assertContains(page, f'Removed (0) by the Head (Fxhead): {NOTE}')

    def test_the_popup_lists_what_follows_at_zero(self):
        self._login(self.qc)
        page = self.client.get(reverse('boq_correct', args=[self.review.project_id]))
        self.assertContains(page, f'id="boqRemoveModal{self.logger.pk}"')
        modal = page.content.decode()
        modal = modal[modal.index(f'id="boqRemoveModal{self.logger.pk}"'):]
        modal = modal[:modal.index('</form>')]
        self.assertIn('<li>FX-RS485 10 → 0</li>', modal)
        self.assertIn('name="quantity" value="0"', modal)
        self.assertIn('name="remove" value="1"', modal)

    def test_an_empty_note_is_answered_beside_the_remove_form(self):
        self._login(self.qc)
        response = self._remove(note='   ')
        self.assertEqual((response.status_code, response.redirect_chain), (200, []))
        self.assertEqual(self._messages(response), [])
        page = response.content.decode()
        modal = page[page.index(f'id="boqRemoveModal{self.logger.pk}"'):]
        modal = modal[:modal.index('</form>')]
        self.assertIn('data-open-on-load="1"', modal)
        self.assertIn(BOQ_REMOVE_NOTE_REQUIRED_MESSAGE, modal)
        # ...and not beside the Override form of the same row.
        override_form = page[page.index(f'name="line_id" value="{self.logger.pk}"'):]
        self.assertNotIn(BOQ_REMOVE_NOTE_REQUIRED_MESSAGE,
                         override_form[:override_form.index('</form>')])
        self.assertEqual(self._qty('FX-LOG', self.review), D(2))
        self.assertFalse(BOQLineOverride.objects.exists())
        self.assertFalse(BOQRecalculation.objects.exists())

    def test_refused_on_an_ordered_line(self):
        self.logger.ordered_quantity = D(2)
        self.logger.save(update_fields=['ordered_quantity'])
        self._login(self.qc)
        response = self._remove()
        self.assertIn(BOQ_REMOVE_ORDERED_MESSAGE, self._messages(response))
        self.assertEqual(self._qty('FX-LOG', self.review), D(2))
        self.assertFalse(BOQLineOverride.objects.exists())

    def test_refused_on_an_item_with_no_line(self):
        quiet = self._no_inverter_site()
        self._login(self.qc)
        response = self._create('FX-LOG', 0, NOTE, site=quiet)
        self.assertIn(BOQ_REMOVE_NO_LINE_MESSAGE.format(code='FX-LOG'), self._messages(response))
        self.assertEqual(self._qty('FX-LOG', quiet), 'absent')


# ===========================================================================
# T3 — refusals on the designer's path
# ===========================================================================

class DesignerRefusalTests(DesignerBase):

    def _assert_untouched(self):
        self.assertEqual(self._removed_state(), (D(2), None, D(10)))
        self.assertFalse(BOQRecalculation.objects.filter(
            item_code='FX-LOG', source=FORMULA_SOURCE_OVERRIDE_SET).exists())

    def test_refused_on_an_ordered_line(self):
        self.logger.ordered_quantity = D(2)
        self.logger.save(update_fields=['ordered_quantity'])
        self.assertIn(BOQ_REMOVE_ORDERED_MESSAGE, self._messages(self._remove()))
        self._assert_untouched()
        # An override to any other figure on that line is unchanged by DW-3 (answer 4).
        self._override('set', self.logger, quantity='3', note='three roofs')
        self.assertEqual(self._qty('FX-LOG'), D(3))

    def test_refused_on_a_mandatory_item(self):
        # No calculated item is mandatory today; the fixture makes one.
        master = self.items['FX-LOG']
        master.is_mandatory = True
        master.save(update_fields=['is_mandatory'])
        self.assertIn(BOQ_REMOVE_MANDATORY_MESSAGE.format(code='FX-LOG'),
                      self._messages(self._remove()))
        self._assert_untouched()

    def test_refused_on_an_item_with_no_line(self):
        inverters = self._lines('FX-INV1')[0]
        self._picker({'FX-LA1': 2})                 # no inverters: FX-LOG goes (D1)
        self.assertEqual(self._qty('FX-LOG'), 'absent')
        self.assertFalse(BOQ.objects.get(project=self.site).items.filter(pk=inverters.pk).exists())
        response = self._override_master('FX-LOG', quantity='0', note=NOTE)
        self.assertIn(BOQ_REMOVE_NO_LINE_MESSAGE.format(code='FX-LOG'), self._messages(response))
        self.assertEqual(self._qty('FX-LOG'), 'absent')
        self.assertFalse(BOQLineOverride.objects.exists())

    def test_remove_is_disabled_while_the_sheet_has_unsaved_changes(self):
        page = self._picker_get().content.decode()
        # The Remove button carries data-ovr, which syncOverrides() disables while dirty...
        button = page[page.index("boq-rm-btn"):]
        self.assertIn('data-ovr', button[:60])
        self.assertIn("document.querySelectorAll('[data-ovr]')", page)
        self.assertIn('button.disabled = dirty;', page)
        # ...and its pop-up refuses to open while dirty, the backstop.
        remove_js = page[page.index("getElementById('boqRemoveModal')"):]
        self.assertIn('if (!button || dirty) { event.preventDefault(); return; }',
                      remove_js[:400])
        self.assertIn('Save your changes first: Override, Remove and Restore work on the '
                      'saved BOQ.', page)


# ===========================================================================
# T4 — the markers on every screen
# ===========================================================================

class MarkerTests(DesignerBase):

    def setUp(self):
        super().setUp()
        self._remove()

    def test_the_picker_greys_it_and_names_who(self):
        row = self._added(self._picker_get())[self.items['FX-LOG'].pk]
        self.assertEqual((row['qty'], row['ovr'], row['rm'], row['note']),
                         ('0', True, DESIGNER_MARK, NOTE))
        self.assertNotIn('follow', row)              # no Remove on a removed line
        page = self._picker_get().content.decode()
        self.assertIn("(calc.rm ? ' class=\"boq-removed-row\"' : '')", page)
        self.assertIn("(calc.rm ? 'Restore' : 'Clear')", page)

    def test_the_correction_screen_and_the_review_panel(self):
        self._to_review()
        self._login(self.qc)
        for name in ('boq_correct', 'design_qc_review'):
            page = self.client.get(reverse(name, args=[self.site.project_id]))
            self.assertContains(page, f'{DESIGNER_MARK}: {NOTE}', msg_prefix=name)
            self.assertContains(page, 'boq-removed-row', msg_prefix=name)

    def test_the_boq_view_names_who_for_design_only(self):
        self._to_review()
        self._login(self.qc)
        page = self.client.get(reverse('boq_detail', args=[self.site.project_id]))
        self.assertContains(page, f'{DESIGNER_MARK}: {NOTE}')
        for profile in (self.scm, self.pm):
            self._login(profile)
            page = self.client.get(reverse('boq_detail', args=[self.site.project_id]))
            self.assertContains(page, 'boq-removed-marker">Removed (0)</div>')
            self.assertNotContains(page, NOTE)
            self.assertNotContains(page, 'Fxdes')
        # Answer 11: the removed line reads 0, not "—" (SCM's BOQ Qty cell).
        self._login(self.scm)
        page = self.client.get(reverse('boq_detail', args=[self.site.project_id]))
        self.assertContains(page, '<td class="align-middle text-muted">0</td>')

    def test_the_site_card_says_removed_and_nothing_else(self):
        rows = _boq_rows(self.site)
        by_code = {row['code']: row for row in rows}
        self.assertTrue(by_code['FX-LOG']['removed'])
        self.assertFalse(by_code['FX-EARTH']['removed'])
        card = render_to_string('projects/_site_drawings_card.html', {'site_drawings': {
            'state': 'released', 'boq_rows': rows, 'drawings': [], 'boq_line_count': len(rows)}})
        self.assertIn('Removed (0)', card)
        self.assertNotIn(NOTE, card)
        self.assertNotIn('Fxdes', card)

    def test_the_download_marks_it_removed(self):
        from openpyxl import load_workbook
        sheet = self.client.get(reverse('opex_boq_download', args=[self.site.project_id]))
        ws = load_workbook(io.BytesIO(sheet.content))['BOQ']
        header = [cell.value for cell in ws[1]]
        rows = {row[0]: dict(zip(header, row)) for row in ws.iter_rows(min_row=2, values_only=True)}
        self.assertEqual((rows['FX-LOG']['Quantity'], rows['FX-LOG']['Calculated']),
                         (0, f'Removed: {NOTE}'))
        self.assertEqual(rows['FX-EARTH']['Calculated'], 'Calculated')

    def test_an_override_to_another_figure_keeps_the_overridden_marker(self):
        self._override('set', self.logger, quantity='3', note='three roofs')
        row = self._added(self._picker_get())[self.items['FX-LOG'].pk]
        self.assertNotIn('rm', row)
        self.assertEqual(row['by'], 'Overridden by the designer (Fxdes)')
        sheet = self.client.get(reverse('opex_boq_download', args=[self.site.project_id]))
        self.assertIn(b'Overridden: three roofs', _sheet_text(sheet.content))

    def test_design_groups_leave_it_out_with_no_new_warning(self):
        live = consolidate([{'project': self.site, 'counted': True,
                             'code': self.site.project_id}])
        codes = {line['item_master__code'] for line in live['lines']}
        self.assertNotIn('FX-LOG', codes)            # no 0 row: aggregation counts > 0
        self.assertIn('FX-EARTH', codes)
        self.assertEqual((live['blanks'], live['unit_warnings'], live['content_gaps']),
                         ([], [], []))

    def test_dw1s_check_is_unchanged(self):
        boq = BOQ.objects.get(project=self.site)
        removed = boq_minimum_content_gaps(boq)
        self._restore()
        self.assertEqual(boq_minimum_content_gaps(boq), removed)


def _sheet_text(content):
    """Every cell value of the BOQ sheet, as bytes, for a substring check."""
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(content))['BOQ']
    return '|'.join(str(v) for row in ws.iter_rows(values_only=True) for v in row).encode()


# ===========================================================================
# T5 — Clear BOQ at the top of the picker
# ===========================================================================

class ClearBoqPlacementTests(DesignerBase):

    def test_the_designer_gets_it_in_the_top_bar(self):
        page = self._picker_get().content.decode()
        for marker in ('id="boqUploadButton"', 'id="boqClearToggle"', 'id="boqClear"'):
            self.assertIn(marker, page)
            self.assertLess(page.index(marker), page.index('id="boqForm"'), marker)
        # Still behind its typed-code confirmation.
        self.assertIn('id="boqClearButton" disabled', page)

    def test_nobody_else_and_not_outside_the_window(self):
        self._to_review()
        self._login(self.designer)                  # design-locked now
        page = self._picker_get().content.decode()
        for marker in ('id="boqClearToggle"', 'id="boqClear"', 'id="boqUploadButton"'):
            self.assertNotIn(marker, page)
        self._login(self.qc)
        page = self._picker_get().content.decode()
        self.assertNotIn('id="boqClearToggle"', page)


# ===========================================================================
# Queries
# ===========================================================================

class QueryTests(DesignerBase):

    def _screens(self):
        self._to_review()
        counts = {}
        for name, profile, url in [
                ('correct (QC)', self.qc, reverse('boq_correct', args=[self.site.project_id])),
                ('review (QC)', self.qc, reverse('design_qc_review', args=[self.site.project_id])),
                ('boq_detail (SCM)', self.scm, reverse('boq_detail', args=[self.site.project_id])),
                ('boq_detail (QC)', self.qc, reverse('boq_detail', args=[self.site.project_id])),
                ('download (PM)', self.pm, reverse('opex_boq_download',
                                                   args=[self.site.project_id]))]:
            self._login(profile)
            self.client.get(url)
            counts[name] = _n(lambda: self.client.get(url))[0]
        with CaptureQueriesContext(connection) as ctx:
            _boq_rows(self.site)
        counts['site card rows'] = len(ctx)
        return counts

    def _picker_count(self):
        self._picker_get()
        return _n(self._picker_get)[0]

    def test_the_markers_and_the_popup_list_cost_no_query(self):
        # The same sheet, with an override to another figure and with a Remove: the Removed
        # marker, its "who" and the pop-up's list are worked out from rows already loaded.
        self._override('set', self.logger, quantity='3', note='three roofs')
        picker_overridden = self._picker_count()
        self._remove()
        picker_removed = self._picker_count()
        self.assertEqual(picker_removed, picker_overridden)
        removed = self._screens()
        self.assertEqual(removed['site card rows'], 1)

        other = self._other_site_with_an_override_to_three()
        self.assertEqual(other, removed)

    def _other_site_with_an_override_to_three(self):
        """_screens() on a fresh copy of the sheet whose FX-LOG is overridden to 3."""
        site, assignment = self._opex_site('FX-B')
        self.site, self.assignment = site, assignment
        self._login(self.designer)
        self._picker({'FX-LA1': 2, 'FX-INV1': 12})
        self._override('set', self._lines('FX-LOG')[0], quantity='3', note='three roofs')
        return self._screens()

    def test_the_remove_posts_are_pinned(self):
        self._picker_get()                          # warm the session
        designer = _n(lambda: self.client.post(
            reverse('opex_boq_override', args=[self.site.project_id]),
            {'action': 'set', 'item_id': str(self.logger.pk), 'quantity': '0',
             'note': NOTE}))
        self.assertEqual(designer[1].status_code, 302)

        review = ReviewerSite(self)
        self._login(self.qc)
        self.client.get(review.url)
        qc = _n(lambda: self.client.post(review.url, {
            'action': 'override_set', 'remove': '1', 'line_id': str(review.logger.pk),
            'quantity': '0', 'note': NOTE}))
        self.assertEqual(qc[1].status_code, 302)
        self.assertEqual({'designer': designer[0], 'qc': qc[0]}, REMOVE_POSTS)


    def test_r6_adds_one_query_to_a_set_to_zero(self):
        """The same Remove on two identical sheets, once with the order-line test stubbed:
        exactly one query apart, the order-line exists()."""
        from unittest import mock
        self._picker_get()
        url = reverse('opex_boq_override', args=[self.site.project_id])
        real = _n(lambda: self.client.post(url, {
            'action': 'set', 'item_id': str(self.logger.pk), 'quantity': '0', 'note': NOTE}))[0]
        site, _assignment = self._opex_site('FX-B')
        self._picker({'FX-LA1': 2, 'FX-INV1': 12}, site=site)
        logger = self._lines('FX-LOG', site)[0]
        url = reverse('opex_boq_override', args=[site.project_id])
        with mock.patch('projects.views.formula_line_blocked', return_value=False):
            stubbed = _n(lambda: self.client.post(url, {
                'action': 'set', 'item_id': str(logger.pk), 'quantity': '0',
                'note': NOTE}))[0]
        self.assertEqual(real - stubbed, 1)


class ReviewerSite:
    """A second site, in QC, with the same sheet as DesignerBase's after its picker save."""

    def __init__(self, case):
        site = case._review_site('FX-R')
        for code, quantity in [('FX-LA1', 2), ('FX-EARTH', 10), ('FX-INV1', 12),
                               ('FX-LOG', 2), ('FX-RS485', 10)]:
            case._line(code, quantity, site=site)
        self.logger = case._lines('FX-LOG', site)[0]
        self.url = reverse('boq_correct', args=[site.project_id])

