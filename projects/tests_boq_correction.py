"""
Session B verification — reviewer correction of a BOQ (quantities and new lines only).

WHY THIS FILE EXISTS
--------------------
Three things here can fail invisibly, and each has its own section below.

  * THE LOCK BYPASS IS SUPPOSED TO BE ONE-SIDED. `project_boq_is_design_locked()` is the
    freeze the designer's own paths take, and correction is exempt from it. If that exempt
    branch is ever reachable by the designer, the freeze is gone and nothing on any screen
    would look different — so the designer's three write paths are re-asserted as REFUSED
    from inside the very window the reviewer is writing in, by direct POST rather than by
    checking that a button is absent.

  * DELETE IS NOT PART OF THIS CAPABILITY. Removing a line is deferred, and "we did not
    build it" is not a test. Every delete surface a reviewer could reach is POSTed at
    directly: the correction endpoint (which has no such action), `boq_detail`'s
    `delete_item`, and the picker, whose save is a whole-sheet reconciliation and could
    therefore delete BY OMISSION if a reviewer were ever let into it.

  * THE RECORD MUST NOT BECOME REWORK. A reviewer fixing a number in place is the
    ALTERNATIVE to failing the attempt. If a BOQCorrection ever reached the attempt count
    or the rework predicate, the designer would be charged for the loop that was avoided,
    which is worse than not recording it at all.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    Program, Project, UserProfile, BOQ, BOQItem, BOQItemMaster, BOQRevision,
    BOQCorrection, BOQ_CORRECTION_LINE_ADDED, BOQ_CORRECTION_QUANTITY_CHANGED,
    DesignAssignment, DesignAttempt,
    DESIGN_IN_DESIGN, DESIGN_IN_QC,
    ATTEMPT_REASON_INITIAL,
    SiteGroup, SiteGroupMembership, SITE_GROUP_DRAFT, SITE_GROUP_LOCKED,
    category_counts_as_designer_rework,
)
from .permissions import (
    user_can_correct_boq, user_can_edit_project_boq,
    project_boq_is_design_locked, project_boq_is_group_locked,
    can_view_boq_corrections,
)


def _profile(username, role, is_design_head=False, is_design_qc=False):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.is_design_head = is_design_head
    profile.is_design_qc = is_design_qc
    profile.save()
    return profile


#: A small stand-in catalogue. The real one is 207 rows and Part 11 already asserts its
#: contents; nothing in this session depends on how many there are, only on the fact that a
#: catalogue row carries code / category / description / unit / sort_order.
OPEX_SEED = [
    ('OPX-001', 'Solar Modules', 'Solar PV Module 540Wp', 'Nos'),
    ('OPX-002', 'Solar Modules', 'Solar PV Module 585Wp', 'Nos'),
    ('OPX-010', 'Inverter',      'String Inverter 100kW', 'Nos'),
    ('OPX-020', 'BOS',           'DC Cable 4Sqmm',        'Mtr'),
    ('OPX-021', 'BOS',           'MC4 Connector Pair',    'Nos'),
]


class CorrectionBase(TestCase):
    """One OPEX site with a BOQ, a designer, a named QC reviewer, a Design Head, and the
    design lock ON — which is the state every interesting test here starts from."""

    def setUp(self):
        BOQItemMaster.objects.bulk_create([
            BOQItemMaster(code=code, description=description, unit=unit,
                          category=category, project_type='OPEX',
                          is_active=True, sort_order=i)
            for i, (code, category, description, unit) in enumerate(OPEX_SEED, start=1)
        ])

        self.designer   = _profile('des_b',   'Design')
        self.qc         = _profile('qc_b',    'Design', is_design_qc=True)
        self.other_des  = _profile('odes_b',  'Design')
        self.head       = _profile('head_b',  'Design', is_design_head=True)
        self.pm         = _profile('pm_b',    'PM')
        self.scm        = _profile('scm_b',   'SCM')
        self.admin      = _profile('adm_b',   'Admin')
        self.ceo        = _profile('ceo_b',   'CEO')

        self.program = Program.objects.create(
            name='Test-SessionB', program_type='OPEX', client_name='SBClient',
            status='Active', short_tender_code='SB')

        self.site, self.assignment = self._opex_site('SB-A')
        self.boq = BOQ.objects.create(project=self.site)
        self.row = self._row('OPX-001', Decimal('12.00'))

        # The design lock ON — the designer marked the BOQ complete and it is with review.
        self.attempt = self._lock_the_boq(self.assignment)

    # ── fixtures ────────────────────────────────────────────────────────────

    def _opex_site(self, code, designer=None, qc_assigned_to=None):
        site = Project(
            project_id=code, customer_name='SBClient', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code=code,
            dc_capacity_kw=Decimal('100.00'), status='Draft',
            assigned_design=designer or self.designer, assigned_pm=self.pm)
        site.save()
        assignment = DesignAssignment.objects.create(
            project=site, status=DESIGN_IN_QC,
            assigned_to=designer or self.designer,
            qc_assigned_to=qc_assigned_to,
            survey_file_bucket='b', survey_file_path=f'{code}/survey/x.pdf')
        return site, assignment

    def _residential_site(self, code):
        site = Project(
            project_id=code, customer_name='House', customer_phone='9876543211',
            site_address='2 Sun Rd', city='Delhi', project_type='Residential',
            dc_capacity_kw=Decimal('10.00'), status='Draft',
            assigned_design=self.designer, assigned_pm=self.pm)
        site.save()
        return site

    def _lock_the_boq(self, assignment):
        """Stamp `boq_submitted_at` on the current attempt — the one field
        project_boq_is_design_locked() reads."""
        attempt = DesignAttempt.objects.create(
            assignment=assignment, attempt_number=1,
            opened_reason=ATTEMPT_REASON_INITIAL,
            boq_submitted_at=timezone.now(),
            boq_submitted_by=assignment.assigned_to)
        assignment.current_attempt_number = 1
        assignment.save(update_fields=['current_attempt_number'])
        return attempt

    def _master(self, code):
        return BOQItemMaster.objects.get(code=code)

    def _row(self, code, quantity=None, boq=None):
        master = self._master(code)
        return BOQItem.objects.create(
            boq=boq or self.boq, item_master=master, serial_no=master.sort_order,
            category=master.category, description=master.description,
            uom=master.unit, boq_quantity=quantity, is_standard_item=True)

    def _login(self, profile):
        self.assertTrue(self.client.login(username=profile.user.username, password='x'))

    def _url(self, site=None):
        return reverse('boq_correct',
                       kwargs={'project_id': (site or self.site).project_id})

    def _add_line(self, code, quantity='', site=None):
        return self.client.post(self._url(site), {
            'action': 'add_line',
            'master_id': str(self._master(code).pk),
            'quantity': str(quantity),
        }, follow=True)

    def _set_quantity(self, item, quantity, site=None):
        return self.client.post(self._url(site), {
            'action': 'set_quantity',
            'item_id': str(item.pk if hasattr(item, 'pk') else item),
            'quantity': str(quantity),
        }, follow=True)

    def _messages(self, response):
        return [str(m) for m in response.context['messages']]


# ===========================================================================
# The predicate itself
# ===========================================================================

class PredicateTests(CorrectionBase):

    def test_the_named_qc_reviewer_may_correct(self):
        site, assignment = self._opex_site('SB-NAMED', qc_assigned_to=self.qc)
        self.assertTrue(user_can_correct_boq(self.qc.user, site))

    def test_an_unnamed_site_admits_the_open_qc_pool(self):
        """qc_assigned_to null is the deliberate default — see user_can_qc_gate_design.
        A rule that only admitted a NAMED reviewer would leave correction dead on most
        sites, which is the same trap Session B.1 recorded for the verdict itself."""
        self.assertIsNone(self.assignment.qc_assigned_to_id)
        self.assertTrue(user_can_correct_boq(self.qc.user, self.site))

    def test_the_design_head_may_correct(self):
        self.assertTrue(user_can_correct_boq(self.head.user, self.site))

    def test_the_assigned_designer_may_not_correct(self):
        """The point of the whole predicate: this is a reviewer action, not the author's."""
        self.assertFalse(user_can_correct_boq(self.designer.user, self.site))

    def test_a_designer_named_as_the_qc_reviewer_still_may_not(self):
        """Settled decision 3 is absolute and is inherited, not restated — the gate
        predicates refuse the assigned designer BEFORE either branch."""
        site, assignment = self._opex_site('SB-SELF')
        assignment.qc_assigned_to = assignment.assigned_to
        assignment.save(update_fields=['qc_assigned_to'])
        self.assertFalse(user_can_correct_boq(assignment.assigned_to.user, site))

    def test_a_plain_designer_with_no_flag_may_not_correct_an_unnamed_site(self):
        self.assertFalse(user_can_correct_boq(self.other_des.user, self.site))

    def test_a_plain_designer_named_on_the_site_may_correct_it_and_no_other(self):
        """A named reviewer needs no flag — and gains nothing portfolio-wide."""
        site, assignment = self._opex_site('SB-B', qc_assigned_to=self.other_des)
        self.assertTrue(user_can_correct_boq(self.other_des.user, site))
        self.assertFalse(user_can_correct_boq(self.other_des.user, self.site))

    def test_pm_scm_admin_and_ceo_may_not_correct(self):
        for profile in (self.pm, self.scm, self.admin, self.ceo):
            with self.subTest(role=profile.role):
                self.assertFalse(user_can_correct_boq(profile.user, self.site))

    def test_residential_is_false_structurally(self):
        """No DesignAssignment can exist on a Residential project, so this returns False at
        the first guard — the same property that makes the design lock safe to AND into
        the shared boq_detail write gate."""
        site = self._residential_site('SB-RES')
        for profile in (self.qc, self.head, self.designer):
            with self.subTest(user=profile.user.username):
                self.assertFalse(user_can_correct_boq(profile.user, site))

    def test_null_project_and_profileless_user_are_false(self):
        self.assertFalse(user_can_correct_boq(self.qc.user, None))
        naked = User.objects.create_user(username='naked_b', password='x')
        UserProfile.objects.filter(user=naked).delete()
        naked.refresh_from_db()
        self.assertFalse(user_can_correct_boq(naked, self.site))

    def test_the_author_gate_is_not_widened(self):
        """W-narrow stands. If this ever fails, user_can_edit_project_boq() was modified —
        which is the thing this session is not allowed to do."""
        self.assertFalse(user_can_edit_project_boq(self.qc.user, self.site))
        self.assertFalse(user_can_edit_project_boq(self.head.user, self.site))
        self.assertTrue(user_can_edit_project_boq(self.designer.user, self.site))

    def test_the_predicate_does_not_consult_the_design_lock(self):
        """True on both sides of the lock — the caller decides, not the predicate."""
        self.assertTrue(project_boq_is_design_locked(self.site))
        self.assertTrue(user_can_correct_boq(self.qc.user, self.site))
        DesignAttempt.objects.filter(pk=self.attempt.pk).update(boq_submitted_at=None)
        self.assertFalse(project_boq_is_design_locked(self.site))
        self.assertTrue(user_can_correct_boq(self.qc.user, self.site))


# ===========================================================================
# VERIFICATION 2 — adding a line to a BOQ the designer is locked out of
# ===========================================================================

class AddLineTests(CorrectionBase):

    def test_the_reviewer_adds_a_catalogue_line_through_the_lock(self):
        """VERIFICATION 2 — the whole point of the session, asserted from inside the
        locked window rather than beside it."""
        self.assertTrue(project_boq_is_design_locked(self.site))
        self._login(self.qc)
        response = self._add_line('OPX-010', '3')
        self.assertEqual(response.status_code, 200)

        row = BOQItem.objects.get(boq=self.boq, item_master__code='OPX-010')
        self.assertEqual(row.boq_quantity, Decimal('3'))

    def test_the_added_row_is_written_the_way_the_picker_writes_one(self):
        """A reviewer's line is not a second class of row: catalogue-linked so Part 6
        aggregation sums it, serial_no from sort_order, spec from the MASTER."""
        self._login(self.qc)
        self._add_line('OPX-020', '150')
        master = self._master('OPX-020')
        row = BOQItem.objects.get(boq=self.boq, item_master=master)
        self.assertEqual(row.item_master_id, master.pk)
        self.assertEqual(row.serial_no, master.sort_order)
        self.assertEqual(row.category, master.category)
        self.assertEqual(row.description, master.description)
        self.assertEqual(row.uom, master.unit)
        self.assertTrue(row.is_standard_item)

    def test_the_edit_record_carries_the_right_attribution(self):
        """VERIFICATION 2 — the record, not just the row."""
        self._login(self.qc)
        self._add_line('OPX-010', '3')
        record = BOQCorrection.objects.get(boq=self.boq)
        self.assertEqual(record.corrected_by_id, self.qc.pk)
        self.assertEqual(record.action, BOQ_CORRECTION_LINE_ADDED)
        self.assertEqual(record.item_code, 'OPX-010')
        self.assertEqual(record.item_description, 'String Inverter 100kW')
        self.assertIsNone(record.quantity_before)
        self.assertEqual(record.quantity_after, Decimal('3'))
        self.assertEqual(record.item_id,
                         BOQItem.objects.get(boq=self.boq,
                                             item_master__code='OPX-010').pk)
        self.assertIsNotNone(record.corrected_at)

    def test_a_line_may_be_added_with_no_quantity(self):
        self._login(self.qc)
        self._add_line('OPX-010', '')
        row = BOQItem.objects.get(boq=self.boq, item_master__code='OPX-010')
        self.assertIsNone(row.boq_quantity)
        record = BOQCorrection.objects.get(boq=self.boq)
        self.assertIsNone(record.quantity_after)

    def test_a_line_already_on_the_sheet_is_refused_and_names_the_alternative(self):
        self._login(self.qc)
        response = self._add_line('OPX-001', '9')
        self.assertEqual(BOQItem.objects.filter(boq=self.boq).count(), 1)
        self.assertEqual(BOQCorrection.objects.count(), 0)
        self.assertTrue(any('already on this BOQ' in m for m in self._messages(response)))

    def test_a_master_id_that_is_not_in_the_catalogue_is_refused(self):
        """Not an active OPEX master -> dropped rather than trusted. This is a POST."""
        residential = BOQItemMaster.objects.create(
            code='ITM-999', description='Rooftop only', unit='Nos', category='BOS',
            project_type='Residential', is_active=True, sort_order=1)
        self._login(self.qc)
        response = self.client.post(self._url(), {
            'action': 'add_line', 'master_id': str(residential.pk), 'quantity': '1',
        }, follow=True)
        self.assertEqual(BOQItem.objects.filter(boq=self.boq).count(), 1)
        self.assertEqual(BOQCorrection.objects.count(), 0)
        self.assertTrue(any('Choose a catalogue item' in m
                            for m in self._messages(response)))

    def test_an_unparseable_or_negative_quantity_is_refused_not_silently_blanked(self):
        self._login(self.qc)
        for bad in ('1,200', '-4', 'abc'):
            with self.subTest(quantity=bad):
                response = self._add_line('OPX-010', bad)
                self.assertFalse(
                    BOQItem.objects.filter(boq=self.boq,
                                           item_master__code='OPX-010').exists())
                self.assertTrue(any('not usable' in m for m in self._messages(response)))
        self.assertEqual(BOQCorrection.objects.count(), 0)

    def test_correction_never_creates_a_boq(self):
        """Stricter than the picker's lazy create, and deliberately: a reviewer must not
        bring a BOQ into existence on a site the designer never opened."""
        site, assignment = self._opex_site('SB-NOBOQ', qc_assigned_to=self.qc)
        self._lock_the_boq(assignment)
        self._login(self.qc)
        response = self.client.get(self._url(site))
        self.assertEqual(response.status_code, 200)
        response = self._add_line('OPX-010', '2', site=site)
        self.assertFalse(BOQ.objects.filter(project=site).exists())
        self.assertTrue(any('no BOQ yet' in m for m in self._messages(response)))


# ===========================================================================
# VERIFICATION 3 — changing a quantity on an existing row
# ===========================================================================

class QuantityTests(CorrectionBase):

    def test_the_reviewer_changes_a_quantity_through_the_lock(self):
        """VERIFICATION 3."""
        self.assertTrue(project_boq_is_design_locked(self.site))
        self._login(self.qc)
        self._set_quantity(self.row, '18')
        self.row.refresh_from_db()
        self.assertEqual(self.row.boq_quantity, Decimal('18'))

    def test_the_record_carries_before_and_after(self):
        """VERIFICATION 3 — 'changed from X to Y', not just 'changed'."""
        self._login(self.qc)
        self._set_quantity(self.row, '18')
        record = BOQCorrection.objects.get(boq=self.boq)
        self.assertEqual(record.corrected_by_id, self.qc.pk)
        self.assertEqual(record.action, BOQ_CORRECTION_QUANTITY_CHANGED)
        self.assertEqual(record.item_id, self.row.pk)
        self.assertEqual(record.item_code, 'OPX-001')
        self.assertEqual(record.quantity_before, Decimal('12'))
        self.assertEqual(record.quantity_after, Decimal('18'))

    def test_more_than_one_correction_on_the_same_row_is_a_history_not_an_overwrite(self):
        """The reason this is a table and not two fields on BOQItem."""
        self._login(self.qc)
        self._set_quantity(self.row, '18')
        self.client.logout()
        self._login(self.head)
        self._set_quantity(self.row, '20')

        records = list(BOQCorrection.objects.filter(item=self.row)
                       .order_by('corrected_at', 'pk'))
        self.assertEqual(len(records), 2)
        self.assertEqual([r.corrected_by_id for r in records], [self.qc.pk, self.head.pk])
        self.assertEqual([(r.quantity_before, r.quantity_after) for r in records],
                         [(Decimal('12'), Decimal('18')),
                          (Decimal('18'), Decimal('20'))])

    def test_a_row_that_had_no_quantity_records_a_null_before(self):
        blank = self._row('OPX-002', None)
        self._login(self.qc)
        self._set_quantity(blank, '7')
        record = BOQCorrection.objects.get(item=blank)
        self.assertIsNone(record.quantity_before)
        self.assertEqual(record.quantity_after, Decimal('7'))

    def test_an_unchanged_quantity_records_nothing(self):
        """Submitting a pre-filled box without editing it must not put a no-op on the
        reviewer's name."""
        self._login(self.qc)
        response = self._set_quantity(self.row, '12')
        self.assertEqual(BOQCorrection.objects.count(), 0)
        self.assertTrue(any('already carries that quantity'
                            in m for m in self._messages(response)))

    def test_a_blank_quantity_is_refused_rather_than_clearing_the_row(self):
        """Clearing is closer to removing the line than to correcting it, and removal is
        out of scope — so it is refused with a message, not guessed at."""
        self._login(self.qc)
        response = self._set_quantity(self.row, '')
        self.row.refresh_from_db()
        self.assertEqual(self.row.boq_quantity, Decimal('12'))
        self.assertEqual(BOQCorrection.objects.count(), 0)
        self.assertTrue(any('states a number' in m for m in self._messages(response)))

    def test_a_row_from_another_sites_boq_is_a_404(self):
        """Object consistency, the same rule delete_item applies — not a silent no-op
        reported as success."""
        other, other_assignment = self._opex_site('SB-OTHER', qc_assigned_to=self.qc)
        other_boq = BOQ.objects.create(project=other)
        foreign = self._row('OPX-010', Decimal('5'), boq=other_boq)
        self._login(self.qc)
        response = self.client.post(self._url(), {
            'action': 'set_quantity', 'item_id': str(foreign.pk), 'quantity': '99'})
        self.assertEqual(response.status_code, 404)
        foreign.refresh_from_db()
        self.assertEqual(foreign.boq_quantity, Decimal('5'))
        self.assertEqual(BOQCorrection.objects.count(), 0)

    def test_an_off_catalogue_row_can_be_corrected_too(self):
        """A pre-Part-11 seeded row or an ad-hoc one is a real quantity on a real sheet."""
        adhoc = BOQItem.objects.create(
            boq=self.boq, item_master=None, serial_no=99, category='Other',
            description='Site-specific bracket', uom='Nos',
            boq_quantity=Decimal('4'), is_standard_item=False)
        self._login(self.qc)
        self._set_quantity(adhoc, '6')
        adhoc.refresh_from_db()
        self.assertEqual(adhoc.boq_quantity, Decimal('6'))
        record = BOQCorrection.objects.get(item=adhoc)
        self.assertEqual(record.item_code, '')          # no master, so no code
        self.assertEqual(record.item_description, 'Site-specific bracket')


# ===========================================================================
# VERIFICATION 4 — delete is not exposed to this predicate, anywhere
# ===========================================================================

class DeleteStaysClosedTests(CorrectionBase):

    def test_the_correction_endpoint_has_no_delete_action(self):
        """A crafted POST naming delete_item is REFUSED with a message, not ignored into a
        bare redirect that would read as success."""
        self._login(self.qc)
        response = self.client.post(self._url(), {
            'action': 'delete_item', 'item_id': str(self.row.pk)}, follow=True)
        self.assertTrue(BOQItem.objects.filter(pk=self.row.pk).exists())
        self.assertTrue(any('removing a line is not a reviewer action' in m.lower()
                            for m in self._messages(response)))

    def test_every_other_crafted_action_is_refused_the_same_way(self):
        self._login(self.qc)
        for action in ('', 'save_design', 'submit_design', 'add_item',
                       'save_draft', 'mark_complete', 'remove_row'):
            with self.subTest(action=action):
                response = self.client.post(
                    self._url(), {'action': action, 'item_id': str(self.row.pk),
                                  'master_id': str(self._master('OPX-010').pk),
                                  'quantity': '5'}, follow=True)
                self.assertTrue(BOQItem.objects.filter(pk=self.row.pk).exists())
                self.assertEqual(BOQItem.objects.filter(boq=self.boq).count(), 1)
        self.assertEqual(BOQCorrection.objects.count(), 0)

    def test_the_correction_screen_offers_no_delete_control(self):
        """The template check is the WEAK one and is here only beside the POST tests
        above — a hidden button is not a permission, and an absent one is not either."""
        self._login(self.qc)
        body = self.client.get(self._url()).content.decode()
        self.assertNotIn('delete_item', body)
        self.assertNotIn('name="action" value="delete', body)
        self.assertIn('set_quantity', body)
        self.assertIn('add_line', body)

    def test_the_reviewer_is_refused_by_boq_detail_delete_item(self):
        """`delete_item` is NOT touched by this session and still takes the W-narrow author
        gate. Asserted by direct POST, on a non-standard row, which is the only kind that
        endpoint can delete at all."""
        adhoc = BOQItem.objects.create(
            boq=self.boq, item_master=None, serial_no=99, category='Other',
            description='Ad-hoc', uom='Nos', is_standard_item=False)
        self._login(self.qc)
        self.client.post(
            reverse('boq_detail', kwargs={'project_id': self.site.project_id}),
            {'action': 'delete_item', 'item_id': str(adhoc.pk)})
        self.assertTrue(BOQItem.objects.filter(pk=adhoc.pk).exists())

    def test_the_reviewer_is_refused_by_the_picker_which_deletes_by_omission(self):
        """THE ONE THAT MATTERS MOST. opex_boq_entry's save is a whole-sheet
        reconciliation: a row absent from the POST is DELETED. That is why correction was
        NOT built as a mode of the picker — running a reviewer through it would hand them
        line removal silently, as a side effect of the mechanism."""
        self._login(self.qc)
        response = self.client.post(
            reverse('opex_boq_entry', kwargs={'project_id': self.site.project_id}),
            {'action': 'save_draft', 'item': []})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(BOQItem.objects.filter(pk=self.row.pk).exists())

    def test_a_reviewer_added_row_cannot_be_deleted_by_the_designer_either(self):
        """It is written is_standard_item=True, like every other catalogue row, and
        delete_item deletes only non-standard rows."""
        self._login(self.qc)
        self._add_line('OPX-010', '3')
        added = BOQItem.objects.get(boq=self.boq, item_master__code='OPX-010')
        self.assertTrue(added.is_standard_item)


# ===========================================================================
# VERIFICATION 5 — the bypass is reviewer-only; the designer stays locked out
# ===========================================================================

class DesignerStaysLockedTests(CorrectionBase):

    def test_the_designer_is_403ed_by_the_correction_endpoint(self):
        self._login(self.designer)
        self.assertEqual(self.client.get(self._url()).status_code, 403)
        self.assertEqual(
            self.client.post(self._url(),
                             {'action': 'set_quantity', 'item_id': str(self.row.pk),
                              'quantity': '99'}).status_code, 403)
        self.row.refresh_from_db()
        self.assertEqual(self.row.boq_quantity, Decimal('12'))

    def test_the_designer_still_cannot_edit_through_the_picker(self):
        """The lock the reviewer is exempt from is still ON for the author, in the same
        instant. This is the assertion that would catch a general loosening."""
        self.assertTrue(project_boq_is_design_locked(self.site))
        self._login(self.designer)
        response = self.client.post(
            reverse('opex_boq_entry', kwargs={'project_id': self.site.project_id}),
            {'action': 'save_draft', 'item': [str(self._master('OPX-001').pk)],
             f'qty_{self._master("OPX-001").pk}': '999'}, follow=True)
        self.row.refresh_from_db()
        self.assertEqual(self.row.boq_quantity, Decimal('12'))
        self.assertTrue(any('with design review' in m for m in self._messages(response)))

    def test_the_designer_still_cannot_edit_through_boq_detail(self):
        self._login(self.designer)
        self.client.post(
            reverse('boq_detail', kwargs={'project_id': self.site.project_id}),
            {'action': 'save_design', f'boq_qty_{self.row.pk}': '999'})
        self.row.refresh_from_db()
        self.assertEqual(self.row.boq_quantity, Decimal('12'))

    def test_the_designer_still_cannot_upload_a_spreadsheet(self):
        self._login(self.designer)
        response = self.client.get(
            reverse('opex_boq_upload', kwargs={'project_id': self.site.project_id}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['stage'], 'locked')

    def test_a_reviewer_correction_does_not_clear_the_lock_for_the_designer(self):
        """The most direct form of the question: the reviewer writes, and the designer's
        own path is exactly as closed afterwards as it was before."""
        self._login(self.qc)
        self._set_quantity(self.row, '18')
        self.client.logout()
        self.assertTrue(project_boq_is_design_locked(self.site))
        self._login(self.designer)
        self.client.post(
            reverse('boq_detail', kwargs={'project_id': self.site.project_id}),
            {'action': 'save_design', f'boq_qty_{self.row.pk}': '999'})
        self.row.refresh_from_db()
        self.assertEqual(self.row.boq_quantity, Decimal('18'))


# ===========================================================================
# The procurement lock is NOT bypassed
# ===========================================================================

class GroupLockTests(CorrectionBase):

    def _lock_the_group(self):
        group = SiteGroup.objects.create(
            program=self.program, name='Batch 1', status=SITE_GROUP_LOCKED,
            created_by=self.pm, locked_by=self.pm, locked_at=timezone.now())
        SiteGroupMembership.objects.create(group=group, project=self.site, added_by=self.pm)
        return group

    def test_a_locked_group_refuses_the_reviewer_too(self):
        """Part 6's lock is final and has no unlock. A reviewer is not an exception to it,
        and correction must not become a way round it."""
        self._lock_the_group()
        self.assertTrue(project_boq_is_group_locked(self.site))
        self._login(self.qc)

        response = self._set_quantity(self.row, '18')
        self.row.refresh_from_db()
        self.assertEqual(self.row.boq_quantity, Decimal('12'))

        response2 = self._add_line('OPX-010', '3')
        self.assertFalse(BOQItem.objects.filter(boq=self.boq,
                                                item_master__code='OPX-010').exists())
        self.assertEqual(BOQCorrection.objects.count(), 0)
        for r in (response, response2):
            self.assertTrue(any('variance against the order' in m
                                for m in self._messages(r)))

    def test_the_screen_still_reads_while_the_group_is_locked(self):
        self._lock_the_group()
        self._login(self.qc)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['group_locked'])
        self.assertNotIn('set_quantity', response.content.decode())


# ===========================================================================
# Access to the screen itself
# ===========================================================================

class ScreenAccessTests(CorrectionBase):

    def test_a_residential_project_404s(self):
        site = self._residential_site('SB-RES2')
        self._login(self.head)
        self.assertEqual(
            self.client.get(reverse('boq_correct',
                                    kwargs={'project_id': site.project_id})).status_code,
            404)

    def test_pm_scm_and_an_unrelated_designer_are_403ed(self):
        for profile in (self.pm, self.scm, self.other_des, self.admin, self.ceo):
            with self.subTest(role=profile.role, user=profile.user.username):
                self.client.logout()
                self._login(profile)
                self.assertEqual(self.client.get(self._url()).status_code, 403)

    def test_the_link_is_offered_on_boq_detail_to_a_reviewer_and_to_nobody_else(self):
        correct_url = self._url()
        self._login(self.qc)
        self.assertIn(correct_url, self.client.get(
            reverse('boq_detail', kwargs={'project_id': self.site.project_id})
        ).content.decode())

        self.client.logout()
        self._login(self.pm)
        self.assertNotIn(correct_url, self.client.get(
            reverse('boq_detail', kwargs={'project_id': self.site.project_id})
        ).content.decode())


# ===========================================================================
# VERIFICATION 6 — the record is not designer rework, and is not a fourth signal
# ===========================================================================

class NotReworkTests(CorrectionBase):

    def test_a_correction_opens_no_attempt_and_changes_no_attempt_field(self):
        before = {
            'count':   DesignAttempt.objects.filter(assignment=self.assignment).count(),
            'current': self.assignment.current_attempt_number,
            'stamp':   self.attempt.boq_submitted_at,
            'status':  self.assignment.status,
        }
        self._login(self.qc)
        self._set_quantity(self.row, '18')
        self._add_line('OPX-010', '3')

        self.assignment.refresh_from_db()
        self.attempt.refresh_from_db()
        self.assertEqual(DesignAttempt.objects.filter(assignment=self.assignment).count(),
                         before['count'])
        self.assertEqual(self.assignment.current_attempt_number, before['current'])
        self.assertEqual(self.attempt.boq_submitted_at, before['stamp'])
        self.assertEqual(self.assignment.status, before['status'])

    def test_the_model_has_no_link_to_an_attempt_or_a_review(self):
        """Structural, not behavioural: rework accounting runs on DesignReview error
        categories and DesignAttempt rows, and this table can never be joined to either
        because it has no field pointing at one."""
        related = {f.name: f.related_model.__name__
                   for f in BOQCorrection._meta.get_fields()
                   if getattr(f, 'related_model', None) is not None}
        self.assertEqual(set(related.values()), {'BOQ', 'BOQItem', 'UserProfile'})

    def test_the_rework_predicate_is_untouched_by_a_correction(self):
        self._login(self.qc)
        self._set_quantity(self.row, '18')
        self.assertFalse(category_counts_as_designer_rework(''))
        self.assertFalse(category_counts_as_designer_rework(None))

    def test_the_record_carries_no_status_or_doneness_field(self):
        """PRE-FLIGHT 4, asserted rather than promised. This BOQ already has THREE
        unreconciled 'finished' signals; a field here that any gate could read would be a
        fourth. Every field is a fact about an edit that already happened."""
        names = {f.name for f in BOQCorrection._meta.get_fields()}
        self.assertEqual(names, {
            'id', 'boq', 'item', 'item_code', 'item_description', 'action',
            'quantity_before', 'quantity_after', 'corrected_by', 'corrected_at'})
        for forbidden in ('status', 'state', 'is_applied', 'is_valid', 'is_complete',
                          'approved', 'superseded_by', 'locked'):
            self.assertNotIn(forbidden, names)

    def test_boq_status_and_the_submitted_stamp_are_untouched(self):
        """The correction writes a quantity and an audit row. It does not move the BOQ
        along any workflow, because it is not a workflow event."""
        self._login(self.qc)
        self._set_quantity(self.row, '18')
        self.boq.refresh_from_db()
        self.assertEqual(self.boq.status, 'Draft')
        self.assertIsNone(self.boq.submitted_at)
        self.assertIsNone(self.boq.submitted_by_id)
        self.assertEqual(self.boq.version, 1)

    def test_no_boqrevision_is_written(self):
        """BOQRevision is the whole-sheet snapshot taken at a WORKFLOW TRANSITION. A
        correction is not one, and writing a revision here would put a version bump on the
        timeline that nothing transitioned."""
        self._login(self.qc)
        self._set_quantity(self.row, '18')
        self._add_line('OPX-010', '3')
        self.assertEqual(BOQRevision.objects.filter(boq=self.boq).count(), 0)


# ===========================================================================
# The audit surface — Admin/CEO, and nobody else
# ===========================================================================

class AuditVisibilityTests(CorrectionBase):

    def setUp(self):
        super().setUp()
        self._login(self.qc)
        self._set_quantity(self.row, '18')
        self.client.logout()
        self.history_url = reverse('boq_history',
                                   kwargs={'project_id': self.site.project_id})

    def test_the_role_helper_admits_admin_and_ceo_only(self):
        self.assertTrue(can_view_boq_corrections(self.admin.user))
        self.assertTrue(can_view_boq_corrections(self.ceo.user))
        for profile in (self.pm, self.scm, self.designer, self.qc, self.head):
            with self.subTest(role=profile.role, user=profile.user.username):
                self.assertFalse(can_view_boq_corrections(profile.user))

    def test_admin_and_ceo_see_the_trail_on_the_boq_history_page(self):
        for profile in (self.admin, self.ceo):
            with self.subTest(role=profile.role):
                self.client.logout()
                self._login(profile)
                response = self.client.get(self.history_url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.context['corrections']), 1)
                self.assertIn('Reviewer corrections', response.content.decode())

    def test_the_designer_and_the_pm_see_the_same_history_page_without_the_trail(self):
        """NOT a tally against the designer — it is an audit record, and it is not on the
        screen of the person it would read as a tally against."""
        for profile in (self.designer, self.pm, self.scm):
            with self.subTest(role=profile.role):
                self.client.logout()
                self._login(profile)
                response = self.client.get(self.history_url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['corrections'], [])
                self.assertNotIn('Reviewer corrections', response.content.decode())

    def test_both_trail_renderings_survive_a_line_added_row(self):
        """The `line_added` branch of both templates, exercised rather than assumed — it is
        the one whose `quantity_before` is null and whose quantity may legitimately be 0."""
        self._login(self.qc)
        self._add_line('OPX-010', '0')
        body = self.client.get(self._url()).content.decode()
        self.assertIn('Line added', body)
        self.assertIn('String Inverter 100kW', body)

        self.client.logout()
        self._login(self.admin)
        body = self.client.get(self.history_url).content.decode()
        self.assertIn('Line added', body)
        self.assertIn('String Inverter 100kW', body)
        self.assertIn('Quantity', body)

    def test_the_read_gate_still_governs_who_reaches_the_page_at_all(self):
        """can_view_boq_corrections() can only ever NARROW — it is ANDed after the BOQ read
        gate, never instead of it."""
        outsider = _profile('outsider_b', 'Site Engineer')
        self.client.logout()
        self._login(outsider)
        self.assertEqual(self.client.get(self.history_url).status_code, 403)


# ===========================================================================
# The record outlives the row it corrected
# ===========================================================================

class RecordDurabilityTests(CorrectionBase):

    def test_deleting_the_item_leaves_a_record_that_can_still_name_it(self):
        """SET_NULL plus the denormalised identity. The designer's picker can remove a line
        on a later attempt, and an audit row that can no longer say what it corrected is
        not an audit trail."""
        self._login(self.qc)
        self._set_quantity(self.row, '18')
        record = BOQCorrection.objects.get(boq=self.boq)

        self.row.delete()
        record.refresh_from_db()
        self.assertIsNone(record.item_id)
        self.assertEqual(record.item_code, 'OPX-001')
        self.assertEqual(record.item_description, 'Solar PV Module 540Wp')
        self.assertEqual(record.quantity_before, Decimal('12'))
        self.assertEqual(record.quantity_after, Decimal('18'))
        self.assertEqual(record.corrected_by_id, self.qc.pk)

    def test_deleting_the_boq_takes_its_corrections_with_it(self):
        """CASCADE from the BOQ, matching BOQRevision — a correction to a BOQ that no
        longer exists is not a record of anything."""
        self._login(self.qc)
        self._set_quantity(self.row, '18')
        self.boq.delete()
        self.assertEqual(BOQCorrection.objects.count(), 0)
