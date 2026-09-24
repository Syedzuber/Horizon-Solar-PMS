"""The design PM gate, LIVE — every gate status reached through the product (prompt 3.1b-2c).

WHY THIS FILE EXISTS
--------------------
Prompt 3.1b-2c flipped two status writes. design_head_qc_pass() now hands the package to
the site's PM (`awaiting_pm_approval`) instead of releasing it, and design_pm_reject() sends
it to `pm_rejected` instead of `awaiting_head_qc`. Four sessions of inert work became
reachable in that one commit, and the three modules that proved it unreachable retired with
it (their behaviour tests live on in tests_design_pm_gate_fixtured).

THIS FILE IS PURE, AND THAT IS ITS POINT. No test here parks a row by fixture and none
writes a status with update(): every site starts at `awaiting_allocation` and is driven
through the real views — allocation, Arka, both Arka gates, CAD, BOQ, Design QC, the
Design Head, the PM, SCM. Storage is stubbed at its point of use, and nothing else is.

  (a) THE WRITER SET — the obligation inherited from the retired absence proofs
      (EXECUTION_MODULE_DEFERRED.md §D24): for each gate status, the set of product
      functions that can write it is EXACTLY the intended set, by PARSE not grep,
      including the statuses computed at run time
  (b) the Head's pass -> awaiting_pm_approval, NOT released, no release stamps
  (c) the PM's approval -> released, and released_at is the APPROVAL time
  (d) the PM's rejection -> pm_rejected, and the PM's words on both of the Head's screens
  (e) the Head's return -> awaiting_pm_approval, and his words on the PM's queue — and the
      TWO-CYCLE test: a return remark from an earlier trip through the gate is not shown on
      a package that came back through a second Head pass
  (f) the Head's send-back -> attempt N+1, reason pm_rejected, the due date moved out, and
      the designer charged for a Group A category only
  (g) the boundary: a site with the PM is not in SCM's pool, cannot be grouped and is not
      counted as released
  (h) the full chain once, end to end
  plus the mirror: writes #5 and #6 of the pre-flight do not fire at the Head's pass, and
  fire once at the PM's approval — measured on a really activated site.
"""
import ast
import os
import re
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import models
from .design_metrics import attempt_cause_split, classify_attempt_causes
from .design_views import (
    _design_mirror_task, _effective_commitment, post_qc_pool, tender_release_completeness,
)
from .models import (
    BOQ, BOQItem, BOQItemMaster, DesignAssignment, Program, Project, SiteGroupMembership,
    StatusTransition, Task,
    ATTEMPT_REASON_PM_REJECTED, DESIGN_ARKA_SUBMITTED, DESIGN_ARTIFACTS_UPLOADED,
    DESIGN_AWAITING_ALLOCATION, DESIGN_AWAITING_HEAD_ARKA, DESIGN_AWAITING_HEAD_QC,
    DESIGN_AWAITING_PM_APPROVAL, DESIGN_FILE_CAD_ZIP, DESIGN_IN_DESIGN, DESIGN_IN_QC,
    DESIGN_PM_REJECTED, DESIGN_RELEASED, ERR_LAYOUT, ERR_SURVEY_INADEQUATE, ERROR_GROUP_A,
    ERROR_GROUP_B, QC_PASSED, REASON_DESIGN_HEAD_PASSED, REASON_DESIGN_HEAD_RETURNED_TO_PM,
    REASON_DESIGN_PM_APPROVED, REASON_DESIGN_PM_REJECTED, SUBJECT_DESIGN_ASSIGNMENT,
    SUBJECT_TASK,
)
from .tests_design_mirror_derivation import _seed_opex
from .utils import resolve_residential_template


# ===========================================================================
# (a) THE WRITER SET — parsed, not grepped
# ===========================================================================
#
# The retired modules proved "nothing writes this status" by a regex walk. That property
# is false from 3.1b-2c on, by design. What replaces it is stronger: the writers of each
# gate status are named, and a writer added anywhere in projects/ that is not named here
# fails the session that adds it — exactly what the three retired modules did, three times.

_PROJECTS_DIR = os.path.dirname(os.path.abspath(__file__))
_STATUS_VALUES = {value for value, _ in models.DESIGN_ASSIGNMENT_STATUS_CHOICES}
#: Queryset and constructor calls that STORE a `status=` keyword. filter()/exclude()/Q()
#: name a value without storing it and are deliberately absent.
_WRITE_CALLS = {'update', 'create', 'get_or_create', 'update_or_create', 'DesignAssignment'}

GATE_STATUSES = (DESIGN_AWAITING_PM_APPROVAL, DESIGN_PM_REJECTED, DESIGN_RELEASED)

#: The three seed commands write `released` straight onto fixture rows. They are named in
#: the pre-flight (A2, A9) and admitted here BY NAME, so a fourth seed is caught too.
SEED_RELEASE_WRITERS = {
    ('management/commands/seed_opex_test_data.py', '_seed_design_state'),
    ('management/commands/seed_scm_handoff_data.py', '_release'),
    ('management/commands/seed_scm_pilot.py', '_design_state'),
    # Demo tooling, local only, guarded by require_local_database().
    ('management/commands/seed_order_demo.py', '_release_designs'),
}

INTENDED_WRITERS = {
    DESIGN_AWAITING_PM_APPROVAL: {('design_views.py', 'design_head_qc_pass'),
                                  ('design_views.py', 'design_head_return_to_pm')},
    DESIGN_PM_REJECTED:          {('design_views.py', 'design_pm_reject')},
    DESIGN_RELEASED:             {('design_views.py', 'design_pm_approve')}
                                 | SEED_RELEASE_WRITERS,
}

#: Every status write whose value is COMPUTED at run time rather than named. Each one is
#: resolved below to the statuses it can produce; a new computed writer must be added here,
#: which is the point — it gets read before it is admitted.
COMPUTED_WRITES = {
    ('design_views.py', 'design_survey_upload', 'restored'),
    ('design_views.py', 'design_survey_link_set', 'restored'),
    ('design_views.py', '_open_next_attempt', 'opening_status'),
}


def _status_value(node):
    """The design status a node names, or None. A `DESIGN_*` constant or a bare literal."""
    if isinstance(node, ast.Name) and node.id.startswith('DESIGN_'):
        # A DESIGN_* name may be a choices list or a frozenset, not a status: str only.
        value = getattr(models, node.id, None)
        return value if isinstance(value, str) and value in _STATUS_VALUES else None
    if isinstance(node, ast.Constant) and isinstance(node.value, str) \
            and node.value in _STATUS_VALUES:
        return node.value
    return None


def _product_sources():
    """Every non-test .py under projects/, management commands included; migrations are
    excluded (they declare choices and columns, and write no row's status)."""
    for root, dirs, files in os.walk(_PROJECTS_DIR):
        dirs[:] = [d for d in dirs if d not in ('migrations', '__pycache__')]
        for name in sorted(files):
            if name.endswith('.py') and not name.startswith('tests'):
                path = os.path.join(root, name)
                yield os.path.relpath(path, _PROJECTS_DIR).replace(os.sep, '/'), path


def parse_status_writes():
    """(writes, computed, functions).

    writes    [(status, file, enclosing function, shape)] for every named status write:
              the chokepoint's status argument, a stored `status=` keyword, an attribute
              assignment to `.status`, and a `'status':` dict key (an extra_fields dict)
    computed  [(file, enclosing function, expression)] for every chokepoint call whose status
              is neither a named status nor None ("no transition")
    functions {(file, name): FunctionDef}, for resolving the computed ones
    """
    writes, computed, functions = [], [], {}
    for rel, path in _product_sources():
        with open(path, encoding='utf-8-sig') as fh:
            tree = ast.parse(fh.read())

        def visit(node, fn):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn = node.name
                functions.setdefault((rel, node.name), node)
            if isinstance(node, ast.Call):
                callee = getattr(node.func, 'id', None) or getattr(node.func, 'attr', None)
                if callee == 'apply_design_status':
                    arg = node.args[1] if len(node.args) > 1 else next(
                        (k.value for k in node.keywords if k.arg == 'new_status'), None)
                    value = _status_value(arg)
                    if value:
                        writes.append((value, rel, fn, 'chokepoint'))
                    elif not (isinstance(arg, ast.Constant) and arg.value is None):
                        computed.append((rel, fn, ast.unparse(arg)))
                elif callee in _WRITE_CALLS:
                    for k in node.keywords:
                        if k.arg == 'status' and _status_value(k.value):
                            writes.append((_status_value(k.value), rel, fn, 'status='))
            if isinstance(node, ast.Assign) and _status_value(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr == 'status':
                        writes.append((_status_value(node.value), rel, fn, '.status ='))
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == 'status' and _status_value(v):
                        writes.append((_status_value(v), rel, fn, "'status':"))
            for child in ast.iter_child_nodes(node):
                visit(child, fn)

        visit(tree, '<module>')
    return writes, computed, functions


def _resolve_computed(rel, fn_name, var, functions):
    """Every status the variable `var` can hold inside `fn_name`: the statuses named in each
    expression assigned to it, and — where that expression calls a function in the same file
    — the statuses that function returns."""
    fn = functions[(rel, fn_name)]
    values = set()
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == var for t in node.targets)):
            continue
        for sub in ast.walk(node.value):
            value = _status_value(sub)
            if value:
                values.add(value)
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) \
                    and (rel, sub.func.id) in functions:
                for ret in ast.walk(functions[(rel, sub.func.id)]):
                    if isinstance(ret, ast.Return) and ret.value is not None:
                        values |= {_status_value(n) for n in ast.walk(ret.value)} - {None}
    return values


class WriterSetTests(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.writes, cls.computed, cls.functions = parse_status_writes()

    def _writers(self, status):
        return {(rel, fn) for value, rel, fn, _ in self.writes if value == status}

    def test_a1_each_gate_status_has_exactly_its_intended_writers(self):
        """SET EQUALITY, per gate status. A missing writer and an extra writer both fail."""
        for status in GATE_STATUSES:
            with self.subTest(status=status):
                self.assertEqual(self._writers(status), INTENDED_WRITERS[status],
                                 [w for w in self.writes if w[0] == status])

    def test_a2_only_the_pms_approval_releases_through_the_chokepoint(self):
        """The seeds write fixture rows with create(); in the PRODUCT, release is the PM's."""
        chokepoint = {(rel, fn) for value, rel, fn, shape in self.writes
                      if value == DESIGN_RELEASED and shape == 'chokepoint'}
        self.assertEqual(chokepoint, {('design_views.py', 'design_pm_approve')})

    def test_a3_every_computed_status_write_is_known_and_can_produce_no_gate_status(self):
        """The other status-writing functions CANNOT produce a gate status — including the
        three whose status is computed at run time, each resolved to what it can return."""
        self.assertEqual({(rel, fn, expr) for rel, fn, expr in self.computed},
                         COMPUTED_WRITES, self.computed)
        for rel, fn, var in COMPUTED_WRITES:
            with self.subTest(function=fn):
                produced = _resolve_computed(rel, fn, var, self.functions)
                self.assertTrue(produced, f'{fn}: {var} resolved to nothing — the parse is '
                                          f'blind to it, not proving anything')
                self.assertFalse(produced & set(GATE_STATUSES), (fn, produced))

    def test_a4_the_parse_is_not_vacuous(self):
        """It finds the writers of statuses that are not gate statuses, too."""
        self.assertIn(('design_views.py', 'design_qc_pass'), self._writers(DESIGN_AWAITING_HEAD_QC))
        self.assertIn(('design_views.py', '_allocate_one'), self._writers(DESIGN_IN_DESIGN))
        self.assertEqual(_resolve_computed('design_views.py', '_open_next_attempt',
                                           'opening_status', self.functions),
                         {DESIGN_ARKA_SUBMITTED, DESIGN_IN_DESIGN})


# ===========================================================================
# The product route — every step a real view
# ===========================================================================

def _profile(username, role, **flags):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    for name, value in flags.items():
        setattr(profile, name, value)
    profile.save()
    return profile


class LiveGateBase(TestCase):
    """A designer, a Design QC reviewer, a Design Head, the sites' PM and an SCM user, on one
    OPEX tender. Every site starts at awaiting_allocation with its survey in place — the last
    state before the Head's first act — and everything after that is a POST."""

    def setUp(self):
        self.designer = _profile('lg_des',  'Design')
        self.qc       = _profile('lg_qc',   'Design', is_design_qc=True)
        self.head     = _profile('lg_head', 'Design', is_design_head=True)
        self.pm       = _profile('lg_pm',   'PM')
        self.scm      = _profile('lg_scm',  'SCM')
        # Nothing mandatory, so BOQ completion turns on the stamp and not on catalogue flags.
        self.master = BOQItemMaster.objects.create(
            code='OPX-LG-1', description='Module 550Wp', unit='Nos',
            category='Solar Modules', project_type='OPEX',
            is_active=True, is_mandatory=False, sort_order=1)
        self.program = Program.objects.create(
            name='Test-PMGateLive', program_type='OPEX', client_name='LGClient',
            status='Active', short_tender_code='LG')

    # ── the site and the people ────────────────────────────────────────────

    def _new_site(self, code):
        site = Project(
            project_id=code, customer_name='LGClient', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code=code, assigned_pm=self.pm,
            dc_capacity_kw=Decimal('100.00'), status='Draft')
        site.save()
        DesignAssignment.objects.create(
            project=site, status=DESIGN_AWAITING_ALLOCATION,
            survey_file_bucket='b', survey_file_path=f'{code}/survey/x.pdf')
        return site

    def _a(self, site):
        return DesignAssignment.objects.get(project=site)

    def _as(self, profile):
        self.client.force_login(profile.user)

    def _post(self, profile, name, site, data=None):
        self._as(profile)
        return self.client.post(reverse(name, kwargs={'project_id': site.project_id}),
                                data or {})

    def _html(self, profile, url):
        self._as(profile)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _messages(self, response):
        return ' '.join(str(m) for m in get_messages(response.wsgi_request))

    def _step(self, profile, name, site, expected, data=None):
        response = self._post(profile, name, site, data)
        self.assertEqual(self._a(site).status, expected,
                         f'{name} did not reach {expected}: {self._messages(response)}')
        return response

    def _ledger(self, site):
        return list(StatusTransition.objects
                    .filter(subject_type=SUBJECT_DESIGN_ASSIGNMENT, subject_id=self._a(site).pk)
                    .order_by('occurred_at', 'pk'))

    # ── the route ──────────────────────────────────────────────────────────

    def _allocate(self, site):
        self._step(self.head, 'design_allocate', site, DESIGN_IN_DESIGN,
                   {'designer_id': self.designer.pk})

    def _upload_cad(self, site):
        upload = SimpleUploadedFile('cad.zip', b'PK\x03\x04stub', content_type='application/zip')
        with patch('projects.design_views.validate_cad_zip', return_value=[]), \
             patch('projects.design_views.upload_design_file',
                   return_value=('Horizon-PMS-Design', f'{site.project_id}/cad_zip/cad.zip')):
            return self._post(self.designer, 'design_artifact_upload', site, {
                'kind': DESIGN_FILE_CAD_ZIP, 'artifact_file': upload, 'remarks': ''})

    def _drive_package(self, site):
        """From in_design to awaiting_head_qc: the designer's whole attempt and Design QC."""
        self._step(self.designer, 'design_arka_submit', site, DESIGN_ARKA_SUBMITTED,
                   {'capacity_kw': '120.00', 'arka_link': 'https://example.com/arka',
                    'remarks': ''})
        self._step(self.qc, 'design_arka_approve', site, DESIGN_AWAITING_HEAD_ARKA)
        self._step(self.head, 'design_arka_head_approve', site, DESIGN_ARKA_SUBMITTED)
        self._upload_cad(site)
        if not BOQ.objects.filter(project=site).exists():
            boq = BOQ.objects.create(project=site, status='Draft')
            BOQItem.objects.create(
                boq=boq, serial_no=1, category='Solar Modules', description='Module 550Wp',
                item_master=self.master, uom='Nos', boq_quantity=Decimal('40'))
        self._step(self.designer, 'design_boq_complete', site, DESIGN_ARTIFACTS_UPLOADED,
                   {'boq_remarks': ''})
        self._step(self.qc, 'design_qc_start', site, DESIGN_IN_QC)
        self._step(self.qc, 'design_qc_pass', site, DESIGN_AWAITING_HEAD_QC)

    def _head_pass(self, site):
        return self._step(self.head, 'design_head_qc_pass', site, DESIGN_AWAITING_PM_APPROVAL)

    def _to_pm(self, code):
        """A new site, allocated, designed, through both review gates and the Head's pass."""
        site = self._new_site(code)
        self._allocate(site)
        self._drive_package(site)
        self._head_pass(site)
        return site

    def _pm_reject(self, site, remark):
        return self._step(self.pm, 'design_pm_reject', site, DESIGN_PM_REJECTED,
                          {'remark': remark})

    def _pm_approve(self, site):
        return self._step(self.pm, 'design_pm_approve', site, DESIGN_RELEASED)

    def _return_to_pm(self, site, remark):
        return self._step(self.head, 'design_head_return_to_pm', site,
                          DESIGN_AWAITING_PM_APPROVAL, {'remark': remark})

    def _send_back(self, site, category=ERR_LAYOUT, redo=('arka', 'cad', 'boq')):
        return self._step(self.head, 'design_head_send_back', site, DESIGN_IN_DESIGN, {
            'error_category': category,
            'pm_rejection_remarks': 'Move the inverters clear of the fire path.',
            'redo_scope_submitted': '1', 'redo': list(redo)})

    def _queue(self):
        return self._html(self.pm, reverse('design_pm_approval_queue'))


# ===========================================================================
# (b) The Head's pass is a handover, not a release
# ===========================================================================

class HeadPassTests(LiveGateBase):

    def test_b_the_pass_hands_the_package_to_the_pm_and_does_not_release_it(self):
        site = self._new_site('LG-B')
        self._allocate(site)
        self._drive_package(site)
        before = len(self._ledger(site))

        response = self._head_pass(site)

        a = self._a(site)
        self.assertEqual(a.status, DESIGN_AWAITING_PM_APPROVAL)
        self.assertNotEqual(a.status, DESIGN_RELEASED)
        self.assertIsNone(a.released_at)
        self.assertIsNone(a.released_by_id)
        self.assertIsNone(a.pm_approved_at)
        self.assertIn('with the site\'s PM for approval', self._messages(response))
        # Write #1 — the attempt's verdict and close — is where it always was.
        attempt = a.attempts.get(attempt_number=1)
        self.assertEqual(attempt.head_verdict, QC_PASSED)
        self.assertEqual(attempt.head_reviewed_by, self.head)
        self.assertIsNotNone(attempt.closed_at)
        self.assertFalse(attempt.head_overturned_qc)
        # Write #4 — one ledger row, and it says HANDOVER.
        rows = self._ledger(site)[before:]
        self.assertEqual([(r.from_status, r.to_status, r.reason_code, r.actor) for r in rows],
                         [(DESIGN_AWAITING_HEAD_QC, DESIGN_AWAITING_PM_APPROVAL,
                           REASON_DESIGN_HEAD_PASSED, self.head)])
        # And the site is in its PM's queue.
        self.assertIn('LG-B', self._queue())


# ===========================================================================
# (c) The PM's approval releases, and released_at is the approval time
# ===========================================================================

class ApproveTests(LiveGateBase):

    def test_c_released_at_is_the_approval_time_not_the_pass_time(self):
        site = self._to_pm('LG-C')
        passed_at = self._a(site).attempts.get(attempt_number=1).head_reviewed_at
        later = timezone.now() + timedelta(days=2)
        self._as(self.pm)
        with patch.object(timezone, 'now', return_value=later):
            self.client.post(reverse('design_pm_approve', args=['LG-C']), {})

        a = self._a(site)
        self.assertEqual(a.status, DESIGN_RELEASED)
        self.assertEqual((a.pm_approved_at, a.pm_approved_by), (later, self.pm))
        self.assertEqual((a.released_at, a.released_by), (later, self.pm))
        self.assertNotEqual(a.released_at, passed_at)
        last = self._ledger(site)[-1]
        self.assertEqual((last.from_status, last.to_status, last.reason_code),
                         (DESIGN_AWAITING_PM_APPROVAL, DESIGN_RELEASED,
                          REASON_DESIGN_PM_APPROVED))


# ===========================================================================
# (d) The PM's rejection, and the PM's words where the Head reads them
# ===========================================================================

class RejectTests(LiveGateBase):

    REMARK = 'The inverter make is not the one tendered.'

    def test_d_rejected_to_pm_rejected_with_the_remark_on_both_head_screens(self):
        site = self._to_pm('LG-D')
        self._pm_reject(site, self.REMARK)

        a = self._a(site)
        self.assertEqual(a.status, DESIGN_PM_REJECTED)
        self.assertEqual(a.attempts.count(), 1, 'a PM rejection opened an attempt by itself')
        self.assertIsNone(a.released_at)
        last = self._ledger(site)[-1]
        self.assertEqual((last.from_status, last.to_status, last.reason_code, last.remark),
                         (DESIGN_AWAITING_PM_APPROVAL, DESIGN_PM_REJECTED,
                          REASON_DESIGN_PM_REJECTED, self.REMARK))

        sites_html = self._html(self.head, reverse('design_head_sites', args=[self.program.pk]))
        self.assertIn(self.REMARK, sites_html)
        review_html = self._html(self.head, reverse('design_qc_review', args=['LG-D']))
        self.assertIn(self.REMARK, review_html)
        self.assertIn('The PM\'s reason', review_html)
        self.assertIn(reverse('design_head_send_back', args=['LG-D']), review_html)


# ===========================================================================
# (e) The Head's return, his words on the PM's queue — and the two-cycle test
# ===========================================================================

class ReturnToPmTests(LiveGateBase):

    RETURN = 'The make is an approved equivalent; see the tender annexure.'

    def test_e1_returned_to_the_pm_with_his_remark_on_the_queue(self):
        site = self._to_pm('LG-E1')
        self._pm_reject(site, 'Wrong inverter make.')
        self._return_to_pm(site, self.RETURN)

        last = self._ledger(site)[-1]
        self.assertEqual((last.from_status, last.to_status, last.reason_code),
                         (DESIGN_PM_REJECTED, DESIGN_AWAITING_PM_APPROVAL,
                          REASON_DESIGN_HEAD_RETURNED_TO_PM))
        html = self._queue()
        self.assertIn('LG-E1', html)
        self.assertIn(self.RETURN, html)
        self.assertIn('Returned to you by the Design Head', html)

    def test_e2_a_first_handover_carries_no_return_remark(self):
        self._to_pm('LG-E2')
        html = self._queue()
        self.assertIn('LG-E2', html)
        self.assertNotIn('Returned to you by the Design Head', html)

    def test_e3_two_cycles_the_stale_return_remark_is_gone(self):
        """B4's PIN. Cycle 1: pass, reject, the Head returns it (his remark shows). The PM
        rejects again, the Head agrees and sends it back, the designer reworks it, and it
        comes to the PM through a SECOND Head pass. The old return row is still on the
        ledger — a reason-keyed read would show it — and it must not show."""
        site = self._to_pm('LG-E3')
        self._pm_reject(site, 'Wrong inverter make.')
        self._return_to_pm(site, self.RETURN)
        self.assertIn(self.RETURN, self._queue())

        self._pm_reject(site, 'Still the wrong make — I will not accept it.')
        self._send_back(site)
        self._drive_package(site)
        self._head_pass(site)

        self.assertEqual(self._a(site).current_attempt_number, 2)
        self.assertTrue(StatusTransition.objects.filter(
            subject_type=SUBJECT_DESIGN_ASSIGNMENT, subject_id=self._a(site).pk,
            reason_code=REASON_DESIGN_HEAD_RETURNED_TO_PM).exists(),
            'the premise: the stale return row is still on the ledger')
        html = self._queue()
        self.assertIn('LG-E3', html)
        self.assertNotIn(self.RETURN, html)
        self.assertNotIn('Returned to you by the Design Head', html)


# ===========================================================================
# (f) The Head's send-back: attempt N+1, the date moved, the designer charged for A only
# ===========================================================================

class SendBackTests(LiveGateBase):

    def _rejected(self, code):
        site = self._to_pm(code)
        self._pm_reject(site, 'The layout blocks the fire path.')
        return site

    def test_f1_group_a_opens_attempt_2_moves_the_date_and_charges_the_designer(self):
        site = self._rejected('LG-F1')
        agreed = _effective_commitment(self._a(site)).proposed_date
        self._as(self.head)
        with patch.object(timezone, 'now', return_value=timezone.now() + timedelta(days=3)):
            self.client.post(reverse('design_head_send_back', args=['LG-F1']), {
                'error_category': ERR_LAYOUT,
                'pm_rejection_remarks': 'Move the inverters clear of the fire path.',
                'redo_scope_submitted': '1', 'redo': ['arka', 'cad', 'boq']})

        a = self._a(site)
        self.assertEqual((a.status, a.current_attempt_number), (DESIGN_IN_DESIGN, 2))
        attempts = list(a.attempts.order_by('attempt_number'))
        first, second = attempts
        self.assertEqual(second.opened_reason, ATTEMPT_REASON_PM_REJECTED)
        self.assertEqual(first.pm_rejection_category, ERR_LAYOUT)
        self.assertEqual(first.head_verdict, QC_PASSED, 'the Head\'s pass was rewritten')
        # §D15 decision (c): out by the whole days since the Head's pass.
        self.assertEqual(_effective_commitment(a).proposed_date, agreed + timedelta(days=3))
        self.assertEqual(classify_attempt_causes(attempts)[2], ERROR_GROUP_A)
        self.assertEqual(attempt_cause_split(attempts)['designer'], 1)

    def test_f2_group_b_is_an_input_problem_and_does_not_charge_the_designer(self):
        site = self._rejected('LG-F2')
        self._send_back(site, category=ERR_SURVEY_INADEQUATE)
        attempts = list(self._a(site).attempts.order_by('attempt_number'))
        self.assertEqual(attempts[1].opened_reason, ATTEMPT_REASON_PM_REJECTED)
        self.assertEqual(classify_attempt_causes(attempts)[2], ERROR_GROUP_B)
        split = attempt_cause_split(attempts)
        self.assertEqual((split['designer'], split['input']), (0, 1))


# ===========================================================================
# (g) The boundary — a site with the PM is not released, by a REAL transition
# ===========================================================================

class BoundaryTests(LiveGateBase):

    def test_g_with_the_pm_or_rejected_by_the_pm_is_not_released_to_scm(self):
        with_pm = self._to_pm('LG-G1')
        rejected = self._to_pm('LG-G2')
        self._pm_reject(rejected, 'Wrong tilt.')

        pool = {a.project_id for a in post_qc_pool(self.program)}
        self.assertNotIn(with_pm.pk, pool)
        self.assertNotIn(rejected.pk, pool)
        self.assertEqual(tender_release_completeness(self.program), (0, 2))

        self._as(self.scm)
        response = self.client.post(reverse('site_group_create', args=[self.program.pk]),
                                    {'name': 'Batch 1',
                                     'project_ids': [with_pm.pk, rejected.pk]})
        self.assertFalse(SiteGroupMembership.objects.filter(
            project__in=[with_pm, rejected]).exists())
        self.assertEqual(self._messages(response).count('not released'), 2)

        # Control: the PM's approval is what puts it in front of SCM.
        self._pm_approve(with_pm)
        self.assertIn(with_pm.pk, {a.project_id for a in post_qc_pool(self.program)})
        self.assertEqual(tender_release_completeness(self.program), (1, 2))


# ===========================================================================
# (h) THE FULL CHAIN, once
# ===========================================================================

class FullChainTests(LiveGateBase):

    def test_h_allocate_to_scm_group_through_a_pm_rejection(self):
        """allocate -> Arka -> artifacts -> QC -> head pass -> PM reject -> Head send back ->
        attempt 2 -> QC -> head pass -> PM approve -> SCM group. The gate did not break the
        pipeline."""
        site = self._new_site('LG-H')
        self._allocate(site)
        self._drive_package(site)
        self._head_pass(site)
        self._pm_reject(site, 'The string sizing is wrong for the east array.')
        self._send_back(site)
        self._drive_package(site)
        self._head_pass(site)
        self._pm_approve(site)

        a = self._a(site)
        self.assertEqual(a.current_attempt_number, 2)
        self.assertEqual((a.released_by, a.pm_approved_by), (self.pm, self.pm))

        self._as(self.scm)
        self.client.post(reverse('site_group_create', args=[self.program.pk]),
                         {'name': 'Batch H', 'project_ids': [site.pk]})
        self.assertTrue(SiteGroupMembership.objects.filter(
            project=site, removed_at__isnull=True).exists())

        gate = [(r.from_status, r.to_status, r.reason_code) for r in self._ledger(site)
                if r.to_status in GATE_STATUSES or r.from_status in GATE_STATUSES]
        self.assertEqual(gate, [
            (DESIGN_AWAITING_HEAD_QC, DESIGN_AWAITING_PM_APPROVAL, REASON_DESIGN_HEAD_PASSED),
            (DESIGN_AWAITING_PM_APPROVAL, DESIGN_PM_REJECTED, REASON_DESIGN_PM_REJECTED),
            (DESIGN_PM_REJECTED, DESIGN_IN_DESIGN, ''),
            (DESIGN_AWAITING_HEAD_QC, DESIGN_AWAITING_PM_APPROVAL, REASON_DESIGN_HEAD_PASSED),
            (DESIGN_AWAITING_PM_APPROVAL, DESIGN_RELEASED, REASON_DESIGN_PM_APPROVED),
        ])


# ===========================================================================
# The mirror — writes #5 and #6 self-cancel at the pass, and fire once at approval
# ===========================================================================

_TASK_UPDATE = re.compile(r'UPDATE\s+"projects_task"\s', re.IGNORECASE)


class MirrorTests(LiveGateBase):
    """On a REALLY activated site, so the Design mirror exists and the hook has a row to
    write. Measured: the queries the request issues, and the mirror's own ledger."""

    @classmethod
    def setUpTestData(cls):
        resolve_residential_template()
        _seed_opex()

    def _mirror_rows(self, mirror):
        return StatusTransition.objects.filter(subject_type=SUBJECT_TASK,
                                               subject_id=mirror.pk).count()

    def test_the_mirror_does_not_move_at_the_pass_and_moves_once_at_approval(self):
        site = self._new_site('LG-M')
        self._as(self.pm)
        self.assertEqual(self.client.post(
            reverse('opex_site_activate', args=['LG-M'])).status_code, 302)
        self._allocate(site)
        self._drive_package(site)
        mirror = _design_mirror_task(site)
        self.assertIsNotNone(mirror, 'activation produced no Design mirror')
        self.assertEqual(mirror.status, Task.IN_PROGRESS)
        rows = self._mirror_rows(mirror)

        self._as(self.head)
        with CaptureQueriesContext(connection) as ctx:
            self.client.post(reverse('design_head_qc_pass', args=['LG-M']), {})
        self.assertEqual(self._a(site).status, DESIGN_AWAITING_PM_APPROVAL)
        self.assertFalse([q['sql'] for q in ctx.captured_queries
                          if _TASK_UPDATE.search(q['sql'])], 'write #5 fired at the pass')
        self.assertEqual(self._mirror_rows(mirror), rows, 'write #6 fired at the pass')
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, Task.IN_PROGRESS)

        self._pm_approve(site)
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, Task.DONE)
        self.assertEqual(self._mirror_rows(mirror), rows + 1)
