"""
RPT-C1 — action codes for the activity rows that had none.

Migration 0043 left ~80 ActivityLog call sites writing a blank `action_code`; the Reports
"What" column could only label those rows by entity type ("Task update"). RPT-C1 gave
each call site a code (activity_codes.FORMERLY_BLANK_CODES), a readable label
(report_activity.CODE_KIND_LABELS), and a command that relabels the old rows by exact
text (relabel_blank_activity_codes). Nothing about WHAT COUNTS may change (decision C2).

WHAT THIS FILE PINS
    Codes          every new code is human activity (human_activity_q and
                   is_human_activity), fits the column, and has exactly one label.
    Call sites     the approved code at all 73 call sites, by function and text (static,
                   so seed and admin paths too); and, posted through the real views,
                   the code written for the high-volume and representative ones.
    Guard          no log_activity() / ActivityLog create outside tests and migrations may
                   write a blank or missing code (Zuber's ruling 7); the wipe_demo trace is
                   the one exception, by name.
    Parity         the same rows give the same site_breakdown() and activity_report()
                   figures blank and coded; only the kind labels move.
    Relabel        dry run writes nothing; --apply touches action_code only; unmatched rows
                   stay blank; a conflict writes nothing; a second run finds 0.

Run alone:
    python manage.py test projects.tests_activity_codes_c1 --settings=solarpms.test_settings
"""
import ast
import dataclasses
import inspect
import re
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .activity_codes import FORMERLY_BLANK_CODES, NON_ACTIVITY_CODES
from .forms import log_catalogue_change
from .management.commands import relabel_blank_activity_codes as relabel
from .models import (ActivityLog, Checklist, ChecklistItem, ChecklistTaskLink, Comment, Issue,
                     ProjectDocument, StockLocation, SystemSettings, TaskAttachment,
                     UserProfile)
from .report_activity import (CODE_KIND_LABELS, ENTITY_KIND_LABELS, action_kind_label,
                              activity_report)
from .site_breakdown import site_breakdown
from .site_dashboard import human_activity_q, is_human_activity
from .tests_grn_damage import stub_delivery_storage, with_grn_photo
from .tests_report_foundations import DAY, ActivityFixture, day_period, ist
from .tests_residential_baseline import (ResidentialBaselineBase, _client_for, _pdf, _photo,
                                         _profile)
from .tests_site_breakdown import NOW

REPO = Path(__file__).resolve().parent.parent
ACTION_CODE_MAX = ActivityLog._meta.get_field('action_code').max_length


# ---------------------------------------------------------------------------
# 1. The codes themselves
# ---------------------------------------------------------------------------

class FormerlyBlankCodeTests(TestCase):

    def test_every_new_code_is_human_activity_exactly_as_a_blank_row_is(self):
        # C2: a blank row passes human_activity_q, so its coded replacement must too.
        rows = [ActivityLog.objects.create(project=None, actor=None, action='x',
                                           action_code=code)
                for code in sorted(FORMERLY_BLANK_CODES)]
        blank = ActivityLog.objects.create(project=None, actor=None, action='x', action_code='')
        kept = set(ActivityLog.objects.filter(human_activity_q()).values_list('pk', flat=True))
        self.assertIn(blank.pk, kept)
        for row in rows:
            with self.subTest(code=row.action_code):
                self.assertIn(row.pk, kept)
                self.assertTrue(is_human_activity(row.action_code))
                self.assertFalse(row.action_code.endswith('_opened'))
                self.assertNotIn(row.action_code, NON_ACTIVITY_CODES)

    def test_every_code_fits_the_column_and_follows_the_style(self):
        for code in FORMERLY_BLANK_CODES:
            with self.subTest(code=code):
                self.assertLessEqual(len(code), ACTION_CODE_MAX)
                self.assertRegex(code, r'\A[a-z]+(_[a-z]+)+\Z')

    def test_each_code_has_exactly_one_readable_label(self):
        self.assertEqual(set(CODE_KIND_LABELS), set(FORMERLY_BLANK_CODES))
        for code, label in CODE_KIND_LABELS.items():
            with self.subTest(code=code):
                self.assertEqual(action_kind_label(code), label)
                self.assertEqual(action_kind_label(code, 'Project'), label,
                                 'a coded row is labelled by its code, not its entity')

    def test_the_site_wording_and_the_blank_fallback(self):
        self.assertEqual(action_kind_label('project_coordinators_updated'), 'Coordinators updated')
        self.assertEqual(action_kind_label('project_activated'), 'Site activated')
        self.assertEqual(action_kind_label('task_file_uploaded'), 'Task file uploaded')
        # Old and unmatched rows keep B2a's entity-type labels.
        self.assertEqual(action_kind_label('', 'Task'), ENTITY_KIND_LABELS['Task'])
        # Codes outside the map still go through the naming rule.
        self.assertEqual(action_kind_label('design_head_qc_passed'), 'Head QC passed')

    def test_every_relabel_pattern_writes_an_approved_code(self):
        for p in relabel.PATTERNS:
            with self.subTest(key=p.key):
                self.assertIn(p.code, FORMERLY_BLANK_CODES)
        self.assertEqual(len({p.key for p in relabel.PATTERNS}), len(relabel.PATTERNS))


# ---------------------------------------------------------------------------
# 2. The code at every call site (static) and the guard
# ---------------------------------------------------------------------------

#: (file, function, a fragment of the action text's source, the code's source, calls).
#: All 73 call sites RPT-C1 coded, located by function and text so a moved line does not
#: break the test but a changed code does.
CALL_SITES = (
    ('projects/grn_damage.py', 'record_damage', 'Commented on issue', "'issue_commented'", 1),
    ('projects/management/commands/seed_opex_test_data.py', '_activate_opex_sites', 'Activated project', "'project_activated'", 1),
    ('projects/management/commands/seed_opex_test_data.py', '_create_residential', 'Activated project', "'project_activated'", 1),
    ('projects/management/commands/seed_opex_test_data.py', '_seed_delivery_challan', 'SCM created Delivery Challan', "'dc_created'", 1),
    ('projects/management/commands/normalise_catalogue_units.py', '_apply', 'normalise_catalogue_units (system)', "'boq_catalogue_unit_fixed'", 1),
    ('projects/notify_views.py', 'admin_notification_set_state', "Notification '", "'notify_event_switched'", 1),
    ('projects/notify_views.py', 'admin_notification_draft_activate', "Notification '", "'notify_template_activated'", 1),
    ('projects/notify_views.py', 'admin_notification_draft_discard', "Notification '", "'notify_template_draft_discarded'", 1),
    ('projects/notify_views.py', 'admin_notification_version_restore', "Notification '", "'notify_template_restored'", 1),
    ('projects/views.py', 'project_field_edit', 'Edited project fields', "'project_fields_edited'", 1),
    ('projects/views.py', 'project_activate', 'Activated project', "'project_activated'", 1),
    ('projects/views.py', 'opex_site_activate', 'Activated project', "'project_activated'", 1),
    ('projects/views.py', 'enable_cascade_scheduling', 'Cascading scheduling enabled', "'cascade_scheduling_enabled'", 1),
    ('projects/views.py', 'assign_coordinators', 'Updated project coordinators', "'project_coordinators_updated'", 1),
    ('projects/views.py', '_apply_boq_acknowledgement', 'BOQ Acknowledged', "'boq_acknowledged'", 1),
    ('projects/views.py', 'boq_submit', 'Submitted BOQ', "'boq_submitted'", 1),
    ('projects/views.py', 'boq_request_revision', 'BOQ Revision Requested', "'boq_revision_requested'", 1),
    ('projects/views.py', 'zoho_deal_closed_webhook', "Project '", "'project_created_from_zoho'", 1),
    ('projects/views.py', 'upload_project_document', 'Uploaded ', "'project_file_uploaded'", 2),
    ('projects/views.py', 'delete_project_document', 'Deleted file', "'file_deleted'", 1),
    ('projects/views.py', 'upload_task_attachment', 'Uploaded ', "'task_file_uploaded'", 2),
    ('projects/views.py', 'delete_task_attachment', 'Deleted file', "'file_deleted'", 1),
    ('projects/views.py', 'checklist_item_complete', "Answered '", "'checklist_item_answered'", 1),
    ('projects/views.py', 'update_issue_status', 'Moved issue to In Progress', "'issue_in_progress'", 1),
    ('projects/views.py', 'assign_issue', 'Assigned issue to', "'issue_assigned'", 1),
    ('projects/views.py', 'assign_issue', 'Unassigned issue', "'issue_unassigned'", 1),
    ('projects/views.py', 'create_task_comment', 'Replied to comment on task', "'task_comment_replied'", 1),
    ('projects/views.py', 'create_task_comment', 'Commented on task', "'task_commented'", 1),
    ('projects/views.py', 'create_issue_comment', 'Replied to comment on issue', "'issue_comment_replied'", 1),
    ('projects/views.py', 'create_issue_comment', 'Commented on issue', "'issue_commented'", 1),
    ('projects/views.py', 'delete_comment', 'Deleted comment on task', "'task_comment_deleted'", 1),
    ('projects/views.py', 'delete_comment', 'Deleted comment on issue', "'issue_comment_deleted'", 1),
    ('projects/views.py', 'create_delivery_challan', 'SCM created Delivery Challan', "'dc_created'", 1),
    ('projects/views.py', 'confirm_grn', 'SE confirmed GRN', "'grn_confirmed'", 1),
    ('projects/views.py', 'override_grn', 'SCM overrode GRN', "'grn_overridden'", 1),
    ('projects/views.py', 'admin_master_switches', 'Auto-schedule working-day', "'system_setting_changed'", 1),
    ('projects/views.py', 'admin_master_switches', 'Master switch', "'system_setting_changed'", 1),
    ('projects/views.py', 'admin_master_switches', 'Cascading scheduling feature gate', "'system_setting_changed'", 1),
    ('projects/views.py', 'admin_master_switches', '{label} set to', "'system_setting_changed'", 1),
    ('projects/views.py', 'admin_user_management', "User '", "'user_deactivated'", 1),
    ('projects/views.py', 'admin_user_management', "User '", "'user_reactivated'", 1),
    ('projects/views.py', 'admin_user_management', "User '", "'user_role_changed'", 1),
    ('projects/views.py', 'admin_notification_prefs', 'Notification pref updated', "'notification_pref_changed'", 1),
    ('projects/views.py', 'admin_departments', "User '", "'user_deactivated'", 1),
    ('projects/views.py', 'admin_departments', "User '", "'user_reactivated'", 1),
    ('projects/views.py', 'admin_departments', "User '", "'user_role_changed'", 1),
    ('projects/views.py', 'admin_user_edit', "User '", "'user_profile_updated'", 1),
    ('projects/views.py', 'admin_user_edit', 'Password reset for user', "'user_password_reset'", 1),
    ('projects/views.py', 'admin_checklist_create', 'Created checklist', "'checklist_created'", 1),
    ('projects/views.py', 'admin_checklist_update', 'Published checklist', "'checklist_published'", 1),
    ('projects/views.py', 'admin_checklist_update', 'Archived checklist', "'checklist_archived'", 1),
    ('projects/views.py', 'admin_checklist_update', 'Renamed checklist', "'checklist_renamed'", 1),
    ('projects/views.py', 'admin_checklist_delete', 'Deleted draft checklist', "'checklist_deleted'", 1),
    ('projects/views.py', 'admin_checklist_item_add', 'Added item', "'checklist_item_added'", 1),
    ('projects/views.py', 'admin_checklist_item_edit', 'Edited item to', "'checklist_item_edited'", 1),
    ('projects/views.py', 'admin_checklist_item_delete', 'Deleted item', "'checklist_item_deleted'", 1),
    ('projects/views.py', 'admin_checklist_item_move', 'Reordered item', "'checklist_item_moved'", 1),
    ('projects/views.py', 'admin_checklist_link_add', 'Assigned checklist', "'checklist_linked'", 1),
    ('projects/views.py', 'admin_checklist_link_delete', 'Unassigned checklist', "'checklist_unlinked'", 1),
    ('projects/views.py', 'stock_location_create', 'Created warehouse', "'stock_location_created'", 1),
    ('projects/views.py', 'stock_location_edit', 'Updated warehouse', "'stock_location_updated'", 1),
    ('projects/views.py', 'stock_location_toggle', '{state.capitalize()} warehouse', "f'stock_location_{state}'", 1),
    ('projects/views.py', 'admin_reset_password', 'Admin reset password', "'user_password_reset'", 1),
    ('projects/views.py', 'subadmin_departments', "User '", "'user_role_changed'", 1),
    ('projects/views.py', 'subadmin_departments', "User '", "'user_profile_updated'", 1),
    ('projects/views.py', 'subadmin_departments', "User '", "'user_deactivated'", 1),
    ('projects/views.py', 'subadmin_departments', "User '", "'user_reactivated'", 1),
    ('projects/views.py', '_subadmin_create_user', "User '", "'user_created'", 1),
    ('projects/views.py', 'admin_boq_item_create', 'Created BOQ catalogue item', "'boq_catalogue_item_created'", 1),
    ('projects/views.py', 'admin_boq_item_edit', 'Updated BOQ catalogue item', "'boq_catalogue_item_updated'", 1),
    ('projects/views.py', 'admin_boq_item_toggle', '{state.capitalize()} BOQ catalogue item', "f'boq_catalogue_item_{state}'", 1),
)

#: Writer functions whose own ActivityLog write is the generic one, not a call site.
#: models.log_activity spreads its kwargs (**values) and keeps '' as its signature default;
#: every CALL of it is checked instead. The wipe_demo trace stays blank by ruling 5.
GUARD_EXEMPT = {
    ('projects/models.py', 'log_activity'),
    ('projects/management/commands/wipe_demo_2026_10_14.py', '_write_trace'),
}
WRITERS = ('log_activity', 'log_catalogue_change')
SKIP_DIRS = {'venv', '.venv', 'migrations', 'node_modules', 'staticfiles', '.git'}


def _production_sources():
    """Every .py outside tests, migrations and the virtualenv, as (repo path, source)."""
    for path in sorted(REPO.rglob('*.py')):
        rel = path.relative_to(REPO)
        if set(rel.parts) & SKIP_DIRS or path.name.startswith('test'):
            continue
        yield rel.as_posix(), path.read_text(encoding='utf-8-sig')   # a few files carry a BOM


def _activity_calls(source):
    """(enclosing function, call node, text expr, code expr or None, spreads kwargs) for
    each ActivityLog write in `source`: log_activity(), log_catalogue_change(),
    ActivityLog.objects.create()/bulk_create() and ActivityLog(...)."""
    tree = ast.parse(source)
    found = []

    def visit(node, func):
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else func
            if isinstance(child, ast.Call):
                fn = child.func
                name = fn.id if isinstance(fn, ast.Name) else getattr(fn, 'attr', '')
                is_model = (name == 'ActivityLog'
                            or (isinstance(fn, ast.Attribute) and name == 'create'
                                and 'ActivityLog' in ast.unparse(fn.value)))
                if name in WRITERS or is_model:
                    kw = {k.arg: k.value for k in child.keywords if k.arg}
                    spreads = any(k.arg is None for k in child.keywords)
                    code = kw.get('action_code')
                    text = kw.get('action') or kw.get('prefix')
                    if name == 'log_activity':
                        if code is None and len(child.args) >= 6:
                            code = child.args[5]
                        if text is None and len(child.args) >= 3:
                            text = child.args[2]
                    elif name == 'log_catalogue_change':
                        if code is None and len(child.args) >= 5:
                            code = child.args[4]
                        if text is None and len(child.args) >= 3:
                            text = child.args[2]
                    found.append((func, child, text, code, spreads))
            visit(child, inner)
    visit(tree, '<module>')
    return found


def _is_blank(expr):
    return expr is None or (isinstance(expr, ast.Constant) and expr.value in ('', None))


def guard_failures(sources):
    """Every ActivityLog write in `sources` with a missing or blank code, and every
    writer helper whose action_code parameter defaults to blank."""
    failures = []
    for rel, source in sources:
        for func, call, _text, code, spreads in _activity_calls(source):
            if (rel, func) in GUARD_EXEMPT:
                continue
            if _is_blank(code):
                why = 'spreads **kwargs with no action_code' if spreads else 'blank or missing action_code'
                failures.append(f'{rel}:{call.lineno} in {func}: {why}')
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (rel, node.name) not in GUARD_EXEMPT:
                args = node.args
                positional = args.posonlyargs + args.args
                defaults = dict(zip([a.arg for a in positional][-len(args.defaults):], args.defaults)) if args.defaults else {}
                defaults.update({a.arg: d for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None})
                if 'action_code' in defaults and _is_blank(defaults['action_code']):
                    failures.append(f'{rel}:{node.lineno} {node.name}(): action_code defaults to blank')
    return failures


class CallSiteCodeTests(SimpleTestCase):

    def test_each_call_site_passes_its_approved_code(self):
        sources = dict(_production_sources())
        calls = {}
        for rel in {row[0] for row in CALL_SITES}:
            for func, call, text, code, _ in _activity_calls(sources[rel]):
                calls.setdefault((rel, func), []).append(
                    (ast.get_source_segment(sources[rel], text) if text is not None else '',
                     ast.unparse(code) if code is not None else ''))
        for rel, func, fragment, code, count in CALL_SITES:
            with self.subTest(site=f'{rel} {func} {fragment!r}'):
                hits = [c for t, c in calls.get((rel, func), []) if fragment in t and c == code]
                self.assertEqual(len(hits), count, f'expected {count} call(s) writing {code}')

    def test_the_table_covers_seventy_three_call_sites(self):
        self.assertEqual(sum(row[4] for row in CALL_SITES), 73)

    def test_log_catalogue_change_requires_its_code(self):
        param = inspect.signature(log_catalogue_change).parameters['action_code']
        self.assertIs(param.default, inspect.Parameter.empty)


class BlankCodeGuardTests(SimpleTestCase):
    """Ruling 7: the suite fails on any production ActivityLog write with no code."""

    def test_no_production_activity_write_has_a_blank_code(self):
        failures = guard_failures(_production_sources())
        self.assertEqual(failures, [], 'Give each of these a code (see activity_codes.py):\n'
                         + '\n'.join(failures))

    def test_the_guard_sees_the_exempt_trace(self):
        # The exemption is real: without it the wipe_demo trace would be reported.
        sources = [(rel, src) for rel, src in _production_sources()
                   if rel.endswith('wipe_demo_2026_10_14.py')]
        self.assertEqual(len(sources), 1)
        with patch.dict(globals(), {'GUARD_EXEMPT': {('projects/models.py', 'log_activity')}}):
            self.assertEqual(len(guard_failures(sources)), 1)

    def _scan(self, src):
        return guard_failures([('projects/fake.py', src)])

    def test_a_missing_code_fails(self):
        self.assertEqual(len(self._scan("def v(p, u):\n    log_activity(p, u, 'Did it')\n")), 1)

    def test_a_blank_code_fails(self):
        self.assertEqual(len(self._scan(
            "def v(p, u):\n    log_activity(p, u, 'Did it', action_code='')\n")), 1)
        self.assertEqual(len(self._scan(
            "def v(p):\n    ActivityLog.objects.create(project=p, action='x')\n")), 1)

    def test_a_coded_write_passes_including_a_computed_one(self):
        self.assertEqual(self._scan(
            "def v(p, u, s):\n    log_activity(p, u, 'x', action_code='thing_done')\n"
            "    log_activity(p, u, 'x', 'T', 1, f'thing_{s}')\n"
            "    ActivityLog.objects.create(project=p, action='x', action_code='a_b')\n"), [])

    def test_a_helper_defaulting_its_code_to_blank_fails(self):
        self.assertEqual(len(self._scan("def helper(p, action_code=''):\n    pass\n")), 1)


# ---------------------------------------------------------------------------
# 3. The code written, through the real views
# ---------------------------------------------------------------------------

class ViewCodeTests(ResidentialBaselineBase):
    """Posted through the real views: each test asserts the code on the row written."""

    def _codes(self, project=None, **filters):
        qs = ActivityLog.objects.filter(**filters)
        if project is not None:
            qs = qs.filter(project=project)
        return list(qs.order_by('pk').values_list('action_code', flat=True))

    def _row(self, prefix, project=None):
        qs = ActivityLog.objects.filter(action__startswith=prefix)
        if project is not None:
            qs = qs.filter(project=project)
        return qs.latest('pk')

    def test_project_activate_writes_project_activated(self):
        # The fixture activated project A through the real view.
        self.assertEqual(self._row('Activated project:', self.project_a).action_code,
                         'project_activated')

    def test_project_field_edit_writes_project_fields_edited(self):
        _client_for(self.pm_a).post(
            reverse('project_field_edit', args=[self.project_a.project_id]),
            {'dc_capacity_kw': '6.00',
             'target_commissioning_date': self.project_a.target_commissioning_date.isoformat(),
             'reason': 'resurvey'})
        self.assertEqual(self._row('Edited project fields:', self.project_a).action_code,
                         'project_fields_edited')

    def test_assign_coordinators_writes_project_coordinators_updated(self):
        second = _profile('c1_coord2', 'Project Coordinator')
        _client_for(self.pm_a).post(
            reverse('assign_coordinators', args=[self.project_a.project_id]),
            {'coordinator_ids': [str(self.coord_a.pk), str(second.pk)]})
        self.assertEqual(self._row('Updated project coordinators', self.project_a).action_code,
                         'project_coordinators_updated')

    def test_project_document_upload_and_delete(self):
        client = _client_for(self.pm_a)
        with patch('projects.supabase_storage.get_supabase_client', return_value=MagicMock()):
            client.post(reverse('upload_project_document', args=[self.project_a.project_id]),
                        {'files': [_pdf('handover.pdf')]})
        self.assertEqual(self._row('Uploaded 1 file(s) to project', self.project_a).action_code,
                         'project_file_uploaded')
        doc = ProjectDocument.objects.get(project=self.project_a)
        client.post(reverse('delete_project_document', args=[self.project_a.project_id, doc.pk]))
        self.assertEqual(self._row('Deleted file:', self.project_a).action_code, 'file_deleted')

    def test_task_attachment_upload_and_delete(self):
        task = self._task(self.project_a, 'AC Cable Work')
        client = _client_for(self.se_a)
        with patch('projects.supabase_storage.get_supabase_client', return_value=MagicMock()):
            client.post(reverse('upload_task_attachment', args=[self.project_a.project_id, task.pk]),
                        {'files': [_photo('roof.jpg')]})
        self.assertEqual(self._row('Uploaded 1 file(s) to task:', self.project_a).action_code,
                         'task_file_uploaded')
        attach = TaskAttachment.objects.get(task=task)
        client.post(reverse('delete_task_attachment',
                            args=[self.project_a.project_id, task.pk, attach.pk]))
        self.assertEqual(self._row('Deleted file:', self.project_a).action_code, 'file_deleted')

    def test_checklist_item_complete_writes_checklist_item_answered(self):
        task = self._task(self.project_a, 'Pre Commissioning Check List')
        checklist = Checklist.objects.create(name='Pre-commissioning')
        item = ChecklistItem.objects.create(checklist=checklist, label='Earth OK', order=1)
        checklist.activate()
        ChecklistTaskLink.objects.create(checklist=checklist, task_name=task.task_name,
                                         project_type='Residential')
        with patch('projects.supabase_storage.get_supabase_client', return_value=MagicMock()):
            _client_for(self.se_a).post(
                reverse('checklist_item_complete',
                        args=[self.project_a.project_id, task.pk, item.pk]),
                {'photo': _photo()})
        self.assertEqual(self._row('Answered ', self.project_a).action_code,
                         'checklist_item_answered')

    def test_task_comment_reply_and_delete(self):
        task = self._task(self.project_a, 'AC Cable Work')
        client = _client_for(self.pm_a)
        url = reverse('create_task_comment', args=[self.project_a.project_id, task.pk])
        client.post(url, {'body': 'First'})
        self.assertEqual(self._row('Commented on task:', self.project_a).action_code,
                         'task_commented')
        parent = Comment.objects.get(task=task, parent=None)
        client.post(url, {'body': 'Reply', 'parent': str(parent.pk)})
        self.assertEqual(self._row('Replied to comment on task:', self.project_a).action_code,
                         'task_comment_replied')
        client.post(reverse('delete_comment', args=[parent.pk]))
        self.assertEqual(self._row('Deleted comment on task:', self.project_a).action_code,
                         'task_comment_deleted')

    def _issue(self):
        _client_for(self.pm_a).post(
            reverse('create_project_issue', args=[self.project_a.project_id]),
            {'title': 'Inverter scratched', 'severity': Issue.MEDIUM,
             'assigned_to': str(self.se_a.pk)})
        return Issue.objects.get(title='Inverter scratched')

    def test_issue_comment_status_assign_and_unassign(self):
        issue = self._issue()
        client = _client_for(self.pm_a)
        client.post(reverse('create_issue_comment', args=[issue.pk]), {'body': 'Looking'})
        self.assertEqual(self._row('Commented on issue:', self.project_a).action_code,
                         'issue_commented')
        client.post(reverse('update_issue_status', args=[issue.pk]))
        self.assertEqual(self._row('Moved issue to In Progress:', self.project_a).action_code,
                         'issue_in_progress')
        client.post(reverse('assign_issue', args=[issue.pk]), {'assigned_to': str(self.pm_a.pk)})
        self.assertEqual(self._row('Assigned issue to', self.project_a).action_code,
                         'issue_assigned')
        client.post(reverse('assign_issue', args=[issue.pk]), {'assigned_to': ''})
        self.assertEqual(self._row('Unassigned issue:', self.project_a).action_code,
                         'issue_unassigned')

    def test_delivery_challan_grn_and_override(self):
        _client_for(self.scm).post(
            reverse('create_delivery_challan', args=[self.project_a.project_id]),
            {'dc_number': 'DC-C1-001', 'dc_date': date.today().isoformat(),
             'line_item_category_0': 'Solar Modules',
             'line_item_description_0': 'Solar Module 540Wp',
             'line_item_qty_0': '10', 'line_item_unit_0': 'Nos'})
        self.assertEqual(self._row('SCM created Delivery Challan', self.project_a).action_code,
                         'dc_created')
        challan = self._make_dc(self.project_a, 'DC-C1-002', ordered=Decimal('10.00'))
        item = challan.line_items.first()
        stub_delivery_storage(self)
        _client_for(self.se_a).post(
            reverse('confirm_grn', args=[self.project_a.project_id, challan.pk]),
            with_grn_photo({f'received_qty_{item.pk}': '8', f'damaged_qty_{item.pk}': '0'}))
        self.assertEqual(self._row('SE confirmed GRN for DC', self.project_a).action_code,
                         'grn_confirmed')
        _client_for(self.scm).post(
            reverse('override_grn', args=[self.project_a.project_id, challan.pk]),
            {f'received_qty_{item.pk}': '10', f'damaged_qty_{item.pk}': '0'})
        self.assertEqual(self._row('SCM overrode GRN for DC', self.project_a).action_code,
                         'grn_overridden')

    def test_master_switches_write_system_setting_changed(self):
        SystemSettings.get()
        last = ActivityLog.objects.order_by('-pk').values_list('pk', flat=True).first() or 0
        _client_for(self.admin).post(reverse('admin_master_switches'),
                                     {'email_enabled': 'on', 'gantt_client_buffer_days': '9',
                                      'gantt_external_min_display_days': '3'})
        # entity_type='System': the login signal's user_login row is not this view's.
        codes = set(ActivityLog.objects.filter(pk__gt=last, entity_type='System')
                    .values_list('action_code', flat=True))
        self.assertTrue(codes, 'the post changed at least one setting')
        self.assertEqual(codes, {'system_setting_changed'})

    def test_user_deactivate_reactivate_and_role_change(self):
        target = _profile('c1_target', 'Site Engineer')
        client = _client_for(self.admin)
        url = reverse('admin_user_management')
        client.post(url, {'action': 'deactivate', 'user_id': str(target.user.pk)})
        client.post(url, {'action': 'reactivate', 'user_id': str(target.user.pk)})
        client.post(url, {'action': 'change_role', 'user_id': str(target.user.pk),
                          'new_role': 'PM'})
        self.assertEqual(self._codes(entity_type='User', entity_id=target.user.pk),
                         ['user_deactivated', 'user_reactivated', 'user_role_changed'])

    def test_notification_pref_writes_notification_pref_changed(self):
        target = _profile('c1_prefs', 'Site Engineer')
        UserProfile.objects.filter(pk=target.pk).update(whatsapp_notifications=True,
                                                        email_notifications=True)
        _client_for(self.admin).post(reverse('admin_notification_prefs'),
                                     {'profile_id': str(target.pk)})
        codes = self._codes(entity_type='Notification')
        self.assertTrue(codes)
        self.assertEqual(set(codes), {'notification_pref_changed'})

    def test_warehouse_create_and_toggle(self):
        client = _client_for(self.admin)
        client.post(reverse('stock_location_create'), {'code': 'WH-C1', 'name': 'Store', 'keeper': ''})
        location = StockLocation.objects.get(code='WH-C1')
        client.post(reverse('stock_location_toggle', args=[location.pk]))
        self.assertEqual(self._codes(entity_type='StockLocation', entity_id=location.pk),
                         ['stock_location_created', 'stock_location_deactivated'])


# ---------------------------------------------------------------------------
# 4. Parity: the same rows, blank and coded, give the same figures
# ---------------------------------------------------------------------------

def _figures(report):
    """A PeriodActivity with the kind labels and bulk codes taken out: everything else
    is a figure that must not move."""
    users = report.active_users
    return dataclasses.replace(
        report,
        active_users=dataclasses.replace(
            users, rows=tuple(dataclasses.replace(r, kinds=()) for r in users.rows)),
        bulk_actions=tuple(dataclasses.replace(b, action_code='') for b in report.bulk_actions))


class ParityTests(ActivityFixture):
    """The same fixture with blank codes, then relabelled through the real command."""

    def _blank(self, site, actor, when, entity_type, action):
        row = ActivityLog.objects.create(project=site, actor=actor, action=action,
                                         action_code='', entity_type=entity_type)
        ActivityLog.objects.filter(pk=row.pk).update(timestamp=when)
        return row

    def _build(self):
        a, b, c = self._site(), self._site(), self._site(status='Draft')
        sites = [a.pk, b.pk, c.pk]
        # The report day: a mix of the patterns, people, sites and a site-less row.
        self._blank(a, self.pm, ist(DAY, 9), 'Project',
                    'Updated project coordinators (added 1, removed 0)')
        self._blank(a, self.pm, ist(DAY, 9, 5), 'Project', 'Edited project fields: dc capacity')
        self._blank(b, self.designer, ist(DAY, 10), 'File', 'Uploaded 2 file(s) to task: Work')
        self._blank(b, self.se, ist(DAY, 11), 'ChecklistItemCompletion',
                    "Answered 'Yes' on checklist item 'Earth OK' on task: Work")
        self._blank(c, self.designer, ist(DAY, 12), 'Task', 'Updated due date for task: Work')
        self._blank(None, self.ceo, ist(DAY, 13), 'User', "User 'Asha' profile updated by admin")
        self._blank(a, self.se, ist(DAY, 14), 'DeliveryChallan',
                    'SE confirmed GRN for DC DC-1 — Received')
        self._log(a, self.pm, ist(DAY, 15))                       # an already coded row
        # A one-kind run over 10 Draft sites, 0.5 s apart: bulk both ways.
        for i, site in enumerate(self._site(status='Draft') for _ in range(10)):
            self._blank(site, self.pm, ist(DAY, 16) + timedelta(seconds=0.5 * i), 'Project',
                        'Updated project coordinators (added 1, removed 0)')
        # The previous day, and the site_breakdown() day (NOW).
        self._blank(b, self.pm, ist(DAY - timedelta(days=1), 10), 'File',
                    'Uploaded 1 file(s) to project')
        self._blank(a, self.se, NOW - timedelta(hours=1), 'Comment', 'Commented on task: Work')
        self._blank(c, self.pm, NOW - timedelta(hours=2), 'Project', 'Activated project: X-1')
        return sites

    def test_the_figures_are_the_same_blank_and_coded(self):
        sites = self._build()
        report_before = activity_report(day_period(DAY))
        breakdown_before = site_breakdown(sites, NOW)
        self.assertTrue(report_before.current.active_users.rows)
        self.assertTrue(report_before.current.bulk_actions, 'the one-kind run is bulk')

        call_command('relabel_blank_activity_codes', '--apply', stdout=StringIO())
        self.assertFalse(ActivityLog.objects.filter(action_code='').exists())

        report_after = activity_report(day_period(DAY))
        self.assertEqual(_figures(report_after.current), _figures(report_before.current))
        self.assertEqual(_figures(report_after.previous), _figures(report_before.previous))
        self.assertEqual(site_breakdown(sites, NOW), breakdown_before)

        # Only the kinds moved: entity-type labels became the codes' labels.
        kinds = dict(self._user(report_after.current, self.pm).kinds)
        self.assertEqual(kinds['Coordinators updated'], 1)
        self.assertEqual(kinds['Site fields edited'], 1)
        self.assertEqual(kinds['Bulk: Coordinators updated'], 1)
        self.assertNotIn('Project update', kinds)
        self.assertEqual(dict(self._user(report_after.current, self.designer).kinds),
                         {'Task file uploaded': 1, 'Due date changed': 1})

    def test_a_mixed_blank_run_is_no_longer_one_bulk_action(self):
        # Ruling 6, accepted: blank rows of different kinds no longer group together,
        # because the bulk rule groups by (actor, code). Alternating activations and
        # coordinator changes over 10 sites was "bulk" only because both were blank.
        for i, site in enumerate(self._site(status='Draft') for _ in range(10)):
            kind = ('Project', 'Activated project: X') if i % 2 else (
                'Project', 'Updated project coordinators (added 1, removed 0)')
            self._blank(site, self.pm, ist(DAY, 16) + timedelta(seconds=0.5 * i), *kind)
        self.assertEqual(len(activity_report(day_period(DAY)).current.bulk_actions), 1)
        call_command('relabel_blank_activity_codes', '--apply', stdout=StringIO())
        after = activity_report(day_period(DAY)).current
        self.assertEqual(after.bulk_actions, ())
        self.assertEqual(self._user(after, self.pm).actions, 10)


# ---------------------------------------------------------------------------
# 5. The relabel command
# ---------------------------------------------------------------------------

class RelabelCommandTests(TestCase):

    def _row(self, entity_type, action, code=''):
        return ActivityLog.objects.create(project=None, actor=None, action=action,
                                          action_code=code, entity_type=entity_type)

    def _run(self, *args):
        out = StringIO()
        call_command('relabel_blank_activity_codes', *args, stdout=out)
        return out.getvalue()

    def test_a_dry_run_writes_nothing_and_counts_each_pattern(self):
        self._row('File', 'Deleted file: a.pdf')
        self._row('File', 'Deleted file: b.pdf')
        out = self._run()
        self.assertIn('MODE: DRY RUN', out)
        self.assertRegex(out, r'F03\s+2\s+File -> file_deleted')
        self.assertIn('TO RELABEL: 2', out)
        self.assertEqual(ActivityLog.objects.filter(action_code='').count(), 2)

    def test_at_most_five_samples_per_pattern(self):
        for n in range(7):
            self._row('Task', f'Updated due date for task: T{n}')
        out = self._run()
        self.assertEqual(len(re.findall(r'Updated due date for task: T\d', out)), 5)

    def test_apply_writes_the_code_and_nothing_else(self):
        row = self._row('Milestone', "Milestone M3 auto-marked Received via completion of "
                                     "Finance task '100% Payment Confirmation' by Asha (Finance)")
        ActivityLog.objects.filter(pk=row.pk).update(timestamp=NOW, entity_id=None)
        before = ActivityLog.objects.filter(pk=row.pk).values().get()
        self._run('--apply')
        after = ActivityLog.objects.filter(pk=row.pk).values().get()
        self.assertEqual(after.pop('action_code'), 'milestone_updated')
        before.pop('action_code')
        self.assertEqual(after, before)

    def test_unmatched_rows_stay_blank(self):
        login = self._row('User', 'User logged in from 1.2.3.4')
        no_entity = self._row('', 'Activated project: HRP-RES-2026-009')   # entity type must match
        trace = self._row('', '14 Oct demo S1-S4 wiped by ruling, see execution-model §12: 3 rows')
        near = self._row('Project', 'Activated project: X extra words')     # \S+ then the end
        out = self._run('--apply')
        self.assertIn('UNMATCHED (stay blank): 4', out)
        for r in (login, no_entity, trace, near):
            r.refresh_from_db()
            self.assertEqual(r.action_code, '')

    def test_an_already_coded_row_is_never_touched(self):
        coded = self._row('Task', 'Updated due date for task: X (was 2026-11-01, now 2026-11-08)',
                          code='due_date_changed')
        other = self._row('File', 'Deleted file: a.pdf', code='something_else')
        self._run('--apply')
        coded.refresh_from_db(); other.refresh_from_db()
        self.assertEqual((coded.action_code, other.action_code),
                         ('due_date_changed', 'something_else'))

    def test_a_second_run_finds_nothing(self):
        self._row('Project', 'Updated project coordinators (added 2, removed 1)')
        self._row('DeliveryChallan', 'SCM created Delivery Challan DC-9 for Vendor')
        self.assertIn('RELABELLED: 2 (planned 2)', self._run('--apply'))
        self.assertIn('TO RELABEL: 0', self._run())

    def test_a_conflict_writes_nothing(self):
        self._row('File', 'Deleted file: a.pdf')
        self._row('File', 'Uploaded 1 file(s) to project')
        clash = relabel.Pattern('Z99', 'File', r'Deleted file: .+', 'task_file_uploaded', 'test')
        compiled = relabel.COMPILED + ((clash, re.compile(clash.regex, re.S)),)
        with patch.object(relabel, 'COMPILED', compiled):
            out = self._run('--apply')
        self.assertIn('CONFLICTS: 1. Nothing written.', out)
        self.assertEqual(ActivityLog.objects.filter(action_code='').count(), 2)

    def test_each_pattern_matches_the_text_its_writer_writes(self):
        samples = {
            'P01': ('Project', 'Activated project: HRP-RES-2026-061'),
            'P03': ('Project', 'Updated project coordinators (added 1, removed 0)'),
            'P05': ('Project', "Project 'HRP-RES-2026-061' created via Zoho CRM webhook"),
            'F01': ('File', 'Uploaded 1 file(s) to project'),
            'F02': ('File', 'Uploaded 2 file(s) to task: Civil Work and MMS Installation'),
            'K01': ('ChecklistItemCompletion',
                    "Answered 'Yes' on checklist item 'Earth OK' on task: Pre Commissioning"),
            'D01': ('DeliveryChallan', 'SCM created Delivery Challan HRP-IPGCL-G/26-1 for Acme'),
            'D02': ('DeliveryChallan', 'SE confirmed GRN for DC HRP-MPUVNL-G/26-1 — Received'),
            'U04': ('User', "User 'Asha' role changed from 'PM' to 'SCM' by System Admin"),
            'U06': ('User', "User 'Asha' details edited by System Admin (role: PM → SCM)"),
            'N01': ('Notification', "Notification pref updated for 'Asha': Email set to OFF"),
            'G05': ('BOQItemMaster', "normalise_catalogue_units (system) updated RESCO BOQ "
                                     "catalogue item 'OPX-001': unit: Nos → Nos."),
            'H01': ('Task', 'Updated due date for task: Net Metering Approval'),
        }
        for key, (et, text) in samples.items():
            with self.subTest(key=key):
                self.assertEqual([p.key for p in relabel.match(et, text)], [key])
