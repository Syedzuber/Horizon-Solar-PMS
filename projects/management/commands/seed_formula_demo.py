"""
B-2b — seed (or remove) three DEMO formula rules on a LOCAL database, for the browser check.

    python manage.py seed_formula_demo --site <RESCO site code>
    python manage.py seed_formula_demo --remove

LOCAL AND DEBUG ONLY, BOTH (Zuber, B-2b Q10). It refuses unless settings.DEBUG is on AND the
database host is this machine (_demo_support.database_is_local(), the interlock the order
demo uses). There is no override flag: rules are global, so a demo rule on a shared
database would recalculate every RESCO BOQ saved there.

THE RULES ARE ON EXISTING CATALOGUE ITEMS, so the picker shows real lines:
    plain   OPX-087 earthing cable    = 5 m x (conventional LA OPX-175 + ESE LA OPX-176)
    group   OPX-184 data logger       = all inverters / 10, rounded up, at least 1
    chained OPX-185 RS485 cable       = 5 m x data loggers (OPX-184, itself computed)
They are DEMO data, not Praveen's rules (those are B-2c). Every rule's summary starts with
DEMO_SUMMARY_PREFIX and the group's name with DEMO_GROUP_PREFIX.

--remove SELECTS BY THOSE PREFIXES, IN THE FORMULA TABLES ONLY. That is a name match, which
_demo_support's manifest exists to avoid for site data; it is acceptable here because the
four formula tables hold no real rows until B-2c and the match never touches a BOQ, a site
or a user. It deletes the demo rules' recalculation records, overrides on lines whose item
no other active rule computes, the rules (drivers go with them) and the group. BOQ LINES ARE
LEFT AS THEY ARE: a computed line becomes an ordinary line the designer can edit or remove.

--site only names the site the instructions are printed for; the rules apply to every
RESCO BOQ saved on this database.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ._demo_support import database_is_local, print_db_banner

DEMO_SUMMARY_PREFIX = 'DEMO — '
DEMO_GROUP_PREFIX = 'demo:'

EARTH_CABLE = 'OPX-087'
LIGHTNING_ARRESTERS = ('OPX-175', 'OPX-176')
DATA_LOGGER = 'OPX-184'
RS485_CABLE = 'OPX-185'
INVERTERS = ('OPX-008', 'OPX-009', 'OPX-010', 'OPX-011', 'OPX-012', 'OPX-013', 'OPX-014',
             'OPX-015', 'OPX-016', 'OPX-017', 'OPX-018', 'OPX-019', 'OPX-020', 'OPX-021',
             'OPX-022', 'OPX-208')

REFUSE_DEBUG = ('seed_formula_demo runs only with DEBUG on. It writes formula rules, which '
                'apply to every RESCO BOQ saved on this database.')
REFUSE_HOST = ('seed_formula_demo runs only against a local database (host localhost or a '
               'socket). Formula rules apply to every RESCO BOQ saved on the database.')


class Command(BaseCommand):
    help = ('Seed (or --remove) three DEMO formula rules on existing catalogue items, for '
            'the B-2b browser check. Local database and DEBUG only.')

    def add_arguments(self, parser):
        parser.add_argument('--site', help='The RESCO site code to print instructions for.')
        parser.add_argument('--remove', action='store_true',
                            help='Remove the DEMO rules, their group and their records.')

    def handle(self, *args, **options):
        print_db_banner(self)
        if not settings.DEBUG:
            raise CommandError(REFUSE_DEBUG)
        if not database_is_local():
            raise CommandError(REFUSE_HOST)
        if options['remove']:
            self._remove()
            return
        if not options['site']:
            raise CommandError('Name the site to try it on: --site <RESCO site code>.')
        self._seed(options['site'])

    # -- seed --------------------------------------------------------------------

    def _seed(self, site_code):
        # Lazy import: management commands import models at call time in this project
        # (no circular import here; it keeps module import cheap for `manage.py help`).
        from projects.models import (
            BOQFormulaDriver, BOQFormulaRule, BOQItemGroup, BOQItemGroupMember,
            BOQItemMaster, Project,
        )

        site = Project.objects.filter(project_id=site_code, is_deleted=False).first()
        if site is None or site.project_type != 'OPEX':
            raise CommandError(f'{site_code} is not a RESCO site on this database.')

        if BOQFormulaRule.objects.filter(summary__startswith=DEMO_SUMMARY_PREFIX).exists():
            raise CommandError('The DEMO rules are already seeded. Run with --remove first.')

        codes = (EARTH_CABLE, DATA_LOGGER, RS485_CABLE) + LIGHTNING_ARRESTERS + INVERTERS
        items = {m.code: m for m in BOQItemMaster.objects.filter(code__in=codes,
                                                                 project_type='OPEX')}
        missing = [code for code in codes if code not in items]
        if missing:
            raise CommandError('These catalogue items are not on this database: '
                               + ', '.join(missing) + '.')

        targets = [items[EARTH_CABLE], items[DATA_LOGGER], items[RS485_CABLE]]
        taken = list(BOQFormulaRule.objects.filter(is_active=True, target__in=targets)
                     .values_list('target__code', flat=True))
        if taken:
            raise CommandError('These items already have an active rule, so the demo would '
                               'clash with it: ' + ', '.join(taken) + '.')

        # Row by row through save(), never bulk_create: every save re-checks the whole rule
        # graph (B-2a), and bulk_create would skip that.
        with transaction.atomic():
            inverters = BOQItemGroup(name=f'{DEMO_GROUP_PREFIX} inverters', label='inverters')
            inverters.save()
            for code in INVERTERS:
                BOQItemGroupMember(group=inverters, item=items[code]).save()

            logger = BOQFormulaRule(
                target=items[DATA_LOGGER], summary=f'{DEMO_SUMMARY_PREFIX}1 data logger per '
                                                   f'10 inverters',
                divisor=10, floor_one_when_driven=True)
            logger.save()
            BOQFormulaDriver(rule=logger, group=inverters).save()

            rs485 = BOQFormulaRule(
                target=items[RS485_CABLE], summary=f'{DEMO_SUMMARY_PREFIX}5 m RS485 cable per '
                                                   f'data logger',
                multiplier=5)
            rs485.save()
            BOQFormulaDriver(rule=rs485, item=items[DATA_LOGGER], label='data loggers').save()

            earth = BOQFormulaRule(
                target=items[EARTH_CABLE], summary=f'{DEMO_SUMMARY_PREFIX}5 m earthing cable '
                                                   f'per lightning arrester',
                multiplier=5)
            earth.save()
            BOQFormulaDriver(rule=earth, item=items['OPX-175'], label='conventional LA').save()
            BOQFormulaDriver(rule=earth, item=items['OPX-176'], label='ESE LA').save()

        w = self.stdout.write
        w('Seeded 3 DEMO rules (they apply to every RESCO BOQ saved on this database):')
        w(f'  plain    {EARTH_CABLE} = 5 m x (OPX-175 + OPX-176)')
        w(f'  group    {DATA_LOGGER} = inverters / 10, rounded up, at least 1')
        w(f'  chained  {RS485_CABLE} = 5 m x {DATA_LOGGER}')
        w(f'Try it on {site.project_id}: /projects/{site.project_id}/boq/entry/ (as '
          f'{site.assigned_design.user.username if site.assigned_design_id else "its designer"}).'
          f' Add an inverter or a lightning arrester with a quantity and save.')
        w('Remove with: python manage.py seed_formula_demo --remove')

    # -- remove ------------------------------------------------------------------

    def _remove(self):
        # Lazy import, as in _seed().
        from projects.models import (
            BOQFormulaRule, BOQItemGroup, BOQLineOverride, BOQRecalculation,
        )

        rules = BOQFormulaRule.objects.filter(summary__startswith=DEMO_SUMMARY_PREFIX)
        target_ids = set(rules.values_list('target_id', flat=True))
        with transaction.atomic():
            # Records first: BOQRecalculation.rule is PROTECT.
            records, _ = BOQRecalculation.objects.filter(rule__in=rules).delete()
            # Overrides only on items no other active rule computes; elsewhere they still mean
            # something.
            still_computed = set(BOQFormulaRule.objects.filter(is_active=True)
                                 .exclude(pk__in=rules).values_list('target_id', flat=True))
            overrides, _ = BOQLineOverride.objects.filter(
                line__item_master_id__in=target_ids - still_computed).delete()
            rule_count = rules.count()
            rules.delete()          # drivers go with them (CASCADE)
            _total, detail = (BOQItemGroup.objects
                              .filter(name__startswith=DEMO_GROUP_PREFIX).delete())
        groups = detail.get('projects.BOQItemGroup', 0)
        self.stdout.write(f'Removed {rule_count} DEMO rule(s), {groups} group(s), {records} '
                          f'recalculation record(s) and {overrides} override(s). BOQ lines '
                          f'are left as they are; computed ones are now ordinary lines.')
