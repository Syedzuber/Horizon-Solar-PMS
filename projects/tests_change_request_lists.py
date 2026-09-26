"""
Session B3a — change requests made findable: the PM's full list, the Head's list on the
design review queue, and the "corrected" button on the tender dashboard.

WHAT IS PINNED, AND THE DECISION EACH TEST CITES
------------------------------------------------
  P    D-14 / D-16 / sign-off 1: the PM queue lists EVERY change request on the viewer's
       sites. with_pm rows fill the "waiting for you" table first; everything else is
       history, newest act first, and the State column names the act its date belongs to.
       Scope unchanged: another PM's site is absent, a coordinator sees the PM's list, a
       Finance user gets a 200 with no rows and no nav entry.
  H    D-15 / sign-off 2 and 4: the review queue's change-request section — `pending` only,
       across tenders, oldest by time WITH THE HEAD, Head authority only, capped at 25
       with the rest counted.
  C    D-13 / sign-off 3: the tender dashboard's corrected form for Head authority (a QC
       reviewer is refused the page), and its count equals the qc_review banner's.
  Q    Query counts for both lists are flat in the number of rows.

Mail is intercepted by ChangeNotificationBase (via ScreensBase): the product flows below
send notifications, and any real HTTP request fails the test.
"""
import os
from datetime import timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .design_metrics import HEAD_CHANGE_REQUEST_LIST_LIMIT
from .design_views import _uncited_correction_counts, _uncited_corrections_since
from .models import (
    DesignAttempt, DesignChangeRequest, Program,
    CHANGE_REQUEST_ACCEPTED, CHANGE_REQUEST_CORRECTED, CHANGE_REQUEST_PENDING,
    CHANGE_REQUEST_PM_REJECTED, CHANGE_REQUEST_REJECTED, CHANGE_REQUEST_WITH_PM,
    CHANGE_REQUEST_WITHDRAWN, CHANGE_REQUEST_ORIGIN_PM, CHANGE_REQUEST_ORIGIN_SCM,
    ATTEMPT_REASON_INITIAL,
)
from .tests_change_request_screens import NAV_MARK, ScreensBase

ALL_SEVEN = {CHANGE_REQUEST_WITH_PM, CHANGE_REQUEST_PENDING, CHANGE_REQUEST_ACCEPTED,
             CHANGE_REQUEST_REJECTED, CHANGE_REQUEST_PM_REJECTED, CHANGE_REQUEST_WITHDRAWN,
             CHANGE_REQUEST_CORRECTED}


class ListsBase(ScreensBase):

    def _seven(self):
        """One request per verdict, every one on a site self.pm manages, each reached
        through the product endpoint that writes it. Returns {verdict: change}."""
        out = {}
        _, _, out[CHANGE_REQUEST_WITH_PM] = self._with_pm('CL-WP')
        _, _, out[CHANGE_REQUEST_PENDING] = self._pending_scm('CL-PE')

        _, a = self._released('CL-AC')
        self._raise(self.pm, a.project)
        self._accept(self._one(a))
        out[CHANGE_REQUEST_ACCEPTED] = self._one(a)

        _, a = self._released('CL-RJ')
        self._raise(self.pm, a.project)
        self._reject(self._one(a))
        out[CHANGE_REQUEST_REJECTED] = self._one(a)

        _, _, change = self._with_pm('CL-PR')
        self._pm_reject(change)
        out[CHANGE_REQUEST_PM_REJECTED] = change

        _, _, change = self._with_pm('CL-WD')
        self._withdraw(change)
        out[CHANGE_REQUEST_WITHDRAWN] = change

        site, a = self._released('CL-CO')
        self._raise(self.pm, site)
        self._boq_correct(site, 12)
        self._correct(self._one(a))
        out[CHANGE_REQUEST_CORRECTED] = self._one(a)

        for verdict, change in out.items():
            change.refresh_from_db()
            self.assertEqual(change.verdict, verdict)
        return out

    def _second_tender(self):
        return Program.objects.create(name='Test-CW-Two', program_type='OPEX',
                                      client_name='CWClient', status='Active',
                                      short_tender_code='CW2')

    def _fixture_requests(self, assignment, n, verdict=CHANGE_REQUEST_PENDING,
                          start_number=2):
        """FIXTURE WRITER — `n` requests on `assignment`, each on its own attempt (the
        partial unique constraint allows one open request per attempt), each with the
        columns its verdict's CHECK constraints require. For the cap and query-count
        tests only, where driving 26 raises through the product would test nothing more."""
        now = timezone.now()
        made = []
        for i in range(n):
            attempt = DesignAttempt.objects.create(
                assignment=assignment, attempt_number=start_number + i,
                opened_reason=ATTEMPT_REASON_INITIAL)
            fields = {'origin': CHANGE_REQUEST_ORIGIN_PM}
            v = verdict[i % len(verdict)] if isinstance(verdict, (list, tuple)) else verdict
            if v in (CHANGE_REQUEST_ACCEPTED, CHANGE_REQUEST_REJECTED):
                fields.update(decided_by=self.head, decided_at=now,
                              rejection_reason='Stands.')
            elif v == CHANGE_REQUEST_PM_REJECTED:
                fields.update(origin=CHANGE_REQUEST_ORIGIN_SCM, pm_decided_by=self.pm,
                              pm_decided_at=now, pm_note='No.')
            elif v == CHANGE_REQUEST_WITHDRAWN:
                fields.update(origin=CHANGE_REQUEST_ORIGIN_SCM, withdrawn_by=self.scm,
                              withdrawn_at=now, withdrawal_note='Error.')
            elif v == CHANGE_REQUEST_CORRECTED:
                fields.update(corrected_by=self.head, corrected_at=now,
                              correction_note='Fixed.')
            elif v == CHANGE_REQUEST_WITH_PM:
                fields.update(origin=CHANGE_REQUEST_ORIGIN_SCM)
            change = DesignChangeRequest.objects.create(
                attempt=attempt, requested_by=self.pm if fields['origin'] == 'pm' else self.scm,
                reason=f'fixture {i}', verdict=v, **fields)
            DesignChangeRequest.objects.filter(pk=change.pk).update(
                requested_at=now - timedelta(hours=n - i))
            change.refresh_from_db()
            made.append(change)
        return made

    def _count_queries(self, profile, name, **kwargs):
        self._login(profile)
        url = reverse(name, kwargs=kwargs)
        self.client.get(url)      # warm
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.client.logout()
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries), response


# ===========================================================================
# P — the PM's full list (D-14)
# ===========================================================================

class PMListTests(ListsBase):

    def test_p1_all_seven_verdicts_with_pm_first(self):
        made = self._seven()
        response = self._queue(self.pm)
        waiting = [r['change_request'].pk for r in response.context['change_rows']]
        history = {r['change_request'].verdict for r in response.context['history_rows']}
        self.assertEqual(waiting, [made[CHANGE_REQUEST_WITH_PM].pk])
        self.assertEqual(history | {CHANGE_REQUEST_WITH_PM}, ALL_SEVEN)
        self.assertNotIn(CHANGE_REQUEST_WITH_PM, history)
        page = response.content.decode()
        # The waiting table renders above the history table, and every act is named.
        self.assertLess(page.index('CL-WP'), page.index('what happened'))
        for label in ('Forwarded to the Design Head', 'Accepted by the Head — new attempt',
                      'Rejected by the Head', 'Rejected by the PM', 'Withdrawn by SCM',
                      'Corrected in the BOQ by the Head'):
            with self.subTest(label=label):
                self.assertIn(label, page)
        self.assertNotIn('SESSION B3a', page)

    def test_p2_history_is_newest_act_first_and_the_date_is_the_act_s(self):
        made = self._seven()
        now = timezone.now()
        # Raised long ago, withdrawn just now: it must lead, and show the withdrawal time.
        self._backdate(made[CHANGE_REQUEST_WITHDRAWN], requested_at=now - timedelta(days=30),
                       withdrawn_at=now - timedelta(minutes=1))
        self._backdate(made[CHANGE_REQUEST_REJECTED], requested_at=now - timedelta(days=1),
                       decided_at=now - timedelta(days=9))
        # Every other act a few days back, so only the two above are in question.
        self._backdate(made[CHANGE_REQUEST_PENDING], pm_decided_at=now - timedelta(days=2))
        self._backdate(made[CHANGE_REQUEST_ACCEPTED], decided_at=now - timedelta(days=3))
        self._backdate(made[CHANGE_REQUEST_PM_REJECTED], pm_decided_at=now - timedelta(days=4))
        self._backdate(made[CHANGE_REQUEST_CORRECTED], corrected_at=now - timedelta(days=5))
        rows = self._queue(self.pm).context['history_rows']
        self.assertEqual(rows[0]['change_request'].pk, made[CHANGE_REQUEST_WITHDRAWN].pk)
        self.assertEqual(rows[-1]['change_request'].pk, made[CHANGE_REQUEST_REJECTED].pk)
        self.assertEqual(rows[0]['last_at'], made[CHANGE_REQUEST_WITHDRAWN].withdrawn_at)
        self.assertEqual(rows[0]['last_by'], self.scm)
        sorts = [r['sort_at'] for r in rows]
        self.assertEqual(sorts, sorted(sorts, reverse=True))

    def test_p3_a_request_never_acted_on_falls_back_to_the_raise(self):
        site, a = self._released('CL-RAW')
        self._raise(self.pm, site)
        row = self._queue(self.pm).context['history_rows'][0]
        change = self._one(a)
        self.assertFalse(row['forwarded'])
        self.assertEqual((row['last_by'], row['last_at']),
                         (self.pm, change.requested_at))
        self.assertIn('Raised · with the Design Head', self._queue(self.pm).content.decode())

    def test_p4_another_pm_s_site_is_not_listed(self):
        pm2 = self._person('cl_pm2', 'PM')
        self._with_pm('CL-MINE')
        other, other_a, other_change = self._with_pm('CL-OTHER', pm=pm2)
        other.coordinators.clear()
        self._pm_reject(other_change, profile=pm2)
        response = self._queue(self.pm)
        listed = {r['site'].project_id for r in response.context['change_rows']}
        listed |= {r['site'].project_id for r in response.context['history_rows']}
        self.assertEqual(listed, {'CL-MINE'})
        self.assertEqual({r['site'].project_id
                          for r in self._queue(pm2).context['history_rows']}, {'CL-OTHER'})

    def test_p5_a_coordinator_sees_the_same_list_as_the_pm(self):
        self._seven()
        pm_resp, co_resp = self._queue(self.pm), self._queue(self.coord)
        for key in ('change_rows', 'history_rows'):
            with self.subTest(key=key):
                self.assertEqual([r['change_request'].pk for r in pm_resp.context[key]],
                                 [r['change_request'].pk for r in co_resp.context[key]])

    def test_p6_finance_gets_a_200_with_no_rows_and_no_nav_entry(self):
        self._seven()
        response = self._queue(self.finance)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['change_rows'], [])
        self.assertEqual(response.context['history_rows'], [])
        self.assertEqual(response.context['rows'], [])
        self.assertNotIn(NAV_MARK, response.content.decode())


# ===========================================================================
# H — the Head's list on the design review queue (D-15)
# ===========================================================================

class HeadListTests(ListsBase):

    def _head_rows(self, profile=None):
        response = self._get(profile or self.head, 'design_qc_queue')
        self.assertEqual(response.status_code, 200)
        return response

    def test_h1_pending_requests_across_two_tenders_in_one_view(self):
        _, _, one = self._pending_scm('CL-T1')
        site2, a2 = self._released('CL-T2')
        tender2 = self._second_tender()
        site2.program = tender2
        site2.save(update_fields=['program'])
        self._raise(self.pm, site2)
        two = self._pending(a2)
        response = self._head_rows()
        rows = response.context['head_change_rows']
        self.assertEqual({r['change_request'].pk for r in rows}, {one.pk, two.pk})
        self.assertEqual({r['program'].pk for r in rows}, {self.program.pk, tender2.pk})
        page = response.content.decode()
        self.assertIn('Test-CW-Two', page)
        self.assertIn(reverse('design_qc_review', kwargs={'project_id': 'CL-T2'}), page)
        self.assertNotIn('SESSION B3a', page)

    def test_h2_sorted_by_time_with_the_head_not_by_raise_time(self):
        now = timezone.now()
        _, _, forwarded = self._pending_scm('CL-S1')
        self._backdate(forwarded, requested_at=now - timedelta(days=20),
                       pm_decided_at=now - timedelta(minutes=5))
        site, a = self._released('CL-S2')
        self._raise(self.pm, site)
        raised = self._pending(a)
        self._backdate(raised, requested_at=now - timedelta(days=3))
        rows = self._head_rows().context['head_change_rows']
        # Raised twenty days ago but forwarded today: it is the NEWEST with the Head.
        self.assertEqual([r['change_request'].pk for r in rows], [raised.pk, forwarded.pk])
        self.assertEqual([r['age_days'] for r in rows], [3, 0])
        self.assertEqual(rows[1]['forwarded_by'], self.pm)

    def test_h3_every_verdict_but_pending_is_excluded(self):
        made = self._seven()
        rows = self._head_rows().context['head_change_rows']
        self.assertEqual([r['change_request'].pk for r in rows],
                         [made[CHANGE_REQUEST_PENDING].pk])

    def test_h4_capped_at_25_and_the_rest_are_counted_not_dropped(self):
        self.assertEqual(HEAD_CHANGE_REQUEST_LIST_LIMIT, 25)
        _, a = self._site('CL-CAP')
        made = self._fixture_requests(a, 26)
        response = self._head_rows()
        rows = response.context['head_change_rows']
        self.assertEqual(len(rows), 25)
        self.assertEqual(response.context['head_change_more'], 1)
        # Oldest by head clock kept; the newest is the one counted.
        self.assertEqual([r['change_request'].pk for r in rows], [c.pk for c in made[:25]])
        page = response.content.decode()
        self.assertIn("1 more is waiting — open a tender's design dashboard to see them "
                      "all.", page)

    def test_h5_no_more_line_at_exactly_25_and_an_empty_state_at_zero(self):
        page = self._head_rows().content.decode()
        self.assertIn('No change requests are waiting for the Design Head.', page)
        _, a = self._site('CL-CAP25')
        self._fixture_requests(a, 25)
        response = self._head_rows()
        self.assertEqual(response.context['head_change_more'], 0)
        self.assertNotIn('more is waiting', response.content.decode())
        self.assertNotIn('more are waiting', response.content.decode())

    def test_h6_a_qc_reviewer_without_head_authority_does_not_see_the_section(self):
        self._pending_scm('CL-QC')
        response = self._head_rows(self.qc)
        self.assertEqual(response.context['head_change_rows'], [])
        page = response.content.decode()
        self.assertNotIn('Change requests waiting for the Design Head', page)
        self.assertNotIn('No change requests are waiting for the Design Head.', page)

    def test_h7_the_deputy_sees_it(self):
        deputy = self._person('cl_dep', 'Design')
        self.head.design_head_deputy = deputy
        self.head.save(update_fields=['design_head_deputy'])
        _, _, change = self._pending_scm('CL-DEP')
        rows = self._head_rows(deputy).context['head_change_rows']
        self.assertEqual([r['change_request'].pk for r in rows], [change.pk])


# ===========================================================================
# C — the corrected button on the tender dashboard (D-13)
# ===========================================================================

class DashboardCorrectedTests(ListsBase):

    def _dashboard(self, profile):
        return self._get(profile, 'design_tender_dashboard', pk=self.program.pk)

    def test_c1_renders_for_head_authority_with_its_note(self):
        _, _, change = self._pending_scm('CL-C1')
        response = self._dashboard(self.head)
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        self.assertIn(reverse('design_change_request_correct', kwargs={'pk': change.pk}), page)
        self.assertIn('name="correction_note" required', page)
        self.assertIn('No BOQ correction has been made since this request was raised.', page)
        self.assertNotIn('SESSION B3a', page)

    def test_c2_a_qc_reviewer_without_head_authority_is_refused_the_page(self):
        self._pending_scm('CL-C2')
        response = self._dashboard(self.qc)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('correction_note', response.content.decode())

    def test_c3_the_count_matches_the_banner_s(self):
        site1, a1 = self._released('CL-C3A')
        self._raise(self.pm, site1)
        self._boq_correct(site1, 12)
        self._boq_correct(site1, 13)
        site2, a2 = self._released('CL-C3B')
        early = self._boq_correct(site2, 11)
        self._raise(self.pm, site2)
        self._boq_correct(site2, 14)
        # The early correction is before site2's raise: excluded by time on both screens.
        c2 = self._pending(a2)
        self.assertLess(early.corrected_at, c2.requested_at)
        for site, a, expected in ((site1, a1, 2), (site2, a2, 1)):
            change = self._pending(a)
            banner = self._get(self.head, 'design_qc_review', project_id=site.project_id)
            banner_count = [cr.uncited_correction_count for cr in banner.context['open_crs']]
            rows = self._dashboard(self.head).context['m']['change_requests']
            dash = {r['change_request'].pk: r['uncited_correction_count'] for r in rows}
            with self.subTest(site=site.project_id):
                self.assertEqual(banner_count, [expected])
                self.assertEqual(dash[change.pk], expected)
                self.assertEqual(len(_uncited_corrections_since(site, change.requested_at)),
                                 expected)
        page = self._dashboard(self.head).content.decode()
        self.assertIn('2 BOQ corrections\n              made since this request was raised '
                      'will be cited.', page)

    def test_c4_the_batched_count_costs_one_query_and_none_when_empty(self):
        site1, a1 = self._released('CL-C4A')
        self._raise(self.pm, site1)
        self._boq_correct(site1, 12)
        site2, a2 = self._released('CL-C4B')
        self._raise(self.pm, site2)
        pairs = [(self._pending(a1), site1), (self._pending(a2), site2)]
        with self.assertNumQueries(0):
            self.assertEqual(_uncited_correction_counts([]), {})
        with self.assertNumQueries(1):
            counts = _uncited_correction_counts(pairs)
        self.assertEqual(counts, {cr.pk: len(_uncited_corrections_since(s, cr.requested_at))
                                  for cr, s in pairs})

    def test_c5_the_form_posts_back_to_the_dashboard(self):
        site, a = self._released('CL-C5')
        self._raise(self.pm, site)
        self._boq_correct(site, 12)
        change = self._pending(a)
        dashboard = reverse('design_tender_dashboard', kwargs={'pk': self.program.pk})
        self._login(self.head)
        response = self.client.post(
            reverse('design_change_request_correct', kwargs={'pk': change.pk}),
            {'correction_note': 'Module count raised to 12.', 'next': dashboard})
        self.client.logout()
        self.assertRedirects(response, dashboard, fetch_redirect_response=False)
        change.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_CORRECTED)


# ===========================================================================
# Q — both lists are flat in the number of rows
# ===========================================================================

MIXED = [CHANGE_REQUEST_WITH_PM, CHANGE_REQUEST_PENDING, CHANGE_REQUEST_ACCEPTED,
         CHANGE_REQUEST_REJECTED, CHANGE_REQUEST_PM_REJECTED, CHANGE_REQUEST_WITHDRAWN,
         CHANGE_REQUEST_CORRECTED]


class QueryCountTests(ListsBase):

    def _report(self, label, one, twenty):
        if os.environ.get('B3A_PRINT_QUERY_COUNTS'):
            print(f'\n{label}: {one} queries at 1 row, {twenty} at 20 rows')

    def test_q1_the_pm_list_is_flat(self):
        _, a = self._site('CL-QP')
        self._fixture_requests(a, 1, verdict=MIXED)
        one, r1 = self._count_queries(self.pm, 'design_pm_approval_queue')
        self.assertEqual(len(r1.context['change_rows']) + len(r1.context['history_rows']), 1)
        self._fixture_requests(a, 19, verdict=MIXED[1:] + MIXED[:1], start_number=3)
        twenty, r20 = self._count_queries(self.pm, 'design_pm_approval_queue')
        self.assertEqual(len(r20.context['change_rows']) + len(r20.context['history_rows']),
                         20)
        self.assertEqual({r['change_request'].verdict for r in r20.context['history_rows']}
                         | {CHANGE_REQUEST_WITH_PM}, ALL_SEVEN)
        self._report('PM list (design_pm_approval_queue)', one, twenty)
        self.assertEqual(one, twenty)

    def test_q2_the_head_list_is_flat(self):
        _, a = self._site('CL-QH')
        self._fixture_requests(a, 1)
        one, r1 = self._count_queries(self.head, 'design_qc_queue')
        self.assertEqual(len(r1.context['head_change_rows']), 1)
        self._fixture_requests(a, 19, start_number=3)
        twenty, r20 = self._count_queries(self.head, 'design_qc_queue')
        self.assertEqual(len(r20.context['head_change_rows']), 20)
        self._report('Head list (design_qc_queue)', one, twenty)
        self.assertEqual(one, twenty)
