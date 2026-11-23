"""
B-4 — Clear BOQ, and fill an empty BOQ by copying another site's.

Rulings: 32 (copy only into an empty BOQ), F8 (only the site's designer clears, only before
submission), Z1 (copied lines take the catalogue's current wording). Zuber's B-4 answers
(23 Nov) Q1-Q9: the source is a site the designer can already open; a failed-back BOQ is
clearable; source='copy'; the clear runs no engine; Tin Shade / Ballast only as a whole
count in Nos; non-RESCO items skipped; mandatory items put back blank; ACCBs re-validated
and numbered fresh; the copy runs the upload's plan and write loop.

Run with:
    python manage.py test projects.tests_boq_duplicate --settings=solarpms.test_settings
"""
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .design_metrics import reviewer_corrections
from .models import (
    ACCBConfig, ActivityLog, BOQ, BOQCorrection, BOQItem, BOQItemMaster, BOQLineOverride,
    BOQRecalculation, DesignAttempt, Project, SiteGroup, SiteGroupMembership, Vendor,
    VendorOrder, VendorOrderLine,
    ATTEMPT_REASON_INITIAL, ATTEMPT_REASON_QC_FAILED, BOQ_CORRECTION_QUANTITY_CHANGED,
    FORMULA_SOURCE_COPY, SITE_GROUP_LOCKED,
)
from .tests_boq_formula_display import DisplayBase
from .tests_accb_config import accb_post_data
from .tests_boq_formula_apply import _profile
from .views import boq_clear_log_text, boq_copy_log_text

D = Decimal

#: B-3c shapes: switchgear from the fixed list, SPD per O/G feeder (mixed on TYPE2).
TYPE2 = {'type': 'Type-2',
         'in': [{'breaker': 'MCCB', 'rating': 100}, {'breaker': 'MCB', 'rating': 63}],
         'out': [{'breaker': 'MCCB', 'rating': 200, 'spd': True},
                 {'breaker': 'MCCB', 'rating': 200, 'spd': False}]}
TYPE1 = {'type': 'Type-1',
         'in': [{'breaker': 'MCB', 'rating': 40}],
         'out': [{'breaker': 'MCCB', 'rating': 100, 'spd': False}]}

#: Picker GET, measured on 54aab27 (before B-4) with this file's fixture under
#: solarpms.test_settings: an empty BOQ (no BOQ row) and DisplayBase.SHEET. B-4 adds nothing:
#: its two entry points are decided from rows the page already loads.
PICKER_BASELINE = {'empty': 17, 'full': 19}
PICKER_BASELINE_FIVE_RULES = {'empty': 20, 'full': 23}


class DuplicateBase(DisplayBase):
    """FX-A (self.site) is the usual source; FX-T (self.target) the empty target. Both are
    the same designer's, so the designer can open both (Q1)."""

    def setUp(self):
        super().setUp()
        for code, description, sort_order in [('OPX-037', 'Ballast Type Structure', 37),
                                              ('OPX-038', 'Tin Shade', 38)]:
            self.items[code] = BOQItemMaster.objects.create(
                code=code, description=description, unit='Nos', category='Structure',
                project_type='OPEX', sort_order=sort_order, is_active=True)
        self.target, self.target_assignment = self._opex_site('FX-T')
        self._login(self.designer)

    # ── fixtures ────────────────────────────────────────────────────────────

    def _accb_add(self, config, site):
        data = {'action': 'add', **accb_post_data(config)}
        response = self.client.post(
            reverse('opex_boq_accb', kwargs={'project_id': site.project_id}), data)
        self.assertEqual(response.status_code, 302)

    def _off_line(self, site, text, quantity):
        return BOQItem.objects.create(
            boq=self._boq(site), item_master=None, serial_no=999, category='Other',
            description=text, uom='Nos',
            boq_quantity=None if quantity is None else D(str(quantity)),
            is_standard_item=False)

    def _source(self):
        """Ordinary lines (one blank), a line with no catalogue item, two ACCBs."""
        self._line('FX-LA1', 2)
        self._line('FX-INV1', 3)
        self._line('FX-PANEL', None)
        self._off_line(self.site, 'Site-made bracket', 4)
        self._accb_add(TYPE2, self.site)
        self._accb_add(TYPE1, self.site)

    def _stamp(self, assignment, attempt_number=1):
        DesignAttempt.objects.create(
            assignment=assignment, attempt_number=attempt_number,
            opened_reason=ATTEMPT_REASON_INITIAL, boq_submitted_at=timezone.now(),
            boq_submitted_by=self.designer)
        assignment.current_attempt_number = attempt_number
        assignment.save(update_fields=['current_attempt_number'])

    def _group_lock(self, site):
        group = SiteGroup.objects.create(program=self.program, name='G-B4',
                                         status=SITE_GROUP_LOCKED, created_by=self.pm)
        SiteGroupMembership.objects.create(group=group, project=site, added_by=self.pm)

    def _copy_url(self, site=None):
        return reverse('opex_boq_copy', kwargs={'project_id': (site or self.target).project_id})

    def _preview(self, code=None, site=None):
        return self.client.post(self._copy_url(site), {
            'phase': 'preview', 'source_code': code or self.site.project_id})

    def _commit(self, code=None, site=None):
        return self.client.post(self._copy_url(site), {
            'phase': 'commit', 'source_code': code or self.site.project_id})

    def _clear(self, site=None, code=None):
        site = site or self.target
        return self.client.post(
            reverse('opex_boq_clear', kwargs={'project_id': site.project_id}),
            {'confirm_code': site.project_id if code is None else code}, follow=True)

    def _target_lines(self):
        return list(BOQItem.objects.filter(boq__project=self.target)
                    .select_related('item_master').order_by('serial_no', 'pk'))

    def _codes(self, site):
        return sorted(line.item_master.code if line.item_master else line.description
                      for line in BOQItem.objects.filter(boq__project=site)
                      .select_related('item_master'))

    def _accbs(self, site):
        return list(ACCBConfig.objects.filter(boq__project=site)
                    .select_related('boq_item').order_by('number'))


# ===========================================================================
# T1 — Clear BOQ
# ===========================================================================

class ClearTests(DuplicateBase):

    def _full_target(self):
        """The target with calculated lines (and their recalculation records), an override,
        two ACCBs and a reviewer correction."""
        self._rules()
        self._picker({'FX-LA1': 2, 'FX-INV1': 3}, site=self.target)
        earth = self._lines('FX-EARTH', self.target)[0]
        self._override('set', earth, site=self.target, quantity='12', note='Longer run')
        self._accb_add(TYPE2, self.target)
        self._accb_add(TYPE1, self.target)
        la1 = self._lines('FX-LA1', self.target)[0]
        BOQCorrection.objects.create(
            boq=la1.boq, item=la1, item_code='FX-LA1', item_description=la1.description,
            action=BOQ_CORRECTION_QUANTITY_CHANGED, quantity_before=D('1'),
            quantity_after=D('2'), corrected_by=self.qc)
        return BOQ.objects.get(project=self.target)

    def test_the_designer_clears_lines_accbs_and_overrides_and_the_records_keep_their_text(self):
        boq = self._full_target()
        lines_before = boq.items.filter(accb_config__isnull=True).count()
        recalcs = list(BOQRecalculation.objects.filter(boq=boq).order_by('pk')
                       .values_list('pk', 'item_code', 'item_description', 'quantity_after'))
        self.assertTrue(recalcs)
        self.assertEqual(BOQLineOverride.objects.filter(line__boq=boq).count(), 1)
        counted_before = reviewer_corrections().filter(boq=boq).count()

        response = self._clear()

        self.assertEqual(BOQItem.objects.filter(boq=boq).count(), 0)
        self.assertEqual(ACCBConfig.objects.filter(boq=boq).count(), 0)
        self.assertEqual(BOQLineOverride.objects.count(), 0)
        # Lines only, never the BOQ row: its counter and its corrections survive.
        boq_after = BOQ.objects.get(pk=boq.pk)
        self.assertEqual(boq_after.next_accb_number, boq.next_accb_number)
        self.assertEqual(boq_after.next_accb_number, 3)
        correction = BOQCorrection.objects.get(boq=boq)
        self.assertIsNone(correction.item_id)
        self.assertEqual(correction.item_code, 'FX-LA1')
        self.assertEqual(reviewer_corrections().filter(boq=boq).count(), counted_before)
        # Recalculation records stay with their copied text, the line set to NULL; the
        # clear writes none (Q4).
        self.assertEqual(list(BOQRecalculation.objects.filter(boq=boq).order_by('pk')
                              .values_list('pk', 'item_code', 'item_description',
                                           'quantity_after')), recalcs)
        self.assertFalse(BOQRecalculation.objects.filter(boq=boq, line__isnull=False).exists())

        log = ActivityLog.objects.get(action_code='boq_cleared')
        self.assertEqual(log.action, boq_clear_log_text(self.designer, self.target,
                                                        lines_before, 2))
        self.assertIn('cleared the BOQ on FX-T: 5 lines, 2 ACCBs', log.action)
        self.assertIn('BOQ cleared: 5 lines and 2 ACCBs removed.',
                      ' '.join(self._messages(response)))

    def test_a_cleared_sites_next_accb_takes_a_new_number(self):
        self._accb_add(TYPE2, self.target)
        self._accb_add(TYPE1, self.target)
        self._clear()
        self._accb_add(TYPE1, self.target)
        self.assertEqual([accb.number for accb in self._accbs(self.target)], [3])

    def test_refused_when_the_boq_is_marked_complete(self):
        self._line('FX-LA1', 2, site=self.target)
        self._stamp(self.target_assignment)
        response = self._clear()
        self.assertEqual(len(self._target_lines()), 1)
        self.assertIn('cannot be cleared', ' '.join(self._messages(response)))

    def test_a_failed_back_boq_is_clearable(self):
        """Q2: QC failed attempt 1 (stamped); attempt 2 is open, so the picker can edit it."""
        self._line('FX-LA1', 2, site=self.target)
        self._stamp(self.target_assignment)
        DesignAttempt.objects.create(assignment=self.target_assignment, attempt_number=2,
                                     opened_reason=ATTEMPT_REASON_QC_FAILED)
        self.target_assignment.current_attempt_number = 2
        self.target_assignment.save(update_fields=['current_attempt_number'])
        self._clear()
        self.assertEqual(self._target_lines(), [])

    def test_refused_under_a_group_lock(self):
        self._line('FX-LA1', 2, site=self.target)
        self._group_lock(self.target)
        self._clear()
        self.assertEqual(len(self._target_lines()), 1)

    def test_refused_with_an_ordered_quantity(self):
        line = self._line('FX-LA1', 2, site=self.target)
        line.ordered_quantity = D('2')
        line.save(update_fields=['ordered_quantity'])
        response = self._clear()
        self.assertEqual(len(self._target_lines()), 1)
        self.assertIn('ordered against', ' '.join(self._messages(response)))

    def test_refused_with_an_order_line_pointing_at_a_line(self):
        line = self._line('FX-LA1', 2, site=self.target)
        order = VendorOrder.objects.create(vendor=Vendor.objects.create(name='Acme'),
                                           project_type='OPEX', created_by=self.pm,
                                           total_amount=D('100.00'))
        VendorOrderLine.objects.create(order=order, boq_item=line, item_code='FX-LA1',
                                       item_description=line.description, quantity=D('2'))
        self._clear()
        self.assertEqual(len(self._target_lines()), 1)
        self.assertEqual(VendorOrderLine.objects.get().boq_item_id, line.pk)

    def test_refused_for_qc_head_and_pm(self):
        self._line('FX-LA1', 2, site=self.target)
        for profile in (self.qc, self.head, self.pm):
            with self.subTest(role=profile.user.username):
                self._login(profile)
                response = self.client.post(
                    reverse('opex_boq_clear', kwargs={'project_id': self.target.project_id}),
                    {'confirm_code': self.target.project_id})
                self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self._target_lines()), 1)

    def test_refused_unless_the_site_code_is_typed(self):
        self._line('FX-LA1', 2, site=self.target)
        for typed in ('', 'FX-A', 'FX-T2'):
            with self.subTest(typed=typed):
                response = self._clear(code=typed)
                self.assertIn('Type the site code FX-T', ' '.join(self._messages(response)))
        self.assertEqual(len(self._target_lines()), 1)
        self._clear(code=' fx-t ')
        self.assertEqual(self._target_lines(), [])

    def test_only_offered_while_the_designer_can_clear(self):
        entry = reverse('opex_boq_entry', kwargs={'project_id': self.target.project_id})
        self.assertNotContains(self.client.get(entry), 'id="boqClear"')   # empty
        line = self._line('FX-LA1', 2, site=self.target)
        self.assertContains(self.client.get(entry), 'id="boqClear"')
        line.ordered_quantity = D('1')
        line.save(update_fields=['ordered_quantity'])
        self.assertNotContains(self.client.get(entry), 'id="boqClear"')
        line.ordered_quantity = None
        line.save(update_fields=['ordered_quantity'])
        self._login(self.qc)
        self.assertNotContains(self.client.get(entry), 'id="boqClear"')
        self._login(self.designer)
        self._stamp(self.target_assignment)
        self.assertNotContains(self.client.get(entry), 'id="boqClear"')


# ===========================================================================
# T2 — the copy
# ===========================================================================

class CopyTests(DuplicateBase):

    def test_catalogue_lines_take_todays_catalogue_wording_not_the_sources(self):
        """Z1. The catalogue text changes between the source's save and the copy."""
        self._line('FX-LA1', 2)
        master = self.items['FX-LA1']
        master.description = 'Conventional lightning arrester, 1.5 m mast'
        master.save(update_fields=['description'])
        self._commit()
        copied = self._lines('FX-LA1', self.target)[0]
        self.assertEqual(copied.description, 'Conventional lightning arrester, 1.5 m mast')
        self.assertEqual(copied.boq_quantity, D('2'))
        self.assertEqual(self._lines('FX-LA1')[0].description, 'Conventional lightning arrester')

    def test_a_line_with_no_catalogue_item_is_copied_with_its_own_text(self):
        self._line('FX-LA1', 2)
        self._off_line(self.site, 'Site-made bracket', 4)
        self._commit()
        off = BOQItem.objects.get(boq__project=self.target, item_master__isnull=True)
        self.assertEqual((off.description, off.boq_quantity, off.uom, off.category,
                          off.is_standard_item),
                         ('Site-made bracket', D('4'), 'Nos', 'Other', False))

    def test_retired_non_resco_fractional_structure_and_repeated_items_are_skipped_and_listed(self):
        self._line('FX-LA1', 2)
        retired = BOQItemMaster.objects.create(code='FX-OLD', description='Old clamp',
                                               unit='Nos', category='Modules',
                                               project_type='OPEX', sort_order=9100,
                                               is_active=False)
        residential = BOQItemMaster.objects.create(code='ITM-900', description='Res item',
                                                   unit='Nos', category='BOS',
                                                   project_type='Residential',
                                                   sort_order=9101, is_active=True)
        self._line('FX-OLD', 5)
        self._line('ITM-900', 1)
        self._line('OPX-038', '16.74')                  # a leftover kWp figure
        kwp = self._line('OPX-037', 40)                 # whole, but still in KWp
        kwp.uom = 'KWp'
        kwp.save(update_fields=['uom'])
        self._line('FX-INV2', 2)
        self._line('FX-INV2', 3)                        # the same item twice
        self.assertIsNotNone(retired.pk and residential.pk)

        skipped = self._preview().context['copy_summary']['skipped']
        self.assertEqual(sorted((code, reason) for code, _description, reason in skipped), [
            ('FX-INV2', 'on the source BOQ more than once; add it by hand'),
            ('FX-INV2', 'on the source BOQ more than once; add it by hand'),
            ('FX-OLD', 'retired from the catalogue'),
            ('ITM-900', 'not a RESCO catalogue item'),
            ('OPX-037', 'count the structures on this site'),
            ('OPX-038', 'count the structures on this site'),
        ])
        result = self._commit()
        self.assertEqual(result.context['copy_summary']['skipped'], skipped)
        self.assertEqual(self._codes(self.target), ['FX-LA1'])

    def test_whole_tin_shade_and_ballast_counts_in_nos_are_copied(self):
        self._line('OPX-038', 12)
        self._line('OPX-037', 3)
        self._commit()
        self.assertEqual(self._qty('OPX-038', self.target), D('12'))
        self.assertEqual(self._qty('OPX-037', self.target), D('3'))

    def test_overrides_are_not_copied_and_are_listed(self):
        """An override whose rule has gone is inert: the line is ordinary and copies, the
        override does not travel. (The calculated case is in CopyFormulaTests.)"""
        line = self._line('FX-LA1', 2)
        BOQLineOverride.objects.create(line=line, note='Two masts here', set_by=self.designer)
        result = self._commit()
        self.assertEqual(self._qty('FX-LA1', self.target), D('2'))
        self.assertFalse(BOQLineOverride.objects.filter(line__boq__project=self.target).exists())
        self.assertEqual(result.context['copy_summary']['overridden'],
                         [('FX-LA1', 'Conventional lightning arrester', 'Two masts here')])

    def test_accbs_copy_their_configuration_and_are_numbered_fresh_on_the_target(self):
        self._line('FX-LA1', 2)
        self._accb_add(TYPE2, self.site)
        self._accb_add(TYPE1, self.site)
        # The target used ACCB 1 and 2 before it was cleared: they stay retired.
        self._accb_add(TYPE1, self.target)
        self._accb_add(TYPE1, self.target)
        self._clear()

        self._commit()
        source, target = self._accbs(self.site), self._accbs(self.target)
        self.assertEqual([a.number for a in target], [3, 4])
        # B-3c: every option and every O/G SPD tick comes across, and has_spd with them.
        self.assertEqual([(a.config_key, a.boq_item.description, a.as_config(), a.feeders,
                           a.has_spd) for a in target],
                         [(a.config_key, a.boq_item.description, a.as_config(), a.feeders,
                           a.has_spd) for a in source])
        self.assertEqual(target[0].as_config(), TYPE2)
        self.assertEqual([a.boq_item.boq_quantity for a in target], [D('1'), D('1')])
        self.assertEqual(BOQ.objects.get(project=self.target).next_accb_number, 5)

    def test_an_accb_that_fails_todays_rules_is_skipped_and_listed(self):
        self._line('FX-LA1', 2)
        self._accb_add(TYPE2, self.site)
        self._accb_add(TYPE1, self.site)
        # B-3c: ACCB 2 as B-3 stored it — a typed rating not in the list, and no
        # per-feeder SPD (one SPD per ACCB).
        ACCBConfig.objects.filter(boq__project=self.site, number=2).update(
            feeders={'in': [{'breaker': 'MCB', 'rating': 32}],
                     'out': [{'breaker': 'MCCB', 'rating': 100}]})
        result = self._commit()
        self.assertEqual([a.number for a in self._accbs(self.target)], [1])
        self.assertEqual(self._accbs(self.target)[0].as_config(), TYPE2)
        (code, description, reason), = result.context['copy_summary']['skipped']
        self.assertEqual((code, description), ('OPX-273', 'ACCB 2'))
        self.assertIn("fails today's ACCB rules", reason)
        self.assertIn('I/C feeder 1: choose the switchgear from the list.', reason)
        self.assertIn('O/G feeder 1: SPD must be ticked or not ticked.', reason)

    def test_blank_quantities_copy_blank_and_are_counted(self):
        self._source()
        summary = self._preview().context['copy_summary']
        self.assertEqual(summary['null_quantity'], 1)
        self.assertContains(self._preview(),
                            '1 of the lines with a blank quantity, as on FX-A')
        self._commit()
        self.assertIsNone(self._qty('FX-PANEL', self.target))

    def test_mandatory_items_missing_from_the_source_are_put_back_blank(self):
        self.items['FX-LOG'].is_mandatory = True
        self.items['FX-LOG'].save(update_fields=['is_mandatory'])
        self._line('FX-LA1', 2)
        result = self._commit()
        self.assertEqual(result.context['copy_summary']['readded'], ['FX-LOG'])
        self.assertIsNone(self._qty('FX-LOG', self.target))

    def test_the_copy_is_logged_on_the_target_in_the_short_form(self):
        self._source()
        self._line('OPX-038', '16.74')
        self._commit()
        log = ActivityLog.objects.get(action_code='boq_copied')
        self.assertEqual(log.project_id, self.target.pk)
        self.assertEqual(log.action, boq_copy_log_text(self.designer, self.site, self.target,
                                                       4, 2, ['OPX-038']))
        self.assertIn('copied the BOQ from FX-A to FX-T: 4 lines, 2 ACCBs; skipped: OPX-038',
                      log.action)
        self.assertFalse(ActivityLog.objects.filter(project=self.site,
                                                    action_code='boq_copied').exists())

    def test_a_lowercase_site_code_finds_the_site(self):
        self._line('FX-LA1', 2)
        self._commit(code='fx-a')
        self.assertEqual(self._codes(self.target), ['FX-LA1'])

    def test_the_copy_link_shows_only_on_an_empty_boq_for_its_designer(self):
        entry = reverse('opex_boq_entry', kwargs={'project_id': self.target.project_id})
        self.assertContains(self.client.get(entry), 'id="boqCopyLink"')
        self._line('FX-LA1', 2, site=self.target)
        self.assertNotContains(self.client.get(entry), 'id="boqCopyLink"')
        BOQItem.objects.filter(boq__project=self.target).delete()
        self._login(self.qc)
        self.assertNotContains(self.client.get(entry), 'id="boqCopyLink"')


# ===========================================================================
# Refusals
# ===========================================================================

class CopyRefusalTests(DuplicateBase):

    def test_a_target_with_lines_is_refused_until_cleared(self):
        self._line('FX-LA1', 2)
        self._line('FX-INV1', 1, site=self.target)
        page = self.client.get(self._copy_url())
        self.assertEqual(page.context['stage'], 'not_empty')
        crafted = self._commit()
        self.assertEqual(crafted.context['stage'], 'not_empty')
        self.assertEqual(self._codes(self.target), ['FX-INV1'])

    def test_a_target_with_only_an_accb_is_not_empty(self):
        self._line('FX-LA1', 2)
        self._accb_add(TYPE1, self.target)
        self.assertEqual(self._commit().context['stage'], 'not_empty')
        self.assertEqual(self._codes(self.target), ['OPX-273'])

    def test_a_source_of_another_project_type_is_refused(self):
        residential = Project(
            project_id='FX-RES', customer_name='FXClient', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='Residential',
            status='Draft', assigned_design=self.designer, assigned_pm=self.pm)
        residential.save()
        BOQ.objects.create(project=residential)
        self._line('FX-LA1', 2, site=residential)
        response = self._preview('FX-RES')
        self.assertEqual(response.context['source_error'],
                         'FX-RES is a Residential site. Only another RESCO site\'s BOQ can '
                         'be copied here.')
        self._commit('FX-RES')
        self.assertEqual(self._target_lines(), [])

    def test_a_site_the_designer_cannot_open_reads_exactly_like_one_that_does_not_exist(self):
        """Q1: no access is added, and the box is not an oracle for which codes exist."""
        other_designer = _profile('fxdes2', 'Design')
        other, _ = self._opex_site('FX-OTHER')
        other.assigned_design = other_designer
        other.save(update_fields=['assigned_design'])
        self._line('FX-LA1', 2, site=other)

        hidden = self._preview('FX-OTHER').context['source_error']
        missing = self._preview('FX-NOPE').context['source_error']
        self.assertEqual(hidden, 'No RESCO site with the code "FX-OTHER" that you can open '
                                 'has BOQ lines to copy.')
        self.assertEqual(hidden.replace('FX-OTHER', 'X'), missing.replace('FX-NOPE', 'X'))
        self._commit('FX-OTHER')
        self.assertEqual(self._target_lines(), [])

    def test_a_deleted_source_is_refused_like_a_missing_one(self):
        self._line('FX-LA1', 2)
        Project.objects.filter(pk=self.site.pk).update(is_deleted=True)
        self.assertIn('No RESCO site with the code "FX-A"',
                      self._preview().context['source_error'])

    def test_a_source_with_no_lines_is_refused(self):
        self.assertEqual(self._preview().context['source_error'],
                         "FX-A's BOQ has no lines to copy.")          # no BOQ row at all
        self._boq(self.site)
        self.assertEqual(self._commit().context['source_error'],
                         "FX-A's BOQ has no lines to copy.")          # a BOQ row, no lines
        self.assertEqual(self._target_lines(), [])

    def test_a_source_whose_every_line_is_skipped_is_refused_and_creates_nothing(self):
        self._line('OPX-038', '16.74')
        preview = self._preview()
        self.assertEqual(preview.context['source_error'],
                         "Nothing on FX-A's BOQ can be copied to this site.")
        self.assertEqual(len(preview.context['copy_summary']['skipped']), 1)
        commit = self._commit()
        self.assertIn("Nothing on FX-A's BOQ can be copied", commit.context['source_error'])
        self.assertFalse(BOQ.objects.filter(project=self.target).exists())

    def test_the_site_itself_is_refused(self):
        self._line('FX-LA1', 2, site=self.target)
        BOQItem.objects.filter(boq__project=self.target).delete()
        self.assertEqual(self._preview('FX-T').context['source_error'],
                         'That is this site. Enter the code of the site to copy from.')

    def test_qc_head_and_pm_are_refused(self):
        self._line('FX-LA1', 2)
        for profile in (self.qc, self.head, self.pm):
            with self.subTest(role=profile.user.username):
                self._login(profile)
                self.assertEqual(self.client.get(self._copy_url()).status_code, 403)
                self.assertEqual(self._commit().status_code, 403)
        self.assertEqual(self._target_lines(), [])

    def test_a_locked_target_is_refused(self):
        self._line('FX-LA1', 2)
        self._stamp(self.target_assignment)
        self.assertEqual(self._commit().context['stage'], 'locked')
        self.assertEqual(self._target_lines(), [])
        self.target_assignment.attempts.all().delete()
        self.target_assignment.current_attempt_number = 0
        self.target_assignment.save(update_fields=['current_attempt_number'])
        self._group_lock(self.target)
        self.assertEqual(self._commit().context['stage'], 'locked')
        self.assertEqual(self._target_lines(), [])


# ===========================================================================
# C7 — formulas after the copy
# ===========================================================================

class CopyFormulaTests(DuplicateBase):

    def test_calculated_lines_are_computed_from_the_targets_own_lines(self):
        self._rules()
        self._picker({'FX-LA1': 2, 'FX-INV1': 3})          # source: EARTH 10, LOG 1, RS485 5
        earth = self._lines('FX-EARTH')[0]
        self._override('set', earth, quantity='99', note='Long run on FX-A')
        self.assertEqual(self._qty('FX-EARTH'), D('99'))

        result = self._commit()
        summary = result.context['copy_summary']
        self.assertEqual(summary['calculated'], ['FX-EARTH', 'FX-LOG', 'FX-RS485'])
        self.assertEqual(summary['overridden'],
                         [('FX-EARTH', 'Earthing cable 16 sq mm', 'Long run on FX-A')])
        # Computed on the target from its copied drivers, not carried from the source.
        self.assertEqual(self._qty('FX-EARTH', self.target), D('10'))
        self.assertEqual(self._qty('FX-LOG', self.target), D('1'))
        self.assertEqual(self._qty('FX-RS485', self.target), D('5'))
        self.assertFalse(BOQLineOverride.objects.filter(line__boq__project=self.target).exists())
        recalcs = BOQRecalculation.objects.filter(boq__project=self.target)
        self.assertEqual(sorted(recalcs.values_list('item_code', 'quantity_after', 'source')), [
            ('FX-EARTH', D('10'), FORMULA_SOURCE_COPY), ('FX-LOG', D('1'), FORMULA_SOURCE_COPY),
            ('FX-RS485', D('5'), FORMULA_SOURCE_COPY)])
        self.assertTrue(all(r.actor_id == self.designer.pk for r in recalcs))

    def test_an_accb_line_drives_a_rule_on_the_target(self):
        """ACCB lines count as drivers (sheet_inputs), so the copied ACCBs are on the sheet
        before the engine runs, and the preview predicted the same figure."""
        self._rule('FX-WMS', '1 weather station per ACCB',
                   [{'item': self.accb_master, 'label': 'ACCBs'}])
        self._line('FX-LA1', 2)
        self._accb_add(TYPE2, self.site)
        self._accb_add(TYPE1, self.site)
        preview = self._preview().context['copy_summary']
        self._commit()
        self.assertEqual(self._qty('FX-WMS', self.target), D('2'))
        self.assertIn((self.items['FX-WMS'].pk, D('2')), preview['computed'])

    def test_with_no_rules_nothing_runs_beyond_the_gate(self):
        self._source()
        with CaptureQueriesContext(connection) as ctx:
            self._commit()
        rule_reads = [q['sql'] for q in ctx.captured_queries
                      if 'boqformularule' in q['sql'].lower()]
        self.assertEqual(len(rule_reads), 1)            # formula_rules_exist()'s exists()
        self.assertFalse(BOQRecalculation.objects.exists())


# ===========================================================================
# Preview equals commit, the source untouched, no corrections
# ===========================================================================

class PreviewEqualsCommitTests(DuplicateBase):

    def _rich_source(self):
        self._source()
        self._line('OPX-038', '16.74')
        self._line('OPX-037', 4)
        self.items['FX-RS485'].is_mandatory = True
        self.items['FX-RS485'].save(update_fields=['is_mandatory'])

    def _compare(self):
        preview = self._preview()
        self.assertEqual(preview.context['stage'], 'preview')
        commit = self._commit()
        self.assertEqual(commit.context['stage'], 'result')
        before, after = preview.context['copy_summary'], commit.context['copy_summary']
        self.assertEqual(set(before), set(after))
        for field in before:
            with self.subTest(field=field):
                self.assertEqual(before[field], after[field])
        return after

    def test_preview_equals_commit_field_by_field_with_no_rules(self):
        self._rich_source()
        summary = self._compare()
        self.assertEqual(summary['lines'], 5)
        self.assertEqual([number for _source, number, _text in summary['accbs']], [1, 2])

    def test_preview_equals_commit_field_by_field_with_five_rules(self):
        self._five_rules()
        self._rich_source()
        self._accb_add(TYPE1, self.target)        # numbers 1 retired on the target
        self._clear()
        summary = self._compare()
        self.assertEqual([number for _source, number, _text in summary['accbs']], [2, 3])
        self.assertTrue(summary['computed'])


class SourceUntouchedTests(DuplicateBase):

    def test_the_source_is_never_written(self):
        self._rules()
        self._picker({'FX-LA1': 2, 'FX-INV1': 3})
        self._override('set', self._lines('FX-EARTH')[0], quantity='99', note='Long run')
        self._off_line(self.site, 'Site-made bracket', 4)
        self._accb_add(TYPE2, self.site)
        self._accb_add(TYPE1, self.site)

        def snapshot():
            return (list(BOQItem.objects.filter(boq__project=self.site).order_by('pk').values()),
                    list(ACCBConfig.objects.filter(boq__project=self.site).order_by('pk')
                         .values()),
                    list(BOQ.objects.filter(project=self.site).values()),
                    list(BOQLineOverride.objects.filter(line__boq__project=self.site)
                         .values()),
                    BOQRecalculation.objects.filter(boq__project=self.site).count(),
                    ActivityLog.objects.filter(project=self.site).count())

        before = snapshot()
        self._preview()
        self._commit()
        self.assertEqual(self._qty('FX-LA1', self.target), D('2'))     # the copy happened
        self.assertEqual(snapshot(), before)


class NoCorrectionTests(DuplicateBase):

    def test_copy_and_clear_write_no_correction_and_leave_the_panel_alone(self):
        self._source()
        correction = BOQCorrection.objects.create(
            boq=self._boq(self.site), item_code='FX-LA1', item_description='x',
            action=BOQ_CORRECTION_QUANTITY_CHANGED, quantity_before=D('1'),
            quantity_after=D('2'), corrected_by=self.qc)
        counted = list(reviewer_corrections().values_list('pk', flat=True))
        self._commit()
        self._clear()
        self.assertEqual(list(BOQCorrection.objects.values_list('pk', flat=True)),
                         [correction.pk])
        self.assertEqual(list(reviewer_corrections().values_list('pk', flat=True)), counted)


# ===========================================================================
# The log line and the query budget
# ===========================================================================

class LogTextTests(DuplicateBase):

    def test_the_longest_copy_line_fits_the_column(self):
        long_name = _profile('fxlong', 'Design')
        long_name.user.first_name = 'A' * 150
        long_name.user.last_name = 'B' * 150
        long_name.user.save(update_fields=['first_name', 'last_name'])
        site = Project(project_id='X' * 30)
        codes = [f'OPX-{n:03d}' for n in range(200)]
        text = boq_copy_log_text(long_name, site, site, 9999, 999, codes)
        self.assertLessEqual(len(text), 255)
        self.assertTrue(text.endswith('…'))
        self.assertLessEqual(len(boq_clear_log_text(long_name, site, 9999, 999)), 255)


class QueryCountTests(DuplicateBase):

    def _count(self, call):
        with CaptureQueriesContext(connection) as ctx:
            call()
        return len(ctx)

    def _picker_counts(self):
        empty_url = reverse('opex_boq_entry', kwargs={'project_id': self.target.project_id})
        full_url = reverse('opex_boq_entry', kwargs={'project_id': self.site.project_id})
        self.client.get(empty_url)               # warm the session, as the baseline was taken
        return {'empty': self._count(lambda: self.client.get(empty_url)),
                'full': self._count(lambda: self.client.get(full_url))}

    def _sheet(self):
        for code, quantity in self.SHEET:
            self._line(code, quantity)

    def test_the_picker_costs_what_it_did_before_b4(self):
        self._sheet()
        self.assertEqual(self._picker_counts(), PICKER_BASELINE)

    def test_the_picker_with_five_rules_costs_what_it_did_before_b4(self):
        self._sheet()
        self._five_rules()
        self.assertEqual(self._picker_counts(), PICKER_BASELINE_FIVE_RULES)

    def _post_counts(self):
        self._source()
        counts = {'preview': self._count(self._preview),
                  'commit': self._count(self._commit)}
        counts['clear'] = self._count(self._clear_no_follow)
        return counts

    def _clear_no_follow(self):
        return self.client.post(
            reverse('opex_boq_clear', kwargs={'project_id': self.target.project_id}),
            {'confirm_code': self.target.project_id})

    def test_copy_and_clear_posts_with_no_rules(self):
        self.assertEqual(self._post_counts(), COPY_COUNTS)

    def test_copy_and_clear_posts_with_five_rules(self):
        self._five_rules()
        self.assertEqual(self._post_counts(), COPY_COUNTS_FIVE_RULES)


#: The copy and clear POSTs, measured on this build with DuplicateBase._source() (three
#: catalogue lines, one line with no catalogue item, two ACCBs) under solarpms.test_settings.
#: The commit is one INSERT per copied line (the upload's write loop) plus four per ACCB
#: (_accb_create: the max(), the counter update, the line and the configuration); the source
#: is one read. The clear is constant: two counts, the two order checks and one delete
#: (Django's collector: a SELECT, two cascades, three SET NULLs, the DELETE). Five rules add
#: load_rules() to the preview, and to the commit load_rules(), the lock, the sheet read,
#: the calculated lines the engine adds and their one recalculation INSERT.
COPY_COUNTS = {'preview': 21, 'commit': 44, 'clear': 24}
COPY_COUNTS_FIVE_RULES = {'preview': 24, 'commit': 55, 'clear': 24}
