"""
apply_dw5_modules — modules by wattage on a database (DW-5, 27 Nov 2026).

    python manage.py apply_dw5_modules            # dry run (the default)
    python manage.py apply_dw5_modules --apply

ONE COMMAND, ONE TRANSACTION (Zuber, Q8). The steps depend on each other and the states
between them are unsafe: with the wattage items created but OPX-001 still mandatory, a
designer who picks 640 Wp gets OPX-001 put back blank and cannot mark the BOQ complete;
with the items but not the rules, earthing sets ignore the new lines. So, all at once:

  1. the four module items (M1): OPX-275..278, 550 / 620 / 630 / 640 Wp, category Module,
     unit Nos, positions 12 / 14 / 16 / 18, `is_module`, each logged like a catalogue create;
  2. OPX-001 marked `is_module` (its wattage stays not recorded), logged;
  3. the design settings row at 640 Wp (M8, Q14), logged;
  4. the rules file (load_formula_rules' own plan and write, on DW5_FILE, the DW-5 file):
     creates the open group "all modules" and replaces R12 so OPX-177 reads the group (M4).
     REFUSED if the file would write anything else: no other rule may change (stop rule);
  5. OPX-001's mandatory flag cleared and the item retired (M5, M6), logged.

THE DRY RUN DOES IT ALL AND ROLLS IT BACK, so what it prints is exactly what --apply would
write (the rules plan needs the new items to exist, which only a real write can give it).
Nothing is kept: no item, no log entry. A second --apply finds everything done and writes
nothing. No BOQ line is touched: existing OPX-001 lines keep their quantities, count in
"all modules", and are recalculated from only when their BOQ is next saved (no sweep).
"""
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from projects.forms import catalogue_snapshot, log_catalogue_change
from projects.models import (
    BOQItemMaster, DesignSettings, MODULE_GROUP_NAME, log_activity,
)

from ._demo_support import print_db_banner
from .load_formula_rules import (
    DEFAULT_FILE, LOADED_ACTION_CODE, REPLACE, CREATE, WRITES, G_SAME,
    plan_load, read_file, write_plan,
)

# DW-6: pinned to the DW-5 file. load_formula_rules' default moved on to the DW-6 file, which
# adds INC2P; this command's own check refuses any rule write but R12's, and the INC 2P rule
# is loaded by load_formula_rules only (and on production only after Praveen's answer).
DW5_FILE = DEFAULT_FILE.parent / 'dw5_formula_rules_27nov.json'

ACTOR = 'apply_dw5_modules (system)'
ACTION_CODE = 'dw5_modules_applied'

LEGACY_CODE = 'OPX-001'
#: (code, wattage, position). Fixed codes, because the rules file names them.
MODULE_ITEMS = (
    ('OPX-275', 550, 12),
    ('OPX-276', 620, 14),
    ('OPX-277', 630, 16),
    ('OPX-278', 640, 18),
)
DEFAULT_WP = 640
#: The one rule DW-5 changes (R12: OPX-177, 2 earthing sets per module).
RULE_KEY = 'R12'


class Refused(Exception):
    """A reason to write nothing; raised inside the transaction so it rolls back."""


def _description(wp):
    # Praveen may reword these; the Head can edit them on the catalogue (Zuber, 27 Nov).
    return f'Solar PV Module {wp} Wp'


class Command(BaseCommand):
    help = ('DW-5: the module items by wattage, the "all modules" group and R12, the design '
            'settings row, and OPX-001 retired, in one transaction. Dry run unless --apply.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write. Without it everything is worked out and rolled back.')
        parser.add_argument('--file', default=str(DW5_FILE),
                            help='The rules file (default: the DW-5 file in projects/data).')

    def _say(self, text=''):
        """One printable line (cp1252 on a piped Windows console): see load_formula_rules."""
        encoding = getattr(self.stdout._out, 'encoding', None) or 'utf-8'
        self.stdout.write(text.encode(encoding, 'replace').decode(encoding))

    def handle(self, *args, **options):
        print_db_banner(self)
        apply = options['apply']
        path = Path(options['file'])
        if not path.is_file():
            raise CommandError(f'No such file: {path}')
        groups, rules, sha = read_file(path)
        self._say(f"apply_dw5_modules - {'APPLY' if apply else 'DRY RUN (worked out, then rolled back; use --apply to write)'}")
        self._say(f'rules file: {path.name}   checksum (parsed rules): {sha}')

        self.lines = []
        try:
            with transaction.atomic():
                written = self._run(groups, rules, sha, path)
                if not apply:
                    # Everything above really ran, so the plan it printed is exact; none
                    # of it is kept.
                    transaction.set_rollback(True)
        except Refused as exc:
            for line in self.lines:
                self._say(line)
            raise CommandError(f'Refused, nothing written: {exc}')
        for line in self.lines:
            self._say(line)
        if not written:
            self._say('Nothing to do: the database already has DW-5.')
        elif apply:
            self._say('Written: ' + ', '.join(written))
        else:
            self._say('Would write: ' + ', '.join(written))

    # -- the steps -------------------------------------------------------------------

    def _run(self, groups, rules, sha, path):
        written = []
        legacy = BOQItemMaster.objects.filter(code=LEGACY_CODE, project_type='OPEX').first()
        if legacy is None:
            raise Refused(f'{LEGACY_CODE} is not in the RESCO catalogue.')

        # 1. The module items.
        for code, wp, position in MODULE_ITEMS:
            item = BOQItemMaster.objects.filter(code=code).first()
            if item is not None:
                # Identity only: the Head may since have reworded or moved it.
                if (item.project_type, item.is_module, item.module_wp) != ('OPEX', True, wp):
                    raise Refused(f'{code} already exists and is not the {wp} Wp module item '
                                  f'({item.description}).')
                self.lines.append(f'  same    {code}  {item.description}')
                continue
            taken = (BOQItemMaster.objects
                     .filter(project_type='OPEX', is_active=True, is_module=True, module_wp=wp)
                     .first())
            if taken is not None:
                raise Refused(f'{wp} Wp is already {taken.code} ({taken.description}).')
            clash = (BOQItemMaster.objects
                     .filter(project_type='OPEX', is_active=True, sort_order=position)
                     .first())
            if clash is not None:
                raise Refused(f'position {position} is already used by {clash.code} '
                              f'({clash.description}).')
            item = BOQItemMaster(code=code, project_type='OPEX', description=_description(wp),
                                 unit='Nos', category='Module', is_active=True,
                                 is_mandatory=False, is_module=True, module_wp=wp,
                                 sort_order=position)
            item.save()
            log_catalogue_change(None, item, f"{ACTOR} created RESCO BOQ catalogue item "
                                             f"'{code}'", None,
                                 action_code='design_boq_item_created')
            self.lines.append(f'  create  {code}  {item.description}  (position {position})')
            written.append(f'item {code}')

        # 2. OPX-001 is a module (wattage not recorded).
        if not legacy.is_module:
            before = catalogue_snapshot(legacy)
            legacy.is_module = True
            legacy.save(update_fields=['is_module', 'updated_at'])
            log_catalogue_change(None, legacy, f"{ACTOR} updated RESCO BOQ catalogue item "
                                               f"'{LEGACY_CODE}'", before,
                                 action_code='design_boq_item_updated')
            self.lines.append(f'  module  {LEGACY_CODE}  marked a module (wattage not recorded)')
            written.append(f'{LEGACY_CODE} module')

        # 3. The design settings row.
        if not DesignSettings.objects.filter(pk=1).exists():
            row = DesignSettings(pk=1, default_module_wp=DEFAULT_WP)
            row.save()
            log_activity(None, None, f'{ACTOR}: design settings: default module wattage: → '
                                     f'{DEFAULT_WP} Wp', entity_type='DesignSettings',
                         entity_id=row.pk, action_code='design_settings_changed')
            self.lines.append(f'  create  design settings, default module wattage {DEFAULT_WP} Wp')
            written.append('design settings')

        # 4. The rules: load_formula_rules' own plan, on the items just written.
        plan = plan_load(groups, rules)
        if plan.problems:
            for problem in plan.problems:
                self.lines.append(f'  REFUSED {problem}')
            raise Refused('the rules file has problems (above).')
        unexpected = [f"group {g.entry['name']}" for g in plan.groups
                      if g.kind != G_SAME and g.entry['name'] != MODULE_GROUP_NAME]
        unexpected += [f"rule {s.entry['key']} ({s.kind})" for s in plan.steps
                       if s.kind in WRITES
                       and not (s.entry['key'] == RULE_KEY and s.kind in (REPLACE, CREATE))]
        if unexpected:
            raise Refused('the rules file would also change ' + ', '.join(unexpected)
                          + '; DW-5 changes only "all modules" and R12.')
        for g in plan.groups:
            if g.kind != G_SAME:
                self.lines.append(f"  group   {g.kind} {g.entry['name']}: "
                                  f"add {', '.join(g.add) or 'nothing'}")
        for s in plan.steps:
            if s.kind in WRITES:
                retired = (f' (rule #{s.active_rule.pk} retired, kept on record)'
                           if s.active_rule is not None else '')
                self.lines.append(f"  rule    {s.kind} {s.entry['key']} {s.entry['target']}: "
                                  f"{s.entry['summary']}, driven by "
                                  f"{', '.join(d.get('group') or d.get('item') for d in s.entry['drivers'])}"
                                  f"{retired}")
        if plan.to_write():
            try:
                counts = write_plan(plan)
            except ValidationError as exc:
                raise Refused('a rule save was refused: ' + ' '.join(exc.messages))
            log_activity(None, None,
                         f'{ACTOR} applied {path.name} (checksum {sha[:12]}): '
                         + ', '.join(f'{n} {k}' for k, n in counts.items()),
                         entity_type='BOQFormulaRule', action_code=LOADED_ACTION_CODE)
            written.append('rules (' + ', '.join(f'{n} {k}' for k, n in counts.items()) + ')')

        # 5. OPX-001 retired for new picks; its lines stay and keep counting (answer 9).
        if legacy.is_mandatory or legacy.is_active:
            before = catalogue_snapshot(legacy)
            legacy.is_mandatory = False
            legacy.is_active = False
            legacy.save(update_fields=['is_mandatory', 'is_active', 'updated_at'])
            log_catalogue_change(None, legacy, f"{ACTOR} retired RESCO BOQ catalogue item "
                                               f"'{LEGACY_CODE}'", before,
                                 action_code='design_boq_item_deactivated')
            self.lines.append(f'  retire  {LEGACY_CODE}  mandatory: no, active: no '
                              f'(its {legacy.boq_items.count()} BOQ line(s) are not touched)')
            written.append(f'{LEGACY_CODE} retired')

        if written:
            log_activity(None, None, f'{ACTOR}: ' + ', '.join(written),
                         entity_type='BOQItemMaster', action_code=ACTION_CODE)
        return written
