"""
Session A2 — accurate screens, and a staleness signal for the BOQ.

Session A removed the Arka approval gate from CAD upload and from BOQ completion. It left
three kinds of residue, and this file covers the two that are behaviour:

  * the designer's dashboard card still read the Arka's head_verdict FIRST and offered no
    action at all until both gates had passed, so it hid work that had become legal;

  * a BOQ completed under one Arka version and left standing when that version is rejected
    and replaced was shown NOWHERE. The CAD has carried a "not the current Arka" badge
    since Part 3, because DesignFile stores the version it was drawn against. The BOQ
    stores nothing, so its equivalent is INFERRED — see _boq_provenance().

The third kind is stale prose (head_review.html, the chips comment, the ArkaSubmission
docstring, the lock-progression comment). Test 2b asserts the two user-visible claims are
gone, because those two were rendered to a reviewer rather than read by a maintainer.

WHAT IS DELIBERATELY NOT ASSERTED: that the gap is closed. It is not. The badge is
informational — no block, no status change, no stored field, no re-scoping of rework.
tests_design_combined_submission.test_05* still pin the gap itself; these pin that a
reviewer can now SEE it.
"""
from decimal import Decimal

from django.urls import reverse

from .tests_design_combined_submission import CombinedSubmissionBase
from .design_views import (
    _boq_provenance, _current_attempt, _current_arka, _workspace_context,
    designer_dashboard_context,
)
from .models import Project, ARKA_APPROVED


class BoqProvenanceHelperTests(CombinedSubmissionBase):
    """_boq_provenance() at the unit level — the four states it can return."""

    def _ctx(self):
        self.assignment.refresh_from_db()
        return _workspace_context(self.site, self.assignment)

    def test_no_stamp_means_no_provenance_and_no_staleness(self):
        self._login(self.designer)
        self._submit_arka()
        ctx = self._ctx()
        self.assertIsNone(ctx['boq_provenance_arka'])
        self.assertFalse(ctx['boq_is_stale'])

    def test_no_arka_means_no_badge_even_with_a_stamp(self):
        """Mirrors `{% if arka and ... %}` on the CAD row: with no current Arka there is
        no version for the BOQ to be stale against, so nothing is claimed."""
        self._seed_boq()
        self._login(self.designer)
        self._complete_boq()

        ctx = self._ctx()
        self.assertTrue(ctx['boq_complete'])
        self.assertIsNone(ctx['arka'])
        self.assertIsNone(ctx['boq_provenance_arka'])
        self.assertFalse(ctx['boq_is_stale'],
                         'nothing to be stale against yet')

    def test_a_boq_completed_under_the_current_arka_is_not_stale(self):
        self._login(self.designer)
        self._submit_arka()
        self._seed_boq()
        self._login(self.designer)
        self._complete_boq()

        ctx = self._ctx()
        self.assertIsNotNone(ctx['boq_provenance_arka'])
        self.assertEqual(ctx['boq_provenance_arka'].version, 1)
        self.assertFalse(ctx['boq_is_stale'])

    def test_a_boq_completed_under_a_superseded_arka_is_stale(self):
        """THE CASE THE BADGE EXISTS FOR — Session A's own repro."""
        self._login(self.designer)
        self._submit_arka()                       # v1
        self._seed_boq()
        self._login(self.designer)
        self._complete_boq()                      # stamped while v1 was live
        self._qc_reject()
        self._login(self.designer)
        self._submit_arka(capacity='140.00')      # v2 replaces it

        ctx = self._ctx()
        self.assertEqual(ctx['arka'].version, 2)
        self.assertEqual(ctx['boq_provenance_arka'].version, 1,
                         'v1 was the layout on the table when the bill was stamped')
        self.assertTrue(ctx['boq_is_stale'])

    def test_a_boq_completed_before_any_arka_becomes_stale_once_one_arrives(self):
        """The path Session A opened: no Arka at stamp time, so no provenance version —
        but once a layout exists the bill predates it, and that is worth saying."""
        self._seed_boq()
        self._login(self.designer)
        self._complete_boq()                      # no Arka at all yet
        self.assertFalse(self._ctx()['boq_is_stale'], 'nothing to compare against')

        self._submit_arka()
        ctx = self._ctx()
        self.assertIsNone(ctx['boq_provenance_arka'],
                          'there was no version live when it was stamped')
        self.assertTrue(ctx['boq_is_stale'])

    def test_the_helper_adds_no_query_of_its_own(self):
        """It reads `arka_history`, which _workspace_context() already fetches. Asserted
        because the cheap version of this feature is a per-screen extra query."""
        self._login(self.designer)
        self._submit_arka()
        self._seed_boq()
        self._login(self.designer)
        self._complete_boq()

        self.assignment.refresh_from_db()
        attempt = _current_attempt(self.assignment)
        arka = _current_arka(attempt)
        history = list(attempt.arka_submissions.order_by('-version'))
        with self.assertNumQueries(0):
            _boq_provenance(attempt, arka, history)

    def test_nothing_is_stored(self):
        """No new field, no writes. The signal is computed at render or not at all."""
        self._login(self.designer)
        self._submit_arka()
        self._seed_boq()
        self._login(self.designer)
        self._complete_boq()
        self._qc_reject()
        self._login(self.designer)
        self._submit_arka(capacity='140.00')

        self.assignment.refresh_from_db()
        attempt = _current_attempt(self.assignment)
        before = {f.name: getattr(attempt, f.name)
                  for f in attempt._meta.fields if f.name.startswith('boq_')}
        self.assertTrue(self._ctx()['boq_is_stale'])
        attempt.refresh_from_db()
        after = {f.name: getattr(attempt, f.name)
                 for f in attempt._meta.fields if f.name.startswith('boq_')}
        self.assertEqual(before, after, 'rendering the badge wrote nothing')


class StalenessBadgeRendersTests(CombinedSubmissionBase):
    """VERIFICATION 4 — the badge appears on the screens the CAD badge already appears on."""

    def _make_stale(self):
        self._login(self.designer)
        self._submit_arka()
        self._upload_cad()                        # CAD paired to v1
        self._seed_boq()
        self._login(self.designer)
        self._complete_boq()                      # BOQ stamped under v1
        self._qc_reject()
        self._login(self.designer)
        self._submit_arka(capacity='140.00')      # v2 supersedes v1

    def test_both_badges_appear_together_on_the_reviewer_screens(self):
        """The CAD's badge and the BOQ's are two halves of the same statement, so a
        screen showing one and not the other would be the worse outcome. Both screens
        include _design_artifacts.html, which is why one edit covers both."""
        self._make_stale()

        for who, url_name in ((self.head, 'design_head_review'),
                              (self.qc,   'design_qc_review')):
            self._login(who)
            body = self.client.get(self._url(url_name)).content.decode()
            self.assertIn('not the current Arka', body,
                          f'{url_name}: the CAD badge')
            self.assertIn('completed under Arka v1, not the current version', body,
                          f'{url_name}: the BOQ badge')

    def test_the_designer_sees_it_too(self):
        """site_workspace.html includes the same partial. The designer is the person who
        would have to re-check the bill, so hiding it from them would be perverse."""
        self._make_stale()
        self._login(self.designer)
        body = self.client.get(self._url('design_site_workspace')).content.decode()
        self.assertIn('completed under Arka v1, not the current version', body)

    def test_a_fresh_boq_shows_the_version_without_a_warning(self):
        self._login(self.designer)
        self._submit_arka()
        self._seed_boq()
        self._login(self.designer)
        self._complete_boq()

        self._login(self.head)
        body = self.client.get(self._url('design_head_review')).content.decode()
        self.assertIn('under Arka v1 (current)', body)
        self.assertNotIn('not the current version', body)

    def test_a_boq_stamped_before_any_arka_says_exactly_that(self):
        self._seed_boq()
        self._login(self.designer)
        self._complete_boq()
        self._submit_arka()

        self._login(self.head)
        body = self.client.get(self._url('design_head_review')).content.decode()
        self.assertIn('completed before any Arka was submitted', body)

    def test_the_footer_renders_with_no_uploaded_file_at_all(self):
        """The artifacts TABLE is inside `{% if not files %}`, so a BOQ-only attempt would
        have shown nothing had the footer gone in the table. This is why it did not."""
        self._seed_boq()
        self._login(self.designer)
        self._complete_boq()
        self._submit_arka()

        self._login(self.head)
        body = self.client.get(self._url('design_head_review')).content.decode()
        self.assertIn('Nothing uploaded yet for this attempt', body)
        self.assertIn('completed before any Arka was submitted', body)


class HeadReviewTextTests(CombinedSubmissionBase):
    """VERIFICATION 2 — the two false claims are gone from what a reviewer actually reads."""

    def test_the_gate_buttons_no_longer_claim_to_unlock_anything(self):
        self._login(self.designer)
        self._submit_arka()
        self._upload_cad()
        self._seed_boq()
        self._login(self.designer)
        self._complete_boq()

        self._login(self.qc)
        body = self.client.get(self._url('design_head_review')).content.decode()
        for gone in ('CAD and BOQ stay locked until he approves',
                     'Unlocks CAD and BOQ upload for the designer'):
            self.assertNotIn(gone, body)
        self.assertIn('CAD and\n              BOQ can be submitted independently', body)


class DesignerDashboardOffersRealActionsTests(CombinedSubmissionBase):
    """VERIFICATION 3 — the card offers what is legal, not what the old gate allowed."""

    def _card(self):
        self.assignment.refresh_from_db()
        return designer_dashboard_context(
            self.designer, Project.objects.filter(pk=self.site.pk))[self.site.pk]

    def test_upload_cad_is_offered_while_the_arka_is_still_with_qc(self):
        """This is the regression the session exists to fix: the card used to say
        "Waiting for Design QC to review your Arka" and offer nothing."""
        self._login(self.designer)
        self._submit_arka()

        card = self._card()
        self.assertEqual(card['action_kind'], 'link')
        self.assertEqual(card['action_label'], 'Upload CAD')

    def test_enter_boq_is_offered_next_once_the_cad_is_in(self):
        self._login(self.designer)
        self._submit_arka()
        self._upload_cad()

        card = self._card()
        self.assertEqual(card['action_kind'], 'link')
        self.assertEqual(card['action_label'], 'Enter BOQ')

    def test_the_arka_pending_fact_is_kept_alongside_the_action(self):
        """"Instead of" was allowed; losing the information was not. The key stays
        populated, and the card's status chip renders it independently."""
        self._login(self.designer)
        self._submit_arka()

        card = self._card()
        self.assertIn('Arka is with Design QC', card['waiting'])
        self.assertIn('do not wait for it', card['waiting'])

    def test_with_both_artifacts_in_the_card_names_the_arka_as_the_blocker(self):
        self._login(self.designer)
        self._submit_arka()
        self._upload_cad()
        self._seed_boq()
        self._login(self.designer)
        self._complete_boq()

        card = self._card()
        self.assertEqual(card['action_kind'], 'none')
        self.assertIn('CAD and BOQ are in', card['waiting'])
        self.assertIn('Arka verdict', card['waiting'])

    def test_an_approved_arka_still_walks_the_same_two_steps(self):
        """The approved path is unchanged — the branch was reordered, not rewritten."""
        self._login(self.designer)
        self._submit_arka()
        self._qc_approve()
        self._head_approve()

        self.assignment.refresh_from_db()
        self.assertEqual(_current_arka(_current_attempt(self.assignment)).head_verdict,
                         ARKA_APPROVED)
        self.assertEqual(self._card()['action_label'], 'Upload CAD')

        self._login(self.designer)
        self._upload_cad()
        self.assertEqual(self._card()['action_label'], 'Enter BOQ')

    def test_the_rendered_card_shows_the_action_and_the_arka_state_together(self):
        """WHY "INSTEAD OF" WAS ACCEPTABLE, asserted on the real page rather than argued.

        _dashboard_design_actions.html renders `waiting` only when no action is offered,
        so making the action a link does suppress that string. It does not suppress the
        FACT: _dashboard_design_chips.html renders the Arka's state on the same card, from
        the same context, and it is still there beside the new button.
        """
        self._login(self.designer)
        self._submit_arka()

        response = self.client.get(reverse('dashboard_design'))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('Upload CAD', body, 'the newly-offered action')
        self.assertIn('Arka awaiting Design QC', body, 'and who is holding the Arka')
