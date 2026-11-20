"""FIN-M-AB — milestone amount backfill by spreadsheet (export_milestone_amounts,
import_milestone_amounts, projects/milestone_amount_import.py).

Pins: the export's columns, order, scope and values, with no writes; the import's dry run
writes nothing; --apply sets only blank amounts, never overwrites (conflict), same value =
already set, blank = not filled; every error refuses its row and blocks the whole --apply;
warnings apply; a finance_note over 150 characters is an error; the cell parser; --actor rules; one log line per amount in FIN-M-L's
wording plus the source; nothing else on the milestone, task, ledger or notifications
moves; a second --apply sets 0; the new code is money and not site activity; the Finance
Head "Amount changed after Received" check does not read imported lines.
"""
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone
from openpyxl import Workbook, load_workbook

from .activity_codes import MONEY_ACTIVITY_CODES, NON_ACTIVITY_CODES
from .finance_head_checks import trust_checks
from .milestone_amount_import import (
    COLUMNS, CONFLICT, DRAFT_MESSAGE, IMPORTED_CODE, INPUT_COLUMNS, NOTE_LIMIT, apply_plan,
    parse_amount, plan_import, read_workbook,
)
from .models import (
    SUBJECT_PAYMENT_MILESTONE, ActivityLog, Notification, NotificationLog, PaymentMilestone,
    ProjectPhase, Project, StatusTransition, Task, log_activity,
)
from .permissions import user_can_import_milestone_amounts
from .site_dashboard import is_human_activity


def _profile(username, role, *, active=True, login_active=True, first='', last='Test'):
    """A post_save signal creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=first or username.title(), last_name=last)
    if not login_active:
        user.is_active = False
        user.save(update_fields=['is_active'])
    profile = user.profile
    profile.role = role
    profile.is_active = active
    profile.save()
    return profile


class ImportBase(TestCase):
    """Finance, an administrator, a BD and a PM, and a builder for sites with M1-M3."""

    def setUp(self):
        self.finance = _profile('ab_fin', 'Finance')
        self.ceo = _profile('ab_ceo', 'CEO')
        self.pm = _profile('ab_pm', 'PM')
        self.bd_a = _profile('ab_bda', 'BD', first='Asha')
        self.bd_z = _profile('ab_bdz', 'BD', first='Zoya')
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def site(self, *, status='Active', project_type='Residential', contract='300000',
             amounts=(None, None, None), received=(None, None, None), bd=None,
             is_deleted=False, is_test=False, milestones=True, name='AB Site'):
        project = Project.objects.create(
            customer_name=name, status=status, project_type=project_type,
            customer_phone='9876543210', site_address='1 Sun Road', city='Lucknow',
            state='Uttar Pradesh', dc_capacity_kw=Decimal('5.00'), assigned_pm=self.pm,
            cascade_scheduling=False, bd=bd, is_deleted=is_deleted, is_test=is_test,
            contract_value=Decimal(contract) if contract is not None else None)
        if milestones:
            for n, (amount, got) in enumerate(zip(amounts, received), start=1):
                PaymentMilestone.objects.create(
                    project=project, milestone_name=f'M{n}',
                    amount=Decimal(amount) if amount is not None else None,
                    amount_received=Decimal(got) if got is not None else None,
                    status=PaymentMilestone.RECEIVED if got is not None
                    else PaymentMilestone.PENDING)
        return project

    def amounts(self, project):
        return [m.amount for m in PaymentMilestone.objects.filter(project=project)
                .order_by('milestone_name')]

    def sheet(self, rows, headers=COLUMNS, name='sheet.xlsx'):
        """An .xlsx with `headers` and one row per dict (missing keys blank)."""
        wb = Workbook()
        ws = wb.active
        ws.append(list(headers))
        for row in rows:
            ws.append([row.get(h) for h in headers])
        path = self.tmp / name
        wb.save(path)
        return str(path)

    def row(self, project, m1=None, m2=None, m3=None, note=None):
        return {'project_code': project.project_id, 'M1_new': m1, 'M2_new': m2,
                'M3_new': m3, 'finance_note': note}

    def run_import(self, path, *args):
        out = StringIO()
        call_command('import_milestone_amounts', path, *args, stdout=out)
        return out.getvalue()

    def apply(self, path, actor='ab_fin'):
        return self.run_import(path, '--apply', '--actor', actor)

    def imported_logs(self):
        return list(ActivityLog.objects.filter(action_code=IMPORTED_CODE).order_by('pk'))


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

class ExportTests(ImportBase):

    def export(self, *args):
        path = self.tmp / 'out.xlsx'
        out = StringIO()
        call_command('export_milestone_amounts', '--out', str(path), *args, stdout=out)
        ws = load_workbook(path).worksheets[0]
        return out.getvalue(), ws, list(ws.iter_rows(values_only=True))

    def test_columns_exactly_and_in_order(self):
        self.site()
        _, ws, rows = self.export()
        self.assertEqual(rows[0], (
            'project_code', 'client_name', 'status', 'bd', 'contract_value',
            'M1_current', 'M2_current', 'M3_current',
            'M1_received_so_far', 'M2_received_so_far', 'M3_received_so_far',
            'M1_new', 'M2_new', 'M3_new', 'finance_note'))
        self.assertEqual(ws.freeze_panes, 'A2')

    def test_input_columns_shaded_others_plain(self):
        self.site()
        _, ws, _ = self.export()
        for index, column in enumerate(COLUMNS, start=1):
            for r in (1, 2):
                filled = ws.cell(row=r, column=index).fill.fill_type == 'solid'
                self.assertEqual(filled, column in INPUT_COLUMNS, (column, r))

    def test_in_scope_sites_only(self):
        keep = self.site(name='Keep')
        held = self.site(name='Held', status='On Hold')
        for kwargs in ({'status': 'Draft', 'milestones': False}, {'status': 'Cancelled'},
                       {'is_test': True}, {'is_deleted': True},
                       {'project_type': 'OPEX', 'milestones': False}):
            self.site(**kwargs)
        out, _, rows = self.export()
        self.assertEqual([r[0] for r in rows[1:]],
                         sorted([keep.project_id, held.project_id]))
        self.assertIn('Rows: 2', out)

    def test_current_and_received_values_numbers_and_blank_inputs(self):
        p = self.site(contract='200000', amounts=('50000', None, '30000.50'),
                      received=('50000', None, None), bd=self.bd_a, name='Values')
        _, ws, rows = self.export()
        row = dict(zip(rows[0], rows[1]))
        self.assertEqual(row['project_code'], p.project_id)
        self.assertEqual(row['client_name'], 'Values')
        self.assertEqual(row['status'], 'Active')
        self.assertEqual(row['bd'], 'Asha Test')
        self.assertEqual(row['contract_value'], 200000)
        self.assertEqual((row['M1_current'], row['M2_current'], row['M3_current']),
                         (50000, None, 30000.5))
        self.assertEqual((row['M1_received_so_far'], row['M2_received_so_far'],
                          row['M3_received_so_far']), (50000, None, None))
        for column in INPUT_COLUMNS:
            self.assertIsNone(row[column])
        self.assertIsInstance(ws.cell(row=2, column=5).value, (int, float))

    def test_export_writes_nothing(self):
        p = self.site(amounts=('1', None, None))
        logs = ActivityLog.objects.count()
        self.export()
        self.assertEqual(ActivityLog.objects.count(), logs)
        self.assertEqual(self.amounts(p), [Decimal('1.00'), None, None])

    def test_by_bd_orders_by_bd_name_then_code_no_bd_last(self):
        z1 = self.site(bd=self.bd_z)
        none = self.site()
        a1 = self.site(bd=self.bd_a)
        z2 = self.site(bd=self.bd_z)
        _, _, rows = self.export('--by-bd')
        self.assertEqual([r[0] for r in rows[1:]],
                         [a1.project_id, z1.project_id, z2.project_id, none.project_id])

    def test_out_must_be_xlsx(self):
        with self.assertRaisesMessage(CommandError, '.xlsx'):
            call_command('export_milestone_amounts', '--out', str(self.tmp / 'x.csv'),
                         stdout=StringIO())


# ---------------------------------------------------------------------------
# The cell parser
# ---------------------------------------------------------------------------

class ParseAmountTests(TestCase):

    def test_accepted(self):
        for raw, want in [('₹ 1,50,000', '150000'), ('150,000', '150000'),
                          (' 12345 ', '12345'), (12345.0, '12345'), ('12345.0', '12345'),
                          (0, '0'), ('0', '0'), (150000, '150000'), (18981.9, '18981.9'),
                          ('₹1,00,000.50', '100000.50'), (Decimal('5.25'), '5.25'),
                          ('1,234,567', '1234567'), ('12,34,567', '1234567'), ('', None),
                          ('  ', None), (None, None), ('₹', None)]:
            with self.subTest(raw=raw):
                got = parse_amount(raw)
                self.assertEqual(got, None if want is None else Decimal(want))

    def test_refused(self):
        for raw in ['abc', 'Rs 500', '-5', -5, '1.234', 1.234, '1500,50', '1,5,0000',
                    '1.5E+05', 'NaN', True, '12 thousand', '--5']:
            with self.subTest(raw=raw):
                with self.assertRaises(ValidationError):
                    parse_amount(raw)


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------

class ReadWorkbookTests(ImportBase):

    def test_not_xlsx_refused(self):
        path = self.tmp / 'amounts.csv'
        path.write_text('project_code,M1_new\n', encoding='utf-8')
        with self.assertRaisesMessage(CommandError, 'is not an .xlsx file'):
            self.run_import(str(path))

    def test_unreadable_xlsx_refused(self):
        path = self.tmp / 'broken.xlsx'
        path.write_text('not a workbook', encoding='utf-8')
        with self.assertRaisesMessage(CommandError, 'could not be read as an Excel workbook'):
            self.run_import(str(path))

    def test_missing_header_refused(self):
        path = self.sheet([], headers=('project_code', 'M1_new', 'M2_new', 'finance_note'))
        with self.assertRaisesMessage(CommandError, 'missing column(s) M3_new'):
            self.run_import(path)

    def test_columns_matched_by_name_not_position(self):
        p = self.site(contract=None)
        headers = ('finance_note', 'M3_new', 'Extra', 'm1_new', ' M2_NEW ', 'project_code')
        path = self.sheet([{'project_code': p.project_id, 'm1_new': 1, ' M2_NEW ': 2,
                            'M3_new': 3, 'finance_note': 'ok', 'Extra': 'x'}],
                          headers=headers)
        rows = read_workbook(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].code, rows[0].raw, rows[0].note),
                         (p.project_id, {'M1': 1, 'M2': 2, 'M3': 3}, 'ok'))

    def test_blank_rows_dropped(self):
        p = self.site()
        path = self.sheet([{}, self.row(p, m1=1), {}])
        self.assertEqual([r.line for r in read_workbook(path)], [3])


# ---------------------------------------------------------------------------
# The import
# ---------------------------------------------------------------------------

class DryRunTests(ImportBase):

    def test_dry_run_writes_nothing_and_lists_planned_sets(self):
        p = self.site()
        path = self.sheet([self.row(p, m1='100000', m2=150000, m3=50000)])
        logs = ActivityLog.objects.count()
        out = self.run_import(path)
        self.assertIn('MODE: DRY RUN', out)
        self.assertIn(f'{p.project_id} | M1 | would set 100000', out)
        self.assertIn(f'{p.project_id} | M2 | would set 150000', out)
        self.assertIn('WOULD SET 3, ALREADY SET 0, NOT FILLED 0, CONFLICTS 0, ERRORS 0, '
                      'WARNINGS 0, ROWS 1', out)
        self.assertIn('Still without amount (if applied): 0', out)
        self.assertEqual(self.amounts(p), [None, None, None])
        self.assertEqual(ActivityLog.objects.count(), logs)

    def test_dry_run_with_errors_reports_and_does_not_raise(self):
        path = self.sheet([{'project_code': 'HRP-RES-2099-999', 'M1_new': 1}])
        out = self.run_import(path)
        self.assertIn('HRP-RES-2099-999 | - | error: unknown project code', out)
        self.assertIn('--apply writes nothing until every error is fixed', out)


class ApplyTests(ImportBase):

    def test_sets_only_blank_amounts_conflict_and_already_set(self):
        p = self.site(contract='300000', amounts=('100000', '150000', None))
        path = self.sheet([self.row(p, m1='100000', m2='140000', m3='50000')])
        out = self.apply(path)
        self.assertIn(f'{p.project_id} | M1 | already set', out)
        self.assertIn(f'{p.project_id} | M2 | conflict: has 150000, sheet 140000', out)
        self.assertIn(f'{p.project_id} | M3 | set 50000', out)
        self.assertIn('SET 1, ALREADY SET 1, NOT FILLED 0, CONFLICTS 1, ERRORS 0', out)
        self.assertEqual(self.amounts(p), [Decimal('100000'), Decimal('150000'),
                                           Decimal('50000')])

    def test_blank_cells_not_filled_partial_sheet_applies(self):
        p = self.site()
        q = self.site()
        path = self.sheet([self.row(p, m2='75000'), self.row(q)])
        out = self.apply(path)
        self.assertIn(f'{p.project_id} | M1 | not filled', out)
        self.assertIn(f'{p.project_id} | M2 | set 75000', out)
        self.assertIn(f'{q.project_id} | M3 | not filled', out)
        self.assertIn('SET 1, ALREADY SET 0, NOT FILLED 5, CONFLICTS 0, ERRORS 0, '
                      'WARNINGS 0, ROWS 2', out)
        self.assertIn('Still without amount: 5', out)
        self.assertEqual(self.amounts(p), [None, Decimal('75000'), None])

    def test_zero_allowed_and_counts_towards_contract(self):
        p = self.site(contract='250000')
        out = self.apply(self.sheet([self.row(p, m1=0, m2='200000', m3='50000')]))
        self.assertIn('ERRORS 0', out)
        self.assertEqual(self.amounts(p), [Decimal('0'), Decimal('200000'), Decimal('50000')])

    def test_contract_rule_skipped_when_site_will_not_hold_all_three(self):
        p = self.site(contract='300000')
        out = self.apply(self.sheet([self.row(p, m1='999999', m2='1')]))
        self.assertIn('SET 2', out)
        self.assertIn('ERRORS 0', out)

    def test_contract_rule_uses_stored_amounts_and_conflicts_keep_stored(self):
        # Stored M1 100000; sheet says 90000 for M1 (conflict, keeps 100000) and fills
        # M2 + M3 to 200000: the site ends at 300000 = contract.
        p = self.site(contract='300000', amounts=('100000', None, None))
        out = self.apply(self.sheet([self.row(p, m1='90000', m2='150000', m3='50000')]))
        self.assertIn('CONFLICTS 1, ERRORS 0', out)
        self.assertEqual(self.amounts(p), [Decimal('100000'), Decimal('150000'),
                                           Decimal('50000')])

    def test_second_apply_sets_nothing(self):
        p = self.site()
        path = self.sheet([self.row(p, m1='100000', m2='150000', m3='50000')])
        self.apply(path)
        out = self.apply(path)
        self.assertIn('SET 0, ALREADY SET 3, NOT FILLED 0, CONFLICTS 0', out)
        self.assertIn('ActivityLog rows written: 0 (expected 0)', out)
        self.assertEqual(len(self.imported_logs()), 3)

    def test_amount_set_between_plan_and_write_is_kept(self):
        # A BD or PM save landing after the plan: the locked re-read sees it and the item
        # becomes a conflict, never an overwrite.
        p = self.site()
        plans = plan_import(read_workbook(self.sheet([self.row(p, m1='100000')])))
        PaymentMilestone.objects.filter(project=p, milestone_name='M1').update(
            amount=Decimal('123'))
        self.assertEqual(apply_plan(plans, self.finance), 0)
        self.assertEqual(plans[0].items[0].outcome, CONFLICT)
        self.assertEqual(self.amounts(p)[0], Decimal('123'))
        self.assertEqual(self.imported_logs(), [])


class ErrorTests(ImportBase):
    """Each error refuses its row; any error refuses the whole --apply."""

    def assert_refused(self, rows, expected):
        good = self.site(name='Good')
        path = self.sheet([self.row(good, m1='1')] + rows)
        out = self.run_import(path)
        self.assertIn(expected, out)
        logs = ActivityLog.objects.count()
        with self.assertRaisesMessage(CommandError, 'Nothing was written'):
            self.apply(path)
        self.assertEqual(self.amounts(good), [None, None, None])
        self.assertEqual(ActivityLog.objects.count(), logs)
        return out

    def test_unknown_code(self):
        self.assert_refused([{'project_code': 'HRP-RES-2099-001', 'M1_new': 5}],
                            'HRP-RES-2099-001 | - | error: unknown project code')

    def test_out_of_scope_sites(self):
        for kwargs, reason in [({'is_deleted': True}, 'site is deleted'),
                               ({'project_type': 'OPEX'}, 'OPEX site'),
                               ({'is_test': True}, 'test site'),
                               ({'status': 'Cancelled'}, 'site is Cancelled'),
                               ({'status': 'Draft', 'milestones': False}, DRAFT_MESSAGE)]:
            with self.subTest(**kwargs):
                p = self.site(**kwargs)
                self.assert_refused([self.row(p, m1='5')],
                                    f'{p.project_id} | - | error: {reason}')

    def test_draft_message_is_the_ruled_text(self):
        self.assertEqual(DRAFT_MESSAGE, 'Draft site — BD sets amounts at Draft')

    def test_milestone_record_missing(self):
        p = self.site(milestones=False)
        self.assert_refused([self.row(p, m2='5')],
                            f'{p.project_id} | M2 | error: no milestone record on this site')

    def test_bad_amounts(self):
        for raw, message in [('-5', 'cannot be negative'),
                             ('1.234', 'more than 2 decimal places'),
                             ('abc', 'is not a number')]:
            with self.subTest(raw=raw):
                p = self.site()
                self.assert_refused([self.row(p, m3=raw)], message)

    def test_duplicate_code(self):
        p = self.site()
        out = self.assert_refused([self.row(p, m1='5'), self.row(p, m2='6')],
                                  f'{p.project_id} appears 2 times in the file')
        self.assertEqual(out.count('appears 2 times'), 2)

    def test_contract_value_mismatch_shows_figures(self):
        p = self.site(contract='180000')
        self.assert_refused(
            [self.row(p, m1='36000', m2='126000', m3='20000')],
            'error: M1 + M2 + M3 must equal the contract value 180000; they would total '
            '182000, a difference of 2000')

    def test_note_too_long(self):
        p = self.site()
        self.assert_refused([self.row(p, m1='5', note='x' * 151)],
                            f'{p.project_id} | - | error: note too long: 151 characters, '
                            f'limit 150')

    def test_note_at_limit_applies_and_is_logged_whole(self):
        p = self.site(contract=None)
        note = 'n' * NOTE_LIMIT
        out = self.apply(self.sheet([self.row(p, m1='5', note=note)]))
        self.assertIn('SET 1', out)
        self.assertIn('ERRORS 0', out)
        self.assertEqual(self.imported_logs()[0].action,
                         f'M1: amount blank → ₹5 (spreadsheet import; note: {note})')

    def test_refused_apply_reports_set_zero(self):
        good = self.site()
        path = self.sheet([self.row(good, m1='1'), {'project_code': 'NOPE', 'M1_new': 1}])
        out = StringIO()
        with self.assertRaises(CommandError):
            call_command('import_milestone_amounts', path, '--apply', '--actor', 'ab_fin',
                         stdout=out)
        self.assertIn('SET 0,', out.getvalue())


class WarningTests(ImportBase):

    def test_below_amount_received_warns_and_applies(self):
        p = self.site(contract='300000', received=(None, None, '60000'))
        out = self.apply(self.sheet([self.row(p, m1='100000', m2='150000', m3='50000')]))
        self.assertIn(f'{p.project_id} | M3 | warning: new amount 50000 is below the '
                      f'60000 already received', out)
        self.assertIn('WARNINGS 1', out)
        self.assertEqual(self.amounts(p)[2], Decimal('50000'))

    def test_contract_value_missing_warns_and_applies(self):
        p = self.site(contract=None)
        out = self.apply(self.sheet([self.row(p, m1='1', m2='2', m3='3')]))
        self.assertIn(f'{p.project_id} | - | warning: contract value not set; total 6 not '
                      f'checked', out)
        self.assertEqual(self.amounts(p), [Decimal('1'), Decimal('2'), Decimal('3')])


class ActorTests(ImportBase):

    def setUp(self):
        super().setUp()
        self.p = self.site()
        self.path = self.sheet([self.row(self.p, m1='5')])

    def test_apply_needs_actor(self):
        with self.assertRaisesMessage(CommandError, '--apply requires --actor'):
            self.run_import(self.path, '--apply')
        self.assertEqual(self.amounts(self.p), [None, None, None])

    def test_actor_refused(self):
        _profile('ab_bd_actor', 'BD')
        _profile('ab_scm', 'SCM')
        _profile('ab_fin_off', 'Finance', active=False)
        _profile('ab_fin_nologin', 'Finance', login_active=False)
        for username, message in [('nobody', 'no such user'),
                                  ('ab_bd_actor', 'may not import'),
                                  ('ab_pm', 'may not import'),
                                  ('ab_scm', 'may not import'),
                                  ('ab_fin_off', 'profile inactive'),
                                  ('ab_fin_nologin', 'login inactive')]:
            with self.subTest(username=username):
                with self.assertRaisesMessage(CommandError, message):
                    self.apply(self.path, actor=username)
                self.assertEqual(self.amounts(self.p), [None, None, None])
                self.assertEqual(self.imported_logs(), [])

    def test_actor_allowed(self):
        self.assertTrue(user_can_import_milestone_amounts(self.finance.user))
        self.assertTrue(user_can_import_milestone_amounts(self.ceo.user))
        for role in ('Admin', 'System Admin'):
            self.assertTrue(user_can_import_milestone_amounts(
                _profile(f'ab_{role.lower().replace(" ", "")}', role).user))
        self.assertFalse(user_can_import_milestone_amounts(self.bd_a.user))
        self.apply(self.path, actor='ab_ceo')
        self.assertEqual(self.amounts(self.p)[0], Decimal('5'))


class LogTests(ImportBase):

    def test_one_line_per_written_milestone(self):
        p = self.site(contract='300000', amounts=('100000', None, None))
        q = self.site(contract=None)
        path = self.sheet([self.row(p, m2='150000.50', m3='49999.50',
                                    note='per final proposal v2'),
                           self.row(q, m1='1000')])
        out = self.apply(path)
        self.assertIn('ActivityLog rows written: 3 (expected 3)', out)
        logs = self.imported_logs()
        self.assertEqual([(l.project_id, l.action) for l in logs], [
            (p.pk, 'M2: amount blank → ₹1,50,000.50 (spreadsheet import; note: per final '
                   'proposal v2)'),
            (p.pk, 'M3: amount blank → ₹49,999.50 (spreadsheet import; note: per final '
                   'proposal v2)'),
            (q.pk, 'M1: amount blank → ₹1,000 (spreadsheet import)'),
        ])
        m2 = PaymentMilestone.objects.get(project=p, milestone_name='M2')
        self.assertEqual((logs[0].entity_type, logs[0].entity_id), ('Milestone', m2.pk))
        self.assertTrue(all(l.actor_id == self.finance.pk for l in logs))
        # No other milestone code is written by the import.
        self.assertFalse(ActivityLog.objects.exclude(action_code=IMPORTED_CODE)
                         .filter(entity_type='Milestone').exists())

    def test_code_is_money_and_not_site_activity(self):
        self.assertIn(IMPORTED_CODE, MONEY_ACTIVITY_CODES)
        self.assertIn(IMPORTED_CODE, NON_ACTIVITY_CODES)
        self.assertFalse(is_human_activity(IMPORTED_CODE))


class NothingElseMovesTests(ImportBase):

    def test_status_received_dates_tasks_ledger_notifications_unchanged(self):
        p = self.site(contract='300000', received=('100000', None, None))
        phase = ProjectPhase.objects.create(project=p, phase_name='Finance', phase_order=1)
        Task.objects.create(phase=phase, task_name='Advance Payment Confirmation',
                            task_order=1, assigned_role=Task.BD)

        def state():
            return (list(PaymentMilestone.objects.filter(project=p).order_by('pk')
                         .values_list('status', 'amount_received', 'received_date',
                                      'invoice_date', 'invoice_amount', 'invoice_number',
                                      'due_date', 'variance_reason',
                                      'milestone_description')),
                    list(Task.objects.values_list('pk', 'status', 'due_date',
                                                  'assigned_to_id')),
                    StatusTransition.objects.count(), Notification.objects.count(),
                    NotificationLog.objects.count())

        before = state()
        self.apply(self.sheet([self.row(p, m1='100000', m2='150000', m3='50000')]))
        self.assertEqual(self.amounts(p), [Decimal('100000'), Decimal('150000'),
                                           Decimal('50000')])
        self.assertEqual(state(), before)

    def test_imported_amount_not_listed_as_changed_after_received(self):
        # Ruling Q1: the Finance Head check reads milestone_amounts_set only, so Finance's
        # first fill of a Received milestone's blank amount is not "changed".
        # The receipt is on the ledger a day ago, so a later amount line WOULD be read as a
        # change; the control below proves the check sees one under the old code.
        p = self.site(contract='300000', received=('100000', None, None))
        m1 = PaymentMilestone.objects.get(project=p, milestone_name='M1')
        StatusTransition.objects.create(
            subject_type=SUBJECT_PAYMENT_MILESTONE, subject_id=m1.pk, project=p,
            from_status=PaymentMilestone.PENDING, to_status=PaymentMilestone.RECEIVED,
            actor=self.finance, actor_role_code='Finance',
            occurred_at=timezone.now() - timedelta(days=1))
        self.apply(self.sheet([self.row(p, m1='100000', m2='150000', m3='50000')]))
        checks = {c['key']: c for c in trust_checks()}
        self.assertEqual(checks['changed_after_received']['count'], 0)

        log_activity(p, self.pm, 'M1: amount ₹1,00,000 → ₹1,10,000', entity_type='Milestone',
                     entity_id=m1.pk, action_code='milestone_amounts_set')
        checks = {c['key']: c for c in trust_checks()}
        self.assertEqual(list(checks['changed_after_received']['items']),
                         [f'milestone-{m1.pk}'])
