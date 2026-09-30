"""
seed_walkthrough's `approvals` area (D-A26) — every material-approval state, WALK-01 ..
WALK-14, and every contractor-bill state, WALK-15 .. WALK-22 (4b-2), on the synthetic
walkthrough database.

What this pins, and why each earns a test:

  * every scenario reaches the state the walk script says it does — status, round, and
    whose turn it is — and each marker the page draws is really there: the proxy step
    and its stub evidence file (WALK-05), the kept approval (WALK-07), the deputy's
    decision (WALK-10), the reassignment (WALK-09), the removed link (WALK-12), the
    pre-dispatch record and pre-order link (WALK-11);
  * the bills went through the real views: the contractor made one through vendor_edit,
    its site worked to Done-and-approved, every PDF and photo a stub row, the replaced PDF
    (WALK-18), the kept confirmation (WALK-19), the shared task raised anyway (WALK-22),
    the Site Engineer's card — and a Site Engineer's page shows none of the money;
  * the figures the walk script prints for the aging page, computed without
    approval_queries, equal approval_queries.aging_rows() — and equal the numbers the
    scenario times were chosen to give;
  * every state went through approvals.py: each request has one round snapshot per round
    and a ledger ending at its status, and no history runs backwards;
  * every notification went to a walk.* account; the two new accounts are SCM and PM,
    .invalid, with both preferences off;
  * a second run writes nothing and prints the same script; the teardown refuses the
    area (its ledger rows are append-only).

The guard's database-name check is met the way tests_walkthrough_seed meets it: by
patching `_walkthrough_support.database_name` (its `_run`), on the SQLite test database.
"""
import re
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from projects import models as m
from projects.approval_queries import AGING_WINDOW_DAYS, aging_rows, work_to_confirm_card
from projects.approvals import round_snapshot
from projects.management.commands._walkthrough_support import (
    STUB_BUCKET, STUB_FILE_PREFIX, AreaManifest,
)
from projects.management.commands.seed_walkthrough import (
    APPROVAL_RECORDS, APPROVAL_USERS, BILL_CONTRACTOR, BILL_DONE_TASKS, BILL_SITE,
    BILL_TENDER, WALK_AGING_WINDOW_DAYS, WALK_PASSWORD, WALK_SCRIPT,
    independent_aging_figures,
)
# Helpers only. Importing that module's TestCase classes by name would make this module
# collect and run them a second time.
from projects.tests_walkthrough_seed import (
    _TempManifestMixin, _build_migrated_reference_data, _row_counts, _run,
)

#: code -> (kind, status, current round, {(party, assignee username)} pending now).
EXPECTED = {
    'WALK-01': ('material_pre_order',    'open',      1, {('pm', 'walk.pm')}),
    'WALK-02': ('material_pre_order',    'open',      1, {('design', 'walk.designhead')}),
    'WALK-03': ('material_pre_order',    'approved',  1, set()),
    'WALK-04': ('material_pre_order',    'rejected',  1, set()),
    'WALK-05': ('material_pre_order',    'approved',  1, set()),
    'WALK-06': ('material_pre_order',    'open',      2, {('pm', 'walk.pm')}),
    'WALK-07': ('material_pre_order',    'open',      2, {('design', 'walk.designhead')}),
    'WALK-08': ('material_pre_order',    'withdrawn', 1, set()),
    'WALK-09': ('material_pre_order',    'open',      1, {('pm', 'walk.pm2')}),
    'WALK-10': ('material_pre_order',    'approved',  1, set()),
    'WALK-11': ('material_pre_dispatch', 'open',      1, {('pm', 'walk.pm')}),
    'WALK-12': ('material_pre_order',    'approved',  1, set()),
    'WALK-13': ('material_pre_order',    'open',      1, {('pm', 'walk.pm')}),
    'WALK-14': ('material_pre_order',    'approved',  1, set()),
    'WALK-15': ('contractor_bill',       'open',      1, {('site_engineer', 'walk.se')}),
    'WALK-16': ('contractor_bill',       'open',      1, {('pm', 'walk.pm')}),
    'WALK-17': ('contractor_bill',       'approved',  1, set()),
    'WALK-18': ('contractor_bill',       'open',      2, {('site_engineer', 'walk.se')}),
    'WALK-19': ('contractor_bill',       'open',      2, {('pm', 'walk.pm')}),
    'WALK-20': ('contractor_bill',       'rejected',  1, set()),
    'WALK-21': ('contractor_bill',       'withdrawn', 1, set()),
    'WALK-22': ('contractor_bill',       'open',      1, {('site_engineer', 'walk.se')}),
}

#: What the scenario times were chosen to give on the aging page, per assignee:
#: (pending, oldest days, decisions, median days, proxies, decided by deputy).
#: walk.pm's eight material turnarounds are 2, 1, 0.5, 1.5, 1.25, 0.25, 1 and 3 days, and
#: its three bill turnarounds — from the Site Engineer's confirmation, the steps being
#: sequential — 1.75 (WALK-17), 0.5 (WALK-19) and 1 (WALK-20): median 1.0. walk.se's five
#: are 2, 0.25, 1, 0.5 and 1 (median 1.0), WALK-18's "work not done" among them. The
#: Head's are 3, 2 and 2 — the last by the deputy.
EXPECTED_AGING = {
    'walk.pm':         (6, 20, 11, 1.0, 1, 0),
    'walk.se':         (3, 7, 5, 1.0, 0, 0),
    'walk.designhead': (2, 6, 3, 2.0, 0, 1),
    'walk.pm2':        (1, 5, 0, None, 0, None),
}

#: code -> (task names in the bill's order, amount) as the bill stands now.
EXPECTED_BILLS = {
    'WALK-15': (['AC Cable Laying'], '48500.00'),
    'WALK-16': (['Inverter Installation'], '112000.00'),
    'WALK-17': (['Civil Work and MMS Installation', 'Module Installation'], '235750.50'),
    'WALK-18': (['LA and Earthing Installation', 'DC Cable Laying with Conduit'], '64000.00'),
    'WALK-19': (['DCDB and ACDB Installation'], '86500.00'),
    'WALK-20': (['RMS Installation'], '310000.00'),
    'WALK-21': (['Solar Generation Meter Installation'], '22000.00'),
    'WALK-22': (['Testing & Commissioning', 'Module Installation'], '55000.00'),
}


class ApprovalsAreaTests(_TempManifestMixin, TestCase):

    @classmethod
    def setUpTestData(cls):
        _build_migrated_reference_data()
        cls.manifest = cls._temp_manifest()
        cls.output = _run('seed_walkthrough', only=['approvals'], manifest=cls.manifest)
        cls.approvals = {a.title.split()[0]: a for a in m.ApprovalRequest.objects.all()}

    def _steps(self, code, **filters):
        return m.ApprovalStep.objects.filter(request=self.approvals[code], **filters)

    # ---- every scenario's state ---------------------------------------------
    def test_only_the_approvals_area_and_its_dependencies_were_seeded(self):
        self.assertEqual(set(AreaManifest.load(self.manifest).areas),
                         {'users', 'reference', 'approvals'})

    def test_twenty_two_scenarios_each_at_its_status_round_and_turn(self):
        self.assertEqual(sorted(self.approvals), sorted(EXPECTED))
        for code, (kind, status, current_round, waiting) in EXPECTED.items():
            with self.subTest(code=code):
                approval = self.approvals[code]
                self.assertEqual((approval.kind, approval.status, approval.current_round),
                                 (kind, status, current_round))
                # Whose turn it is: pending AND due. A bill's PM step is pending but not
                # yet due while the Site Engineer's is open (the steps are sequential).
                pending = set(self._steps(code, verdict='pending', round=current_round,
                                          activated_at__isnull=False)
                              .values_list('party', 'assignee__user__username'))
                self.assertEqual(pending, waiting)
                lines = m.MaterialApprovalLine.objects.filter(detail__request=approval)
                if kind == 'contractor_bill':
                    self.assertFalse(lines.exists())
                else:
                    # 1-3 material lines, every one on the unit vocabulary.
                    self.assertTrue(1 <= lines.count() <= 3)

    def test_walk_02_the_pm_approved_and_design_is_waiting(self):
        self.assertTrue(self._steps('WALK-02', party='pm', verdict='approved').exists())
        self.assertEqual(self.approvals['WALK-02'].raised_by.user.username, 'walk.scm2')

    def test_walk_03_approved_by_the_pm_after_one_day_and_the_head_after_three(self):
        pm = self._steps('WALK-03', party='pm').get()
        head = self._steps('WALK-03', party='design').get()
        self.assertEqual(round((pm.decided_at - pm.activated_at) / timedelta(days=1), 2), 1)
        self.assertEqual(round((head.decided_at - head.activated_at) / timedelta(days=1), 2), 3)
        self.assertEqual(head.decided_by.user.username, 'walk.designhead')

    def test_walk_04_rejected_with_a_note(self):
        step = self._steps('WALK-04', party='pm').get()
        self.assertEqual(step.verdict, 'rejected')
        self.assertIn('approved list', step.note)

    def test_walk_05_proxy_with_metadata_only_evidence(self):
        step = self._steps('WALK-05', party='pm').get()
        self.assertTrue(step.is_proxy)
        self.assertEqual(step.proxy_channel, m.APPROVAL_PROXY_PHONE)
        self.assertEqual(step.decided_by.user.username, 'walk.pm')
        self.assertEqual(step.recorded_by.user.username, 'walk.scm')
        self.assertIn('phone call', step.proxy_evidence)
        evidence = m.ApprovalAttachment.objects.get(step=step)
        self.assertEqual(evidence.bucket, STUB_BUCKET)
        self.assertTrue(evidence.file_name.startswith(STUB_FILE_PREFIX))

    def test_walk_06_resubmitted_by_scm2_with_one_quantity_changed(self):
        approval = self.approvals['WALK-06']
        one = round_snapshot(approval, 1)['material']['lines']
        two = round_snapshot(approval, 2)['material']['lines']
        self.assertEqual([(line['quantity'], line['unit']) for line in one],
                         [('40.00', 'Meter'), ('12', 'Set')])
        self.assertEqual([(line['quantity'], line['unit']) for line in two],
                         [('48.00', 'Meter'), ('12', 'Set')])
        resubmit = m.StatusTransition.objects.get(
            subject_type='approval_request', subject_id=approval.pk,
            reason_code=m.REASON_RESUBMITTED)
        self.assertEqual(resubmit.actor.user.username, 'walk.scm2')
        self.assertEqual(self._steps('WALK-06', round=1, party='pm').get().verdict,
                         'changes_requested')

    def test_walk_07_keeps_the_pm_approval_with_a_reason(self):
        kept = self._steps('WALK-07', round=2, party='pm').get()
        self.assertEqual(kept.verdict, 'approved')
        self.assertIsNotNone(kept.carried_from_id)
        self.assertEqual(kept.carried_from.round, 1)
        self.assertGreaterEqual(len(re.sub(r'\s', '', kept.carry_reason)), 15)
        self.assertEqual(self._steps('WALK-07', round=1, party='design').get().verdict,
                         'changes_requested')

    def test_walk_08_withdrawn_with_a_note_and_nobody_left_waiting(self):
        approval = self.approvals['WALK-08']
        self.assertIn('lot 4', approval.withdrawal_note)
        step = self._steps('WALK-08').get()
        self.assertEqual((step.verdict, step.superseded_by.user.username),
                         ('superseded', 'walk.scm'))

    def test_walk_09_reassigned_from_pm_to_pm2_with_a_note(self):
        old = self._steps('WALK-09', verdict='superseded').get()
        new = self._steps('WALK-09', verdict='pending').get()
        self.assertEqual(old.assignee.user.username, 'walk.pm')
        self.assertEqual(old.superseded_by.user.username, 'walk.scm2')
        self.assertIn('on leave', old.note)
        self.assertEqual(new.assignee.user.username, 'walk.pm2')

    def test_walk_10_design_decided_by_the_deputy_not_the_head(self):
        step = self._steps('WALK-10', party='design').get()
        self.assertEqual(step.assignee.user.username, 'walk.designhead')
        self.assertEqual(step.decided_by.user.username, 'walk.designdeputy')

    def test_walk_11_pre_dispatch_against_m1_linked_to_walk_03(self):
        detail = self.approvals['WALK-11'].material_detail
        self.assertEqual(detail.vendor_order.po_number, 'WALK-APO-M1')
        self.assertEqual(detail.pre_order_request, self.approvals['WALK-03'])
        self.assertEqual(self.approvals['WALK-11'].vendor.name, 'WALK Modules Co')

    def test_walk_12_two_links_one_removed_with_a_reason(self):
        links = m.ApprovalOrderLink.objects.filter(approval=self.approvals['WALK-12'])
        self.assertEqual(
            {(link.vendor_order.po_number, link.removed_at is None) for link in links},
            {('WALK-APO-S1', True), ('WALK-APO-S2', False)})
        removed = links.get(removed_at__isnull=False)
        self.assertIn('next batch', removed.removal_note)

    def test_walk_13_is_the_oldest_waiting_and_walk_14_is_outside_the_window(self):
        now = timezone.now()
        oldest = (m.ApprovalStep.objects.filter(verdict='pending',
                                                activated_at__isnull=False,
                                                request__status='open')
                  .order_by('activated_at').first())
        self.assertEqual(oldest.request, self.approvals['WALK-13'])
        decided = self._steps('WALK-14', party='pm').get().decided_at
        self.assertLess(decided, now - timedelta(days=AGING_WINDOW_DAYS))
        others = m.ApprovalStep.objects.filter(decided_at__isnull=False).exclude(
            request=self.approvals['WALK-14'])
        self.assertFalse(others.filter(
            decided_at__lt=now - timedelta(days=AGING_WINDOW_DAYS)).exists())

    def test_four_po_pi_records_with_distinct_numbers_and_totals_and_no_payment(self):
        orders = m.VendorOrder.objects.filter(
            po_number__in=[po for _v, po, _t, _d in APPROVAL_RECORDS])
        self.assertEqual(orders.count(), 4)
        self.assertEqual(len(set(orders.values_list('total_amount', flat=True))), 4)
        self.assertFalse(m.PaymentRequest.objects.filter(vendor_order__in=orders).exists())


    # ---- the contractor bills (4b-2) ----------------------------------------
    def _bill(self, code):
        return m.ContractorBillDetail.objects.get(request=self.approvals[code])

    def _page(self, username, code):
        client = Client(SERVER_NAME='localhost')
        client.force_login(User.objects.get(username=username))
        response = client.get(reverse('approval_detail', args=[self.approvals[code].pk]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_contractor_was_made_one_through_vendor_edit(self):
        vendor = m.Vendor.objects.get(name=BILL_CONTRACTOR)
        self.assertEqual(vendor.kind, m.VENDOR_KIND_CONTRACTOR)
        self.assertEqual(vendor.created_by.user.username, 'walk.scm')
        for code in EXPECTED_BILLS:
            self.assertEqual(self.approvals[code].vendor, vendor, code)

    def test_the_bill_site_is_active_with_walk_se_on_its_tasks_and_ten_done_and_approved(self):
        site = m.Project.objects.get(project_id=BILL_SITE)
        self.assertEqual((site.status, site.program.short_tender_code,
                          site.assigned_pm.user.username), ('Active', BILL_TENDER[0], 'walk.pm'))
        se_tasks = m.Task.objects.filter(phase__project=site, assigned_role='Site Engineer',
                                         is_mirror=False)
        self.assertEqual(set(se_tasks.values_list('assigned_to__user__username', flat=True)),
                         {'walk.se'})
        done = se_tasks.filter(status=m.Task.DONE, approved_at__isnull=False)
        self.assertEqual(sorted(done.values_list('task_name', flat=True)),
                         sorted(BILL_DONE_TASKS))
        self.assertEqual(list(se_tasks.exclude(pk__in=done).values_list('task_name', flat=True)),
                         ['Net Meter Installation'])

    def test_each_bill_names_its_tasks_and_amount_and_a_stub_pdf(self):
        for code, (names, amount) in EXPECTED_BILLS.items():
            with self.subTest(code=code):
                bill = self._bill(code)
                self.assertEqual(bill.project.project_id, BILL_SITE)
                self.assertEqual([link.task.task_name
                                  for link in bill.task_links.order_by('pk')], names)
                self.assertEqual(format(bill.amount, '.2f'), amount)
                self.assertEqual(bill.pdf_bucket, STUB_BUCKET)
                self.assertTrue(bill.pdf_file_name.startswith(STUB_FILE_PREFIX))

    def test_every_site_engineer_confirmation_carries_a_stub_photo(self):
        confirmed = m.ApprovalStep.objects.filter(
            request__kind='contractor_bill', party='site_engineer', verdict='approved',
            carried_from__isnull=True)
        self.assertEqual(set(a.title.split()[0] for a in m.ApprovalRequest.objects.filter(
            steps__in=confirmed)), {'WALK-16', 'WALK-17', 'WALK-19', 'WALK-20'})
        for step in confirmed:
            photos = list(step.evidence_files.all())
            self.assertEqual(len(photos), 1)
            self.assertEqual(photos[0].bucket, STUB_BUCKET)
            self.assertTrue(photos[0].file_name.startswith(STUB_FILE_PREFIX))
            self.assertTrue(photos[0].file_name.endswith('.jpg'))

    def test_walk_18_work_not_done_then_a_task_added_and_the_pdf_replaced(self):
        not_done = self._steps('WALK-18', round=1, party='site_engineer').get()
        self.assertEqual(not_done.verdict, 'changes_requested')
        self.assertIsNone(self._steps('WALK-18', round=2, party='site_engineer')
                          .get().carried_from)
        one = round_snapshot(self.approvals['WALK-18'], 1)['bill']
        two = round_snapshot(self.approvals['WALK-18'], 2)['bill']
        self.assertEqual([t['task_name'] for t in one['tasks']],
                         ['LA and Earthing Installation'])
        self.assertEqual([t['task_name'] for t in two['tasks']],
                         EXPECTED_BILLS['WALK-18'][0])
        self.assertNotEqual(one['pdf']['path'], two['pdf']['path'])
        self.assertEqual(two['pdf']['path'], self._bill('WALK-18').pdf_path)
        self.assertEqual(one['amount'], two['amount'])

    def test_walk_19_kept_the_site_engineer_and_changed_only_the_amount(self):
        kept = self._steps('WALK-19', round=2, party='site_engineer').get()
        self.assertIsNotNone(kept.carried_from)
        self.assertTrue(kept.carry_reason.startswith('The work is unchanged'))
        one = round_snapshot(self.approvals['WALK-19'], 1)['bill']
        two = round_snapshot(self.approvals['WALK-19'], 2)['bill']
        self.assertEqual((one['amount'], two['amount']), ('90000.00', '86500.00'))
        self.assertEqual(one['tasks'], two['tasks'])
        self.assertEqual(one['pdf'], two['pdf'])

    def test_walk_20_rejected_and_walk_21_withdrawn(self):
        self.assertEqual(self._steps('WALK-20', party='pm').get().verdict, 'rejected')
        self.assertEqual(set(self._steps('WALK-21').values_list('verdict', flat=True)),
                         {'superseded'})

    def test_walk_22_shares_a_task_with_walk_17_and_draws_its_warning(self):
        shared = set(self._bill('WALK-22').task_links.values_list('task_id', flat=True)) & set(
            self._bill('WALK-17').task_links.values_list('task_id', flat=True))
        self.assertEqual([m.Task.objects.get(pk=pk).task_name for pk in shared],
                         ['Module Installation'])
        self.assertIn('is also on bill WSW/26-27/017', self._page('walk.scm', 'WALK-22'))

    def test_the_site_engineers_card_lists_their_three_bills_oldest_first(self):
        card = work_to_confirm_card(User.objects.get(username='walk.se'))
        self.assertEqual([row['title'].split()[0] for row in card['rows']],
                         ['WALK-18', 'WALK-22', 'WALK-15'])

    def test_the_site_engineers_pages_show_the_work_and_none_of_the_money(self):
        money = ('WSW/26-27', '₹', 'SEEDED-NO-FILE-bill', 'File unavailable')
        for code in ('WALK-15', 'WALK-18', 'WALK-19', 'WALK-22'):
            with self.subTest(code=code):
                page = self._page('walk.se', code)
                for name in EXPECTED_BILLS[code][0]:
                    self.assertIn(name.replace('&', '&amp;'), page)
                for text in money + ('also on bill',):
                    self.assertNotIn(text, page)
        self.assertIn('No change to the work in this round.', self._page('walk.se', 'WALK-19'))
        # SCM reads the same bill in full, its PDF row unavailable (seeded, no file).
        page = self._page('walk.scm', 'WALK-18')
        self.assertIn('WSW/26-27/018', page)
        self.assertIn('File unavailable', page)

    # ---- the aging figures ----------------------------------------------------
    def test_the_window_is_restated_not_imported_and_agrees(self):
        self.assertEqual(WALK_AGING_WINDOW_DAYS, AGING_WINDOW_DAYS)

    def test_independent_figures_equal_approval_queries(self):
        now = timezone.now()
        mine = independent_aging_figures(now)
        page = aging_rows(now - timedelta(days=AGING_WINDOW_DAYS), now=now)
        self.assertEqual(
            [(e['username'], e['pending_count'], e['oldest_days'], e['decisions'],
              e['median_days'], e['proxy_share'], e['by_deputy'])
             for e in mine['assignees']],
            [(e['profile'].user.username, e['pending_count'], e['oldest_days'],
              e['decisions'], e['median_days'], e['proxy_share'], e['by_deputy'])
             for e in page['assignees']])
        self.assertEqual((mine['carried'], mine['fresh_approved'], mine['carry_rate']),
                         (page['carried'], page['fresh_approved'], page['carry_rate']))
        self.assertEqual(mine['total_pending'],
                         sum(e['pending_count'] for e in page['assignees']))

    def test_the_figures_are_the_ones_the_scenario_times_were_chosen_to_give(self):
        figures = independent_aging_figures(timezone.now())
        self.assertEqual(figures['total_pending'], 12)
        # Carried: WALK-07's PM approval and WALK-19's Site Engineer confirmation.
        self.assertEqual((figures['carried'], figures['fresh_approved'],
                          figures['carry_rate']), (2, 0, 1.0))
        got = {e['username']: (e['pending_count'], e['oldest_days'], e['decisions'],
                               e['median_days'], e['proxies'], e['by_deputy'])
               for e in figures['assignees']}
        self.assertEqual(got, EXPECTED_AGING)
        self.assertEqual([e['username'] for e in figures['assignees']],
                         ['walk.pm', 'walk.se', 'walk.designhead', 'walk.pm2'])

    # ---- through the chokepoint ---------------------------------------------
    def test_every_round_has_its_snapshot_and_every_ledger_ends_at_the_status(self):
        for code, approval in self.approvals.items():
            with self.subTest(code=code):
                self.assertEqual(
                    m.ApprovalRoundSnapshot.objects.filter(request=approval).count(),
                    approval.current_round)
                last = (m.StatusTransition.objects
                        .filter(subject_type='approval_request', subject_id=approval.pk)
                        .order_by('occurred_at', 'pk').last())
                self.assertEqual(last.to_status, approval.status)

    def test_no_history_runs_backwards_and_none_is_in_the_future(self):
        rows = list(m.StatusTransition.objects.filter(subject_type='approval_request')
                    .order_by('subject_id', 'pk')
                    .values_list('subject_id', 'occurred_at'))
        for previous, current in zip(rows, rows[1:]):
            if previous[0] == current[0]:
                self.assertLessEqual(previous[1], current[1], current[0])
        now = timezone.now()
        self.assertFalse(m.StatusTransition.objects.filter(occurred_at__gt=now).exists())
        self.assertFalse(m.ApprovalStep.objects.filter(decided_at__gt=now).exists())

    # ---- people and notifications -------------------------------------------
    def test_the_two_new_accounts(self):
        for username, _first, _last, role, _phone in APPROVAL_USERS:
            with self.subTest(username=username):
                user = User.objects.get(username=username)
                self.assertEqual(user.profile.role, role)
                self.assertTrue(user.email.endswith('.invalid'))
                self.assertFalse(user.profile.email_notifications)
                self.assertFalse(user.profile.whatsapp_notifications)

    def test_nothing_was_sent_on_an_external_channel(self):
        self.assertFalse(m.NotificationLog.objects.filter(
            channel__in=('email', 'whatsapp'), status='sent').exists())

    # ---- the walk script ----------------------------------------------------
    def test_the_walk_script_names_every_scenario_with_its_url_and_no_password(self):
        self.assertNotIn(WALK_PASSWORD, self.output)
        self.assertIn('Seed date:', self.output)
        self.assertIn('real clock', self.output)
        for code, login, _text in WALK_SCRIPT:
            url = reverse('approval_detail', args=[self.approvals[code].pk])
            self.assertIn(f'{code} | {login} | {url} |', self.output)
        self.assertIn('Total pending: 12', self.output)

    # ---- running again, and tearing down --------------------------------------
    def test_a_second_run_writes_nothing_and_prints_the_script_again(self):
        before = _row_counts()
        output = _run('seed_walkthrough', only=['approvals'], manifest=self.manifest)
        self.assertEqual(_row_counts(), before)
        self.assertIn('already present and complete', output)
        self.assertIn('WALK-13 | walk.pm |', output)

    def test_the_teardown_refuses_the_area_and_deletes_nothing(self):
        before = _row_counts()
        dry = _run('teardown_walkthrough', only=['approvals'], manifest=self.manifest,
                   dry_run=True)
        self.assertRegex(dry, r'approvals\s+REFUSED')
        self.assertEqual(_row_counts(), before)


class ApprovalNoticesTests(_TempManifestMixin, TransactionTestCase):
    """The notices, on a database that really commits.

    approvals.py sends every notice from a transaction.on_commit() callback. Inside
    TestCase nothing ever commits, so ApprovalsAreaTests sees none; here each view
    request and chokepoint call commits as it does on Postgres, so this proves the
    area's rule — commit inside each SimClock step — and that every notice went to a
    walk.* account. Slower (a flush per test), so it is one test.
    """

    def test_every_notice_went_to_a_walk_account_at_simulated_time(self):
        _build_migrated_reference_data()
        _run('seed_walkthrough', only=['approvals'], manifest=self._temp_manifest())
        notices = m.Notification.objects.filter(link__startswith='/approvals/')
        self.assertTrue(notices.exists(), 'the approval actions notify their approvers')
        self.assertFalse(m.Notification.objects.exclude(
            recipient__user__username__startswith='walk.').exists())
        now = timezone.now()
        # WALK-13 was raised 20 days ago: its notice to walk.pm was written then, inside
        # that clock step — not at the end of the run.
        walk_13 = m.ApprovalRequest.objects.get(title__startswith='WALK-13')
        raised = notices.get(recipient__user__username='walk.pm',
                             link__contains=f'/approvals/{walk_13.pk}/')
        self.assertLess(raised.created_at, now - timedelta(days=19))
        self.assertFalse(notices.filter(created_at__gt=now).exists())
