"""
DW-2b — QC and the Head may edit, add and remove ACCBs.

WHY THIS FILE EXISTS
--------------------
Until DW-2b a reviewer who found a wrong ACCB could only fail the BOQ back (B-3 P7). Zuber's
rulings (26-27 Nov 2026), pinned here:

  * DW-R3 — QC and the Head edit an ACCB through the designer's own configurator, in a
    pop-up on the correction screen, each in their own turn (the `accb_edit` row of
    permissions.DESIGN_REVIEWER_TURN_WINDOWS; its generated cases are in
    tests_design_reviewer_turn). Each change is a reviewer correction, counted against
    the designer.
  * answer 6 — edit, add (a duplicate is an add) and remove, one BOQCorrection each,
    refused on a line on an SCM order (Q1: ordered quantity, an order line naming the line,
    or an order sized against the site carrying the configuration).
  * answer 3 — never under a change request, for the Head either.
  * A4/Q6 — a reason is required; an empty one is answered beside the form, with the
    pop-up open again and the setup kept.
  * A6 — a remove that would leave no DCDB, ACDB or ACCB (DW-1's test) is refused.
  * A1/A8 — the designer's endpoint and the reviewer's share _accb_apply(), so B-3c's
    validation is the same on both, and the designer's path is unchanged.
  * A7/Q4 — every BOQCorrection reader words and counts ACCB corrections.

RUNNER: seeds catalogue codes in setUp (ACCBBase), so it runs with migrations disabled —
`manage.py test projects --settings=solarpms.test_settings`.
"""
import json
from decimal import Decimal
from io import BytesIO

from django.contrib.messages import get_messages
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .accb import (
    ACCB_CHANGE_REQUEST_MESSAGE, ACCB_LAST_BOARD_MESSAGE, ACCB_ON_ORDER_MESSAGE,
    ACCB_ON_ORDER_NOTE, ACCB_REASON_REQUIRED_MESSAGE, ACCB_REENTER_MESSAGE,
    accb_config_key, accb_description,
)
from .design_metrics import reviewer_correction_drill, reviewer_correction_panel, tender_metrics
from .design_views import aggregate_group_boq
from .models import (
    ACCBConfig, ActivityLog, BOQ, BOQCorrection, BOQItem, BOQItemGroup, BOQItemGroupMember,
    DesignAssignment, DesignChangeRequest,
    BOQ_CORRECTION_ACCB_ADD, BOQ_CORRECTION_ACCB_EDIT, BOQ_CORRECTION_ACCB_REMOVE,
    CHANGE_REQUEST_ORIGIN_PM, CHANGE_REQUEST_PENDING,
    DESIGN_ARTIFACTS_UPLOADED, DESIGN_AWAITING_HEAD_QC, DESIGN_IN_DESIGN, DESIGN_IN_QC,
)
from .permissions import REVIEWER_TURN_QC_PASSED_MESSAGE
from .tests_accb_config import (
    ACCBBase, EX_TYPE1, EX_TYPE1_TEXT, EX_TYPE2, EX_TYPE2_TEXT, accb_post_data, cfg,
)
from .tests_accb_order import ACCBOrderFixture
from .views import BOQ_DOWNLOAD_ACCB_SHEET, BOQ_CORRECTION_GROUP_LOCKED_MESSAGE, accb_lines_on_order

D = Decimal

#: EX_TYPE2 with its one O/G feeder changed and SPD ticked — the browser check's edit.
EX_TYPE2_EDITED = cfg('Type-2', [('MCCB', 100), ('MCB', 63)], [('MCCB', 250, True)])
REASON = 'O/G feeder is 250A on the SLD'


class ReviewerACCBBase(ACCBBase):
    """ACCBBase's catalogue and people, with a site in Design QC: the BOQ stamped (attempt
    1), one module line and ACCB 1 (EX_TYPE2), written by the designer's own create helper."""

    def setUp(self):
        super().setUp()
        self.site, self.assignment = self._opex_site('AC-QC', status=DESIGN_IN_QC)
        self._line('OPX-001', 10)
        self.accb = self._seed_accb(EX_TYPE2)
        self._design_lock()
        self.url = reverse('boq_correct', kwargs={'project_id': self.site.project_id})

    # ── helpers ─────────────────────────────────────────────────────────────

    def _status(self, status):
        DesignAssignment.objects.filter(pk=self.assignment.pk).update(status=status)

    def _post(self, who, action, config=None, accb=None, reason=REASON, extra=None):
        self._login(who)
        data = {'accb': '1', 'action': action, 'reason': reason}
        if accb is not None:
            data['accb_id'] = str(accb.pk)
        if config is not None:
            data.update(accb_post_data(config))
        data.update(extra or {})
        response = self.client.post(self.url, data)
        self.client.logout()
        return response

    def _messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def _boards(self):
        """Load DW-1's three groups (the test database has none, so DW-1 skips): the module
        stands in for an inverter, OPX-002 is a DCDB and OPX-039 an ACDB."""
        for name, code in (('all inverters', 'OPX-001'), ('all DCDBs', 'OPX-002'),
                           ('all ACDBs', 'OPX-039')):
            group = BOQItemGroup.objects.create(name=name, label=name)
            BOQItemGroupMember.objects.create(group=group, item=self._master(code))

    def _pending_change_request(self):
        return DesignChangeRequest.objects.create(
            attempt=self.assignment.attempts.get(attempt_number=1), requested_by=self.pm,
            reason='One more feeder.', verdict=CHANGE_REQUEST_PENDING,
            origin=CHANGE_REQUEST_ORIGIN_PM)


# ===========================================================================
# T3 — QC edits, adds, duplicates and removes, one correction each
# ===========================================================================

class ReviewerActionTests(ReviewerACCBBase):

    def test_qc_edits_an_accb(self):
        response = self._post(self.qc, 'save', EX_TYPE2_EDITED, accb=self.accb)
        self.assertRedirects(response, self.url + '#accbs', fetch_redirect_response=False)
        line = BOQItem.objects.get(pk=self.accb.boq_item_id)
        self.assertEqual(line.description, accb_description(EX_TYPE2_EDITED))
        self.assertEqual(line.boq_quantity, D('1'))
        accb = ACCBConfig.objects.get(pk=self.accb.pk)
        self.assertEqual(accb.config_key, accb_config_key(EX_TYPE2_EDITED))
        self.assertEqual(accb.updated_by, self.qc)
        correction = BOQCorrection.objects.get()
        self.assertEqual(
            (correction.action, correction.accb_number, correction.item, correction.item_code,
             correction.description_before, correction.description_after,
             correction.quantity_before, correction.quantity_after, correction.reason,
             correction.corrected_by, correction.designer_at_correction),
            (BOQ_CORRECTION_ACCB_EDIT, 1, line, 'OPX-273', EX_TYPE2_TEXT,
             accb_description(EX_TYPE2_EDITED), D('1'), D('1'), REASON, self.qc,
             self.designer))
        self.assertEqual(correction.accb_text(),
                         f'ACCB 1 edited: {EX_TYPE2_TEXT} → {accb_description(EX_TYPE2_EDITED)}')
        log = ActivityLog.objects.get(action_code='boq_accb_changed')
        self.assertEqual(log.action, 'Qcacc (QC) changed ACCB 1 on AC-QC '
                                     '(Type-2 · I/C 2 · O/G 1 · SPD 1; feeders changed)')
        self.assertIn('ACCB 1 saved. The correction is recorded against your name.',
                      self._messages(response))

    def test_qc_adds_an_accb(self):
        self._post(self.qc, 'add', EX_TYPE1)
        accb = ACCBConfig.objects.get(number=2)
        self.assertEqual(accb.boq_item.description, EX_TYPE1_TEXT)
        correction = BOQCorrection.objects.get()
        self.assertEqual(
            (correction.action, correction.accb_number, correction.item_id,
             correction.description_before, correction.description_after,
             correction.quantity_before, correction.quantity_after),
            (BOQ_CORRECTION_ACCB_ADD, 2, accb.boq_item_id, '', EX_TYPE1_TEXT, None, D('1')))
        self.assertEqual(correction.accb_text(), f'ACCB 2 added: {EX_TYPE1_TEXT}')
        self.assertEqual(ActivityLog.objects.get(action_code='boq_accb_added').action,
                         'Qcacc (QC) added ACCB 2 on AC-QC (Type-1 · I/C 3 · O/G 2 · SPD 1)')

    def test_qc_duplicates_an_accb_as_an_add(self):
        response = self._post(self.qc, 'duplicate', accb=self.accb)
        copy = ACCBConfig.objects.get(number=2)
        self.assertEqual(copy.config_key, self.accb.config_key)
        correction = BOQCorrection.objects.get()
        self.assertEqual((correction.action, correction.accb_number,
                          correction.description_after),
                         (BOQ_CORRECTION_ACCB_ADD, 2, EX_TYPE2_TEXT))
        self.assertIn('copy of ACCB 1',
                      ActivityLog.objects.get(action_code='boq_accb_added').action)
        self.assertIn('ACCB 1 duplicated as ACCB 2. The correction is recorded against your '
                      'name.', self._messages(response))

    def test_qc_removes_an_accb_and_the_record_keeps_the_text(self):
        line_id = self.accb.boq_item_id
        self._post(self.qc, 'remove', accb=self.accb)
        self.assertFalse(BOQItem.objects.filter(pk=line_id).exists())
        self.assertFalse(ACCBConfig.objects.filter(pk=self.accb.pk).exists())
        correction = BOQCorrection.objects.get()
        self.assertEqual(
            (correction.action, correction.accb_number, correction.item,
             correction.item_description, correction.description_before,
             correction.description_after, correction.quantity_before,
             correction.quantity_after),
            (BOQ_CORRECTION_ACCB_REMOVE, 1, None, EX_TYPE2_TEXT, EX_TYPE2_TEXT, '', D('1'),
             None))
        self.assertEqual(correction.accb_text(), f'ACCB 1 removed: {EX_TYPE2_TEXT}')
        self.assertEqual(ActivityLog.objects.get(action_code='boq_accb_removed').action,
                         'Qcacc (QC) removed ACCB 1 on AC-QC (Type-2 · I/C 2 · O/G 1 · SPD 1)')
        # B-3a.1: the number is retired.
        self._post(self.qc, 'add', EX_TYPE1)
        self.assertTrue(ACCBConfig.objects.filter(number=2).exists())

    def test_the_head_is_tagged_head(self):
        self._post(self.head, 'save', EX_TYPE2_EDITED, accb=self.accb)
        self.assertTrue(ActivityLog.objects.get(action_code='boq_accb_changed')
                        .action.startswith('Headacc (Head) changed ACCB 1'))

    def test_an_unchanged_save_records_nothing(self):
        response = self._post(self.qc, 'save', EX_TYPE2, accb=self.accb)
        self.assertFalse(BOQCorrection.objects.exists())
        self.assertIn('ACCB 1: nothing changed, so nothing was recorded.',
                      self._messages(response))

    def test_an_accb_on_another_site_is_a_404(self):
        other, _ = self._opex_site('AC-OTHER')
        foreign = self._seed_accb(EX_TYPE1, site=other)
        response = self._post(self.qc, 'remove', accb=foreign)
        self.assertEqual(response.status_code, 404)
        self.assertTrue(ACCBConfig.objects.filter(pk=foreign.pk).exists())

    def test_each_action_is_one_correction_on_the_designers_count(self):
        """Answer 6 / Q4: four actions, four corrections credited to the designer."""
        self._post(self.qc, 'save', EX_TYPE2_EDITED, accb=self.accb)
        self._post(self.qc, 'add', EX_TYPE1)
        self._post(self.qc, 'duplicate', accb=self.accb)
        self._post(self.qc, 'remove', accb=ACCBConfig.objects.get(number=3))
        self.assertEqual(BOQCorrection.objects.count(), 4)
        row = next(r for r in reviewer_correction_panel(tender_metrics(self.program)['sites'])
                   ['rows'] if r['designer'].pk == self.designer.pk)
        self.assertEqual((row['accb_changes'], row['quantity_edits'], row['lines_added'],
                          row['sites_corrected']), (4, 0, 0, 1))


# ===========================================================================
# The turn, the change request, and the refusals (A4, A5, A6)
# ===========================================================================

class TurnAndRefusalTests(ReviewerACCBBase):

    def test_qc_is_allowed_while_handed_in_and_in_qc(self):
        for status in (DESIGN_ARTIFACTS_UPLOADED, DESIGN_IN_QC):
            with self.subTest(status=status):
                self._status(status)
                before = BOQCorrection.objects.count()
                self._post(self.qc, 'add', EX_TYPE1)
                self.assertEqual(BOQCorrection.objects.count(), before + 1)

    def test_qc_is_refused_at_awaiting_head_qc_and_the_head_is_allowed(self):
        self._status(DESIGN_AWAITING_HEAD_QC)
        response = self._post(self.qc, 'save', EX_TYPE2_EDITED, accb=self.accb)
        # QC's own correction turn is closed too, so the screen itself sends QC away.
        self.assertIn(REVIEWER_TURN_QC_PASSED_MESSAGE, self._messages(response))
        self.assertFalse(BOQCorrection.objects.exists())
        self._post(self.head, 'save', EX_TYPE2_EDITED, accb=self.accb)
        self.assertEqual(BOQCorrection.objects.get().corrected_by, self.head)

    def test_a_pending_change_request_refuses_both_with_no_head_exception(self):
        """Answer 3: the Head may still correct quantities here (BC-1), not ACCBs."""
        self._pending_change_request()
        for who in (self.qc, self.head):
            with self.subTest(who=who.user.username):
                response = self._post(who, 'save', EX_TYPE2_EDITED, accb=self.accb)
                self.assertFalse(BOQCorrection.objects.exists())
                self.assertEqual(ACCBConfig.objects.get(pk=self.accb.pk).config_key,
                                 accb_config_key(EX_TYPE2))
        self.assertIn(ACCB_CHANGE_REQUEST_MESSAGE, self._messages(response))
        # The Head's screen is open (BC-1) but offers no ACCB buttons, and says why.
        self._login(self.head)
        html = self.client.get(self.url).content.decode()
        self.assertNotIn('data-accb-intent', html)
        self.assertIn("A change request is open on this design, so ACCBs can&#x27;t be "
                      "changed until it is decided.", html)

    def test_the_designer_is_refused(self):
        self._login(self.designer)
        response = self.client.post(self.url, {'accb': '1', 'action': 'remove',
                                               'accb_id': str(self.accb.pk), 'reason': 'x'})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(ACCBConfig.objects.filter(pk=self.accb.pk).exists())

    def test_an_ordered_line_is_refused_for_edit_and_remove_not_duplicate(self):
        BOQItem.objects.filter(pk=self.accb.boq_item_id).update(ordered_quantity=D('1'))
        for action, config in (('save', EX_TYPE2_EDITED), ('remove', None)):
            with self.subTest(action=action):
                response = self._post(self.qc, action, config, accb=self.accb)
                self.assertIn(ACCB_ON_ORDER_MESSAGE.format(number=1), self._messages(response))
                self.assertTrue(ACCBConfig.objects.filter(pk=self.accb.pk,
                                                          config_key=self.accb.config_key)
                                .exists())
        self.assertFalse(BOQCorrection.objects.exists())
        # A duplicate changes nothing on the order (answer 6).
        self._post(self.qc, 'duplicate', accb=self.accb)
        self.assertEqual(BOQCorrection.objects.get().action, BOQ_CORRECTION_ACCB_ADD)

    def test_an_ordered_accb_shows_greyed_buttons_and_the_note(self):
        BOQItem.objects.filter(pk=self.accb.boq_item_id).update(ordered_quantity=D('1'))
        self._login(self.qc)
        html = self.client.get(self.url).content.decode()
        self.assertIn(ACCB_ON_ORDER_NOTE, html)
        self.assertNotIn(f'data-accb-id="{self.accb.pk}"', html)
        self.assertIn('data-accb-intent="add"', html)

    def test_the_group_lock_refuses(self):
        self._group_lock()
        response = self._post(self.qc, 'add', EX_TYPE1)
        self.assertIn(BOQ_CORRECTION_GROUP_LOCKED_MESSAGE, self._messages(response))
        self.assertEqual(ACCBConfig.objects.count(), 1)

    def test_an_old_format_accb(self):
        """A5 (Q9): an edit posts a fresh setup and B-3c validates it; a duplicate of an
        old-format ACCB is told to re-enter the switchgear, as the designer is."""
        legacy = self._seed_legacy_accb()
        response = self._post(self.qc, 'save', accb=legacy, extra={
            'accb_type': 'Type-2', 'in_option': ['MCCB-100'], 'out_option': [''],
            'out_spd': ['1']})
        self.assertIn('O/G feeder 1: choose the switchgear from the list.',
                      self._messages(response))
        response = self._post(self.qc, 'duplicate', accb=legacy)
        self.assertIn(f'ACCB {legacy.number} cannot be duplicated: {ACCB_REENTER_MESSAGE}.',
                      self._messages(response))
        self.assertFalse(BOQCorrection.objects.exists())
        # Re-entered in full, the edit saves.
        self._post(self.qc, 'save', EX_TYPE1, accb=legacy)
        self.assertEqual(BOQCorrection.objects.get().description_before,
                         legacy.boq_item.description)


class LastBoardTests(ReviewerACCBBase):
    """A6: a remove that would leave the BOQ with no DCDB, ACDB or ACCB is refused."""

    def test_the_only_board_cannot_be_removed(self):
        self._boards()
        response = self._post(self.qc, 'remove', accb=self.accb)
        self.assertIn(ACCB_LAST_BOARD_MESSAGE, self._messages(response))
        self.assertTrue(ACCBConfig.objects.filter(pk=self.accb.pk).exists())
        self.assertFalse(BOQCorrection.objects.exists())

    def test_with_a_dcdb_present_the_remove_is_allowed(self):
        self._boards()
        self._line('OPX-002', 1)
        self._post(self.qc, 'remove', accb=self.accb)
        self.assertFalse(ACCBConfig.objects.filter(pk=self.accb.pk).exists())

    def test_a_second_accb_is_a_board_too(self):
        self._boards()
        self._seed_accb(EX_TYPE1)
        self._post(self.qc, 'remove', accb=self.accb)
        self.assertEqual(list(ACCBConfig.objects.values_list('number', flat=True)), [2])

    def test_edits_and_adds_are_never_refused_for_it(self):
        self._boards()
        self._post(self.qc, 'save', EX_TYPE2_EDITED, accb=self.accb)
        self._post(self.qc, 'add', EX_TYPE1)
        self.assertEqual(BOQCorrection.objects.count(), 2)


class ReasonTests(ReviewerACCBBase):
    """A4/Q6: no reason, nothing saved; the message beside the form, the pop-up open again
    on the card, the typed setup kept."""

    def test_an_empty_reason_is_answered_beside_the_form(self):
        for reason in ('', '   '):
            with self.subTest(reason=repr(reason)):
                self._login(self.qc)
                data = {'accb': '1', 'action': 'save', 'accb_id': str(self.accb.pk),
                        'reason': reason}
                data.update(accb_post_data(EX_TYPE2_EDITED))
                response = self.client.post(self.url, data)
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                self.assertIn('id="accbReasonError">Give a reason for this ACCB change. '
                              'Nothing was saved.</div>', html)
                self.assertIn('data-open-on-load="1"', html)
                # The pop-up's data, as json_script hands it to the page.
                data = json.loads(json.dumps(response.context['accb_data']))
                self.assertEqual(data['error'], {
                    'id': self.accb.pk, 'intent': 'edit',
                    'message': ACCB_REASON_REQUIRED_MESSAGE,
                    'posted': {'type': 'Type-2',
                               'in': [{'option': 'MCCB-100'}, {'option': 'MCB-63'}],
                               'out': [{'option': 'MCCB-250', 'spd': True}]}})
        self.assertFalse(BOQCorrection.objects.exists())
        self.assertEqual(ACCBConfig.objects.get(pk=self.accb.pk).config_key,
                         accb_config_key(EX_TYPE2))

    def test_a_remove_with_no_reason_opens_its_confirmation_again(self):
        self._login(self.qc)
        response = self.client.post(self.url, {'accb': '1', 'action': 'remove',
                                               'accb_id': str(self.accb.pk), 'reason': ''})
        self.assertEqual(response.context['accb_error']['intent'], 'remove')
        self.assertTrue(ACCBConfig.objects.filter(pk=self.accb.pk).exists())

    def test_a_typed_option_outside_the_list_comes_back_blank(self):
        self._login(self.qc)
        response = self.client.post(self.url, {
            'accb': '1', 'action': 'add', 'reason': '', 'accb_type': 'Type-9',
            'in_option': ['<b>x</b>'], 'out_option': ['MCB-25'], 'out_spd': ['0']})
        self.assertEqual(response.context['accb_error']['posted'], {
            'type': 'Type-1', 'in': [{'option': ''}],
            'out': [{'option': 'MCB-25', 'spd': False}]})
        self.assertNotIn('<b>x</b>', response.content.decode())


# ===========================================================================
# A1 — one save path: B-3c validation is the same on both
# ===========================================================================

#: Bad setups, each posted as an add through the designer's endpoint and the reviewer's.
BAD_SETUPS = [
    ('unknown type', {'accb_type': 'Type-3', 'in_option': ['MCB-25'],
                      'out_option': ['MCB-25'], 'out_spd': ['0']}),
    ('blank incomer', {'accb_type': 'Type-1', 'in_option': [''],
                       'out_option': ['MCB-25'], 'out_spd': ['0']}),
    ('a rating not in the list', {'accb_type': 'Type-1', 'in_option': ['MCCB-150'],
                                  'out_option': ['MCB-25'], 'out_spd': ['0']}),
    ('missing SPD value', {'accb_type': 'Type-1', 'in_option': ['MCB-25'],
                           'out_option': ['MCB-25', 'MCB-30'], 'out_spd': ['0']}),
    ('SPD on an incomer', {'accb_type': 'Type-1', 'in_option': ['MCB-25'], 'in_spd': ['1'],
                           'out_option': ['MCB-25'], 'out_spd': ['0']}),
    ('no outgoing feeder', {'accb_type': 'Type-1', 'in_option': ['MCB-25']}),
    ('21 incomers', {'accb_type': 'Type-1', 'in_option': ['MCB-25'] * 21,
                     'out_option': ['MCB-25'], 'out_spd': ['0']}),
]


class SameValidationTests(ReviewerACCBBase):

    def test_both_paths_refuse_the_same_setups_in_the_same_words(self):
        designer_site, _ = self._opex_site('AC-DES', status=DESIGN_IN_DESIGN)
        designer_url = reverse('opex_boq_accb', kwargs={'project_id': 'AC-DES'})
        for label, setup in BAD_SETUPS:
            with self.subTest(setup=label):
                self._login(self.designer)
                designer = self._messages(self.client.post(
                    designer_url, dict(setup, action='add')))
                self.client.logout()
                reviewer = self._messages(self._post(self.qc, 'add', extra=setup))
                self.assertTrue(designer, label)
                self.assertEqual(reviewer, designer)
                self.assertEqual(reviewer[-1], 'Nothing was saved.')
        self.assertEqual(ACCBConfig.objects.count(), 1)
        self.assertFalse(BOQCorrection.objects.exists())


# ===========================================================================
# A7 — every reader of BOQCorrection words and counts the ACCB corrections
# ===========================================================================

class ReaderTests(ReviewerACCBBase):

    def setUp(self):
        super().setUp()
        self._post(self.qc, 'save', EX_TYPE2_EDITED, accb=self.accb)
        self._post(self.qc, 'add', EX_TYPE1)
        self._post(self.qc, 'remove', accb=ACCBConfig.objects.get(number=2))
        self.edited = accb_description(EX_TYPE2_EDITED)

    def _assert_three_in(self, html):
        # The descriptions hold '&', which the page escapes.
        self.assertIn(f'ACCB 1 edited: {EX_TYPE2_TEXT} → {self.edited}'.replace('&', '&amp;'),
                      html)
        self.assertIn(f'ACCB 2 added: {EX_TYPE1_TEXT}'.replace('&', '&amp;'), html)
        self.assertIn(f'ACCB 2 removed: {EX_TYPE1_TEXT}'.replace('&', '&amp;'), html)

    def test_the_correction_screen_trail(self):
        self._login(self.qc)
        response = self.client.get(self.url)
        html = response.content.decode()
        self._assert_three_in(html)
        self.assertIn(f'Reason: {REASON}', html)
        self.assertEqual(len(response.context['corrections']), 3)

    def test_the_boq_history_trail(self):
        self._login(self.admin)
        response = self.client.get(reverse('boq_history',
                                           kwargs={'project_id': self.site.project_id}))
        self._assert_three_in(response.content.decode())
        self.assertEqual(len(response.context['corrections']), 3)

    def test_the_tender_dashboard_column_and_drill(self):
        self._login(self.head)
        response = self.client.get(
            reverse('design_tender_dashboard', kwargs={'pk': self.program.pk})
            + f'?corrections={self.designer.pk}')
        row = next(r for r in response.context['corrections']['rows']
                   if r['designer'].pk == self.designer.pk)
        self.assertEqual((row['accb_changes'], row['sites_corrected'], row['reviewed']),
                         (3, 1, 1))
        self.assertEqual(response.context['corrections']['total_edits'], 3)
        html = response.content.decode()
        self.assertIn('ACCB changes', html)
        self._assert_three_in(html)
        drill = response.context['corrections_drill']
        self.assertEqual(len(drill[0]['corrections']), 3)

    def test_the_change_request_cited_rows(self):
        """None can be cited today (refused under a change request); the page's cited-rows
        table still words one in the model's text if it ever is."""
        from django.template import Context, Template
        correction = BOQCorrection.objects.get(action=BOQ_CORRECTION_ACCB_EDIT)
        source = open('projects/templates/projects/design/change_request.html',
                      encoding='utf-8').read()
        start = source.index('{% for c in rows %}')
        end = source.index('{% endfor %}', start) + len('{% endfor %}')
        html = Template(source[start:end]).render(Context({'rows': [correction]}))
        self.assertIn(f'ACCB 1 edited: {EX_TYPE2_TEXT}'.replace('&', '&amp;'), html)

    def test_corrections_made_under_no_request_are_not_change_request_evidence(self):
        from .design_views import _uncited_corrections_since
        later = timezone.now()
        self.assertEqual(_uncited_corrections_since(self.site, later), [])


# ===========================================================================
# Downstream: the SCM key and the download follow the edit
# ===========================================================================

class DownstreamTests(ReviewerACCBBase):

    def test_scm_grouping_and_the_download_show_the_new_configuration(self):
        self._post(self.qc, 'save', EX_TYPE2_EDITED, accb=self.accb)
        accb_lines = aggregate_group_boq([self.site.pk])['accb_lines']
        self.assertEqual([(row['config_key'], row['description']) for row in accb_lines],
                         [(accb_config_key(EX_TYPE2_EDITED), accb_description(EX_TYPE2_EDITED))])
        from openpyxl import load_workbook
        self._login(self.qc)
        response = self.client.get(reverse('opex_boq_download',
                                           kwargs={'project_id': self.site.project_id}))
        sheet = load_workbook(BytesIO(response.content))[BOQ_DOWNLOAD_ACCB_SHEET]
        rows = [tuple(c.value for c in row) for row in sheet.iter_rows(min_row=5)]
        self.assertEqual([(r[0], r[2]) for r in rows if r[0] is not None],
                         [(1, accb_description(EX_TYPE2_EDITED))])


# ===========================================================================
# Q1 — "on an SCM order" on real RESCO order lines
# ===========================================================================

class OnOrderTests(ACCBOrderFixture):

    def test_an_order_with_the_site_and_the_configuration(self):
        self.seed_two_configurations()      # a1: Type-2; a2: Type-2 reordered, Type-1
        lines = lambda site: list(BOQItem.objects.filter(boq__project=site,  # noqa: E731
                                                         accb_config__isnull=False)
                                  .select_related('accb_config'))
        self.assertEqual(accb_lines_on_order(lines(self.a1), self.a1), set())
        self.raise_a1_a2(order_total='1000000', amounts={self.opex_module: '600000'})
        a2_lines = lines(self.a2)
        with CaptureQueriesContext(connection) as queries:
            on_a2 = accb_lines_on_order(a2_lines, self.a2)
        self.assertEqual(on_a2, {line.pk for line in a2_lines})
        # One query for both of A2's lines.
        self.assertEqual(len(queries), 1)
        # Q1's accepted false positive: an ACCB added to A1 after the order, with a setup
        # ordered for A2 on the same order, counts as ordered.
        late = self.accb(self.a1, EX_TYPE1)
        self.assertIn(late.boq_item_id, accb_lines_on_order(lines(self.a1), self.a1))
        # A setup on no order is not.
        fresh = self.accb(self.a1, cfg('Type-1', [('MCB', 30)], [('MCB', 30, False)]))
        self.assertNotIn(fresh.boq_item_id, accb_lines_on_order(lines(self.a1), self.a1))

    def test_no_lines_no_query(self):
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(accb_lines_on_order([], self.a1), set())
        self.assertEqual(len(queries), 0)


# ===========================================================================
# P9 — query counts
# ===========================================================================

class QueryCountTests(ReviewerACCBBase):
    """Baselines measured on 3abfb03 (pre-flight P9): the correction GET was 23 for QC with
    0, 1 or 3 ACCBs and 20 for the Head at awaiting_head_qc; boq_history 10; the tender
    dashboard 39 (40 with the drill). DW-2b adds the one order check when there is an ACCB
    to show (Q7), flat in the number of ACCBs."""

    def _count(self, call):
        with CaptureQueriesContext(connection) as queries:
            call()
        return len(queries)

    def _get(self, who):
        self._login(who)
        self.client.get(self.url)    # warm the session
        count = self._count(lambda: self.client.get(self.url))
        self.client.logout()
        return count

    def test_the_correction_get(self):
        ACCBConfig.objects.all().delete()
        BOQItem.objects.filter(item_master__code='OPX-273').delete()
        self.assertEqual(self._get(self.qc), 23)
        self._seed_accb(EX_TYPE1)
        self.assertEqual(self._get(self.qc), 24)
        self._seed_accb(EX_TYPE2)
        self._seed_accb(EX_TYPE1)
        self.assertEqual(self._get(self.qc), 24)
        self._status(DESIGN_AWAITING_HEAD_QC)
        self.assertEqual(self._get(self.head), 21)

    def test_the_reviewer_posts(self):
        """New actions, pinned as measured. Against the designer's 17 / 18 / 19 / 21 for
        save / add / duplicate / remove: boq_correct's gate (turn, attempt, change requests,
        group lock), the order check (edit and remove), the role lookup that tags the log
        line, the correction row and the activity entry. The remove adds DW-1's rule read
        (A6; one more for the lines when the formula groups exist)."""
        self._login(self.qc)
        self.client.get(self.url)
        post = lambda data: self._count(lambda: self.client.post(  # noqa: E731
            self.url, dict({'accb': '1', 'reason': REASON}, **data)))
        self.assertEqual(post(dict(accb_post_data(EX_TYPE2_EDITED), action='save',
                                   accb_id=str(self.accb.pk))), 25)
        self.assertEqual(post(dict(accb_post_data(EX_TYPE1), action='add')), 25)
        self.assertEqual(post({'action': 'duplicate', 'accb_id': str(self.accb.pk)}), 26)
        self.assertEqual(post({'action': 'remove', 'accb_id': str(self.accb.pk)}), 30)

    def test_the_trail_readers(self):
        self._post(self.qc, 'save', EX_TYPE2_EDITED, accb=self.accb)
        self._post(self.qc, 'add', EX_TYPE1)
        self._login(self.admin)
        history = reverse('boq_history', kwargs={'project_id': self.site.project_id})
        self.client.get(history)
        self.assertEqual(self._count(lambda: self.client.get(history)), 10)
        self.client.logout()
        sites = tender_metrics(self.program)['sites']
        with CaptureQueriesContext(connection) as queries:
            panel = reviewer_correction_panel(sites)
        self.assertEqual(len(queries), 3)
        with CaptureQueriesContext(connection) as queries:
            reviewer_correction_drill(panel, self.designer.pk)
        self.assertEqual(len(queries), 1)


class TaggedLogLineTests(ReviewerACCBBase):

    def test_the_longest_tagged_line_fits_in_255(self):
        """Q5's " (Head)" tag on B-3's worst case: a 301-character name, the longest site
        code, the largest ACCB number and the longest change detail."""
        from types import SimpleNamespace
        from .accb import accb_summary
        from .models import Project
        from .tests_accb_config import _longest_key_config
        from .views import accb_log_text
        profile = SimpleNamespace(user=SimpleNamespace(get_full_name=lambda: 'N' * 301,
                                                       username='u'))
        project = SimpleNamespace(
            project_id='S' * Project._meta.get_field('project_id').max_length)
        longest = accb_summary(_longest_key_config())
        text = accb_log_text(profile, 'changed', 32767, project, f'{longest} → {longest}',
                             tag=' (Head)')
        self.assertLessEqual(len(text), ActivityLog._meta.get_field('action').max_length)
