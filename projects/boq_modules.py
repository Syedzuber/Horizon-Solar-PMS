"""DW-5 — modules by wattage (rulings DW-R11, DW-R14, answers 9 and 11).

A site's modules are one catalogue item per wattage (550, 620, 630, 640 Wp at launch; the
Head adds more by adding an item). Each is `is_module` with a `module_wp`. OPX-001 "Solar PV
Module", which predates wattages, is `is_module` with no wattage and is retired for new
picks: its existing lines stay, count in the "all modules" group, and show as "Wattage not
recorded" until the designer moves their count to a wattage (never on its own).

WHAT LIVES HERE: which catalogue items the Modules pop-up offers, how the site's module
lines are split into "one line per wattage" and "other module lines", how the pop-up's POST
becomes creates/updates/deletes (planned here, written by the picker inside its own
transaction so the rules recalculate once), and the Head's group-membership sync.

MODULE LINES ARE WRITTEN ONLY BY THE POP-UP on the picker. The picker's per-item sheet and
its off-catalogue block leave them out, so a save that does not carry the Modules section
(a page drawn before DW-5, a hand-built POST) leaves every module line as it was. The DW-7
upload and a reviewer's quantity correction still reach wattage lines as ordinary lines.
"""
import logging
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError

from .models import (
    BOQItem, BOQItemGroup, BOQItemGroupMember, MODULE_GROUP_NAME, log_activity,
)
from .number_input import decimal_field_max, parse_decimal_input

logger = logging.getLogger(__name__)

#: The label of a module line whose item has no wattage (OPX-001).
WATTAGE_NOT_RECORDED = 'Wattage not recorded'

#: What the picker says when the pop-up names one wattage twice (Zuber, Q6: refused, the
#: designer puts the whole count on one row).
MODULE_DUPLICATE_MESSAGE = ('{label} is chosen twice. Each wattage can appear once: put its '
                            'whole count on one row.')
MODULE_NO_WATTAGE_MESSAGE = 'Choose a wattage on every module row.'
MODULE_NO_COUNT_MESSAGE = 'Enter a count for {label}, or set it to 0 to remove it.'
MODULE_UNREADABLE_MESSAGE = ('The module rows could not be read. Reload the page and enter '
                             'them again.')

#: The reviewer's add_line refusal (M9): reviewers correct a module line's quantity, they do
#: not add module lines.
MODULE_REVIEWER_ADD_MESSAGE = ('Modules are entered by the designer, one line per wattage. '
                               'Correct an existing module line\'s quantity instead.')

#: The upload's refusal for a row on OPX-001 or any retired module item (P7, Zuber Q11).
MODULE_RETIRED_UPLOAD_MESSAGE = ('item code "{code}" ({description}) is retired: modules are '
                                 'now one item per wattage. Download the BOQ again and enter '
                                 'modules on the wattage rows.')

#: The B-4 copy's reason for skipping an OPX-001 line (Zuber Q12).
MODULE_RETIRED_COPY_REASON = ('retired from the catalogue: modules are one item per wattage '
                              'now; add them in Modules')


def module_choices(catalogue):
    """The wattages the pop-up offers: the ACTIVE module items with a wattage, lowest first.

    `catalogue` is get_opex_boq_catalogue() (already active and RESCO), so this costs no
    query; the picker has it loaded.
    """
    return sorted((m for m in catalogue if m.is_module and m.module_wp),
                  key=lambda m: (m.module_wp, m.sort_order, m.code))


def module_label(master):
    """'640 Wp', or 'Wattage not recorded' for OPX-001."""
    if master is None or not master.module_wp:
        return WATTAGE_NOT_RECORDED
    return f'{master.module_wp} Wp'


def is_module_line(line):
    """True for a BOQ line on a module item. item_master must be joined in (no query)."""
    return line.item_master_id is not None and line.item_master.is_module


def split_module_lines(lines, module_by_id):
    """(wattage_lines, other_lines) for a BOQ's module lines, in the order given.

    `wattage_lines` maps an active wattage item's pk to its line: the one line the pop-up's
    row for that wattage stands for. Every other module line is in `other_lines` and is
    addressed by its own pk: an OPX-001 line, a line on a module item the Head has retired,
    or a second line on one wattage (none can be made by the pop-up; one could arrive by
    other means, and it must stay visible rather than be folded away).
    """
    wattage, other = {}, []
    for line in lines:
        if line.item_master_id in module_by_id and line.item_master_id not in wattage:
            wattage[line.item_master_id] = line
        else:
            other.append(line)
    return wattage, other


def other_line_label(line, module_by_id):
    """How an "other" module line is named in the block and the pop-up."""
    master = line.item_master
    if not master.module_wp:
        return WATTAGE_NOT_RECORDED
    if master.pk in module_by_id:
        return f'{master.module_wp} Wp (another line)'
    return f'{master.module_wp} Wp (retired)'


@dataclass
class ModuleSave:
    """What the pop-up's POST does to this BOQ's module lines. Written by the picker."""
    create: list = field(default_factory=list)   # [(master, quantity)]
    update: list = field(default_factory=list)   # [(line, quantity)]
    delete: list = field(default_factory=list)   # [line]
    errors: list = field(default_factory=list)

    @property
    def changes(self):
        return bool(self.create or self.update or self.delete)


def _blank(raw):
    return raw is None or not str(raw).strip()


def _count(raw, label, errors):
    """The posted count, or None (an error is recorded). Blank is refused: a module row
    says how many, and 0 is how a row is removed."""
    if raw is None or not str(raw).strip():
        errors.append(MODULE_NO_COUNT_MESSAGE.format(label=label))
        return None
    try:
        return parse_decimal_input(raw, places=2, min_value=0,
                                   max_value=decimal_field_max(BOQItem, 'boq_quantity'),
                                   field_label=label)
    except ValidationError as exc:
        errors.extend(exc.messages)
        return None


def plan_module_save(post, lines, module_by_id):
    """Plan the Modules section of one picker POST against the module lines read under the
    BOQ lock. Pure apart from reading `post`: no query, nothing written.

    The section, posted on every save of an editable picker page:
        module_item / module_qty      one pair per wattage row, in step
        module_line                   the pk of each "other" module line the page drew
        module_line_qty_<pk>          its count
        module_line_to_<pk>           '' to keep it, or a wattage item's pk to move it there

    A FULL RECONCILIATION, like the rest of the sheet: a wattage line whose wattage has no
    row (or a row at 0) is deleted, a row with no line creates one. An "other" line the page
    did not draw is left alone (it arrived after the page was drawn). Moving an "other" line
    gives its count to that wattage and deletes the line; nothing moves unless the designer
    chose the wattage. A wattage named twice, by two rows or by a row and a move, is refused
    (Q6), and any refusal leaves the whole sheet unsaved.
    """
    plan = ModuleSave()
    wattage_lines, other_lines = split_module_lines(lines, module_by_id)
    others = {line.pk: line for line in other_lines}

    wanted = {}      # wattage item pk -> count
    seen = set()

    def claim(master_id):
        if master_id in seen:
            plan.errors.append(MODULE_DUPLICATE_MESSAGE.format(
                label=module_label(module_by_id[master_id])))
            return False
        seen.add(master_id)
        return True

    items, counts = post.getlist('module_item'), post.getlist('module_qty')
    if len(items) != len(counts):
        plan.errors.append(MODULE_UNREADABLE_MESSAGE)
        return plan
    for raw_id, raw_count in zip(items, counts):
        if not raw_id.isdigit() or int(raw_id) not in module_by_id:
            plan.errors.append(MODULE_NO_WATTAGE_MESSAGE)
            continue
        master_id = int(raw_id)
        line = wattage_lines.get(master_id)
        if _blank(raw_count) and line is not None and line.boq_quantity is None:
            # A saved line with no quantity (a B-4 copy can make one), posted back blank:
            # unchanged, so an ordinary save of the page is not refused over it.
            if claim(master_id):
                wanted[master_id] = None
            continue
        count = _count(raw_count, module_label(module_by_id[master_id]), plan.errors)
        if count is None or not claim(master_id):
            continue
        wanted[master_id] = count

    for raw in post.getlist('module_line'):
        if not raw.isdigit() or int(raw) not in others:
            continue
        line = others[int(raw)]
        label = other_line_label(line, module_by_id)
        raw_count = post.get(f'module_line_qty_{line.pk}')
        target = (post.get(f'module_line_to_{line.pk}') or '').strip()
        if _blank(raw_count) and not target and line.boq_quantity is None:
            continue    # blank and still blank: unchanged, as above
        count = _count(raw_count, label, plan.errors)
        if count is None:
            continue
        if target:
            if not target.isdigit() or int(target) not in module_by_id:
                plan.errors.append(MODULE_NO_WATTAGE_MESSAGE)
                continue
            if not claim(int(target)):
                continue
            wanted[int(target)] = count
            plan.delete.append(line)
        elif count == 0:
            plan.delete.append(line)
        elif count != line.boq_quantity:
            plan.update.append((line, count))

    for master_id, count in wanted.items():
        line = wattage_lines.get(master_id)
        if count is None or count == 0:      # None: a blank line left blank (unchanged)
            continue
        if line is None:
            plan.create.append((module_by_id[master_id], count))
        elif count != line.boq_quantity:
            plan.update.append((line, count))
    for master_id, line in wattage_lines.items():
        if wanted.get(master_id, 0) == 0:
            plan.delete.append(line)
    return plan


def write_module_save(boq, plan):
    """Carry out a ModuleSave. The caller holds the BOQ lock (prepare_formula_save) and runs
    apply_formulas() afterwards, so the rules recalculate once for the whole save. A created
    line is the picker's create, field for field."""
    for line in plan.delete:
        line.delete()
    for line, count in plan.update:
        line.boq_quantity = count
        line.save(update_fields=['boq_quantity'])
    for master, count in plan.create:
        BOQItem.objects.create(
            boq=boq, item_master=master, serial_no=master.sort_order,
            category=master.category, description=master.description, uom=master.unit,
            boq_quantity=count, is_standard_item=True)


def sync_module_group_membership(item, actor):
    """Keep `item` in step with the "all modules" group after a Head's catalogue save (M3,
    option b). Returns a word for the message ('added', 'removed') or None.

    ADDED WHEN is_module IS SET, removed when it is cleared. Clearing is only possible on an
    item no BOQ line uses (the form refuses it otherwise, Q3), so removal never makes an
    existing line stop counting. The member's save() re-checks the stored rules (B-2a).

    A MISSING GROUP IS SKIPPED AND LOGGED, as DW-1 skips a missing group: on a database where
    the rules were never loaded there is nothing to join, and refusing the Head's save over
    it would block a catalogue edit for a formula concern. apply_dw5_modules creates the
    group on production in the same transaction as the items.
    """
    group = BOQItemGroup.objects.filter(name=MODULE_GROUP_NAME).first()
    if group is None:
        if item.is_module:
            logger.warning('DW-5: formula group %r not found; %s was not added to it. Load '
                           'the DW-5 rules file.', MODULE_GROUP_NAME, item.code)
        return None
    member = BOQItemGroupMember.objects.filter(group=group, item=item).first()
    if item.is_module and member is None:
        BOQItemGroupMember(group=group, item=item).save()
        log_activity(None, actor, f"Added RESCO BOQ catalogue item '{item.code}' to the "
                                  f"formula group '{MODULE_GROUP_NAME}'",
                     entity_type='BOQItemMaster', entity_id=item.pk,
                     action_code='boq_group_member_added')
        return 'added'
    if not item.is_module and member is not None:
        member.delete()
        log_activity(None, actor, f"Removed RESCO BOQ catalogue item '{item.code}' from the "
                                  f"formula group '{MODULE_GROUP_NAME}'",
                     entity_type='BOQItemMaster', entity_id=item.pk,
                     action_code='boq_group_member_removed')
        return 'removed'
    return None
