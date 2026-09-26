"""
Session B3b — change requests: the wording sweep and the two metric defects.

WHAT IS PINNED, AND THE DECISION EACH TEST CITES (Zuber, 26 Sep 2026)
----------------------------------------------------------------------
  A5   The six things that misled a user, first:
         1. the Design Head is told a forwarded or Q5 SCM request is a BOQ change request;
         2. the group-lock paragraph says a change request of EITHER kind is refused;
         3. the tender dashboard says what lands in its queue, and names "corrected";
         4. the suspension alert names the third way out, corrected in the BOQ;
         5. a stale accept names the verdict properly ("the BOQ", "the PM");
         6. quality analytics says a change request counts against the SITE's PM (D-22).
  D-18 An SCM-raised request's screens and messages say "BOQ change request", a PM-raised
       one's say "design change request", a screen showing both says "change request".
  D-18 No screen or message says a `with_pm` request waits for the Design Head.
  D-20 m_cr_rejection_rate's denominator is requests that REACHED the Head.
  D-21 m_cr_by_stage puts a post-release request in "Raised after release" and leaves a
       pre-release one where it was.

Fixtures are ChangeWindowBase's and ListsBase's: every request is raised, forwarded,
decided or withdrawn through its product endpoint. Mail is intercepted by
ChangeNotificationBase; no HTTP leaves the test.
"""
from datetime import timedelta
from types import SimpleNamespace

from django.utils import timezone

from .design_analytics import compute, m_cr_by_stage, m_cr_rejection_rate, rate
from .models import (
    ActivityLog, DesignAnalyticsPreference, DesignAssignment, DesignAttempt, Notification,
    CHANGE_REQUEST_ACCEPTED, CHANGE_REQUEST_CORRECTED, CHANGE_REQUEST_PM_REJECTED,
    CHANGE_REQUEST_WITHDRAWN, DESIGN_AWAITING_HEAD_QC, QC_PASSED, QC_PENDING,
)
from .tests_change_request_lists import ALL_SEVEN, ListsBase

# Phrases that put a request in the Head's hands. Lower-cased before matching.
WAITS_FOR_HEAD = ('awaiting the design head', 'waiting for the design head')


class WordingBase(ListsBase):

    def _page(self, profile, name, **kwargs):
        return self._get(profile, name, **kwargs).content.decode()

    def _dashboard(self):
        return self._page(self.head, 'design_tender_dashboard', pk=self.program.pk)

    def _stage_panel(self, key):
        result = compute([self.program], {key})
        return next(p['data'] for p in result['panels'] if p['metric'].key == key)

    def _analytics_page(self, *keys):
        DesignAnalyticsPreference.objects.update_or_create(
            profile=self.head, defaults={'metrics': list(keys)})
        return self._page(self.head, 'design_quality_analytics_tender', pk=self.program.pk)


# ===========================================================================
# A5 — the six messages that misled a user
# ===========================================================================

class MisleadingWordingTests(WordingBase):

    def test_a5_1_the_head_is_told_an_scm_request_is_a_boq_change_request(self):
        # Forwarded by the PM: the Heads' subject and body.
        site, _, change = self._with_pm('WD-A1')
        sent = len(self.mail)
        self._forward(change)
        mail = self._mail_for(self.head, sent)
        self.assertEqual(mail['subject'], 'WD-A1: BOQ change request forwarded to the Design Head')
        self.assertIn('a BOQ change request is waiting for the Design Head', mail['html'])
        # Q5: no PM stage, so it reaches the Heads at raise.
        pmless, _ = self._pm_less('WD-A1Q')
        sent = len(self.mail)
        self._raise(self.scm, pmless)
        mail = self._mail_for(self.head, sent)
        self.assertEqual(mail['subject'], 'WD-A1Q: BOQ change request raised')
        self.assertIn('a BOQ change request is waiting for the Design Head', mail['html'])
        # A PM's own request is still a design change request.
        own, _ = self._released('WD-A1P')
        sent = len(self.mail)
        self._raise(self.pm, own)
        self.assertEqual(self._mail_for(self.head, sent)['subject'],
                         'WD-A1P: design change request raised')

    def test_a5_2_the_lock_paragraph_says_either_kind_is_refused(self):
        site, _ = self._released('WD-A2')
        group = self._group('Batch A2', [site])
        page = self._page(self.scm, 'site_group_detail', pk=group.pk)
        self.assertIn('a change request on a member site — design or BOQ — will be refused',
                      page)
        self.assertNotIn('a design change request on a member site', page)

    def test_a5_3_the_dashboard_says_what_lands_in_its_queue_and_names_corrected(self):
        page = self._dashboard()
        self.assertIn('a BOQ change request once the site\'s PM\n      forwards it', page)
        self.assertNotIn('A PM change request lands here', page)
        self._pending_scm('WD-A3')
        page = self._dashboard()
        self.assertIn('Recording it as corrected in the BOQ opens nothing either', page)

    def test_a5_4_the_suspension_alert_names_the_corrected_outcome(self):
        site, _ = self._pre_release('WD-A4')
        self._raise(self.pm, site)
        page = self._page(self.head, 'design_qc_queue')
        self.assertIn('until he accepts it, rejects it or records it as corrected in the BOQ',
                      page)
        self.assertNotIn('PM change request', page)

    def test_a5_5_a_stale_triage_names_the_verdict_as_it_is_spelled(self):
        seven = self._seven()
        cases = {
            CHANGE_REQUEST_CORRECTED: 'CL-CO: that change request has already been '
                                      'corrected in the BOQ.',
            CHANGE_REQUEST_PM_REJECTED: 'CL-PR: that change request has already been '
                                        'rejected by the PM. It never reached the Design '
                                        'Head. Nothing was changed.',
            CHANGE_REQUEST_WITHDRAWN: 'CL-WD: that change request has already been '
                                      'withdrawn. It never reached the Design Head. '
                                      'Nothing was changed.',
            # Unchanged from Part 4.6 — _verdict_phrase() of a one-word label.
            CHANGE_REQUEST_ACCEPTED: 'CL-AC: that change request has already been accepted.',
        }
        for verdict, expected in cases.items():
            with self.subTest(verdict=verdict):
                self.assertEqual(self._messages(self._accept(seven[verdict])), expected)

    def test_a5_6_the_rate_is_counted_against_the_sites_pm(self):
        page = self._analytics_page()        # change_request_rate is core: always on
        self.assertIn("Counted against the site's PM (the PM assigned to the site, "
                      "whoever raised the\n          request)", page)
        self.assertNotIn('the PM who raised the request', page)


# ===========================================================================
# D-18 — the kind, by audience
# ===========================================================================

class KindWordingTests(WordingBase):

    def test_w1_an_scm_raised_requests_screens_say_boq_change_request(self):
        site, _, change = self._with_pm('WD-W1')
        form = self._page(self.scm, 'design_change_request_form', project_id='WD-W1')
        self.assertIn('Raise BOQ change request', form)
        self.assertIn('<strong>BOQ change request</strong>', form)
        self.assertIn('BOQ change requests waiting for you', self._queue(self.pm).content.decode())

        self._forward(change)
        dashboard = self._dashboard()
        self.assertIn('BOQ change request\n              · raised by', dashboard)
        self.assertIn('BOQ change request awaiting your decision', dashboard)
        banner = self._page(self.head, 'design_qc_review', project_id='WD-W1')
        self.assertIn('BOQ change request\n        · cw_scm', banner)
        self.assertIn('SCM · BOQ change request', self._page(self.head, 'design_qc_queue'))

    def test_w2_a_pm_raised_requests_screens_and_messages_say_design_change_request(self):
        site, assignment = self._released('WD-W2')
        response = self._raise(self.pm, site)
        self.assertTrue(self._messages(response).startswith(
            'WD-W2: design change request raised on attempt 1'))
        form = self._page(self.pm, 'design_change_request_form', project_id='WD-W2')
        self.assertIn('Raise design change request', form)
        self.assertIn('<strong>Design change request</strong>', form)
        dashboard = self._dashboard()
        self.assertIn('Design change request\n              · raised by', dashboard)
        self.assertIn('Design change request awaiting your decision', dashboard)

        sent = len(self.mail)
        response = self._accept(self._pending(assignment))
        self.assertTrue(self._messages(response).startswith(
            'WD-W2: design change request accepted — attempt 2 opened'))
        self.assertEqual(self._mail_for(self.pm, sent)['subject'],
                         'WD-W2: design change request accepted — attempt 2 opened')
        self.assertIn('accepted your design change request',
                      self._mail_for(self.pm, sent)['html'])

    def test_w3_a_screen_showing_both_kinds_says_change_request(self):
        pm_site, _ = self._released('WD-W3P')
        self._raise(self.pm, pm_site)
        self._pending_scm('WD-W3S')
        dashboard = self._dashboard()
        self.assertIn('Change requests awaiting your decision', dashboard)
        banner_q = self._page(self.head, 'design_qc_queue')
        for page in (dashboard, banner_q, self._queue(self.pm).content.decode()):
            self.assertNotIn('PM change request', page)
            self.assertNotIn('SCM change request', page)
        # An accepted request's rework chip does not know the origin; it says neither.
        site, assignment = self._released('WD-W3A')
        self._raise(self.pm, site)
        self._accept(self._pending(assignment))
        form = self._page(self.pm, 'design_change_request_form', project_id='WD-W3A')
        self.assertIn('Rework: change request', form)
        self.assertIn('<h5 class="mb-0">Change request — WD-W3A</h5>', form)

    def test_w4_the_scm_group_screens_offer_a_boq_change(self):
        site, _ = self._released('WD-W4')
        self._released('WD-W4P')                  # stays in the pool: the list's button
        group = self._group('Batch W4', [site])
        for page in (self._page(self.scm, 'site_group_list', pk=self.program.pk),
                     self._page(self.scm, 'site_group_detail', pk=group.pk)):
            with self.subTest():
                self.assertIn('>Request BOQ change</a>', page)
                self.assertNotIn('Request design change', page)

    def test_w5_every_scm_transition_logs_and_tells_a_boq_change_request(self):
        site, assignment, change = self._with_pm('WD-W5')
        mark = self._mark()
        self._pm_reject(change)
        note = self._note_for(mark, self.scm)
        self.assertIn('rejected your BOQ change request', note.message)
        _, _, change = self._with_pm('WD-W5B')
        mark = self._mark()
        self._withdraw(change, profile=self.scm2)
        self.assertIn('the BOQ change request cw_scm raised', self._note_for(mark, self.pm).message)
        actions = list(ActivityLog.objects.filter(entity_type='DesignChangeRequest')
                       .values_list('action', flat=True))
        self.assertTrue(any(a.startswith('BOQ change request rejected by the PM') for a in actions))
        self.assertTrue(any(a.startswith('BOQ change request withdrawn') for a in actions))
        self.assertFalse(any(a.startswith(('SCM change request', 'PM change request'))
                             for a in actions))


# ===========================================================================
# D-18 — a request with the PM is never said to wait for the Head
# ===========================================================================

class WithPmNeverWaitsForTheHeadTests(WordingBase):

    def test_n1_no_screen_or_message_says_a_with_pm_request_waits_for_the_head(self):
        site, assignment, change = self._with_pm('WD-N1')
        group = self._group('Batch N1', [site])
        texts = {
            'form as SCM': self._page(self.scm, 'design_change_request_form',
                                      project_id='WD-N1'),
            'form as PM': self._page(self.pm, 'design_change_request_form',
                                     project_id='WD-N1'),
            'PM queue': self._queue(self.pm).content.decode(),
            'tender dashboard': self._dashboard(),
            'second raise': self._messages(self._raise(self.scm2, site, reason='again')),
            'lock': self._messages(self._lock(group)),
            'stale accept': self._messages(self._accept(change)),
            'raise notes': ' '.join(n.message for n in Notification.objects.all()),
        }
        for where, text in texts.items():
            with self.subTest(where=where):
                self.assertTrue(text)
                for phrase in WAITS_FOR_HEAD:
                    self.assertNotIn(phrase, text.lower())
        # The review queue's Head list is the one screen whose heading IS "waiting for the
        # Design Head" — true of what it lists. The with-PM request is not in it.
        review = self._page(self.head, 'design_qc_queue')
        self.assertIn('No change requests are waiting for the Design Head.', review)
        self.assertNotIn('WD-N1', review)


# ===========================================================================
# D-20 — the rejection rate's denominator
# ===========================================================================

class RejectionRateTests(WordingBase):

    def test_d20_only_requests_that_reached_the_head_are_counted(self):
        self._seven()
        # One more rejected and one more accepted, so the figure clears MIN_DENOMINATOR.
        _, a = self._released('WD-D20R')
        self._raise(self.pm, a.project)
        self._reject(self._one(a))
        _, a = self._released('WD-D20A')
        self._raise(self.pm, a.project)
        self._accept(self._one(a))

        data = self._stage_panel('cr_rejection_rate')
        self.assertEqual((data['rejected'], data['accepted'], data['corrected'],
                          data['pending']), (2, 2, 1, 1))
        self.assertEqual((data['reached'], data['not_reached'], data['total']), (6, 3, 9))
        self.assertEqual((data['figure']['numerator'], data['figure']['n'],
                          data['figure']['value']), (2, 6, 33.3))
        # What changed: before B3b the denominator was every request raised — 2/9.
        self.assertEqual(rate(data['rejected'], data['total'])['value'], 22.2)

        page = self._analytics_page('cr_rejection_rate')
        self.assertIn('of 6 that\n            reached the Head.', page)
        self.assertIn('3 more never reached the Head', page)

    def test_d20_every_verdict_is_classified(self):
        """A verdict added later must be put on one side of the line on purpose."""
        crs = [SimpleNamespace(verdict=v) for v in sorted(ALL_SEVEN)]
        data = m_cr_rejection_rate({'change_requests': crs})
        self.assertEqual(data['reached'] + data['not_reached'], len(ALL_SEVEN))
        self.assertEqual(data['not_reached'], 3)    # with_pm, pm_rejected, withdrawn


# ===========================================================================
# D-21 — "Raised after release"
# ===========================================================================

class ByStageTests(WordingBase):

    @staticmethod
    def _counts(data):
        return {s['label']: s['count'] for s in data['stages']}

    def test_d21_post_release_requests_land_in_the_new_bucket(self):
        # Post-release: an SCM request (origin) and a PM request after the Head's pass.
        self._with_pm('WD-S1')
        pm_site, _ = self._released('WD-S2')
        self._raise(self.pm, pm_site)
        # Pre-release, in QC.
        qc_site, _ = self._pre_release('WD-S3')
        self._raise(self.pm, qc_site)
        # Pre-release, with the Head: QC passed, the Head gate open, no Head verdict yet.
        head_site, head_a = self._pre_release('WD-S4')
        earlier = timezone.now() - timedelta(hours=1)
        DesignAttempt.objects.filter(assignment=head_a).update(
            qc_verdict=QC_PASSED, qc_reviewed_by=self.qc, qc_reviewed_at=earlier,
            head_started_at=earlier)
        DesignAssignment.objects.filter(pk=head_a.pk).update(status=DESIGN_AWAITING_HEAD_QC)
        self._raise(self.pm, head_site)

        counts = self._counts(self._stage_panel('cr_by_stage'))
        self.assertEqual(counts, {
            'Raised while in design': 0,
            'Raised while with Design QC': 1,
            'Raised while with the Head': 1,
            'Raised after release': 2,
        })

    def test_d21_the_rule_by_field_and_value(self):
        now = timezone.now()
        hour = timedelta(hours=1)

        def attempt(pk, **kw):
            base = dict(pk=pk, attempt_number=1, qc_started_at=now - 5 * hour,
                        qc_reviewed_at=None, head_started_at=None, head_verdict=QC_PENDING,
                        head_reviewed_at=None)
            base.update(kw)
            return SimpleNamespace(**base)

        attempts = [
            # 1: Head gate open, no verdict — a PM request here stays "with the Head".
            attempt(1, qc_reviewed_at=now - 4 * hour, head_started_at=now - 4 * hour),
            # 2: Head passed at -3h, released after — a PM request at -1h is post-release.
            attempt(2, qc_reviewed_at=now - 4 * hour, head_started_at=now - 4 * hour,
                    head_verdict=QC_PASSED, head_reviewed_at=now - 3 * hour),
            # 3: fixtured, no Head pass at all — an SCM request is post-release by origin.
            attempt(3, qc_reviewed_at=now - 4 * hour),
            # 4: released before Part 9 — QC only, no Head verdict. The recorded caveat.
            attempt(4, qc_reviewed_at=now - 4 * hour),
            # 5: in QC.
            attempt(5),
        ]
        crs = [
            SimpleNamespace(attempt_id=1, origin='pm', requested_at=now - 2 * hour),
            SimpleNamespace(attempt_id=2, origin='pm', requested_at=now - 3.5 * hour),
            SimpleNamespace(attempt_id=2, origin='pm', requested_at=now - hour),
            SimpleNamespace(attempt_id=3, origin='scm', requested_at=now - hour),
            SimpleNamespace(attempt_id=4, origin='pm', requested_at=now - hour),
            SimpleNamespace(attempt_id=5, origin='pm', requested_at=now - hour),
        ]
        data = m_cr_by_stage({'change_requests': crs,
                              'attempt_by_id': {t.pk: t for t in attempts}})
        self.assertEqual(self._counts(data), {
            'Raised while in design': 0,
            'Raised while with Design QC': 1,          # attempt 5
            'Raised while with the Head': 3,           # attempt 1, 2 before the pass, 4
            'Raised after release': 2,                 # attempt 2 after the pass, 3 (SCM)
        })
        self.assertEqual(data['total'], 6)
