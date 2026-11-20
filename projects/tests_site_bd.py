"""FIN-M-B5-1 — a site's BD (Project.bd): the field, who sets it, the overview line, the
backfill command and the "No BD set" query.

Rulings (FR7, FR12; FIN-M-B5-1 answers, 18 Nov 2026):
* Project.bd is a UserProfile FK, PROTECT, blank by default.
* Set / change / clear: CEO, Admin, System Admin with an active profile and login
  (user_can_set_site_bd), Residential sites only, from the overview's BD modal. Choices:
  active BD users plus the current holder. Every change logs "BD: old → new" under
  `bd_assigned`, which counts as latest activity like `pm_assigned`.
* backfill_site_bd: dry run by default, --apply needs --actor, sets the OCR task's
  assignee only when an active BD, never overwrites, logs `bd_backfilled` (in
  NON_ACTIVITY_CODES).
* Scope for the backfill and "No BD set": non-deleted, non-test Residential sites in
  every status except Cancelled, Draft included.

Run with:
    python manage.py test projects.tests_site_bd --settings=solarpms.test_settings
"""
import re
from decimal import Decimal
from io import StringIO

from django.contrib.auth.models import AnonymousUser, User
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .activity_codes import MONEY_ACTIVITY_CODES, NON_ACTIVITY_CODES
from .forms import SiteBdForm
from .models import (
    ActivityLog, Project, ProjectPhase, Task, TaskTemplate, TaskTemplatePhase,
    TaskTemplateTask,
)
from .permissions import user_can_set_site_bd, user_is_site_bd
from .site_bd import (
    BD_ASSIGNED, BD_BACKFILLED, OCR_TASK_CODE, SiteBdChangeRefused, bd_scope_projects,
    set_site_bd, site_in_bd_scope, sites_without_bd,
)
from .site_dashboard import is_human_activity


def _profile(username, role, *, active=True, login_active=True):
    """A post_save signal creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title(), last_name='Test')
    if not login_active:
        user.is_active = False
        user.save(update_fields=['is_active'])
    profile = user.profile
    profile.role = role
    profile.is_active = active
    profile.save()
    return profile


def _client(profile):
    client = Client(SERVER_NAME='localhost')
    client.force_login(profile.user)
    return client


def _text(html):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html)).strip()


class SiteBdBase(TestCase):
    """People in every role that matters, and builders for Residential sites."""

    def setUp(self):
        self.ceo = _profile('sb_ceo', 'CEO')
        self.admin = _profile('sb_admin', 'Admin')
        self.sysadmin = _profile('sb_sysadmin', 'System Admin')
        self.pm = _profile('sb_pm', 'PM')
        self.pc = _profile('sb_pc', 'Project Coordinator')
        self.finance = _profile('sb_fin', 'Finance')
        self.scm = _profile('sb_scm', 'SCM')
        self.bd1 = _profile('sb_bd1', 'BD')
        self.bd2 = _profile('sb_bd2', 'BD')
        self.bd_off = _profile('sb_bdoff', 'BD', active=False)          # profile deactivated
        self.bd_nologin = _profile('sb_bdnl', 'BD', login_active=False)  # login switched off
        template = TaskTemplate.objects.create(code='RESIDENTIAL', label='Residential',
                                               project_type='Residential', version_no=1)
        tphase = TaskTemplatePhase.objects.create(template=template, code='DOCS',
                                                  label='Documentation', sort_order=1)
        self.ocr_tt = TaskTemplateTask.objects.create(
            phase=tphase, code=OCR_TASK_CODE, label='OCR, Documentation & Verification',
            sort_order=1)

    def site(self, *, status='Active', project_type='Residential', ocr=(), bd=None,
             is_deleted=False, is_test=False):
        """A site with one OCR task per entry in `ocr` (each the assignee, or None)."""
        project = Project.objects.create(
            customer_name='BD Site', status=status, project_type=project_type,
            customer_phone='9876543210', site_address='1 Sun Road', city='Lucknow',
            state='Uttar Pradesh', dc_capacity_kw=Decimal('5.00'), assigned_pm=self.pm,
            cascade_scheduling=False, bd=bd, is_deleted=is_deleted, is_test=is_test)
        project.coordinators.add(self.pc)
        if ocr:
            phase = ProjectPhase.objects.create(project=project, phase_name='Documentation',
                                                phase_order=1)
            for n, assignee in enumerate(ocr, start=1):
                Task.objects.create(phase=phase, task_name='OCR, Documentation & Verification',
                                    task_order=n, assigned_role=Task.BD, assigned_to=assignee,
                                    template_task=self.ocr_tt)
        return project

    def bd_logs(self, project, code=BD_ASSIGNED):
        return list(ActivityLog.objects.filter(project=project, action_code=code)
                    .order_by('pk').values_list('action', 'actor_id'))


# ---------------------------------------------------------------------------
# The field
# ---------------------------------------------------------------------------

class FieldTests(SiteBdBase):

    def test_blank_by_default(self):
        self.assertIsNone(self.site().bd_id)

    def test_protect_keeps_a_bd_profile_on_a_site(self):
        from django.db.models import ProtectedError
        self.site(bd=self.bd1)
        with self.assertRaises(ProtectedError):
            self.bd1.delete()


# ---------------------------------------------------------------------------
# The permission helpers
# ---------------------------------------------------------------------------

class HelperTests(SiteBdBase):

    def test_user_is_site_bd_true_for_the_sites_active_bd(self):
        self.assertTrue(user_is_site_bd(self.bd1.user, self.site(bd=self.bd1)))

    def test_user_is_site_bd_false_for_everyone_else(self):
        project = self.site(bd=self.bd1)
        for user in (self.bd2.user, self.pm.user, self.ceo.user, AnonymousUser()):
            with self.subTest(user=str(user)):
                self.assertFalse(user_is_site_bd(user, project))
        self.assertFalse(user_is_site_bd(self.bd1.user, self.site()))  # empty seat
        self.assertFalse(user_is_site_bd(self.bd1.user, None))

    def test_user_is_site_bd_false_for_an_inactive_bd_set_on_the_site(self):
        for profile in (self.bd_off, self.bd_nologin):
            with self.subTest(profile=profile.user.username):
                project = self.site(bd=profile)
                self.assertFalse(user_is_site_bd(profile.user, project))

    def test_user_is_site_bd_false_once_the_role_changes(self):
        project = self.site(bd=self.bd1)
        self.bd1.role = 'Finance'
        self.bd1.save()
        self.assertFalse(user_is_site_bd(self.bd1.user, project))

    def test_user_can_set_site_bd(self):
        for profile in (self.ceo, self.admin, self.sysadmin):
            with self.subTest(role=profile.role):
                self.assertTrue(user_can_set_site_bd(profile.user))
        for profile in (self.pm, self.pc, self.finance, self.scm, self.bd1):
            with self.subTest(role=profile.role):
                self.assertFalse(user_can_set_site_bd(profile.user))
        self.assertFalse(user_can_set_site_bd(AnonymousUser()))
        off_admin = _profile('sb_admin_off', 'Admin', active=False)
        self.assertFalse(user_can_set_site_bd(off_admin.user))

    def test_helpers_cost_no_query(self):
        project = self.site(bd=self.bd1)
        user = User.objects.select_related('profile').get(pk=self.bd1.user.pk)
        with self.assertNumQueries(0):
            user_is_site_bd(user, project)
            user_can_set_site_bd(user)


# ---------------------------------------------------------------------------
# The form's choices
# ---------------------------------------------------------------------------

class FormChoiceTests(SiteBdBase):

    def choices(self, project):
        return set(SiteBdForm(project=project).fields['bd'].queryset)

    def test_active_bd_users_only(self):
        self.assertEqual(self.choices(self.site()), {self.bd1, self.bd2})

    def test_the_current_inactive_bd_stays_a_choice(self):
        for profile in (self.bd_off, self.bd_nologin):
            with self.subTest(profile=profile.user.username):
                form = SiteBdForm(project=self.site(bd=profile))
                self.assertEqual(set(form.fields['bd'].queryset), {self.bd1, self.bd2, profile})
                self.assertEqual(form.initial['bd'], profile.pk)
                label = form.fields['bd'].label_from_instance(profile)
                self.assertTrue(label.endswith('(inactive)'))

    def test_never_a_pm_or_another_role(self):
        choices = self.choices(self.site(bd=self.bd1))
        for profile in (self.pm, self.pc, self.ceo, self.admin, self.finance, self.scm):
            self.assertNotIn(profile, choices)


# ---------------------------------------------------------------------------
# The view: set, change, clear, refusals
# ---------------------------------------------------------------------------

class SetBdViewTests(SiteBdBase):

    def post(self, profile, project, bd, expected=''):
        return _client(profile).post(reverse('project_set_bd', args=[project.project_id]), {
            'bd': '' if bd is None else bd.pk,
            'expected_bd': expected,
        })

    def test_ceo_admin_and_system_admin_set_change_and_clear(self):
        for actor in (self.ceo, self.admin, self.sysadmin):
            with self.subTest(role=actor.role):
                project = self.site()
                response = self.post(actor, project, self.bd1)
                self.assertRedirects(response, reverse('project_overview',
                                                       args=[project.project_id]),
                                     fetch_redirect_response=False)
                project.refresh_from_db()
                self.assertEqual(project.bd, self.bd1)

                self.post(actor, project, self.bd2, expected=self.bd1.pk)
                project.refresh_from_db()
                self.assertEqual(project.bd, self.bd2)

                self.post(actor, project, None, expected=self.bd2.pk)
                project.refresh_from_db()
                self.assertIsNone(project.bd)

                self.assertEqual(self.bd_logs(project), [
                    ('BD: blank → Sb_Bd1 Test', actor.pk),
                    ('BD: Sb_Bd1 Test → Sb_Bd2 Test', actor.pk),
                    ('BD: Sb_Bd2 Test → blank', actor.pk),
                ])

    def test_saving_the_same_bd_writes_no_line(self):
        project = self.site(bd=self.bd1)
        self.post(self.ceo, project, self.bd1, expected=self.bd1.pk)
        self.assertEqual(self.bd_logs(project), [])

    def test_saving_with_the_current_inactive_bd_does_not_blank_it(self):
        project = self.site(bd=self.bd_off)
        self.post(self.ceo, project, self.bd_off, expected=self.bd_off.pk)
        project.refresh_from_db()
        self.assertEqual(project.bd, self.bd_off)
        self.assertEqual(self.bd_logs(project), [])

    def test_a_stale_form_is_refused(self):
        project = self.site(bd=self.bd2)       # someone set bd2 after the form showed blank
        self.post(self.ceo, project, self.bd1, expected='')
        project.refresh_from_db()
        self.assertEqual(project.bd, self.bd2)
        self.assertEqual(self.bd_logs(project), [])

    def test_non_permitted_roles_cannot_set_bd_even_by_hand_made_post(self):
        for actor in (self.pm, self.bd1, self.pc, self.finance, self.scm):
            with self.subTest(role=actor.role):
                project = self.site()
                client = _client(actor)
                url = reverse('project_set_bd', args=[project.project_id])
                self.assertEqual(client.get(url).status_code, 403)
                response = client.post(url, {'bd': self.bd1.pk, 'expected_bd': ''})
                self.assertEqual(response.status_code, 403)
                project.refresh_from_db()
                self.assertIsNone(project.bd)
                self.assertEqual(self.bd_logs(project), [])

    def test_a_deactivated_admin_is_refused(self):
        off_admin = _profile('sb_admin_off2', 'Admin', active=False)
        project = self.site()
        self.post(off_admin, project, self.bd1)
        project.refresh_from_db()
        self.assertIsNone(project.bd)

    def test_tender_sites_are_refused_whatever_the_role(self):
        project = self.site(project_type='OPEX')
        url = reverse('project_set_bd', args=[project.project_id])
        self.assertEqual(_client(self.ceo).get(url).status_code, 404)
        self.assertEqual(self.post(self.ceo, project, self.bd1).status_code, 404)
        project.refresh_from_db()
        self.assertIsNone(project.bd)

    def test_a_pm_or_an_inactive_bd_cannot_be_chosen(self):
        project = self.site()
        for person in (self.pm, self.bd_off, self.ceo):
            with self.subTest(person=person.user.username):
                self.post(self.ceo, project, person)
                project.refresh_from_db()
                self.assertIsNone(project.bd)
        self.assertEqual(self.bd_logs(project), [])

    def test_get_returns_the_modal_body(self):
        project = self.site(bd=self.bd1)
        response = _client(self.admin).get(reverse('project_set_bd', args=[project.project_id]))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('name="bd"', html)
        self.assertIn(f'name="expected_bd" value="{self.bd1.pk}"', html)
        self.assertNotIn('sb_pm', html.lower())

    def test_cancelled_and_test_sites_are_refused(self):
        """Follow-up ruling 1: the view and the writer take only sites in
        bd_scope_projects(), so Cancelled and test sites are refused like tender ones."""
        for label, project in (('cancelled', self.site(status='Cancelled')),
                               ('test', self.site(is_test=True))):
            with self.subTest(site=label):
                url = reverse('project_set_bd', args=[project.project_id])
                self.assertEqual(_client(self.ceo).get(url).status_code, 404)
                self.assertEqual(self.post(self.admin, project, self.bd1).status_code, 404)
                with self.assertRaises(SiteBdChangeRefused):
                    set_site_bd(project, self.bd1, self.ceo, expected_old_id=None)
                project.refresh_from_db()
                self.assertIsNone(project.bd)
                self.assertEqual(self.bd_logs(project), [])

    def test_the_writer_refuses_a_site_that_left_scope_after_the_form_opened(self):
        project = self.site()
        Project.objects.filter(pk=project.pk).update(status='Cancelled')
        with self.assertRaises(SiteBdChangeRefused):
            set_site_bd(project, self.bd1, self.ceo, expected_old_id=None)
        self.assertIsNone(Project.objects.get(pk=project.pk).bd_id)

    def test_the_writer_refuses_a_non_residential_site_and_an_inactive_bd(self):
        with self.assertRaises(SiteBdChangeRefused):
            set_site_bd(self.site(project_type='OPEX'), self.bd1, self.ceo,
                        expected_old_id=None)
        with self.assertRaises(SiteBdChangeRefused):
            set_site_bd(self.site(), self.bd_nologin, self.ceo, expected_old_id=None)


# ---------------------------------------------------------------------------
# The overview line
# ---------------------------------------------------------------------------

def _bd_pill(html):
    start = html.find('id="site-bd-pill"')
    if start == -1:
        return ''
    return html[start:html.find('</span>', start)]


class OverviewTests(SiteBdBase):

    def page(self, profile, project):
        response = _client(profile).get(reverse('project_overview', args=[project.project_id]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_line_shows_not_set_name_and_inactive(self):
        self.assertIn('Not set', _text(_bd_pill(self.page(self.pm, self.site()))))
        self.assertIn('Sb_Bd1 Test', _text(_bd_pill(self.page(self.pm, self.site(bd=self.bd1)))))
        pill = _text(_bd_pill(self.page(self.pm, self.site(bd=self.bd_off))))
        self.assertIn('Sb_Bdoff Test (inactive)', pill)

    def test_pencil_only_for_the_setters(self):
        project = self.site()
        url = reverse('project_set_bd', args=[project.project_id])
        for profile in (self.ceo, self.admin, self.sysadmin):
            with self.subTest(role=profile.role):
                html = self.page(profile, project)
                self.assertIn(url, _bd_pill(html))
                self.assertIn('id="bdEditModal"', html)
        for profile in (self.pm, self.pc, self.finance, self.bd1):
            with self.subTest(role=profile.role):
                html = self.page(profile, project)
                self.assertTrue(_bd_pill(html))          # the line itself is shown
                self.assertNotIn(url, html)
                self.assertNotIn('id="bdEditModal"', html)

    def test_pencil_hidden_on_cancelled_and_test_sites(self):
        for label, project in (('cancelled', self.site(status='Cancelled', bd=self.bd1)),
                               ('test', self.site(is_test=True, bd=self.bd1))):
            with self.subTest(site=label):
                html = self.page(self.ceo, project)
                self.assertIn('Sb_Bd1 Test', _text(_bd_pill(html)))   # the line stays
                self.assertNotIn(reverse('project_set_bd', args=[project.project_id]), html)
                self.assertNotIn('id="bdEditModal"', html)

    def test_no_bd_line_on_a_tender_site(self):
        html = self.page(self.ceo, self.site(project_type='OPEX'))
        self.assertEqual(_bd_pill(html), '')
        self.assertNotIn('id="bdEditModal"', html)


class OverviewQueryCountTests(TestCase):
    """The BD line costs no query: `bd__user` rides on the overview's existing
    select_related. Numbers measured on this fixture BEFORE FIN-M-B5-1 (19 Nov 2026), with
    the same shape as the measurement: one phase, one PM task, a warm-up request first."""

    BEFORE = {
        ('Draft', 'Residential', 'CEO'): 23, ('Draft', 'Residential', 'PM'): 28,
        ('Draft', 'Residential', 'BD'): 24,
        ('Active', 'Residential', 'CEO'): 31, ('Active', 'Residential', 'Admin'): 31,
        ('Active', 'Residential', 'System Admin'): 31, ('Active', 'Residential', 'PM'): 36,
        ('Active', 'Residential', 'BD'): 32, ('Active', 'Residential', 'Finance'): 32,
        ('Active', 'OPEX', 'CEO'): 46, ('Active', 'OPEX', 'PM'): 54,
    }

    def test_overview_query_count_unchanged(self):
        roles = {r: _profile(f'qc_{i}', r) for i, r in enumerate(
            ('CEO', 'Admin', 'System Admin', 'PM', 'BD', 'Finance'))}
        for (status, ptype, role), expected in self.BEFORE.items():
            for bd in (None, roles['BD']):
                if bd is not None and ptype != 'Residential':
                    continue
                with self.subTest(status=status, type=ptype, role=role, bd=bool(bd)):
                    project = Project.objects.create(
                        customer_name='X', status=status, project_type=ptype,
                        customer_phone='9876543210', site_address='a', city='Lucknow',
                        state='UP', dc_capacity_kw=Decimal('5'), assigned_pm=roles['PM'],
                        cascade_scheduling=False, bd=bd)
                    phase = ProjectPhase.objects.create(project=project, phase_name='I',
                                                        phase_order=1)
                    Task.objects.create(phase=phase, task_name='T', task_order=1,
                                        assigned_role=Task.PM, assigned_to=roles['PM'])
                    client = _client(roles[role])
                    url = reverse('project_overview', args=[project.project_id])
                    client.get(url)  # warm-up, as in the measurement
                    with CaptureQueriesContext(connection) as ctx:
                        self.assertEqual(client.get(url).status_code, 200)
                    self.assertEqual(len(ctx.captured_queries), expected)


# ---------------------------------------------------------------------------
# The backfill command
# ---------------------------------------------------------------------------

class BackfillTests(SiteBdBase):

    def setUp(self):
        super().setUp()
        self.s_set = self.site(ocr=[self.bd1])
        self.s_pm = self.site(ocr=[self.pm])
        self.s_inactive = self.site(ocr=[self.bd_off])
        self.s_nologin = self.site(ocr=[self.bd_nologin])
        self.s_unassigned = self.site(ocr=[None])
        self.s_draft = self.site(status='Draft')
        self.s_several = self.site(ocr=[self.bd1, self.bd2])
        self.s_manual = self.site(ocr=[self.bd1], bd=self.bd2)
        self.s_deleted = self.site(ocr=[self.bd1], is_deleted=True)
        self.s_test = self.site(ocr=[self.bd1], is_test=True)
        self.s_cancelled = self.site(ocr=[self.bd1], status='Cancelled')
        self.s_opex = self.site(ocr=[self.bd1], project_type='OPEX')

    def run_cmd(self, *args):
        out = StringIO()
        call_command('backfill_site_bd', *args, stdout=out)
        return out.getvalue()

    def bds(self):
        return dict(Project.objects.values_list('pk', 'bd_id'))

    def line(self, out, project):
        return next(l for l in out.splitlines() if l.startswith(f'{project.project_id} |'))

    def test_dry_run_writes_nothing_and_prints_the_plan(self):
        before = self.bds()
        out = self.run_cmd()
        self.assertEqual(self.bds(), before)
        self.assertFalse(ActivityLog.objects.filter(action_code=BD_BACKFILLED).exists())
        self.assertIn('MODE: DRY RUN', out)
        self.assertIn('would set to Sb_Bd1 Test (sb_bd1)', self.line(out, self.s_set))
        self.assertIn('WOULD SET 1, ALREADY SET 1, SKIPPED 6', out)
        self.assertIn('CHECKED 8', out)
        self.assertIn('ActivityLog rows written: 0 (expected 0)', out)

    def test_apply_needs_an_actor(self):
        with self.assertRaises(CommandError):
            self.run_cmd('--apply')
        self.assertIsNone(Project.objects.get(pk=self.s_set.pk).bd_id)

    def test_actor_must_be_allowed_to_set_a_bd(self):
        """Follow-up ruling 2: --actor passes user_can_set_site_bd or nothing is written."""
        _profile('sb_admin_gone', 'Admin', active=False)
        before = self.bds()
        for username in ('sb_pm', 'sb_bd1', 'sb_pc', 'sb_fin', 'sb_scm', 'sb_admin_gone'):
            with self.subTest(actor=username):
                with self.assertRaises(CommandError):
                    self.run_cmd('--apply', '--actor', username)
        self.assertEqual(self.bds(), before)
        self.assertFalse(ActivityLog.objects.filter(action_code=BD_BACKFILLED).exists())
        for username in ('sb_ceo', 'sb_sysadmin'):
            with self.subTest(actor=username):
                self.run_cmd('--apply', '--actor', username)   # admitted, no error
        self.assertEqual(Project.objects.get(pk=self.s_set.pk).bd_id, self.bd1.pk)

    def test_apply_sets_only_active_bd_assignees(self):
        out = self.run_cmd('--apply', '--actor', 'sb_admin')
        bds = self.bds()
        self.assertEqual(bds[self.s_set.pk], self.bd1.pk)
        for site in (self.s_pm, self.s_inactive, self.s_nologin, self.s_unassigned,
                     self.s_draft, self.s_several, self.s_deleted, self.s_test,
                     self.s_cancelled, self.s_opex):
            self.assertIsNone(bds[site.pk], site.project_id)
        self.assertEqual(bds[self.s_manual.pk], self.bd2.pk)   # never overwritten

        self.assertIn('set to Sb_Bd1 Test', self.line(out, self.s_set))
        self.assertIn('skipped: OCR task held by a PM (Sb_Pm Test)', self.line(out, self.s_pm))
        self.assertIn('skipped: OCR task held by an inactive BD',
                      self.line(out, self.s_inactive))
        self.assertIn('skipped: OCR task held by an inactive BD',
                      self.line(out, self.s_nologin))
        self.assertIn('skipped: OCR task unassigned', self.line(out, self.s_unassigned))
        self.assertIn('skipped: no OCR task', self.line(out, self.s_draft))
        self.assertIn('skipped: several OCR tasks', self.line(out, self.s_several))
        self.assertIn('already set: Sb_Bd2 Test', self.line(out, self.s_manual))
        for site in (self.s_deleted, self.s_test, self.s_cancelled, self.s_opex):
            self.assertNotIn(f'{site.project_id} |', out)        # out of scope, not listed

        self.assertIn('SET 1, ALREADY SET 1, SKIPPED 6', out)
        self.assertIn('ActivityLog rows written: 1 (expected 1)', out)
        self.assertEqual(self.bd_logs(self.s_set, BD_BACKFILLED), [
            ("BD: blank → Sb_Bd1 Test (from the OCR task's assignee)", self.admin.pk)])

        # The "No BD set" list: the six skipped in-scope sites, nothing else.
        listing = out.split('No BD set (after this run): ', 1)[1]
        self.assertTrue(listing.startswith('6.'))
        for site in (self.s_pm, self.s_inactive, self.s_nologin, self.s_unassigned,
                     self.s_draft, self.s_several):
            self.assertIn(f'  {site.project_id} |', listing)
        self.assertNotIn(self.s_set.project_id, listing)

    def test_second_apply_changes_nothing(self):
        self.run_cmd('--apply', '--actor', 'sb_admin')
        before = self.bds()
        out = self.run_cmd('--apply', '--actor', 'sb_admin')
        self.assertEqual(self.bds(), before)
        self.assertIn('SET 0, ALREADY SET 2, SKIPPED 6', out)
        self.assertIn('ActivityLog rows written: 0 (expected 0)', out)
        self.assertEqual(ActivityLog.objects.filter(action_code=BD_BACKFILLED).count(), 1)

    def test_backfill_rows_are_not_latest_activity_but_form_rows_are(self):
        self.assertIn(BD_BACKFILLED, NON_ACTIVITY_CODES)
        self.assertNotIn(BD_ASSIGNED, NON_ACTIVITY_CODES)
        self.assertFalse(is_human_activity(BD_BACKFILLED))
        self.assertTrue(is_human_activity(BD_ASSIGNED))
        self.assertTrue(is_human_activity('pm_assigned'))   # bd_assigned is treated alike
        self.assertFalse({BD_ASSIGNED, BD_BACKFILLED} & MONEY_ACTIVITY_CODES)


# ---------------------------------------------------------------------------
# The "No BD set" query
# ---------------------------------------------------------------------------

class SitesWithoutBdTests(SiteBdBase):

    def test_scope_has_one_definition_in_both_forms(self):
        """site_in_bd_scope() (the overview's no-query form) agrees with
        bd_scope_projects() on every combination of the scope's terms."""
        sites = []
        for status in ('Draft', 'Active', 'In Progress', 'Commissioned', 'On Hold',
                       'Cancelled'):
            for ptype in ('Residential', 'OPEX', 'CAPEX'):
                for deleted in (False, True):
                    for test in (False, True):
                        sites.append(self.site(status=status, project_type=ptype,
                                               is_deleted=deleted, is_test=test))
        in_queryset = set(bd_scope_projects().values_list('pk', flat=True))
        for site in sites:
            with self.subTest(site=(site.status, site.project_type, site.is_deleted,
                                    site.is_test)):
                self.assertEqual(site_in_bd_scope(site), site.pk in in_queryset)
        self.assertEqual(len(in_queryset), 5)   # five live statuses, Residential, real

    def test_scope(self):
        empty = self.site()
        draft = self.site(status='Draft')
        on_hold = self.site(status='On Hold')
        inactive = self.site(bd=self.bd_off)
        nologin = self.site(bd=self.bd_nologin)
        moved = self.site(bd=self.bd2)
        self.bd2.role = 'Finance'
        self.bd2.save()
        good = self.site(bd=self.bd1)
        deleted = self.site(is_deleted=True)
        test_site = self.site(is_test=True)
        cancelled = self.site(status='Cancelled')
        opex = self.site(project_type='OPEX')

        found = set(sites_without_bd())
        self.assertEqual(found, {empty, draft, on_hold, inactive, nologin, moved})
        for site in (good, deleted, test_site, cancelled, opex):
            self.assertNotIn(site, found)

    def test_one_query(self):
        for _ in range(3):
            self.site()
        self.site(bd=self.bd_off)
        self.site(bd=self.bd1)
        with CaptureQueriesContext(connection) as ctx:
            rows = [(s.project_id, s.bd and s.bd.user.username) for s in sites_without_bd()]
        self.assertEqual(len(rows), 4)
        self.assertEqual(len(ctx.captured_queries), 1)
