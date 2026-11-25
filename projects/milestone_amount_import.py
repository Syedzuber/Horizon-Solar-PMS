"""FIN-M-AB (ruling FR23), written 2026-11-20. The one-time backfill of the agreed M1-M3
amounts on existing Residential sites, from a workbook Finance fills.

    export_milestone_amounts --out <file.xlsx> [--by-bd]    writes the workbook
    import_milestone_amounts <file.xlsx>                    dry run
    import_milestone_amounts <file.xlsx> --apply --actor <username>

WHY. 109 of 111 live Residential milestones had no agreed amount (17 Nov restore), and a
receipt cannot be recorded without one (FR10). BD sets amounts on NEW sites at Draft (R6);
for existing sites Finance (Santosh or Payal) fills them once from a sheet, and each BD
then checks their sites' figures outside the system.

SCOPE. bd_scope_projects() (non-deleted, non-test Residential, not Cancelled) minus Draft
(ruling Q3): a Draft site has no milestone rows until activation and BD sets its amounts.

THE RULES, in the order a row meets them:
  * Blank cell = no change. A partly filled sheet is normal.
  * An amount is never overwritten. A different figure for a milestone that already has
    one is a CONFLICT (listed, not written); the same figure is ALREADY SET.
  * ERRORS refuse the row, and any error refuses the whole --apply: unknown code, site
    out of scope (Draft has its own message), milestone row missing, a bad amount
    (negative, text, more than 2 decimals; 0 is allowed and counts towards the total,
    ruling Q2), a code twice in the file, or M1+M2+M3 != contract value when the site
    would end up with all three (BD's rule in views.set_milestone_amounts: exact match),
    or a finance_note longer than NOTE_LIMIT characters.
  * WARNINGS do not block: the new amount is below what was already received, or the
    contract value is blank so the total cannot be checked.

THE WRITE. One transaction. Each milestone is re-read under a row lock and written only
if its amount is still blank, with save(update_fields=['amount']) — the same column write
the PM edit makes — and one ActivityLog line in FIN-M-L's wording plus the source:
"M2: amount blank → ₹1,50,000 (spreadsheet import; note: …)". Code
`milestone_amounts_imported`: a money code (hidden outside the money audience) that does
not count as site activity, and which the Finance Head "Amount changed after Received"
check does not read (activity_codes.py says why). Nothing else changes: no status,
received amount, date, task, transition or notification.

TODO: three code paths now write PaymentMilestone.amount (BD's set_milestone_amounts,
the PM edit in project_overview, and this module), each with its own log line. They
should become one shared function. Deferred by ruling: this session may not touch the
two view paths.
"""
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import transaction
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from projects.models import PaymentMilestone, Project, log_activity
from projects.number_input import decimal_field_max, parse_decimal_input
from projects.site_bd import BD_SCOPE_FILTER, CANCELLED, display_name, bd_scope_projects
# The milestone log's own rupee formatter, so an imported line reads exactly like the PM
# edit's ("₹1,50,000", paise only when not zero). Imported at module level: views does not
# import this module, so there is no cycle; only the two commands and the tests load it.
from projects.views import _milestone_rupees
# FIN-M-B5-5a (H5, FR24): setting an agreed amount recomputes the milestone's status.
from projects.receipts import recompute_milestone_from_receipts

DRAFT = 'Draft'
MILESTONE_NAMES = (PaymentMilestone.M1, PaymentMilestone.M2, PaymentMilestone.M3)
MILESTONE_ENTITY = 'Milestone'    # ActivityLog.entity_type of every milestone line
IMPORTED_CODE = 'milestone_amounts_imported'

#: The workbook's columns, in order (FIN-M-AB task 1).
COLUMNS = (
    'project_code', 'client_name', 'status', 'bd', 'contract_value',
    'M1_current', 'M2_current', 'M3_current',
    'M1_received_so_far', 'M2_received_so_far', 'M3_received_so_far',
    'M1_new', 'M2_new', 'M3_new', 'finance_note',
)
#: The columns Finance fills; shaded in the export, and the only ones the import reads
#: besides project_code. Every other column is for reference and is ignored.
INPUT_COLUMNS = ('M1_new', 'M2_new', 'M3_new', 'finance_note')
REQUIRED_COLUMNS = ('project_code',) + INPUT_COLUMNS
NEW_COLUMN = {name: f'{name}_new' for name in MILESTONE_NAMES}

SHEET_TITLE = 'Milestone amounts'
INPUT_FILL = 'FFF2CC'             # light yellow: "fill these in"

# Outcomes of one milestone in the plan.
SET = 'set'
ALREADY_SET = 'already set'
NOT_FILLED = 'not filled'
CONFLICT = 'conflict'

DRAFT_MESSAGE = 'Draft site — BD sets amounts at Draft'

# Refused above this (ruling, 20 Nov): the note goes into the log line, whose column holds
# 255 characters, and log_activity() would otherwise cut a long note short with "…".
# 150 leaves room for "M2: amount blank → ₹1,00,00,000.00 (spreadsheet import; note: )".
NOTE_LIMIT = 150

# Commas are accepted only where a grouping puts them — Indian (1,50,000) or western
# (150,000) — so a decimal comma such as "1500,50" is refused rather than read as 150050.
_INDIAN_GROUPED = re.compile(r'^\d{1,2}(,\d{2})*,\d{3}$')
_WESTERN_GROUPED = re.compile(r'^\d{1,3}(,\d{3})+$')
_NUMBER_TEXT = re.compile(r'^-?[\d,]*(\.\d*)?$')


class ImportFileError(Exception):
    """The workbook as a whole cannot be read: wrong type, unreadable, or a required
    header missing. Nothing in it is looked at further."""


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

def import_scope_projects():
    """The sites the export lists and the import may write: bd_scope_projects() minus
    Draft (ruling Q3). A queryset, so callers order and select_related it."""
    return bd_scope_projects().exclude(status=DRAFT)


def out_of_scope_reason(project):
    """Why `project` (already loaded) may not be written, or None when it may. Read off
    the row against the same constants bd_scope_projects() filters on, so the two cannot
    disagree; Draft is asked last so a deleted Draft says "deleted"."""
    if project.is_deleted:
        return 'site is deleted'
    if project.project_type != BD_SCOPE_FILTER['project_type']:
        return f'{project.project_type} site — amounts are set on Residential sites only'
    if project.is_test:
        return 'test site'
    if project.status == CANCELLED:
        return 'site is Cancelled'
    if project.status == DRAFT:
        return DRAFT_MESSAGE
    return None


def sites_still_without_amount():
    """In-scope milestones whose agreed amount is still blank — the "Still without
    amount" figure. One COUNT query."""
    return PaymentMilestone.objects.filter(project__in=import_scope_projects(),
                                           amount__isnull=True).count()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_rows(*, by_bd=False):
    """One dict per in-scope site, keyed by COLUMNS, the input columns blank.

    Two queries: the sites with their BD and BD's user (select_related, for the name),
    and every milestone on them (prefetch). Ordered by code, or with by_bd by BD name
    then code, sites with no BD last (task 3: the BD check list)."""
    sites = list(import_scope_projects()
                 .select_related('bd__user')
                 .prefetch_related('milestones')
                 .order_by('project_id'))
    if by_bd:
        # Sorted in Python, not by the database: the key is the displayed name, which is
        # the full name or the username, so it cannot be one ORDER BY column.
        sites.sort(key=lambda p: (p.bd is None,
                                  display_name(p.bd).lower() if p.bd else '',
                                  p.project_id))
    rows = []
    for site in sites:
        by_name = {m.milestone_name: m for m in site.milestones.all()}
        row = {
            'project_code': site.project_id,
            'client_name': site.customer_name,
            'status': site.status,
            'bd': display_name(site.bd) if site.bd else '',
            'contract_value': site.contract_value,
        }
        for name in MILESTONE_NAMES:
            m = by_name.get(name)
            row[f'{name}_current'] = m.amount if m else None
            row[f'{name}_received_so_far'] = m.amount_received if m else None
        for column in INPUT_COLUMNS:
            row[column] = None
        rows.append(row)
    return rows


def write_workbook(rows, path):
    """Write `rows` to a one-sheet .xlsx at `path`: header row frozen, input columns
    shaded, amounts as numbers (openpyxl stores a Decimal as a number cell)."""
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_TITLE
    ws.append(list(COLUMNS))
    for row in rows:
        ws.append([row[c] for c in COLUMNS])

    fill = PatternFill(fill_type='solid', start_color=INPUT_FILL, end_color=INPUT_FILL)
    bold = Font(bold=True)
    amount_columns = {c for c in COLUMNS if c == 'contract_value'
                      or c.endswith(('_current', '_received_so_far', '_new'))}
    for index, column in enumerate(COLUMNS, start=1):
        letter = get_column_letter(index)
        ws.cell(row=1, column=index).font = bold
        ws.column_dimensions[letter].width = max(14, len(column) + 2)
        for r in range(1, len(rows) + 2):
            cell = ws.cell(row=r, column=index)
            if column in INPUT_COLUMNS:
                cell.fill = fill
            if r > 1 and column in amount_columns:
                cell.number_format = '0.00'
    ws.column_dimensions[get_column_letter(COLUMNS.index('client_name') + 1)].width = 28
    ws.column_dimensions[get_column_letter(COLUMNS.index('finance_note') + 1)].width = 40
    ws.freeze_panes = 'A2'
    wb.save(path)


# ---------------------------------------------------------------------------
# Reading the workbook
# ---------------------------------------------------------------------------

@dataclass
class SheetRow:
    line: int                     # the Excel row number, for messages
    code: str
    raw: dict                     # {'M1': cell value, ...} as openpyxl gave it
    note: str


def _cell_text(value):
    """A cell as trimmed text; Excel's whole-number float (12345.0) as '12345'."""
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def read_workbook(path):
    """The data rows of the FIRST sheet of the .xlsx at `path`, matched by header name
    (trimmed, case-insensitive), so Finance may reorder or add columns. Wholly blank rows
    are dropped. Raises ImportFileError for a file that is not a readable .xlsx or lacks
    a required header."""
    if Path(path).suffix.lower() != '.xlsx':
        raise ImportFileError(f'{path} is not an .xlsx file. Save the sheet as an Excel '
                              f'workbook (.xlsx) and run again. Nothing was read.')
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except FileNotFoundError:
        raise ImportFileError(f'{path}: no such file. Nothing was read.')
    except Exception as exc:
        raise ImportFileError(f'{path} could not be read as an Excel workbook ({exc}). '
                              f'Nothing was read.')
    try:
        ws = wb.worksheets[0]
        all_rows = list(ws.iter_rows(values_only=True))
    finally:
        wb.close()
    if not all_rows:
        raise ImportFileError(f'{path}: the first sheet is empty. Nothing was read.')

    headers = [_cell_text(h).lower() for h in all_rows[0]]
    position = {}
    for index, header in enumerate(headers):
        position.setdefault(header, index)
    missing = [c for c in REQUIRED_COLUMNS if c.lower() not in position]
    if missing:
        raise ImportFileError(f'{path}: missing column(s) {", ".join(missing)} in the first '
                              f'row of the first sheet. Do not rename the headers. '
                              f'Nothing was read.')

    def cell(values, column):
        index = position[column.lower()]
        return values[index] if index < len(values) else None

    rows = []
    for line, values in enumerate(all_rows[1:], start=2):
        if not any(_cell_text(v) for v in values):
            continue
        rows.append(SheetRow(
            line=line,
            code=_cell_text(cell(values, 'project_code')),
            raw={name: cell(values, NEW_COLUMN[name]) for name in MILESTONE_NAMES},
            note=_cell_text(cell(values, 'finance_note')),
        ))
    return rows


def parse_amount(value):
    """One *_new cell -> Decimal, or None when blank. Raises ValidationError.

    Accepted: a number cell; text with ₹, spaces and grouping commas ("₹ 1,50,000");
    Excel's "12345.0". Refused: any other text, a negative, more than 2 decimals, a
    misplaced comma. Zero is allowed (ruling Q2: BD enters 0 for a milestone that does
    not apply). The rest is parse_decimal_input, the app's one number reader."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValidationError(f'Amount: "{value}" is not a number.')
    if isinstance(value, (int, float, Decimal)):
        # str() of a float is its shortest round-trip spelling (18981.9, not
        # 18981.900000000001), so a cell holding 2 decimals reads as 2 decimals.
        text = str(value)
    else:
        text = str(value)
        for ch in ('₹', ' ', ' '):
            text = text.replace(ch, '')
        if not text:
            return None
        if not _NUMBER_TEXT.match(text):
            raise ValidationError(f'Amount: "{value}" is not a number.')
        if ',' in text:
            whole = text.lstrip('-').split('.')[0]
            if not (_INDIAN_GROUPED.match(whole) or _WESTERN_GROUPED.match(whole)):
                raise ValidationError(f'Amount: "{value}" has a comma in the wrong place.')
            text = text.replace(',', '')
    return parse_decimal_input(text, places=2, min_value=0,
                               max_value=decimal_field_max(PaymentMilestone, 'amount'),
                               field_label='Amount')


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

@dataclass
class Item:
    """One milestone of one row: what the run does with it."""
    name: str                     # 'M1' / 'M2' / 'M3'
    outcome: str                  # SET / ALREADY_SET / NOT_FILLED / CONFLICT
    new: object = None            # the sheet's Decimal, when it has one
    current: object = None        # the stored amount
    milestone_pk: object = None


@dataclass
class RowPlan:
    sheet: SheetRow
    project: object = None
    items: list = field(default_factory=list)
    errors: list = field(default_factory=list)     # (Mx or '', message)
    warnings: list = field(default_factory=list)   # (Mx or '', message)


def plain(value):
    """An amount for command output: plain digits, no ₹ or commas, paise only when not
    zero ('150000', '18981.90')."""
    if value is None:
        return 'blank'
    value = Decimal(value)
    return str(int(value)) if value == value.to_integral_value() else f'{value:.2f}'


def plan_import(sheet_rows):
    """Check every row and decide each milestone's outcome. Writes nothing.

    Two queries whatever the file's size: every project the file names (any scope, so an
    out-of-scope code gets its reason rather than "unknown"), and every milestone on the
    in-scope ones."""
    counts = Counter(r.code.upper() for r in sheet_rows if r.code)
    codes = {r.code for r in sheet_rows if r.code}
    projects = {p.project_id.upper(): p
                for p in Project.objects.filter(project_id__in=codes)}
    in_scope = [p for p in projects.values() if out_of_scope_reason(p) is None]
    milestones = {}
    for m in PaymentMilestone.objects.filter(project__in=in_scope):
        milestones[(m.project_id, m.milestone_name)] = m

    plans = []
    for sheet in sheet_rows:
        plan = RowPlan(sheet=sheet)
        plans.append(plan)
        if not sheet.code:
            plan.errors.append(('', 'no project_code on this row'))
            continue
        if counts[sheet.code.upper()] > 1:
            plan.errors.append(('', f'{sheet.code} appears {counts[sheet.code.upper()]} '
                                    f'times in the file'))
        project = projects.get(sheet.code.upper())
        if project is None:
            plan.errors.append(('', 'unknown project code'))
            continue
        plan.project = project
        reason = out_of_scope_reason(project)
        if reason:
            plan.errors.append(('', reason))
            continue
        if len(sheet.note) > NOTE_LIMIT:
            plan.errors.append(('', f'note too long: {len(sheet.note)} characters, '
                                    f'limit {NOTE_LIMIT}'))

        for name in MILESTONE_NAMES:
            try:
                new = parse_amount(sheet.raw[name])
            except ValidationError as exc:
                plan.errors.append((name, exc.messages[0]))
                continue
            m = milestones.get((project.pk, name))
            if m is None:
                if new is not None:
                    plan.errors.append((name, 'no milestone record on this site'))
                continue
            current = m.amount
            if new is None:
                # A blank cell on a milestone that has its amount changes nothing, and
                # the milestone is not waiting for Finance: counted as already set.
                outcome = NOT_FILLED if current is None else ALREADY_SET
            elif current is None:
                outcome = SET
                if m.amount_received is not None and new < m.amount_received:
                    plan.warnings.append((name, f'new amount {plain(new)} is below the '
                                                f'{plain(m.amount_received)} already '
                                                f'received'))
            elif current == new:
                outcome = ALREADY_SET
            else:
                outcome = CONFLICT
            plan.items.append(Item(name=name, outcome=outcome, new=new, current=current,
                                   milestone_pk=m.pk))

        if plan.errors:
            continue
        # BD's contract rule (views.set_milestone_amounts): checked only when this row
        # sets something and the site would then hold all three amounts — stored ones
        # plus the ones set here; a conflict keeps the stored figure. Exact match, 0
        # counts (ruling Q2).
        if not any(i.outcome == SET for i in plan.items):
            continue
        final = {i.name: (i.new if i.outcome == SET else i.current) for i in plan.items}
        if not all(final.get(n) is not None for n in MILESTONE_NAMES):
            continue
        total = sum((final[n] for n in MILESTONE_NAMES), Decimal('0'))
        contract = project.contract_value
        if contract is None:
            plan.warnings.append(('', f'contract value not set; total {plain(total)} '
                                      f'not checked'))
        elif total != contract:
            # Ruling Q4: the same figures BD's own message gives.
            plan.errors.append(('', f'M1 + M2 + M3 must equal the contract value '
                                    f'{plain(contract)}; they would total {plain(total)}, '
                                    f'a difference of {plain(abs(contract - total))}'))
    return plans


def summarise(plans):
    """{'set', 'already_set', 'not_filled', 'conflicts', 'errors', 'warnings', 'rows'}.
    A row with an error writes nothing, so its milestones count only as its errors."""
    totals = Counter()
    for plan in plans:
        totals['errors'] += len(plan.errors)
        totals['warnings'] += len(plan.warnings)
        if plan.errors:
            continue
        for item in plan.items:
            totals[{SET: 'set', ALREADY_SET: 'already_set', NOT_FILLED: 'not_filled',
                    CONFLICT: 'conflicts'}[item.outcome]] += 1
    totals['rows'] = len(plans)
    return totals


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------

class ImportRefused(Exception):
    """--apply was asked for a plan that has errors; nothing was written."""


def imported_line(name, new, note):
    """FIN-M-L's amount wording plus the source (ruling Q1); "; note: …" only when
    Finance wrote one. The old value is always blank: only blank amounts are set."""
    source = f'spreadsheet import; note: {note}' if note else 'spreadsheet import'
    return f'{name}: amount {_milestone_rupees(None)} → {_milestone_rupees(new)} ({source})'


def apply_plan(plans, actor):
    """Write every SET in `plans` in one transaction; return the number written.

    Refuses (ImportRefused) when any row has an error — all or nothing. Each milestone is
    re-read under select_for_update and written only if its amount is STILL blank: an
    amount BD or a PM saved between the plan and this write is kept, and the item turns
    into a conflict, so a concurrent edit is never overwritten. The lock also stops a BD
    save landing between the check and the write."""
    if any(plan.errors for plan in plans):
        raise ImportRefused('the file has errors')
    to_set = [(plan, item) for plan in plans for item in plan.items if item.outcome == SET]
    written = 0
    with transaction.atomic():
        # Locks every row to be written in one query, in pk order so two runs cannot
        # deadlock; the map is what the loop checks "still blank" against.
        locked = {m.pk: m for m in PaymentMilestone.objects.select_for_update()
                  .filter(pk__in=[item.milestone_pk for _, item in to_set]).order_by('pk')}
        for plan, item in to_set:
            milestone = locked[item.milestone_pk]
            if milestone.amount is not None:
                item.outcome, item.current = CONFLICT, milestone.amount
                continue
            milestone.amount = item.new
            milestone.save(update_fields=['amount'])
            amount = imported_line(item.name, item.new, plan.sheet.note)
            # A money line (milestone_amounts_imported is in MONEY_ACTIVITY_CODES), so
            # viewers outside the site's money audience never see it; and not site
            # activity (NON_ACTIVITY_CODES). Linked to the milestone like the PM edit's.
            log_activity(plan.project, actor, amount, entity_type=MILESTONE_ENTITY,
                         entity_id=milestone.pk, action_code='milestone_amounts_imported')
            # FIN-M-B5-5a (H5): the amount is now set, so the status follows confirmed
            # receipts — inside this transaction, on the row already locked above. A no-op
            # while CLIENT_RECEIPTS_LIVE is off (FR38).
            recompute_milestone_from_receipts(milestone, actor, 'agreed amount imported')
            written += 1
    return written
