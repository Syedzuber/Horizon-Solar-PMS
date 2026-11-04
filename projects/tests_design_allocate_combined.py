"""DA-2 — one-step designer + Design QC allocation, bulk QC, link lock, bulk counter.

WHAT IS PINNED HERE
-------------------
  (a) design_allocate, combined form: S1 (an unchanged designer is never re-allocated, on
      BOTH the combined and the designer-only post), the S3 order that makes a
      designer/reviewer swap possible, the submitted-pair check, and ONE transaction —
      a refusal at any step leaves nothing written.
  (b) design_bulk_allocate: Keep current QC / Open pool / a named reviewer, de-duplication,
      named refusals (empty, unknown id, other tender, failing site), the ruled Keep
      conflict, and the "N allocated ... M unchanged" counts.
  (c) The link button is hidden exactly where design_survey_link_set() refuses.
  (d) The bulk counter and the two QC resets, run in Node against the page's own script.
"""
import json
import re
import shutil
import subprocess
import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock, skipUnless

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from . import design_views
from .design_views import BULK_QC_KEEP, QC_LOCKED_REFUSAL, _effective_commitment
from .models import (
    ActivityLog, DesignAssignment, DesignAttempt, DueDateCommitment, Program, Project,
    StatusTransition,
    DESIGN_AWAITING_ALLOCATION, DESIGN_IN_DESIGN, DESIGN_IN_QC, DESIGN_RELEASED,
    DESIGN_SURVEY_RETURNED,
)


def _profile(username, first, last, role='Design', **flags):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=first, last_name=last)
    profile = user.profile
    profile.role = role
    profile.is_active = True
    for field, value in flags.items():
        setattr(profile, field, value)
    profile.save()
    return profile


class AllocationBase(TestCase):

    def setUp(self):
        self.head = _profile('ca_head', 'Hema', 'Head', is_design_head=True)
        self.d1 = _profile('ca_d1', 'Dev', 'One')
        self.d2 = _profile('ca_d2', 'Dina', 'Two')
        self.q = _profile('ca_q', 'Qadir', 'Reviewer', is_design_qc=True)
        self.r = _profile('ca_r', 'Rita', 'Reviewer', is_design_qc=True)
        self.program = Program.objects.create(
            name='Test-Combined', program_type='OPEX', client_name='CAClient',
            status='Active', short_tender_code='CA')
        self.client = Client()
        self.client.force_login(self.head.user)

    def _site(self, code, status=DESIGN_AWAITING_ALLOCATION, designer=None, qc=None,
              program=None, link=''):
        site = Project(
            project_id=code, customer_name='CAClient', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=program or self.program, site_code=code,
            dc_capacity_kw=Decimal('100.00'), status='Draft',
            assigned_design=designer)
        site.save()
        assignment = DesignAssignment.objects.create(
            project=site, status=status, assigned_to=designer, qc_assigned_to=qc,
            survey_file_bucket='b', survey_file_path=f'{code}/survey/x.pdf',
            survey_folder_url=link)
        if designer is not None:
            # The agreed date allocation would have written.
            DueDateCommitment.objects.create(
                assignment=assignment, proposed_date=timezone.localdate() + timedelta(days=2),
                proposed_by=self.head, approved_by=self.head, approved_at=timezone.now(),
                is_current=True)
        return site, assignment

    def _a(self, code):
        return DesignAssignment.objects.select_related('project').get(project__project_id=code)

    def _counts(self):
        return (DueDateCommitment.objects.count(), ActivityLog.objects.count(),
                StatusTransition.objects.count())

    def _logs_since(self, last_pk):
        return list(ActivityLog.objects.filter(pk__gt=last_pk).order_by('pk')
                    .values_list('action_code', 'action'))

    def _last_log_pk(self):
        return ActivityLog.objects.order_by('-pk').values_list('pk', flat=True).first() or 0

    def _message(self, response):
        message = list(get_messages(response.wsgi_request))[-1]
        return message.level_tag, str(message), message.extra_tags

    def _allocate(self, code, **data):
        return self.client.post(reverse('design_allocate', kwargs={'project_id': code}), data)

    def _bulk(self, **data):
        return self.client.post(reverse('design_bulk_allocate', kwargs={'pk': self.program.pk}),
                                data)


# ===========================================================================
# (a) The combined single-site form
# ===========================================================================

class CombinedAllocateTests(AllocationBase):

    def test_same_designer_new_qc_changes_only_the_reviewer(self):
        self._site('CA1', DESIGN_IN_DESIGN, self.d1, self.q)
        agreed = _effective_commitment(self._a('CA1')).proposed_date
        commitments, last = DueDateCommitment.objects.count(), self._last_log_pk()

        response = self._allocate('CA1', designer_id=self.d1.pk, qc_id=self.r.pk)

        a = self._a('CA1')
        self.assertEqual((a.assigned_to_id, a.qc_assigned_to_id), (self.d1.pk, self.r.pk))
        self.assertEqual(DueDateCommitment.objects.count(), commitments)
        self.assertEqual(_effective_commitment(a).proposed_date, agreed)
        self.assertEqual([code for code, _ in self._logs_since(last)], ['design_qc_reassigned'])
        self.assertEqual(self._message(response),
                         ('success', 'CA1: Design QC assigned to Rita Reviewer.', 'site:CA1'))

    def test_nothing_changed_writes_nothing(self):
        self._site('CA2', DESIGN_IN_DESIGN, self.d1, self.q)
        before, stamped = self._counts(), self._a('CA2').updated_at

        response = self._allocate('CA2', designer_id=self.d1.pk, qc_id=self.q.pk)

        self.assertEqual(self._counts(), before)
        self.assertEqual(self._a('CA2').updated_at, stamped)
        self.assertEqual(self._message(response), ('info', 'Nothing changed for CA2.', 'site:CA2'))
        self.assertTrue(response['Location'].endswith('#site-CA2'))

    def test_same_designer_with_no_qc_field_is_also_nothing_changed(self):
        """S1 on the designer-only post too: the old path re-ran _allocate_one here and reset
        the agreed date."""
        self._site('CA3', DESIGN_IN_DESIGN, self.d1, self.q)
        before = self._counts()
        response = self._allocate('CA3', designer_id=self.d1.pk)
        self.assertEqual(self._counts(), before)
        self.assertEqual(self._message(response), ('info', 'Nothing changed for CA3.', 'site:CA3'))

    def test_new_designer_same_qc_runs_allocate_once_and_leaves_qc(self):
        self._site('CA4', DESIGN_IN_DESIGN, self.d1, self.q)
        last = self._last_log_pk()
        with mock.patch.object(design_views, '_allocate_one',
                               wraps=design_views._allocate_one) as spy:
            response = self._allocate('CA4', designer_id=self.d2.pk, qc_id=self.q.pk)
        self.assertEqual(spy.call_count, 1)
        a = self._a('CA4')
        self.assertEqual((a.assigned_to_id, a.qc_assigned_to_id), (self.d2.pk, self.q.pk))
        self.assertEqual([code for code, _ in self._logs_since(last)], ['design_reallocated'])
        level, text, _ = self._message(response)
        self.assertEqual(level, 'success')
        self.assertTrue(text.startswith('CA4 allocated to Dina Two — due '), text)
        self.assertNotIn('Design QC', text)

    def test_swap_designer_and_reviewer_in_one_save(self):
        self._site('CA5', DESIGN_IN_DESIGN, self.d1, self.q)
        last = self._last_log_pk()

        response = self._allocate('CA5', designer_id=self.q.pk, qc_id=self.d1.pk)

        a = self._a('CA5')
        self.assertEqual(a.assigned_to_id, self.q.pk)
        self.assertEqual(a.project.assigned_design_id, self.q.pk)
        self.assertEqual(a.qc_assigned_to_id, self.d1.pk)
        logs = self._logs_since(last)
        self.assertEqual([code for code, _ in logs],
                         ['design_qc_unassigned', 'design_reallocated', 'design_qc_assigned'])
        self.assertEqual(logs[0][1], 'Design QC reviewer cleared — gate 1 returns to the open '
                                     'pool (was Qadir Reviewer)')
        self.assertTrue(logs[1][1].startswith('Site reallocated from Dev One to Qadir Reviewer; '
                                              'due '), logs[1][1])
        self.assertEqual(logs[2][1], 'Design QC reviewer assigned: Dev One')
        level, text, _ = self._message(response)
        self.assertEqual(level, 'success')
        self.assertTrue(text.endswith(' Design QC assigned to Dev One.'), text)

    def test_submitted_pair_of_one_person_is_refused(self):
        self._site('CA6', DESIGN_IN_DESIGN, self.d1, self.q)
        before = self._counts()
        response = self._allocate('CA6', designer_id=self.d2.pk, qc_id=self.d2.pk)
        self.assertEqual(self._counts(), before)
        a = self._a('CA6')
        self.assertEqual((a.assigned_to_id, a.qc_assigned_to_id), (self.d1.pk, self.q.pk))
        self.assertEqual(self._message(response)[:2], (
            'danger', 'CA6: Dina Two cannot be both the designer and the Design QC reviewer of '
                      'this site — nobody reviews their own work.'))

    def test_a_failure_in_step_d_rolls_back_step_c(self):
        self._site('CA7', DESIGN_IN_DESIGN, self.d1, self.q)
        before, last = self._counts(), self._last_log_pk()
        real = design_views.assign_design_qc

        def failing(assignment, reviewer, actor):
            if reviewer is not None:
                raise ValueError('forced failure in step d')
            return real(assignment, reviewer, actor)

        with mock.patch.object(design_views, 'assign_design_qc', side_effect=failing):
            response = self._allocate('CA7', designer_id=self.d2.pk, qc_id=self.r.pk)

        a = self._a('CA7')
        self.assertEqual((a.assigned_to_id, a.qc_assigned_to_id), (self.d1.pk, self.q.pk))
        self.assertEqual(a.project.assigned_design_id, self.d1.pk)
        self.assertEqual(self._counts(), before)
        self.assertEqual(self._logs_since(last), [])
        self.assertEqual(self._message(response)[:2],
                         ('danger', 'CA7: forced failure in step d.'))

    def test_a_gate1_verdict_refuses_the_qc_change_and_the_designer_change_with_it(self):
        _, assignment = self._site('CA8', DESIGN_IN_DESIGN, self.d1, self.q)
        DesignAttempt.objects.create(assignment=assignment, attempt_number=1,
                                     qc_verdict='passed', qc_reviewed_by=self.q,
                                     qc_reviewed_at=timezone.now())
        DesignAssignment.objects.filter(pk=assignment.pk).update(current_attempt_number=1)
        before = self._counts()

        response = self._allocate('CA8', designer_id=self.d2.pk, qc_id=self.r.pk)

        a = self._a('CA8')
        self.assertEqual((a.assigned_to_id, a.qc_assigned_to_id), (self.d1.pk, self.q.pk))
        self.assertEqual(self._counts(), before)
        self.assertEqual(self._message(response)[:2],
                         ('danger', f'CA8: {QC_LOCKED_REFUSAL}.'))

    def test_designer_only_post_keeps_the_old_refusal_when_the_designer_is_the_qc(self):
        """No qc_id: the reviewer is not touched, so _allocate_one's refusal stands, worded
        exactly as before DA-2."""
        self._site('CA9', DESIGN_IN_DESIGN, self.d1, self.q)
        before = self._counts()
        response = self._allocate('CA9', designer_id=self.q.pk)
        self.assertEqual(self._counts(), before)
        level, text, _ = self._message(response)
        self.assertEqual(level, 'danger')
        self.assertEqual(text, 'CA9 cannot be allocated to Qadir Reviewer, who is already its '
                               'Design QC reviewer — nobody reviews their own work, so change '
                               'the reviewer first.')

    def test_first_allocation_with_a_reviewer(self):
        self._site('CA10')
        response = self._allocate('CA10', designer_id=self.d1.pk, qc_id=self.q.pk)
        a = self._a('CA10')
        self.assertEqual((a.status, a.assigned_to_id, a.qc_assigned_to_id),
                         (DESIGN_IN_DESIGN, self.d1.pk, self.q.pk))
        level, text, _ = self._message(response)
        self.assertEqual(level, 'success')
        self.assertTrue(text.startswith('CA10 allocated to Dev One — due '))
        self.assertTrue(text.endswith(' Design QC assigned to Qadir Reviewer.'))

    def test_blank_qc_clears_to_the_open_pool(self):
        self._site('CA11', DESIGN_IN_DESIGN, self.d1, self.q)
        response = self._allocate('CA11', designer_id=self.d1.pk, qc_id='')
        self.assertIsNone(self._a('CA11').qc_assigned_to_id)
        self.assertEqual(self._message(response)[1],
                         'CA11: Design QC reviewer cleared — any QC reviewer may now take '
                         'this site.')


# ===========================================================================
# (b) Bulk
# ===========================================================================

class BulkQcTests(AllocationBase):

    def test_keep_current_leaves_every_reviewer_as_it_was(self):
        for code, qc in (('B1', self.q), ('B2', None), ('B3', self.r)):
            self._site(code, qc=qc)
        for qc_field in ({'qc_id': BULK_QC_KEEP}, {}):          # explicit, and the old shape
            with self.subTest(qc_field=qc_field):
                self._bulk(designer_id=self.d1.pk if not qc_field else self.d2.pk,
                           site_ids=['B1', 'B2', 'B3'], **qc_field)
                self.assertEqual([self._a(c).qc_assigned_to_id for c in ('B1', 'B2', 'B3')],
                                 [self.q.pk, None, self.r.pk])

    def test_open_pool_clears_every_reviewer(self):
        for code, qc in (('B4', self.q), ('B5', self.r)):
            self._site(code, qc=qc)
        response = self._bulk(designer_id=self.d1.pk, qc_id='', site_ids=['B4', 'B5'])
        self.assertEqual([self._a(c).qc_assigned_to_id for c in ('B4', 'B5')], [None, None])
        self.assertIn('Design QC cleared to the open pool on 2 site(s): B4, B5.',
                      self._message(response)[1])

    def test_a_named_reviewer_is_set_on_every_site(self):
        for code in ('B6', 'B7'):
            self._site(code, qc=self.q)
        response = self._bulk(designer_id=self.d1.pk, qc_id=self.r.pk, site_ids=['B6', 'B7'])
        self.assertEqual([self._a(c).qc_assigned_to_id for c in ('B6', 'B7')],
                         [self.r.pk, self.r.pk])
        self.assertIn('Design QC set to Rita Reviewer on 2 site(s): B6, B7.',
                      self._message(response)[1])

    def test_duplicates_are_allocated_once(self):
        self._site('B8')
        self._site('B9')
        response = self._bulk(designer_id=self.d1.pk, site_ids=['B8', 'B8', ' B9 ', 'B8'])
        for code in ('B8', 'B9'):
            self.assertEqual(DueDateCommitment.objects.filter(
                assignment__project__project_id=code).count(), 1)
        self.assertTrue(self._message(response)[1].startswith(
            '2 site(s) allocated to Dev One, due '))
        self.assertIn(': B8, B9.', self._message(response)[1])

    def test_an_empty_selection_is_refused(self):
        for ids in ([], ['', '  ']):
            with self.subTest(ids=ids):
                response = self._bulk(designer_id=self.d1.pk, site_ids=ids)
                self.assertEqual(self._message(response)[:2],
                                 ('danger', 'Select at least one site to allocate.'))

    def test_unknown_and_other_tender_ids_are_refused_by_name_not_404(self):
        self._site('B10')
        other = Program.objects.create(name='Other', program_type='OPEX', client_name='X',
                                       status='Active', short_tender_code='OT')
        self._site('OT1', program=other)
        before = self._counts()
        response = self._bulk(designer_id=self.d1.pk, site_ids=['B10', 'NOPE', 'OT1'])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._counts(), before)
        self.assertEqual(self._message(response)[:2], (
            'danger', 'Nothing was allocated — not a site of this tender: NOPE, OT1.'))
        self.assertNotIn('#site-', response['Location'])

    def test_the_failing_site_is_named_and_nothing_is_written(self):
        self._site('B11')
        self._site('B12', DESIGN_IN_QC, self.d2)
        before = self._counts()
        response = self._bulk(designer_id=self.d1.pk, site_ids=['B11', 'B12'])
        self.assertEqual(self._counts(), before)
        self.assertIsNone(self._a('B11').assigned_to_id)
        level, text, tags = self._message(response)
        self.assertEqual(level, 'danger')
        self.assertTrue(text.startswith('Nothing was allocated — B12 has already started '
                                        'design work (status in_qc)'), text)
        self.assertEqual(tags, 'site:B12 page')
        self.assertTrue(response['Location'].endswith('#site-B12'))

    def test_counts_allocated_and_unchanged(self):
        self._site('B13')                                       # will be allocated
        self._site('B14', DESIGN_IN_DESIGN, self.d1)           # already Dev One's
        self._site('B15', DESIGN_IN_QC, self.d1, self.q)       # already Dev One's, not allocatable
        before = DueDateCommitment.objects.count()
        response = self._bulk(designer_id=self.d1.pk, site_ids=['B13', 'B14', 'B15'])
        self.assertEqual(DueDateCommitment.objects.count(), before + 1)
        level, text, _ = self._message(response)
        self.assertEqual(level, 'success')
        self.assertTrue(text.startswith('1 site(s) allocated to Dev One, due '), text)
        self.assertTrue(text.endswith('2 unchanged: B14, B15.'), text)

    def test_all_unchanged_is_info_not_success(self):
        self._site('B16', DESIGN_IN_DESIGN, self.d1)
        response = self._bulk(designer_id=self.d1.pk, site_ids=['B16'])
        self.assertEqual(self._message(response)[:2],
                         ('info', '0 site(s) allocated to Dev One. 1 unchanged: B16.'))

    def test_keep_refuses_when_a_sites_reviewer_is_the_chosen_designer(self):
        self._site('B17', qc=self.d1)
        self._site('B18', qc=None)
        self._site('B19', qc=self.d1)
        before = self._counts()
        response = self._bulk(designer_id=self.d1.pk, qc_id=BULK_QC_KEEP,
                              site_ids=['B17', 'B18', 'B19'])
        self.assertEqual(self._counts(), before)
        level, text, tags = self._message(response)
        self.assertEqual(level, 'danger')
        self.assertEqual(text, 'Nothing was allocated — Dev One is already the Design QC '
                               'reviewer of B17, B19. Choose Open pool or another QC reviewer '
                               'for these sites.')
        self.assertTrue(text.endswith('Choose Open pool or another QC reviewer for these sites.'))
        self.assertEqual(tags, 'site:B17 page')

    def test_open_pool_resolves_what_keep_refuses(self):
        self._site('B20', DESIGN_IN_DESIGN, self.d2, self.d1)
        last = self._last_log_pk()
        self._bulk(designer_id=self.d1.pk, qc_id='', site_ids=['B20'])
        a = self._a('B20')
        self.assertEqual((a.assigned_to_id, a.qc_assigned_to_id), (self.d1.pk, None))
        self.assertEqual([c for c, _ in self._logs_since(last)],
                         ['design_qc_unassigned', 'design_reallocated'])

    def test_reviewer_equal_to_designer_is_refused(self):
        self._site('B21')
        response = self._bulk(designer_id=self.d1.pk, qc_id=self.d1.pk, site_ids=['B21'])
        self.assertIsNone(self._a('B21').assigned_to_id)
        self.assertEqual(self._message(response)[1],
                         'Nothing was allocated — Dev One cannot be both the designer and the '
                         'Design QC reviewer of the same sites.')


# ===========================================================================
# (c) The link button follows the view's own lock
# ===========================================================================

class LinkLockTests(AllocationBase):

    LINK = 'https://drive.google.com/drive/folders/old'

    def test_the_button_is_hidden_exactly_where_the_view_refuses(self):
        link = self.LINK
        cases = [('L1', DESIGN_AWAITING_ALLOCATION, None, link),
                 ('L2', DESIGN_IN_DESIGN, self.d1, link),
                 ('L3', DESIGN_IN_QC, self.d1, link),
                 ('L4', DESIGN_RELEASED, self.d1, link),
                 ('L5', DESIGN_SURVEY_RETURNED, self.d1, link),
                 ('L6', DESIGN_IN_DESIGN, self.d1, '')]           # no link yet: "Add link"
        for code, status, designer, link in cases:
            self._site(code, status, designer, link=link)
        html = self.client.get(reverse('design_head_sites',
                                       kwargs={'pk': self.program.pk})).content.decode()
        rows = {r['site'].project_id: r for r in self.client.get(
            reverse('design_head_sites', kwargs={'pk': self.program.pk})).context['rows']}

        for code, status, _designer, link in cases:
            with self.subTest(code=code, status=status, link=bool(link)):
                body = re.search(r'<tbody id="site-%s".*?</tbody>' % code, html, re.S).group(0)
                shown = ('Edit link' in body) or ('Add link' in body)
                response = self.client.post(
                    reverse('design_survey_link_set', kwargs={'project_id': code}),
                    {'survey_folder_url': 'https://drive.google.com/drive/folders/new'})
                refused = 'cannot be changed after allocation' in self._message(response)[1]
                self.assertEqual(rows[code]['link_locked'], refused)
                self.assertEqual(shown, not refused)
                self.assertEqual(f'id="lk{rows[code]["site"].pk}"' in body, not refused)
        # The lock fires where expected — the test is not vacuous.
        self.assertTrue(rows['L2']['link_locked'])
        self.assertFalse(rows['L1']['link_locked'])
        self.assertFalse(rows['L6']['link_locked'])


# ===========================================================================
# (d) Markup, and the page's own Alpine logic in Node
# ===========================================================================

class CombinedFormMarkupTests(AllocationBase):

    def _html(self):
        return self.client.get(reverse('design_head_sites',
                                       kwargs={'pk': self.program.pk})).content.decode()

    @staticmethod
    def _tbody(html, code):
        return re.search(r'<tbody id="site-%s".*?</tbody>' % code, html, re.S).group(0)

    def test_the_combined_form_is_prefilled_and_labelled(self):
        self._site('M1', DESIGN_IN_DESIGN, self.d1, self.q)
        self._site('M2')
        html = self._html()
        allocated, fresh = self._tbody(html, 'M1'), self._tbody(html, 'M2')

        form = re.search(r'<form method="post" action="%s".*?</form>'
                         % reverse('design_allocate', kwargs={'project_id': 'M1'}),
                         allocated, re.S).group(0)
        self.assertIn(f'data-current-designer="{self.d1.pk}"', form)
        self.assertIn(f'data-current-qc="{self.q.pk}"', form)
        self.assertRegex(form, r'<option value="%d"\s+selected>\s+Dev One' % self.d1.pk)
        self.assertIn('name="qc_id"', form)
        self.assertRegex(form, r'<option value="%d"\s+selected\s' % self.q.pk)
        self.assertIn('>Save</button>', form)
        self.assertIn('Changing the designer resets the due date to today + 2 working days.', form)
        self.assertIn('Design QC reset to the open pool', form)
        # The separate Save QC form is not drawn on an allocatable row.
        self.assertNotIn(reverse('design_assign_qc', kwargs={'project_id': 'M1'}), allocated)

        fresh_form = re.search(r'<form method="post" action="%s".*?</form>'
                               % reverse('design_allocate', kwargs={'project_id': 'M2'}),
                               fresh, re.S).group(0)
        self.assertIn('>Allocate</button>', fresh_form)
        self.assertIn('data-current-designer=""', fresh_form)

    def test_the_bulk_bar_carries_the_qc_selector_and_counter(self):
        self._site('M3')
        html = self._html()
        self.assertIn(f'<option value="{BULK_QC_KEEP}" selected>Keep current QC</option>', html)
        self.assertIn('<option value="">Open pool — any QC reviewer</option>', html)
        self.assertIn('x-text="selectionSummary()"', html)
        self.assertIn('x-model="selected"', html)
        self.assertIn(f'data-bulk-keep="{BULK_QC_KEEP}"', html)


@skipUnless(shutil.which('node'), 'Node is not installed')
class AlpineLogicTests(AllocationBase):
    """Runs the page's OWN inline script under Node with a minimal Alpine/DOM stub, so the
    counter wording and the two QC resets are pinned as shipped, not as re-typed here."""

    HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const comps = {}, els = {};
global.Alpine = { data: (name, f) => { comps[name] = f; } };
global.document = {
  addEventListener: (ev, cb) => { if (ev === 'alpine:init') cb(); },
  getElementById: (id) => id === 'headSitesFilter'
    ? { textContent: '{"q": "", "status": "designer"}' } : els[id],
};
global.window = { location: { pathname: '/', hash: '' }, history: { replaceState() {} } };
eval(src);
function make(name, dataset) {
  const watchers = {};
  const c = comps[name]();
  c.$el = { dataset }; c.$watch = (p, cb) => { watchers[p] = cb; }; c.$nextTick = () => {};
  c.init();
  return [c, watchers];
}
const out = {};
const [page, pw] = make('headSites', { bulkKeep: 'keep' });
out.bulkDefault = page.bulkQc;
els['site-A'] = { dataset: { chip: 'designer', search: 'a delhi', pinned: '0' } };
els['site-B'] = { dataset: { chip: 'qc', search: 'b delhi', pinned: '0' } };
page.selected = ['A', 'B'];
out.filtered = page.selectionSummary();
out.visible = page.visibleSelected();
page.status = '';
out.unfiltered = page.selectionSummary();
page.bulkQc = '7'; page.bulkDesigner = '7'; pw.bulkDesigner('7');
out.bulkReset = [page.bulkQc, page.bulkQcReset];
const [row, rw] = make('allocForm', { currentDesigner: '3', currentQc: '5' });
out.rowSeed = [row.designer, row.qc, row.currentDesigner];
row.designer = '5'; rw.designer('5');
out.rowReset = [row.qc, row.qcReset];
console.log(JSON.stringify(out));
"""

    def test_counter_and_resets(self):
        self._site('N1')
        html = self.client.get(reverse('design_head_sites',
                                       kwargs={'pk': self.program.pk})).content.decode()
        script = re.search(r'<script>\r?\n(/\*.*?DA-1.*?)</script>', html, re.S).group(1)
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'page.js').write_text(script, encoding='utf-8')
            Path(tmp, 'harness.js').write_text(self.HARNESS, encoding='utf-8')
            run = subprocess.run(['node', str(Path(tmp, 'harness.js')), str(Path(tmp, 'page.js'))],
                                 capture_output=True, text=True, encoding='utf-8',
                                 timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        out = json.loads(run.stdout)
        self.assertEqual(out['bulkDefault'], 'keep')
        self.assertEqual(out['filtered'],
                         '1 selected · 1 hidden by filter (not included until shown)')
        self.assertEqual(out['visible'], ['A'])
        self.assertEqual(out['unfiltered'], '2 selected')
        self.assertEqual(out['bulkReset'], ['keep', True])
        self.assertEqual(out['rowSeed'], ['3', '5', '3'])
        self.assertEqual(out['rowReset'], ['', True])
