"""
Management command: remove exactly what `seed_order_demo` recorded creating, and
nothing else — and refuse outright if an order or a payment has come to reference it.

    python manage.py teardown_order_demo              # dry run — reports only
    python manage.py teardown_order_demo --confirm    # delete

TYPE BOTH COMMAND NAMES IN FULL. `se`+Tab does not disambiguate `seed_order_demo`
from `send_eod_digest`, which mails the whole company, nor from the three other
seeds. The runbook already records that collision; it is repeated here because this
is where somebody will be typing fast.

THE MANIFEST IS THE ONLY THING THIS COMMAND READS
-------------------------------------------------
It deletes a list of primary keys. It does not search for rows to delete, and it runs
no query that could select a row the seed did not create. `teardown_opex_test_data`'s
docstring argues that design out in full — why a name-prefix sweep was the wrong
mechanism, and why there is deliberately no fallback to one when the manifest is
missing — and this command uses the same `Manifest` class, the same refusal message
and the same reverse walk. What differs is stated below; nothing else is restated.

ITS MANIFEST IS AT ~/.horizon-pms-orderdemo/, NOT ~/.horizon-pms-demo/. The two demo
data sets are independent by construction: this command cannot touch a DEMO row and
`teardown_opex_test_data` cannot touch an ORDDEMO one, because neither can name a pk
the other wrote. See `seed_order_demo`'s docstring for why that separation is worth a
second manifest file.

THE ORDER GUARD — THE ONE THING THIS COMMAND HAS THAT THE OTHER DOES NOT
-------------------------------------------------------------------------
`seed_order_demo` creates NO VendorOrder and NO PaymentRequest, on purpose: raising
one is the demonstration. So by the time anyone runs this, the operator has probably
raised one — against these vendors, these sites, these tenders, these BOQ rows.

DELETING THE SEED OUT FROM UNDER THAT ORDER MUST NOT BE POSSIBLE, and "the FK will
stop it" is not good enough, because half of these FKs would not stop it — they would
quietly corrupt the order instead:

    VendorOrderSite.project        PROTECT    loud
    VendorOrderProgram.program     PROTECT    loud
    VendorOrder.vendor             PROTECT    loud
    VendorOrder.created_by         PROTECT    loud
    VendorOrderDocument.uploaded_by PROTECT   loud
    PaymentRequest.requested_by    PROTECT    loud

    PaymentRequest.project         CASCADE    SILENT — deletes the payment outright
    PaymentRequest.vendor          SET_NULL   SILENT — the payee is erased
    PaymentRequest.boq_item        SET_NULL   SILENT
    PaymentRequest.approved_by     SET_NULL   SILENT
    PaymentRequest.confirmed_by    SET_NULL   SILENT
    VendorOrderSite.via_site_group SET_NULL   SILENT — "picked via group X" is erased
    VendorOrderLine.boq_item       SET_NULL   SILENT

A PROTECT would abort the transaction with a traceback nobody reads as "your order is
in the way". A SET_NULL or that one CASCADE would succeed, and the operator would
find a payment gone or an order pointing at nothing, with nothing recording why.

`PaymentRequest.vendor_order` (PROTECT) is deliberately absent from that list: it
points at a VendorOrder, and the seed creates none, so it can never be a reference
INTO seeded data. Every column that can be is above.

So the guard runs BEFORE anything is deleted, names every reference it found, and
exits non-zero. It is a positive check over those thirteen relations, not a guess:
a reference this command does not know about is a reference it cannot report, so
adding a column to any of those models means adding a line to `_order_references()`.
There is no --force. Removing the order first is the operator's decision, made in the
product, not a flag on a cleanup script.

DELETION ORDER
--------------
The manifest is written in creation order and walked in REVERSE, which puts children
before parents and leaves before both: memberships before groups, BOQ items before
BOQs, attempts before assignments, sites before tenders, profiles before users.
`Project.program` is PROTECT and Django does not resolve a PROTECT inside a single
cascade even when the protecting row is part of the same delete, so the order is
load-bearing, not tidiness.

A row already gone — cascaded away by an earlier step, or deleted by hand — is
reported and is NOT an error. Absence is the desired end state.

STATUS TRANSITIONS ARE DELETED, AND THAT IS THE SAME DELIBERATE EXCEPTION TO R-4
--------------------------------------------------------------------------------
`StatusTransition` is append-only: `save()` refuses to touch an existing row and
`delete()` raises `AppendOnlyViolation`. `QuerySet.delete()` operates in SQL and
bypasses both, as the model's own docstring says out loud, and that is the route
taken here — per manifest entry, so ORDER stays the mechanism.

The justification is `teardown_opex_test_data`'s, unchanged and equally narrow:
`StatusTransition.project` is SET_NULL precisely so a hard-deleted project cannot
erase its own history, so without this every teardown would leave orphaned ledger
rows behind permanently, growing by one set per cycle, in the table the dwell-time
reports read. It is only ever safe because the MANIFEST BOUNDS IT, on a database the
interlock has already proved is local. Do not generalise it.

NO STORAGE STEP, AND THAT IS NOT AN OMISSION
---------------------------------------------
`seed_order_demo` creates no DesignFile and leaves `survey_file_path` empty on every
assignment, precisely so nothing here has to reach into the private Supabase bucket
that this machine addresses with production credentials. There is nothing in object
storage to remove, so there is no code here that could remove the wrong thing.
"""
import sys

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from projects.management.commands._demo_support import (
    MANIFEST_MISSING_MESSAGE, Manifest, OVERRIDE_FLAG, print_db_banner,
    require_local_database,
)
from projects.management.commands.seed_order_demo import DEFAULT_MANIFEST_PATH

WRITES = [
    'DELETE every row listed in the order-demo manifest, in reverse creation order',
    'including its StatusTransition rows, bypassing the append-only guard',
]


class Command(BaseCommand):
    help = ('Delete exactly the rows seed_order_demo recorded creating. Refuses if any '
            'VendorOrder or PaymentRequest references them, refuses without a manifest '
            'and refuses a non-local database. Dry run unless --confirm.')

    def add_arguments(self, parser):
        parser.add_argument('--confirm', action='store_true',
                            help='Actually delete. Without this the command only reports.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report only. This is also the default.')
        parser.add_argument('--manifest', type=str, default='',
                            help=f'Manifest to read. Default: {DEFAULT_MANIFEST_PATH}')
        parser.add_argument(OVERRIDE_FLAG, action='store_true',
                            help='Required to run against a non-local database.')

    def handle(self, *args, **options):
        # Always the first line of output, before anything else is decided.
        print_db_banner(self)
        require_local_database(
            self, options.get('i_know_this_is_not_local', False), WRITES)

        confirm       = options['confirm']
        manifest_path = options['manifest'].strip() or DEFAULT_MANIFEST_PATH

        manifest = Manifest.load(manifest_path)
        if manifest is None:
            raise CommandError(MANIFEST_MISSING_MESSAGE.format(path=manifest_path))

        self.stdout.write(f'Manifest      : {manifest_path}')
        seeded_db = manifest.meta.get('database', {})
        self.stdout.write(f'Written at    : {manifest.meta.get("written_at", "(unknown)")}')
        self.stdout.write(f'Seeded against: host={seeded_db.get("host", "?")} '
                          f'name={seeded_db.get("name", "?")}')
        self.stdout.write(f'Rows recorded : {len(manifest.entries)}')
        self.stdout.write('')

        if not manifest.entries:
            self.stdout.write('Manifest is empty — nothing to delete.')
            return

        # ---- resolve each entry to a model, in reverse creation order ----
        # An entry naming a model this build does not have is reported, never guessed
        # at: a manifest written before a model was renamed must not be silently
        # half-applied.
        plan, unknown = [], []
        for row in reversed(manifest.entries):
            model = self._resolve(row['model'])
            (plan.append((model, row)) if model is not None else unknown.append(row))

        present, missing = [], []
        for model, row in plan:
            (present if model.objects.filter(pk=row['pk']).exists()
             else missing).append((model, row))

        # ---- THE ORDER GUARD — before the plan is even printed ----
        # It runs on a DRY RUN TOO. The dry run is what somebody uses to find out
        # whether this is safe, so it has to answer that question.
        blocked = self._order_references(present)
        if blocked:
            self.stderr.write(self.style.ERROR(
                'REFUSING TO DELETE: a vendor order or a payment request references '
                'this demo data.'))
            self.stderr.write('')
            for line in blocked:
                self.stderr.write(f'  {line}')
            self.stderr.write('')
            self.stderr.write(
                'seed_order_demo deliberately creates no order and no payment — '
                'raising one is\n'
                'the demonstration, so these are the operator\'s own rows. Deleting '
                'the sites,\n'
                'tenders, vendors and BOQ rows underneath them would either abort on '
                'a PROTECT\n'
                'or, worse, succeed: PaymentRequest.project is CASCADE and six other '
                'columns are\n'
                'SET_NULL, so a payment would vanish or an order would quietly lose '
                'what it was\n'
                'raised against.\n'
                '\n'
                'There is no --force. Delete the order and its payments through the '
                'product first,\n'
                'then run this again. The manifest is left in place.')
            sys.exit(1)

        self.stdout.write('To delete, by model (deletion order is reverse of creation):')
        for label, count in self._tally(present):
            self.stdout.write(f'  {label:<34} {count}')
        self.stdout.write(f'  {"TOTAL":<34} {len(present)}')
        if missing:
            self.stdout.write('')
            self.stdout.write('Already gone (cascaded, or removed by hand) — not an error:')
            for label, count in self._tally(missing):
                self.stdout.write(f'  {label:<34} {count}')
        if unknown:
            self.stdout.write('')
            for row in unknown:
                self.stderr.write(self.style.WARNING(
                    f'  UNKNOWN MODEL  {row["model"]} pk={row["pk"]} — this build has '
                    f'no such model; left alone.'))
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            'No VendorOrder or PaymentRequest references any of it.'))
        self.stdout.write('')

        if not confirm:
            self.stdout.write(self.style.WARNING(
                'DRY RUN — nothing deleted, manifest left in place. Re-run with '
                '--confirm to delete.'))
            return

        # ---- delete rows, one manifest entry at a time, in reverse order ----
        # Per-entry rather than one queryset per model, because ORDER is the whole
        # mechanism: a bulk delete per model would re-sort the work by model and lose
        # the dependency ordering the manifest encodes.
        #
        # Model.objects.filter(pk=...).delete() is a QuerySet delete: it does not call
        # the instance's delete(), which is what lets StatusTransition rows go at all.
        # See the module docstring — a bounded, deliberate exception to R-4.
        deleted, vanished = {}, 0
        with transaction.atomic():
            for model, row in plan:
                count, _detail = model.objects.filter(pk=row['pk']).delete()
                if count:
                    deleted[row['model']] = deleted.get(row['model'], 0) + 1
                else:
                    vanished += 1

        self.stdout.write('Deleted:')
        for label in sorted(deleted):
            self.stdout.write(self.style.SUCCESS(f'  {label:<34} {deleted[label]}'))
        self.stdout.write(f'  {"TOTAL":<34} {sum(deleted.values())}')
        if vanished:
            self.stdout.write(f'{vanished} manifest row(s) were already gone — '
                              f'cascaded or removed by hand. Not an error.')
        self.stdout.write('')

        # The manifest describes rows that no longer exist. Leaving it in place would
        # make the next teardown report a database-wide "already gone", which reads as
        # a failure; and the next seed writes a fresh one anyway.
        try:
            manifest.path.unlink()
            self.stdout.write(f'Manifest consumed and removed: {manifest.path}')
        except OSError as exc:
            self.stdout.write(self.style.WARNING(
                f'Rows deleted, but the manifest could not be removed '
                f'({manifest.path}): {exc}. Delete it by hand.'))

        self.stdout.write(self.style.SUCCESS(
            'Teardown complete — hard delete, no is_deleted flag was set.'))

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _resolve(label):
        try:
            app_label, model_name = label.split('.', 1)
            return apps.get_model(app_label, model_name)
        except (ValueError, LookupError):
            return None

    @staticmethod
    def _tally(pairs):
        counts = {}
        for _model, row in pairs:
            counts[row['model']] = counts.get(row['model'], 0) + 1
        return sorted(counts.items())

    def _order_references(self, present):
        """Human-readable lines for every VendorOrder / PaymentRequest row that points
        at something in the manifest. Empty list means the teardown is safe.

        ONE QUERY PER RELATION, over pk lists taken from the MANIFEST — never a scan
        for "demo-looking" orders. The safety property the row deletion has is the
        property this check has: it can only ever ask about rows the seed created.

        Every relation into the seeded models is listed, PROTECT and SET_NULL alike,
        with the full table in the module docstring. A column added to any of these
        models without a line here is a reference this command would not report and
        would therefore silently break.
        """
        from django.contrib.auth.models import User

        from projects.models import (BOQItem, PaymentRequest, Program, Project,
                                     SiteGroup, UserProfile, Vendor, VendorOrder,
                                     VendorOrderDocument, VendorOrderLine,
                                     VendorOrderProgram, VendorOrderSite)

        def pks(model):
            return [row['pk'] for m, row in present if m is model]

        project_pks = pks(Project)
        program_pks = pks(Program)
        vendor_pks  = pks(Vendor)
        group_pks   = pks(SiteGroup)
        item_pks    = pks(BOQItem)
        profile_pks = pks(UserProfile)
        user_pks    = pks(User)

        # (queryset, how to describe one row). Ordered order-first, then payments.
        checks = [
            (VendorOrderSite.objects.filter(project_id__in=project_pks)
             .select_related('order', 'project'),
             lambda r: f'VendorOrder #{r.order_id} is sized against site '
                       f'{r.project.project_id} (VendorOrderSite.project, PROTECT)'),
            (VendorOrderSite.objects.filter(via_site_group_id__in=group_pks)
             .select_related('order', 'via_site_group'),
             lambda r: f'VendorOrder #{r.order_id} records group '
                       f'"{r.via_site_group.name}" as how a site was picked '
                       f'(VendorOrderSite.via_site_group, SET_NULL)'),
            (VendorOrderProgram.objects.filter(program_id__in=program_pks)
             .select_related('order', 'program'),
             lambda r: f'VendorOrder #{r.order_id} is sized against tender '
                       f'"{r.program.name}" (VendorOrderProgram.program, PROTECT)'),
            (VendorOrderLine.objects.filter(boq_item_id__in=item_pks)
             .select_related('order'),
             lambda r: f'VendorOrder #{r.order_id} has a line traced to a seeded BOQ '
                       f'row (VendorOrderLine.boq_item, SET_NULL)'),
            (VendorOrder.objects.filter(vendor_id__in=vendor_pks)
             .select_related('vendor'),
             lambda r: f'VendorOrder #{r.pk} was placed with vendor '
                       f'"{r.vendor.name}" (VendorOrder.vendor, PROTECT)'),
            (VendorOrder.objects.filter(created_by_id__in=profile_pks),
             lambda r: f'VendorOrder #{r.pk} was raised by a seeded account '
                       f'(VendorOrder.created_by, PROTECT)'),
            (VendorOrderDocument.objects.filter(uploaded_by_id__in=profile_pks),
             lambda r: f'VendorOrder #{r.order_id} has a document uploaded by a seeded '
                       f'account (VendorOrderDocument.uploaded_by, PROTECT)'),
            (PaymentRequest.objects.filter(project_id__in=project_pks)
             .select_related('project'),
             lambda r: f'PaymentRequest #{r.pk} is anchored to site '
                       f'{r.project.project_id} (PaymentRequest.project, CASCADE — '
                       f'deleting the site would DELETE THE PAYMENT)'),
            (PaymentRequest.objects.filter(vendor_id__in=vendor_pks)
             .select_related('vendor'),
             lambda r: f'PaymentRequest #{r.pk} is payable to vendor '
                       f'"{r.vendor.name}" (PaymentRequest.vendor, SET_NULL)'),
            (PaymentRequest.objects.filter(boq_item_id__in=item_pks),
             lambda r: f'PaymentRequest #{r.pk} cites a seeded BOQ row '
                       f'(PaymentRequest.boq_item, SET_NULL)'),
            (PaymentRequest.objects.filter(requested_by_id__in=user_pks),
             lambda r: f'PaymentRequest #{r.pk} was raised by a seeded account '
                       f'(PaymentRequest.requested_by, PROTECT)'),
            (PaymentRequest.objects.filter(approved_by_id__in=profile_pks),
             lambda r: f'PaymentRequest #{r.pk} was approved by a seeded account '
                       f'(PaymentRequest.approved_by, SET_NULL)'),
            (PaymentRequest.objects.filter(confirmed_by_id__in=user_pks),
             lambda r: f'PaymentRequest #{r.pk} was paid by a seeded account '
                       f'(PaymentRequest.confirmed_by, SET_NULL)'),
        ]

        lines = []
        for queryset, describe in checks:
            for row in queryset.order_by('pk'):
                lines.append(describe(row))
        return lines
