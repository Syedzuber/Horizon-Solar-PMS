"""
normalise_catalogue_units — rewrite old unit spellings in the BOQ catalogue to the one
shared list (B-0, ruling 13).

    python manage.py normalise_catalogue_units            # dry run (the default)
    python manage.py normalise_catalogue_units --apply    # write and log each change

WHAT IT CHANGES: BOQItemMaster.unit only, and only where the stored value is a known old
spelling of a list code (projects/units.py UNIT_SPELLINGS: 'No.' and 'No' → Nos,
'Mtr' → Meter, 'LOT' → Lot). That is a spelling fix, never a change of unit, so ruling 12
(no unit change after use) is not engaged.

WHAT IT NEVER CHANGES:
- BOQ lines (BOQItem). A line keeps the unit text it was created with (ruling 13).
- A unit with no mapping. It is listed for a person to decide, and left alone.
- OPX-038 Tin Shade. Ruling 11 (its unit) is still open with Praveen; its 'No' would
  map to Nos, which would settle that question by spelling. It is listed as skipped.

Each applied change is logged through the same before/after text as the catalogue
screens (forms.log_catalogue_change), with no actor and the command named in the text.
Running it twice changes nothing the second time: every mapped value is a list code
after the first run, and list codes are not keys of the map.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from projects.forms import catalogue_snapshot, log_catalogue_change
from projects.models import BOQItemMaster
from projects.units import UNIT_CODES, UNIT_SPELLINGS

#: Items left alone whatever their unit, each with the reason printed beside it.
SKIPPED_CODES = {
    'OPX-038': 'ruling 11 (Tin Shade unit) still open',
}


class Command(BaseCommand):
    help = ('Rewrite old catalogue unit spellings (No., No, Mtr, LOT) to the shared unit '
            'list. Dry run unless --apply. Never touches BOQ lines.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write the changes. Without it, only print them.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Print only (the default; accepted for clarity).')

    def handle(self, *args, **options):
        apply = options['apply']
        if apply and options['dry_run']:
            self.stderr.write('Choose one of --apply and --dry-run.')
            return

        # Every catalogue row, both templates, with the number of BOQ lines built from
        # it — printed so a reader sees what each rename sits under. ALL lines, deleted
        # sites included: the count is information here, not a filter.
        items = (BOQItemMaster.objects
                 .annotate(line_count=Count('boq_items'))
                 .order_by('project_type', 'sort_order', 'code'))

        mode = 'APPLY' if apply else 'DRY RUN (nothing written; use --apply to write)'
        self.stdout.write(f'normalise_catalogue_units — {mode}')

        changed, skipped, unmapped, already = [], [], [], 0
        for item in items:
            if item.unit in UNIT_CODES:
                already += 1
                continue
            if item.code in SKIPPED_CODES:
                skipped.append(item)
                continue
            target = UNIT_SPELLINGS.get(item.unit)
            if target is None:
                unmapped.append(item)
                continue
            changed.append((item, target))

        for item, target in changed:
            self.stdout.write(f'  {item.code:<10} {item.unit} → {target}'
                              f'   ({item.line_count} BOQ lines)')
            if apply:
                self._apply(item, target)

        for item in skipped:
            self.stdout.write(f'  SKIPPED  {item.code:<10} {item.unit!r}'
                              f'   ({item.line_count} BOQ lines) — {SKIPPED_CODES[item.code]}')
        for item in unmapped:
            self.stdout.write(f'  NO MAPPING  {item.code:<10} {item.unit!r}'
                              f'   ({item.line_count} BOQ lines) — left unchanged')

        verb = 'changed' if apply else 'would change'
        self.stdout.write(f'{len(changed)} {verb}, {len(skipped)} skipped, '
                          f'{len(unmapped)} with no mapping, {already} already on the list.')

    def _apply(self, item, target):
        """Rewrite one item's unit and log it, in one transaction.

        RACE: the UPDATE is conditional on the unit still being the value printed. If a
        catalogue screen saved this row after the list was read, the condition fails, 0
        rows update, nothing is logged, and the row is reported as left alone. The screen
        save already stored a list code (the form allows nothing else), so there is
        nothing left for this command to do on it.
        """
        old = item.unit
        before = catalogue_snapshot(item)
        with transaction.atomic():
            updated = (BOQItemMaster.objects
                       .filter(pk=item.pk, unit=old)
                       .update(unit=target, updated_at=timezone.now()))
            if not updated:
                self.stdout.write(f'    {item.code}: changed since it was read — left alone')
                return
            item.unit = target
            kind = 'RESCO ' if item.project_type == 'OPEX' else ''
            log_catalogue_change(
                None, item,
                f"normalise_catalogue_units (system) updated {kind}BOQ catalogue item "
                f"'{item.code}'",
                before, action_code='boq_catalogue_unit_fixed')
