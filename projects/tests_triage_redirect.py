"""
SEC-1 — the `next` target of the change-request triage views must never leave the site.

_triage_redirect() is where Accept, Reject and Correct land. The tender dashboard's queue
posts `next` so the Head stays on the queue; anything else falls back to the site's QC
review page. Before SEC-1 the check was `startswith('/') and not startswith('//')`, which
`/\\evil.com` and `/<tab>/evil.com` both passed — browsers read the first as `//evil.com`,
and the URL parser drops the tab in the second. The function now uses Django's
url_has_allowed_host_and_scheme().

WHAT IS PINNED
--------------
  U    The function itself, called with a RequestFactory POST: the real dashboard path
       (with and without a query and fragment) is followed unchanged, a leading space is
       stripped as before, and every hostile form falls back to QC review.
  V    One real caller (Reject) with `next=/\\evil.com`: the Location is QC review.
"""
from types import SimpleNamespace

from django.test import RequestFactory, SimpleTestCase
from django.urls import reverse

from .design_views import _triage_redirect
from .models import CHANGE_REQUEST_REJECTED
from .tests_design_change_window import ChangeWindowBase


# ===========================================================================
# U — _triage_redirect() called directly
# ===========================================================================

class TriageRedirectUnitTests(SimpleTestCase):
    """No database: the function reads only request.POST, the request's host and scheme,
    and project.project_id for the fallback reverse()."""

    def setUp(self):
        self.factory = RequestFactory()
        # A stand-in project: the fallback needs only project_id to reverse() QC review.
        self.project = SimpleNamespace(project_id='SEC1-SITE')
        self.fallback = reverse('design_qc_review',
                                kwargs={'project_id': self.project.project_id})
        self.dashboard = reverse('design_tender_dashboard', kwargs={'pk': 7})

    def _target(self, data, secure=False):
        request = self.factory.post('/any/', data, secure=secure)
        return _triage_redirect(request, self.project)

    def test_u1_the_dashboard_path_is_followed_unchanged(self):
        self.assertEqual(self._target({'next': self.dashboard}), self.dashboard)

    def test_u2_the_dashboard_path_with_query_and_fragment_is_followed_unchanged(self):
        url = self.dashboard + '?q=abc#row-5'
        self.assertEqual(self._target({'next': url}), url)

    def test_u3_a_local_path_is_followed_on_an_https_request_too(self):
        # require_https=request.is_secure() must not refuse a scheme-less local path.
        self.assertEqual(self._target({'next': self.dashboard}, secure=True),
                         self.dashboard)

    def test_u4_a_leading_space_is_stripped_then_followed_as_before(self):
        self.assertEqual(self._target({'next': ' ' + self.dashboard}), self.dashboard)

    def test_u5_other_hostile_targets_fall_back_to_qc_review(self):
        # The backslash and tab forms — the two the old check let through — have their
        # own tests below, so a regression names them directly.
        hostile = [
            '//evil.com',             # protocol-relative
            'https://evil.com/',      # absolute, other host
            'http:evil.com',          # scheme with no slashes
            'javascript:alert(1)',    # script scheme
        ]
        for value in hostile:
            with self.subTest(next=value):
                self.assertEqual(self._target({'next': value}), self.fallback)

    def test_u6_a_backslash_after_the_slash_falls_back(self):
        # The SEC-1 hole: browsers read /\ as //, so this left the site before the fix.
        self.assertEqual(self._target({'next': '/\\evil.com'}), self.fallback)

    def test_u7_a_tab_after_the_slash_falls_back(self):
        # Also passed the old startswith('/') check: the URL parser drops the tab and
        # sees //evil.com.
        self.assertEqual(self._target({'next': '/\t/evil.com'}), self.fallback)

    def test_u8_empty_or_missing_next_falls_back(self):
        self.assertEqual(self._target({'next': ''}), self.fallback)
        self.assertEqual(self._target({'next': '   '}), self.fallback)
        self.assertEqual(self._target({}), self.fallback)


# ===========================================================================
# V — one real caller, through the URL
# ===========================================================================

class TriageRedirectViewTests(ChangeWindowBase):

    def test_v1_reject_with_a_backslash_next_lands_on_qc_review(self):
        site, assignment = self._released('SEC1-V1')
        self._raise(self.pm, site)
        change = self._requests(assignment).get()

        self._login(self.head)
        response = self.client.post(
            reverse('design_change_request_reject', kwargs={'pk': change.pk}),
            {'rejection_reason': 'The current version stands.', 'next': '/\\evil.com'})
        self.client.logout()

        qc_review = reverse('design_qc_review', kwargs={'project_id': site.project_id})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], qc_review)
        self.assertNotIn('evil.com', response['Location'])
        # The verdict itself still applied: only the landing changed.
        change.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_REJECTED)
