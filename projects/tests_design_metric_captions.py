"""
D32 — the design quality captions describe what the figures now measure.

Three sessions changed what these figures count and left the on-screen text alone each
time: 07bdaa2 (the B-06 accounting), 9104ee7 (`pm_rejected` as a third attempt cause) and
6cdb61e (the Design Head's send-back). The corrections live in METRIC_CATALOGUE,
METRIC_GROUPS and two templates.

ASSERTED ON THE RENDERED HTML, NOT ON THE CATALOGUE. A caption that is right in
design_analytics.py but never reaches a screen is exactly as useless to the reader as a
wrong one, and a catalogue assertion would pass for it.

The figure tests pin, on one Group A PM rejection, the facts the corrected captions now
state, so the text and the number cannot drift apart silently a fourth time. One of them
pins the §D16 disagreement AS IT STANDS — it is a statement of the open question, not an
endorsement, and the session that decides §D16 is expected to change it.
"""
import re
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .design_analytics import compute
from .design_metrics import tender_metrics
from .models import (
    DesignAssignment, DesignAttempt, Program, Project,
    ATTEMPT_REASON_INITIAL, ATTEMPT_REASON_PM_REJECTED,
    DESIGN_RELEASED, ERR_LAYOUT, ERROR_GROUP_A, QC_PASSED,
    error_category_group,
)


def _profile(username, role, is_design_head=False):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.is_design_head = is_design_head
    profile.save()
    return profile


def _flat(html):
    """Whitespace collapsed, so a caption wrapped across template lines still matches."""
    return re.sub(r'\s+', ' ', html)


#: Corrected text that must reach the quality analytics page.
ANALYTICS_NOW = (
    # rework_multiplier (C2)
    'attempts a Group A QC failure or Group A PM rejection opened, plus uncategorised '
    'pre-Part-9 QC failures.',
    # error_distribution (C7)
    'across package failures, PM rejections and Arka rejections.',
    # group_b_failures (C10) and the Group B blurb (G2)
    'or the site differed from it. Never counted as designer rework.',
    'Problems that arrived with the site, not with the design. Never counted as designer '
    'rework.',
    # the error-distribution legend (T6)
    'from package failures and PM rejections ·',
    # G1 and G3: the false clause deleted, so the blurb is the first sentence alone.
    '>What the design team produced.<',
    '>How much the requirement moved after work started.<',
)

#: Text that was false or incomplete and must not come back.
ANALYTICS_GONE = (
    'Group A causes only',
    'never appears here',
    'Never folded into any designer figure',
    'Counted here and nowhere else',
    'counted against the PM who moved it',
    'across both package failures',
    'attempts a Group A failure opened',
)

#: The tender dashboard's Rework footer gains the source it omitted (D5).
DASHBOARD_NOW = 'Rework includes attempts opened by a PM rejection the Head classed Group A.'


class CaptionBase(TestCase):
    """One OPEX tender, one designer, one released site whose second attempt was opened by a
    PM rejection the Head classed Group A — the event all three sessions changed the
    accounting of."""

    def setUp(self):
        self.head = _profile('head_d32', 'Design', is_design_head=True)
        self.designer = _profile('designer_d32', 'Design')
        self.pm = _profile('pm_d32', 'PM')
        self.program = Program.objects.create(
            name='D32-Tender', program_type='OPEX', client_name='C', status='Active',
            short_tender_code='D32')

        # The fixture is only meaningful if the category is a designer error.
        self.assertEqual(error_category_group(ERR_LAYOUT), ERROR_GROUP_A)

        now = timezone.now()
        site = Project(
            project_id='D32-S1', customer_name='C', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code='D32-S1', assigned_pm=self.pm,
            dc_capacity_kw=Decimal('100.00'), status='Draft')
        site.save()
        assignment = DesignAssignment.objects.create(
            project=site, status=DESIGN_RELEASED, assigned_to=self.designer,
            assigned_at=now - timedelta(days=40), released_at=now - timedelta(days=2),
            current_attempt_number=2,
            survey_file_bucket='b', survey_file_path='D32-S1/survey/x.pdf')

        def passed(number, reason, **extra):
            return DesignAttempt.objects.create(
                assignment=assignment, attempt_number=number, opened_reason=reason,
                qc_verdict=QC_PASSED, qc_remarks='ok', qc_reviewed_by=self.head,
                qc_started_at=now - timedelta(days=20),
                qc_reviewed_at=now - timedelta(days=18),
                head_verdict=QC_PASSED, head_remarks='ok', head_reviewed_by=self.head,
                head_started_at=now - timedelta(days=17),
                head_reviewed_at=now - timedelta(days=15), **extra)

        # Attempt 1 passed both gates; the PM rejected it and the Head sent it back as a
        # layout error. Attempt 2 is the send-back's, and it was released.
        passed(1, ATTEMPT_REASON_INITIAL,
               pm_rejection_category=ERR_LAYOUT, pm_rejection_remarks='layout clash')
        passed(2, ATTEMPT_REASON_PM_REJECTED)

        self.client.force_login(self.head.user)


class CorrectedCaptionsRenderTests(CaptionBase):

    def _analytics(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return _flat(response.content.decode())

    def test_the_tender_analytics_page_shows_the_corrected_captions(self):
        html = self._analytics(reverse('design_quality_analytics_tender',
                                       kwargs={'pk': self.program.pk}))
        for text in ANALYTICS_NOW:
            self.assertIn(text, html)
        for text in ANALYTICS_GONE:
            self.assertNotIn(text, html)

    def test_the_all_tenders_analytics_page_shows_the_corrected_captions(self):
        html = self._analytics(reverse('design_quality_analytics'))
        for text in ANALYTICS_NOW:
            self.assertIn(text, html)
        for text in ANALYTICS_GONE:
            self.assertNotIn(text, html)

    def test_the_tender_dashboard_names_the_pm_rejection_in_rework(self):
        response = self.client.get(reverse('design_tender_dashboard',
                                           kwargs={'pk': self.program.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertIn(DASHBOARD_NOW, _flat(response.content.decode()))


class CaptionedFiguresTests(CaptionBase):
    """What the corrected captions claim, checked against the figures themselves."""

    def _panels(self):
        result = compute([self.program],
                         {'first_pass_rate', 'rework_multiplier', 'error_distribution'})
        return {p['metric'].key: p['data'] for p in result['panels']}

    def test_rework_counts_the_group_a_pm_rejection_on_both_screens(self):
        row = self._panels()['rework_multiplier']['rows'][0]
        self.assertEqual((row['figure']['numerator'], row['figure']['n']), (1.0, 1))
        self.assertEqual(row['uncategorised'], 0)
        workload = tender_metrics(self.program)['workload'][0]
        self.assertEqual(workload['rework_attempts'], 1)
        self.assertEqual(workload['rework'], 1.0)

    def test_the_error_distribution_counts_it_under_package(self):
        data = self._panels()['error_distribution']
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['by_source'], {'package': 1, 'arka': 0})
        self.assertEqual([(c['category'], c['count']) for c in data['team']],
                         [(ERR_LAYOUT, 1)])

    def test_first_pass_does_not_count_it_which_is_section_d16(self):
        """§D16, OPEN. The same send-back is designer rework above and costs no first-pass
        here. Pinned as it stands; the session that decides §D16 changes this."""
        team = self._panels()['first_pass_rate']['team']
        self.assertEqual((team['numerator'], team['n']), (1, 1))
