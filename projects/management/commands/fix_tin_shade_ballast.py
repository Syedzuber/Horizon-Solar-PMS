"""
fix_tin_shade_ballast — Tin Shade and Ballast structure counted in Nos (B-0b; Praveen's
rulings F5 and G5, 22 Nov 2026).

    python manage.py fix_tin_shade_ballast --catalogue                 # dry run
    python manage.py fix_tin_shade_ballast --catalogue --apply
    python manage.py fix_tin_shade_ballast --export tsb.csv            # read-only
    python manage.py fix_tin_shade_ballast --apply-counts tsb.csv      # dry run
    python manage.py fix_tin_shade_ballast --apply-counts tsb.csv --apply
    python manage.py fix_tin_shade_ballast --apply-counts tsb.csv --apply \
        --allow-lines 691,765 --reason "SCM confirmed the order is for MMS 2P only"

WHY: OPX-038 Tin Shade and OPX-037 Ballast Type Structure are MMS structure, and F5
rules that structure is counted in Nos (INC work, OPX-203/OPX-204, stays in KWp). Their
BOQ lines hold kWp figures (186.88 on one site) under an item that is a count, so every
reader of those lines is wrong today. A person re-counts each line from the site's design
or survey (G5); this command supplies the list and writes the counts back.

THREE STEPS, ONE PER RUN:

--catalogue      OPX-038 'No' → Nos and OPX-037 KWp → Nos. Nothing else in the catalogue,
                 and no BOQ line. OPX-038's change is a spelling (units.UNIT_SPELLINGS
                 already maps 'No' to Nos). OPX-037's is a real unit change on an item in
                 use, which ruling 12 forbids; F5 approves it ONCE, here. The catalogue
                 form's refusal (forms.BOQItemMasterForm.clean_unit) is untouched, so this
                 command is the only place the exception exists.

--export PATH    A CSV of every BOQ line on the two items, deleted sites included, for the
                 re-count. new_count and note are left empty for the team. The summary
                 also lists orders and approvals for these items that name no site
                 (tender-wide) — a flag only, never a refusal.

--apply-counts PATH
                 Reads the filled CSV and writes each new_count, with the unit Nos, to its
                 line — only if the line still holds the quantity the export showed, and
                 only if the site has not since been locked into a procurement group or
                 ordered against (checked live, not read from the CSV).
                 --allow-lines ID,... with --reason TEXT applies the listed lines despite
                 the lock/order refusal only; every other check still applies, and the
                 log entry records the reasons overridden and the stated reason.

Dry run unless --apply. Every write is logged in the activity log with before → after
values and the system actor "fix_tin_shade_ballast (system)". No correction record is
written (B-0b Q6): this is a data fix, and a BOQCorrection would be credited to the
designer in the reviewer-corrections panel and could be cited by a change-request verdict.
No status, notification or permission changes.
"""
import csv
import re
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from projects.forms import catalogue_snapshot, log_catalogue_change
from projects.models import (
    DESIGN_FILE_BOQ_EXCEL, DESIGN_FILE_BOQ_PDF, DESIGN_RELEASED, ApprovalRequest, BOQItem,
    BOQItemMaster, DesignAttempt, DesignFile, MaterialApprovalLine, VendorOrder,
    VendorOrderLine, log_activity,
)
from projects.number_input import decimal_field_max, parse_decimal_input
from projects.permissions import project_boq_is_group_locked
from projects.units import COUNT_UNITS

#: The two structure items F5 moves to Nos. INC work (OPX-203, OPX-204) is not here.
ITEM_CODES = ('OPX-037', 'OPX-038')

#: The shared list's code for a count of pieces (projects/units.py UNITS).
TARGET_UNIT = 'Nos'
assert TARGET_UNIT in COUNT_UNITS, 'Nos must be a count unit on the shared list'

#: Words that name the two items in free text. MaterialApprovalLine has no catalogue link,
#: only a description, so an approval for these items can be found by its words alone.
#: 'tin shade' also matches "INC work Tin shade"; that refuses more rows, never fewer,
#: which is the safe direction for a guard whose refusals Zuber decides case by case.
ITEM_KEYWORDS = ('tin shade', 'ballast')

# CONSOLE OUTPUT USES "->", NOT "→". A Windows console that is piped or redirected writes
# in cp1252, which has no "→", and the write raises UnicodeEncodeError mid-run (seen on
# the first local dry run). The activity log and the CSV are UTF-8 and keep B-0's "→".
ACTOR = 'fix_tin_shade_ballast (system)'
CATALOGUE_ACTION_CODE = 'boq_catalogue_unit_fixed'
LINE_ACTION_CODE = 'boq_line_count_applied'
OPX_037_APPROVAL = 'one-off unit change approved by Praveen (F5, 22 Nov)'

IST = ZoneInfo('Asia/Kolkata')

EXPORT_COLUMNS = (
    'line_id', 'site_code', 'site_name', 'site_status', 'design_status', 'released',
    'site_deleted', 'designer', 'item_code', 'item_description', 'current_uom',
    'current_quantity', 'boq_marked_complete', 'last_correction', 'locked_or_ordered',
    'lock_reason', 'boq_files_uploaded', 'new_count', 'note',
)
#: The columns --apply-counts reads. The rest are for the people filling it in.
REQUIRED_COLUMNS = ('line_id', 'current_quantity', 'new_count')

_ACTION_MAX = 255     # ActivityLog.action max_length; log_activity cuts anything longer


def _affected_lines():
    """Every BOQ line on the two items, with what the export and the apply read.

    DELETED SITES INCLUDED, deliberately — the one exception to the soft-delete filter
    every site queryset carries. A deleted site that is restored brings its lines back,
    and they would still hold kWp figures under a Nos item (the same reason B-0 counts
    deleted sites as "in use"). BOQItem and Project have no filtering manager, so the
    plain queryset already includes them.
    """
    return (BOQItem.objects
            .filter(item_master__code__in=ITEM_CODES)
            .select_related('item_master', 'boq__project',
                            'boq__project__design_assignment__assigned_to__user'))


def lock_reasons(line):
    """Why this line's site may no longer have its quantity changed, as a list of short
    reasons; empty when nothing stands in the way (B-0b Q4).

    Read live at apply time as well as at export, because the CSV can sit with the team
    for days and a site can be grouped or ordered meanwhile. Three things count, each on
    THIS site and on OPX-037/038 only:
      - a locked procurement group (the same predicate every BOQ write path obeys);
      - a VendorOrderLine on either item on an order sized against this site, or one
        naming this BOQ line directly (Residential single-site lines);
      - a MaterialApprovalLine whose description names either item, on a request that
        names this site or a group the site is currently in.
    An order or approval raised against a whole tender (programs only, no site) does not
    count: it names no site, so it cannot be tied to this line.
    """
    project = line.boq.project
    reasons = []
    if project_boq_is_group_locked(project):
        reasons.append('locked procurement group')

    # One order per row: an order sized against this site with a line on either item
    # (by FK, or by its code snapshot when the catalogue FK has gone null), or an order
    # line pointing at this very BOQ line.
    on_items = Q(item_master__code__in=ITEM_CODES) | Q(item_code__in=ITEM_CODES)
    order_ids = sorted(set(
        VendorOrderLine.objects
        .filter(Q(boq_item=line) | (on_items & Q(order__sites__project=project)))
        .values_list('order_id', flat=True)))
    if order_ids:
        reasons.append('VendorOrderLine on order ' + ', '.join(f'#{i}' for i in order_ids))

    # Requests naming the site directly, or through a group it has not left. The
    # membership terms sit in one filter() so they describe the SAME membership row.
    words = Q()
    for word in ITEM_KEYWORDS:
        words |= Q(description__icontains=word)
    for_site = (Q(detail__request__projects=project)
                | Q(detail__request__site_groups__memberships__project=project,
                    detail__request__site_groups__memberships__removed_at__isnull=True))
    requests = sorted(set(
        MaterialApprovalLine.objects.filter(words & for_site)
        .values_list('detail__request_id', 'detail__request__status')))
    if requests:
        reasons.append('MaterialApprovalLine on approval ' + ', '.join(
            f'#{pk} ({status})' for pk, status in requests))
    return reasons


def _current_attempt(project):
    """The site's current design attempt, or None (no assignment, or not opened yet)."""
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None:
        return None
    return (DesignAttempt.objects
            .filter(assignment=assignment,
                    attempt_number=assignment.current_attempt_number)
            .first())


def _when(stamp):
    return stamp.astimezone(IST).strftime('%Y-%m-%d %H:%M') if stamp else ''


def _qty_text(value):
    return 'blank' if value is None else str(value)


def _yes(flag):
    return 'yes' if flag else 'no'


def _export_row(line):
    project = line.boq.project
    assignment = getattr(project, 'design_assignment', None)
    designer = ''
    if assignment is not None and assignment.assigned_to is not None:
        user = assignment.assigned_to.user
        designer = user.get_full_name() or user.username
    attempt = _current_attempt(project)
    # Meta.ordering on BOQCorrection is newest first, so first() is the latest.
    correction = line.corrections.first()
    last_correction = ''
    if correction is not None:
        last_correction = (f'{correction.corrected_at.astimezone(IST):%Y-%m-%d} '
                           f'{_qty_text(correction.quantity_before)} → '
                           f'{_qty_text(correction.quantity_after)}')
    files = attempt is not None and DesignFile.objects.filter(
        attempt=attempt, kind__in=(DESIGN_FILE_BOQ_EXCEL, DESIGN_FILE_BOQ_PDF)).exists()
    reasons = lock_reasons(line)
    design_status = assignment.status if assignment is not None else ''
    return {
        'line_id':             line.pk,
        'site_code':           project.project_id,
        'site_name':           project.site_name or project.customer_name,
        'site_status':         project.status,
        'design_status':       design_status,
        'released':            _yes(design_status == DESIGN_RELEASED),
        'site_deleted':        _yes(project.is_deleted),
        'designer':            designer,
        'item_code':           line.item_master.code,
        'item_description':    line.item_master.description,
        'current_uom':         line.uom,
        'current_quantity':    '' if line.boq_quantity is None else str(line.boq_quantity),
        'boq_marked_complete': _when(attempt.boq_submitted_at) if attempt else '',
        'last_correction':     last_correction,
        'locked_or_ordered':   _yes(reasons),
        'lock_reason':         '; '.join(reasons),
        'boq_files_uploaded':  _yes(files),
        'new_count':           '',
        'note':                '',
    }


def _parse_allow_lines(raw, reason):
    """The line ids named by --allow-lines, as a set; empty without the flag.

    THE OVERRIDE NEEDS A STATED REASON, and both halves are refused alone. An override
    with no reason would leave a log entry saying a lock was bypassed and not why; a
    reason with no lines overrides nothing and is a sign the command line is not what the
    person meant to type.
    """
    if raw is None:
        if reason:
            raise CommandError('--reason goes with --allow-lines; there is nothing to '
                               'override without it.')
        return set()
    if not reason:
        raise CommandError('--allow-lines needs --reason "<why the lock is overridden>".')
    parts = [p.strip() for p in raw.split(',') if p.strip()]
    if not parts or any(not re.fullmatch(r'\d+', p) for p in parts):
        raise CommandError(f'--allow-lines {raw!r}: give line ids separated by commas, '
                           f'e.g. 691,765.')
    return {int(p) for p in parts}


def _console(text):
    """Free text (an item description, an approval title) made safe to print on a
    Windows console writing cp1252: anything it cannot show becomes "?" instead of
    raising mid-run. See the "->" note above ACTOR."""
    return text.encode('cp1252', 'replace').decode('cp1252')


def tender_wide_documents():
    """Orders and approvals for these items that name NO site — one printable line each.

    FLAG ONLY (B-0b build answers, 2). These never block a line (an order or approval
    sized against a whole tender cannot be tied to one site's BOQ line), but the person
    running the export should know they exist before the counts go in.

    Orders: no VendorOrderSite row, and a line on OPX-037/038 (by FK or code snapshot)
    or whose text says "tin shade" / "ballast". Approvals: no site and no site group
    named, and a material line whose text says either. Read-only.
    """
    words = Q()
    for word in ITEM_KEYWORDS:
        words |= Q(lines__item_description__icontains=word)
    order_q = (Q(lines__item_master__code__in=ITEM_CODES)
               | Q(lines__item_code__in=ITEM_CODES) | words)
    # distinct(): the line join returns one row per matching line; one entry per order.
    orders = (VendorOrder.objects.filter(order_q, sites__isnull=True).distinct()
              .prefetch_related('programs__program', 'lines__item_master')
              .order_by('pk'))

    def matches(code, text):
        return code in ITEM_CODES or any(w in (text or '').lower() for w in ITEM_KEYWORDS)

    def tender_names(programs):
        return ', '.join(p.short_tender_code or p.name for p in programs) or '(no tender)'

    found = []
    for order in orders:
        items = [f'{line.item_code} {line.item_description}'.strip()
                 for line in order.lines.all()
                 if matches(line.item_code or (line.item_master.code if line.item_master
                                               else ''), line.item_description)]
        numbers = ' / '.join(f'{kind} {value}' for kind, value in
                             (('PO', order.po_number), ('PI', order.pi_number)) if value)
        tenders = tender_names(p.program for p in order.programs.all())
        found.append(f'order #{order.pk} | tender {tenders} | '
                     f'{numbers or "no PO/PI number"} | {"; ".join(items)}')

    words = Q()
    for word in ITEM_KEYWORDS:
        words |= Q(material_detail__lines__description__icontains=word)
    requests = (ApprovalRequest.objects
                .filter(words, projects__isnull=True, site_groups__isnull=True)
                .distinct()
                .prefetch_related('programs', 'material_detail__lines')
                .order_by('pk'))
    for request in requests:
        items = [line.description for line in request.material_detail.lines.all()
                 if matches('', line.description)]
        found.append(f'approval #{request.pk} ({request.status}) | tender '
                     f'{tender_names(request.programs.all())} | {request.title} | '
                     f'{"; ".join(items)}')
    return [_console(text) for text in found]


def _read_csv(path):
    """The rows of a filled CSV. UTF-8 first (what --export writes); Windows-1252 if that
    fails, because Excel's plain "CSV" save re-encodes the file that way and the team is
    likely to open it in Excel. Only line_id, current_quantity, new_count and note are
    read, and those are plain ASCII in either encoding."""
    for encoding in ('utf-8-sig', 'cp1252'):
        try:
            with open(path, newline='', encoding=encoding) as handle:
                reader = csv.DictReader(handle)
                fields = [(f or '').strip() for f in (reader.fieldnames or [])]
                rows = [{(k or '').strip(): (v or '').strip() for k, v in row.items()}
                        for row in reader]
            return fields, rows
        except UnicodeDecodeError:
            continue
    raise CommandError(f'{path}: could not read the file as UTF-8 or Windows-1252.')


class Command(BaseCommand):
    help = ('Tin Shade (OPX-038) and Ballast (OPX-037) counted in Nos: --catalogue fixes '
            'the two units, --export lists their BOQ lines as a CSV, --apply-counts writes '
            'the re-entered counts. Dry run unless --apply.')

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument('--catalogue', action='store_true',
                          help='Set OPX-038 and OPX-037 to Nos in the catalogue.')
        mode.add_argument('--export', metavar='PATH',
                          help='Write every BOQ line on the two items to a CSV. Read-only.')
        mode.add_argument('--apply-counts', metavar='PATH',
                          help='Write the new_count values from a filled CSV.')
        parser.add_argument('--apply', action='store_true',
                            help='Write the changes. Without it, only print them.')
        parser.add_argument('--allow-lines', metavar='ID,ID,...',
                            help='With --apply-counts: apply these lines even though their '
                                 'site is locked or ordered. Needs --reason.')
        parser.add_argument('--reason', metavar='TEXT',
                            help='Why the --allow-lines override is made; written to the log.')

    def handle(self, *args, **options):
        apply = options['apply']
        allow_raw, reason = options['allow_lines'], (options['reason'] or '').strip()
        if (allow_raw is not None or options['reason'] is not None) \
                and not options['apply_counts']:
            raise CommandError('--allow-lines and --reason go only with --apply-counts.')
        if options['export']:
            if apply:
                raise CommandError('--export is read-only; --apply does not go with it.')
            return self._export(options['export'])
        if options['catalogue']:
            return self._catalogue(apply)
        allowed = _parse_allow_lines(allow_raw, reason)
        return self._apply_counts(options['apply_counts'], apply, allowed, reason)

    # ── --catalogue ────────────────────────────────────────────────────────────

    def _catalogue(self, apply):
        mode = 'APPLY' if apply else 'DRY RUN (nothing written; use --apply to write)'
        self.stdout.write(f'fix_tin_shade_ballast --catalogue — {mode}')
        changed = already = missing = 0
        for code in ITEM_CODES:
            item = BOQItemMaster.objects.filter(code=code).first()
            if item is None:
                missing += 1
                self.stdout.write(f'  {code}: not in the catalogue — nothing to do')
                continue
            if item.unit == TARGET_UNIT:
                already += 1
                self.stdout.write(f'  {code}: already {TARGET_UNIT} — nothing to do')
                continue
            lines = item.boq_items.count()
            self.stdout.write(f'  {code}: {item.unit} -> {TARGET_UNIT}   ({lines} BOQ lines, '
                              f'not touched by this step)')
            if apply and self._fix_unit(item):
                changed += 1
            elif not apply:
                changed += 1
        verb = 'changed' if apply else 'would change'
        self.stdout.write(f'{changed} {verb}, {already} already {TARGET_UNIT}, '
                          f'{missing} not found.')

    def _fix_unit(self, item):
        """Rewrite one item's unit and log it, in one transaction. True if written.

        RACE: the UPDATE is conditional on the unit still being the value printed. If a
        catalogue screen saved the row after it was read, 0 rows update, nothing is
        logged, and the row is reported as left alone for a re-run to look at again.
        updated_at is set by hand because update() bypasses auto_now.
        """
        old = item.unit
        before = catalogue_snapshot(item)
        with transaction.atomic():
            updated = (BOQItemMaster.objects
                       .filter(pk=item.pk, unit=old)
                       .update(unit=TARGET_UNIT, updated_at=timezone.now()))
            if not updated:
                self.stdout.write(f'    {item.code}: changed since it was read — left alone')
                return False
            item.unit = TARGET_UNIT
            prefix = f"{ACTOR} updated RESCO BOQ catalogue item '{item.code}'"
            if item.code == 'OPX-037':
                prefix += f' ({OPX_037_APPROVAL})'
            log_catalogue_change(None, item, prefix, before,
                                 action_code=CATALOGUE_ACTION_CODE)
        return True

    # ── --export ───────────────────────────────────────────────────────────────

    def _export(self, path):
        rows = [_export_row(line) for line in _affected_lines()]
        rows.sort(key=lambda r: (r['site_code'], r['line_id']))
        # utf-8-sig: the BOM makes Excel read site names with non-ASCII characters
        # correctly instead of guessing the Windows code page.
        with open(path, 'w', newline='', encoding='utf-8-sig') as handle:
            writer = csv.DictWriter(handle, fieldnames=EXPORT_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        sites = {r['site_code'] for r in rows}
        released = {r['site_code'] for r in rows if r['released'] == 'yes'}
        deleted = {r['site_code'] for r in rows if r['site_deleted'] == 'yes'}
        locked = sum(1 for r in rows if r['locked_or_ordered'] == 'yes')
        files = sum(1 for r in rows if r['boq_files_uploaded'] == 'yes')
        tender_wide = tender_wide_documents()
        self.stdout.write(f'Wrote {path}')
        self.stdout.write(f'{len(rows)} lines, {len(sites)} sites, {len(released)} released '
                          f'sites, {len(deleted)} deleted sites; {locked} lines locked or '
                          f'ordered, {files} with BOQ files uploaded; {len(tender_wide)} '
                          f'tender-wide orders/approvals on these items (flag only).')
        for text in tender_wide:
            self.stdout.write(f'  tender-wide: {text}')

    # ── --apply-counts ─────────────────────────────────────────────────────────

    def _apply_counts(self, path, apply, allowed, reason):
        fields, rows = _read_csv(path)
        absent = [c for c in REQUIRED_COLUMNS if c not in fields]
        if absent:
            raise CommandError(f'{path}: missing column(s) {", ".join(absent)}.')
        # An override may only name a line the CSV lists: an id from anywhere else is a
        # typo, and the whole run is refused before anything is read or written.
        in_csv = {int(r['line_id']) for r in rows if re.fullmatch(r'\d+', r.get('line_id', ''))}
        stray = sorted(allowed - in_csv)
        if stray:
            raise CommandError(f'--allow-lines names line(s) not in {path}: '
                               f'{", ".join(str(i) for i in stray)}.')

        mode = 'APPLY' if apply else 'DRY RUN (nothing written; use --apply to write)'
        self.stdout.write(f'fix_tin_shade_ballast --apply-counts {path} — {mode}')
        max_qty = decimal_field_max(BOQItem, 'boq_quantity')
        lines_by_id = {line.pk: line for line in _affected_lines()}

        buckets = {k: [] for k in ('applied', 'empty', 'bad', 'not_item', 'locked',
                                   'stale', 'unchanged')}
        seen = {}
        for number, row in enumerate(rows, start=2):    # row 1 is the header
            raw_id, raw_count = row.get('line_id', ''), row.get('new_count', '')
            label = f'row {number} (line {raw_id or "?"})'
            if not raw_count:
                buckets['empty'].append(label)
                continue
            if not re.fullmatch(r'\d+', raw_id):
                buckets['bad'].append(f'{label}: line_id is not a number')
                continue
            line_id = int(raw_id)
            if line_id in seen:
                buckets['bad'].append(f'{label}: line listed twice (first on row '
                                      f'{seen[line_id]})')
                continue
            seen[line_id] = number
            try:
                count = parse_decimal_input(raw_count, places=0, min_value=Decimal(1),
                                            max_value=max_qty, field_label='new_count')
            except ValidationError as exc:
                buckets['bad'].append(f'{label}: {raw_count!r} — {" ".join(exc.messages)}')
                continue
            count = Decimal(int(count))          # "12.0" is 12; store and log it as 12
            raw_current = row.get('current_quantity', '')
            try:
                expected = Decimal(raw_current) if raw_current else None
            except InvalidOperation:
                buckets['bad'].append(f'{label}: current_quantity {raw_current!r} is not '
                                      f'a number')
                continue

            line = lines_by_id.get(line_id)
            if line is None:
                if BOQItem.objects.filter(pk=line_id).exists():
                    # The id names a real line on some other item — a hand-edited or
                    # mistyped id, never one this command exported. Refused, not stale.
                    buckets['not_item'].append(f'{label}: not a Tin Shade / Ballast line')
                else:
                    buckets['stale'].append(f'{label}: changed since export (line gone)')
                continue
            if line.boq_quantity != expected:
                buckets['stale'].append(
                    f'{label}: changed since export (now {_qty_text(line.boq_quantity)}, '
                    f'CSV says {_qty_text(expected)})')
                continue
            # THE OVERRIDE BYPASSES THIS REFUSAL AND NOTHING ELSE. Every check above (whole
            # number, other item, listed twice, stale) has already run for an allowed line
            # too; only the lock/order state, read live here, is set aside — and the
            # reasons it found go into the log beside the person's stated reason.
            reasons = lock_reasons(line)
            override = ''
            if reasons:
                if line_id not in allowed:
                    buckets['locked'].append(f'{label} {line.boq.project.project_id}: '
                                             f'{"; ".join(reasons)}')
                    continue
                override = '; '.join(reasons)
            if line.boq_quantity == count and line.uom == TARGET_UNIT:
                buckets['unchanged'].append(f'{label}: already {count} {TARGET_UNIT}')
                continue

            change = (f'{label} {line.boq.project.project_id} {line.item_master.code}: '
                      f'quantity {_qty_text(line.boq_quantity)} -> {count}, '
                      f'uom {line.uom} -> {TARGET_UNIT}')
            if override:
                change += f' [lock override: {override}]'
            if apply and not self._write_count(line, expected, count, row.get('note', ''),
                                               override, reason):
                buckets['stale'].append(f'{label}: changed since export (during apply)')
                continue
            buckets['applied'].append(change)

        self._report(buckets, apply)

    def _write_count(self, line, expected, count, note, override='', reason=''):
        """Write one count and its log entries, in one transaction. True if written.

        `override` is the lock/order reasons set aside by --allow-lines (blank when there
        was nothing to override); it is logged as "lock override: <reasons>; reason:
        <the person's text>".

        RACE: the UPDATE is conditional on the line still being on one of the two items
        and still holding the quantity the CSV showed. A designer or reviewer saving the
        line after it was read makes it match 0 rows, so nothing is written or logged and
        the row is reported as stale. The lock re-check just before this call is not
        inside the condition — a group locked or an order raised in the instant between
        the two is not caught; the window is one row's work wide.
        """
        held = (Q(boq_quantity__isnull=True) if expected is None
                else Q(boq_quantity=expected))
        old_uom = line.uom
        project = line.boq.project
        with transaction.atomic():
            updated = (BOQItem.objects
                       .filter(held, pk=line.pk, item_master__code__in=ITEM_CODES)
                       .update(boq_quantity=count, uom=TARGET_UNIT))
            if not updated:
                return False
            parts = []
            if expected != count:
                parts.append(f'quantity: {_qty_text(expected)} → {count}')
            if old_uom != TARGET_UNIT:
                parts.append(f'uom: {old_uom} → {TARGET_UNIT}')
            prefix = (f'{ACTOR} updated BOQ line #{line.pk} {line.item_master.code} on '
                      f'{project.project_id}')
            main = f'{prefix}: {"; ".join(parts)}'
            extras = []
            if override:
                extras.append(f'lock override: {override}; reason: {reason}')
            if note:
                extras.append(f'note: {note}')
            joined = '; '.join([main] + extras)
            # When the override and note do not fit beside the values, each gets an entry
            # of its own rather than pushing the values past 255 characters, where
            # log_activity would cut them off.
            entries = ([joined] if len(joined) <= _ACTION_MAX
                       else [main] + [f'{prefix}: {extra}' for extra in extras])
            for text in entries:
                log_activity(project, None, text, entity_type='BOQItem',
                             entity_id=line.pk, action_code=LINE_ACTION_CODE)
        return True

    def _report(self, buckets, apply):
        titles = (
            ('applied',   'applied' if apply else 'would apply'),
            ('empty',     'skipped (empty new_count)'),
            ('bad',       'refused (bad value)'),
            ('not_item',  'refused (not on OPX-037/038)'),
            ('locked',    'refused (locked or ordered)'),
            ('stale',     'skipped (changed since export)'),
            ('unchanged', 'skipped (already that count in Nos)'),
        )
        for key, title in titles:
            if buckets[key]:
                self.stdout.write(f'{title}:')
                for entry in buckets[key]:
                    self.stdout.write(f'  {entry}')
        self.stdout.write('Summary: ' + ', '.join(
            f'{title} {len(buckets[key])}' for key, title in titles) + '.')
