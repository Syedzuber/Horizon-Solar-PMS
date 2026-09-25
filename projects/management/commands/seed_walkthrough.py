"""
Management command: build the synthetic WALKTHROUGH database — every implemented
feature, in every state a person can reach, for walking by hand.

    python manage.py seed_walkthrough                 # all areas
    python manage.py seed_walkthrough --only design   # one area (plus users/reference)
    python manage.py seed_walkthrough --dry-run       # plan only, writes nothing

TYPE THE NAME IN FULL. `se`+Tab does not disambiguate it from `send_eod_digest`, which
mails the whole company.

Read docs/WALKTHROUGH_DATA.md before a walk — it is the map of what this builds. The
audit behind it is docs/WALKTHROUGH_SEED_AUDIT.md.

ONLY ON A WALKTHROUGH DATABASE
------------------------------
The first line of every run is the database host and name. Then four checks, all of
which must pass, with no override flag (`_walkthrough_support.refusal_reasons()`):
local host; a name starting `solarpms_walk`; no user outside `.invalid` except the one
address the Residential template demands; no email/WhatsApp NotificationLog row 'sent'.

EVERY STATE IS DRIVEN THROUGH THE PRODUCT
-----------------------------------------
Rows are not written to look like a state. Each state is reached by POSTing to the real
view, as the person the product requires, through `django.test.Client` — the way
`seed_scm_pilot` already activates sites. After every step the database is re-read and
the command stops if the state did not move; a 302 alone proves nothing, because the
product answers most refusals with one.

That is why a released site here carries everything a released site implies: survey
link, allocation and due-date commitment, an Arka approved at both gates by two
different people, a CAD archive, a BOQ, the QC and Head passes, the PM's approval, and
the StatusTransition row for every hop. It also keeps `tests_design_pm_gate_live` true:
this file writes no design status at all, so it is not, and need not be, in that
test's writer set.

Request-free cores are called directly only where the audit found one AND no view
fronts it more faithfully. Three writes have NO product path and are made directly,
each marked `# NO PRODUCT PATH` and listed in the doc: the first Admin account on an
empty database, and the Design Head's deputy link.

WHAT IS STUBBED, AND WHY A DOWNLOAD DOES NOTHING
------------------------------------------------
`_walkthrough_support.seed_sandbox()` is active for the whole run: storage uploads are
stubbed and SUPABASE_URL/KEY are blanked (the local .env carries PRODUCTION storage
credentials), and the notification API keys are blanked. Every stubbed file is uploaded
under a name beginning `SEEDED-NO-FILE-`, into a bucket that does not exist, so a walker
who clicks its download sees why nothing arrives.

TIME
----
Histories are driven under `SimClock`: the product writes every timestamp itself, at a
simulated past instant, so ages and pools look real. Nothing is backdated afterwards,
and the clock never runs backwards within a history.

IDEMPOTENT BY AREA
------------------
Each area is recorded in the manifest (`~/.horizon-pms-walkthrough/<db>.json`, bound to
this database's oid). A second run finds every area present and complete, says so, and
writes nothing. An area that is present but damaged, or present without a manifest,
is refused: the reset is DROP DATABASE and re-seed (see the doc).

NEVER PRINTS A PASSWORD. The walkthrough password is in docs/WALKTHROUGH_DATA.md.
"""
import io
import uuid
import zipfile
from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.test import Client
from django.urls import reverse

from projects.management.commands._walkthrough_support import (
    AreaManifest, SimClock, WALK_EMAIL_DOMAIN, database_fingerprint,
    default_manifest_path, permitted_real_email, print_db_banner,
    require_walkthrough_database, rows_since, seed_sandbox, stub_file_name, take_marks,
)

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
#: The one password every walkthrough account shares. It is deliberately NOT printed by
#: this command; docs/WALKTHROUGH_DATA.md carries it. >= 8 chars for UserCreateForm.
WALK_PASSWORD = 'WalkPass!2026'

#: username, first, last, role, flags. Every last name is letters only (the form
#: refuses anything else). Order matters only for readability.
USERS = [
    ('walk.admin',        'Walk', 'Admin',        'Admin',               ()),
    ('walk.sysadmin',     'Walk', 'Sysadmin',     'System Admin',        ()),
    ('walk.pm',           'Walk', 'Pm',           'PM',                  ()),
    ('walk.coord',        'Walk', 'Coord',        'Project Coordinator', ()),
    ('walk.se',           'Walk', 'Se',           'Site Engineer',       ()),
    ('walk.qaqc',         'Walk', 'Qaqc',         'Site Engineer',       ('is_qaqc',)),
    ('walk.design',       'Walk', 'Design',       'Design',              ()),
    ('walk.designqc',     'Walk', 'Designqc',     'Design',              ('is_design_qc',)),
    ('walk.designhead',   'Walk', 'Designhead',   'Design',              ('is_design_head',)),
    ('walk.designdeputy', 'Walk', 'Designdeputy', 'Design',              ()),
    ('walk.scm',          'Walk', 'Scm',          'SCM',                 ('is_warehouse_keeper',)),
    ('walk.finance',      'Walk', 'Finance',      'Finance',             ('is_payment_approver',)),
    ('walk.finpay',       'Walk', 'Finpay',       'Finance',             ()),
    ('walk.finassignee',  'Walk', 'Finassignee',  'Finance',             ()),
    ('walk.ceo',          'Walk', 'Ceo',          'CEO',                 ()),
    ('walk.bd',           'Walk', 'Bd',           'BD',                  ()),
]

#: The one account whose address is NOT .invalid — see permitted_real_email().
REAL_ADDRESS_USERNAME = 'walk.finassignee'

AREAS = ['users', 'reference', 'design', 'changes', 'procurement', 'delivery',
         'execution', 'residential']
DEPENDS = {'users': [], 'reference': ['users']}
for _area in AREAS[2:]:
    DEPENDS[_area] = ['users', 'reference']

TENDERS = {
    'design':      ('WALKDSN', 'WALK Design Walk'),
    'changes':     ('WALKCR',  'WALK Change Requests'),
    'procurement': ('WALKPRC', 'WALK Procurement and Payments'),
    'delivery':    ('WALKDLV', 'WALK Deliveries'),
    'execution':   ('WALKEXE', 'WALK Site Execution'),
}
CLIENT_NAME = 'WALK Client (synthetic)'

WAREHOUSES = [
    ('WALK-WH-DEL',  'WALK Delhi Central Warehouse', 'walk.scm'),
    ('WALK-WH-MUM',  'WALK Mumbai Store',            'walk.scm'),
    ('WALK-WH-SITE', 'WALK Site Container',          None),
]
VENDORS = [
    ('WALK Modules & Structure Supplies', ['Solar Modules', 'Structure']),
    ('WALK Inverter & Switchgear Supplies', ['Inverter', 'BOS']),
    ('WALK Cable & BOS Supplies', ['BOS', 'Other']),
]

#: The OPEX catalogue rows a designer's BOQ uses — the first N active OPEX masters by
#: sort order, quantities scaled per site so aggregates are worth reading.
OPEX_BOQ_ROWS = 6

#: The design route, in order. `upto` names the last step taken.
DESIGN_ROUTE = ['survey', 'allocate', 'arka', 'arka_qc', 'arka_head', 'cad', 'boq',
                'qc_start', 'qc_pass', 'head_pass', 'pm_approve']


class Command(BaseCommand):
    help = ('Build the synthetic walkthrough database by driving the real views. Runs '
            'only on a local solarpms_walk* database of synthetic users. Idempotent '
            'by area. Never prints a password.')

    def add_arguments(self, parser):
        parser.add_argument('--only', choices=AREAS, action='append', default=[],
                            help='Seed only this area (repeatable). Its dependencies '
                                 '(users, reference) are seeded first if absent.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Print the plan and write nothing.')
        parser.add_argument('--manifest', type=str, default='',
                            help='Manifest path. Default: '
                                 '~/.horizon-pms-walkthrough/<database name>.json')

    # ================================================================== handle
    def handle(self, *args, **options):
        print_db_banner(self)
        require_walkthrough_database(self)

        wanted = self._resolve_areas(options['only'])
        path = options['manifest'].strip() or default_manifest_path()
        manifest = self._load_manifest(path)

        plan = []
        for area in wanted:
            if area in manifest.areas:
                missing = manifest.missing_rows(area)
                if missing:
                    raise CommandError(
                        f'Area {area!r} is recorded in the manifest but {len(missing)} of '
                        f'its rows are gone. It cannot be rebuilt in place (its history '
                        f'rows are append-only). Reset with DROP DATABASE and re-seed — '
                        f'see docs/WALKTHROUGH_DATA.md.')
                plan.append((area, 'present'))
            else:
                if self._area_marker_exists(area):
                    raise CommandError(
                        f'Area {area!r} has WALK rows in this database but no manifest '
                        f'entry — a previous run stopped part-way, or the manifest was '
                        f'lost. Reset with DROP DATABASE and re-seed.')
                plan.append((area, 'seed'))

        self.stdout.write('Plan:')
        for area, verdict in plan:
            self.stdout.write(f'  {area:<12} '
                              f'{"already present - skipped" if verdict == "present" else "WILL SEED"}')
        self.stdout.write(f'Manifest: {manifest.path}')
        if options['dry_run']:
            self.stdout.write(self.style.WARNING('DRY RUN - nothing written.'))
            return
        if all(v == 'present' for _a, v in plan):
            self.stdout.write(self.style.SUCCESS(
                'Every requested area is already present and complete. Nothing written.'))
            self._report(manifest, [a for a, _v in plan])
            return

        self.clock = SimClock()
        self.clients = {}
        with seed_sandbox():
            for area, verdict in plan:
                if verdict == 'present':
                    continue
                self._load_people()
                marks = take_marks()
                self.stdout.write(f'Seeding {area} ...')
                getattr(self, f'_area_{area}')()
                manifest.record_area(area, rows_since(marks))
                manifest.save()
                self.stdout.write(self.style.SUCCESS(
                    f'  {area}: {len(manifest.entries(area))} rows recorded'))

        self._report(manifest, [a for a, _v in plan])

    # ------------------------------------------------------------ bookkeeping
    def _resolve_areas(self, only):
        if not only:
            return list(AREAS)
        wanted = []
        for area in only:
            for dep in DEPENDS[area] + [area]:
                if dep not in wanted:
                    wanted.append(dep)
        return [a for a in AREAS if a in wanted]

    def _load_manifest(self, path):
        fingerprint = database_fingerprint()
        manifest = AreaManifest.load(path)
        if manifest is None:
            if User.objects.filter(username__startswith='walk.').exists():
                raise CommandError(
                    f'This database already has walk.* users but there is no readable '
                    f'manifest at {path}. Reset with DROP DATABASE and re-seed.')
            return AreaManifest(path, fingerprint)
        if manifest.fingerprint != fingerprint:
            if User.objects.filter(username__startswith='walk.').exists():
                raise CommandError(
                    f'The manifest at {path} was written against a different database '
                    f'({manifest.fingerprint}); this one is {fingerprint} and already '
                    f'holds walk.* users. Reset with DROP DATABASE and re-seed.')
            # A fresh database with a leftover manifest from the one it replaced: the old
            # file describes rows that no longer exist anywhere. Start a new one.
            stale = manifest.path.with_suffix('.stale.json')
            manifest.path.replace(stale)
            self.stdout.write(f'Old manifest belonged to a dropped database; moved to {stale}')
            return AreaManifest(path, fingerprint)
        return manifest

    def _area_marker_exists(self, area):
        from projects.models import Program, Project, StockLocation, Vendor
        if area == 'users':
            return User.objects.filter(username__startswith='walk.').exists()
        if area == 'reference':
            return (StockLocation.objects.filter(code__startswith='WALK-').exists()
                    or Vendor.objects.filter(name__startswith='WALK ').exists())
        if area == 'residential':
            return Project.objects.filter(customer_name__startswith='WALK Residential').exists()
        return Program.objects.filter(short_tender_code=TENDERS[area][0]).exists()

    def _load_people(self):
        from projects.models import UserProfile
        self.p = {p.user.username: p for p in
                  UserProfile.objects.select_related('user')
                  .filter(user__username__startswith='walk.')}

    # --------------------------------------------------------------- driving
    def _client(self, profile):
        client = self.clients.get(profile.pk)
        if client is None:
            client = Client(SERVER_NAME='localhost')
            client.force_login(profile.user)
            self.clients[profile.pk] = client
        return client

    def _post(self, who, name, kwargs=None, data=None, *, check=None, what='',
              hx=False, json=None, ok=(200, 302)):
        """POST as `who` (a username), then re-read the database with `check`.

        `check` is a no-argument callable returning truthy when the step did what it
        was for. A failed check stops the run and quotes the product's own messages.
        """
        profile = self.p[who]
        url = reverse(name, kwargs=kwargs or {})
        extra = {'HTTP_HX_REQUEST': 'true'} if hx else {}
        client = self._client(profile)
        if json is not None:
            response = client.post(url, data=json, content_type='application/json', **extra)
        else:
            response = client.post(url, data or {}, **extra)
        messages = ' | '.join(str(m) for m in get_messages(response.wsgi_request))
        label = what or name
        if response.status_code not in ok:
            raise CommandError(f'{label}: {name} as {who} returned HTTP '
                               f'{response.status_code}. {messages}')
        if check is not None and not check():
            raise CommandError(f'{label}: {name} as {who} did not reach the expected '
                               f'state. Product said: {messages or "(nothing)"}')
        return response

    def _get(self, who, name, kwargs=None):
        response = self._client(self.p[who]).get(reverse(name, kwargs=kwargs or {}))
        if response.status_code != 200:
            raise CommandError(f'GET {name} as {who} returned {response.status_code}')
        return response

    # ================================================================== users
    def _area_users(self):
        """Sixteen accounts, their flags, their preferences, and the master switches.

        Every account after the first goes through the Admin's own `user_create` view;
        every flag through `admin_user_edit`; preferences through
        `admin_notification_prefs`; the switches through `admin_master_switches`.
        """
        from projects.forms import UserCreateForm
        from projects.models import SystemSettings

        # --- the first Admin -------------------------------------------------------
        # NO PRODUCT PATH. On an empty database there is no Admin to sign in as, and
        # `user_create` is Admin-only; `createsuperuser` would leave a blank-role profile.
        # So UserCreateForm validates it and the four writes `views.user_create` performs
        # after validation are replicated, as seed_opex_test_data does.
        username, first, last, role, _flags = USERS[0]
        form = UserCreateForm({
            'first_name': first, 'last_name': last, 'username': username,
            'email': f'{username}@{WALK_EMAIL_DOMAIN}', 'password': WALK_PASSWORD,
            'role': role, 'phone_number': '9100000000', 'is_active': True,
        })
        if not form.is_valid():
            raise CommandError(f'UserCreateForm refused {username}: {form.errors.as_json()}')
        cd = form.cleaned_data
        admin = User.objects.create_user(
            username=cd['username'], password=cd['password'], first_name=cd['first_name'],
            last_name=cd['last_name'], email=cd['email'], is_active=True, is_staff=True)
        profile = admin.profile
        profile.role, profile.phone_number, profile.is_active = role, cd['phone_number'], True
        profile.save()
        self._load_people()

        # --- everyone else, through the Admin's own screen --------------------------
        for index, (username, first, last, role, _flags) in enumerate(USERS[1:], start=1):
            email = (permitted_real_email() if username == REAL_ADDRESS_USERNAME
                     else f'{username}@{WALK_EMAIL_DOMAIN}')
            self._post('walk.admin', 'user_create', data={
                'first_name': first, 'last_name': last, 'username': username,
                'email': email, 'password': WALK_PASSWORD, 'role': role,
                'phone_number': f'91000000{index:02d}', 'is_active': 'on',
            }, check=lambda u=username: User.objects.filter(username=u).exists(),
                what=f'create {username}')
        self._load_people()

        # --- capability flags, through admin_user_edit ------------------------------
        for username, first, last, role, flags in USERS:
            if not flags:
                continue
            user = self.p[username].user
            data = {'first_name': first, 'last_name': last, 'username': username,
                    'email': user.email, 'phone_number': self.p[username].phone_number,
                    'role': role, 'new_password': '', 'confirm_password': ''}
            data.update({flag: 'on' for flag in flags})
            self._post('walk.admin', 'admin_user_edit', {'user_id': user.pk}, data,
                       check=lambda u=username, f=flags: all(
                           getattr(type(self.p[u]).objects.get(pk=self.p[u].pk), x)
                           for x in f),
                       what=f'flags on {username}')

        # --- the Design Head's deputy ------------------------------------------------
        # NO PRODUCT PATH. `design_head_deputy` has no portal writer; its only editor is
        # the Django admin change form, which needs model permissions no portal Admin
        # holds. Set on the Head's profile, exactly the field the predicate reads.
        head = self.p['walk.designhead']
        head.design_head_deputy = self.p['walk.designdeputy']
        head.save(update_fields=['design_head_deputy'])

        # --- preferences off, for every account ---------------------------------------
        for username, *_rest in USERS:
            profile = self.p[username]
            self._post('walk.admin', 'admin_notification_prefs',
                       data={'profile_id': profile.pk},   # both boxes absent = both off
                       check=lambda pk=profile.pk: not type(profile).objects.filter(
                           pk=pk).filter(email_notifications=True).exists()
                       and not type(profile).objects.filter(
                           pk=pk, whatsapp_notifications=True).exists(),
                       what=f'preferences off for {username}')

        # --- master switches: email and WhatsApp OFF --------------------------------
        self._post('walk.admin', 'admin_master_switches', data={
            'in_app_notifications_enabled': 'on',
            'gantt_client_buffer_days': '3', 'gantt_external_min_display_days': '3',
        }, check=lambda: (not SystemSettings.get().email_enabled
                          and not SystemSettings.get().whatsapp_enabled),
            what='master switches off')
        self._load_people()

    # ============================================================== reference
    def _area_reference(self):
        """Warehouses, vendors, and the seven OPEX installation checklists."""
        from projects.models import Checklist, StockLocation, Vendor, VendorCategory

        for code, name, keeper in WAREHOUSES:
            self._post('walk.scm', 'stock_location_create', data={
                'code': code, 'name': name,
                'keeper': self.p[keeper].pk if keeper else '',
            }, check=lambda c=code: StockLocation.objects.filter(code=c).exists(),
                what=f'warehouse {code}')

        categories = {c.name: c.pk for c in VendorCategory.objects.all()}
        for index, (name, cats) in enumerate(VENDORS, start=1):
            self._post('walk.scm', 'vendor_add', data={
                'name': name, 'contact_person': f'Walk Sales {index}',
                'phone': f'98000000{index:02d}',
                'email': f'vendor{index}@{WALK_EMAIL_DOMAIN}',
                'address': 'Synthetic address (walkthrough only)',
                'categories': [categories[c] for c in cats],
            }, check=lambda n=name: Vendor.objects.filter(name=n).exists(),
                what=f'vendor {name}')

        # Reference content, idempotent by code, and a real command: reused, not copied.
        if not Checklist.objects.filter(code__startswith='OPEX-INST-').exists():
            call_command('seed_opex_installation_checklists', stdout=io.StringIO())

    # ================================================================ helpers
    def _program(self, area):
        from projects.models import Program
        code, name = TENDERS[area]
        with self.clock.at(60, history=f'program:{code}'):
            self._post('walk.pm', 'program_create', data={
                'program_type': 'OPEX', 'name': name, 'client_name': CLIENT_NAME,
                'status': 'Active', 'short_tender_code': code, 'planned_site_count': 8,
            }, check=lambda: Program.objects.filter(short_tender_code=code).exists(),
                what=f'tender {code}')
        return Program.objects.get(short_tender_code=code)

    def _site(self, program, code, days_ago, city='Walk City', capacity='150.00'):
        from projects.models import Project
        with self.clock.at(days_ago, history=code):
            self._post('walk.pm', 'opex_site_create', {'pk': program.pk}, {
                'site_code': code, 'site_address': f'{code}, synthetic address',
                'city': city, 'state': 'Walk State', 'dc_capacity_kw': capacity,
                'customer_contact_person': 'Walk Site Incharge',
                'customer_phone': '9200000000',
                'customer_email': f'{code.lower()}@{WALK_EMAIL_DOMAIN}',
            }, check=lambda: Project.objects.filter(project_id=code).exists(),
                what=f'site {code}')
        return Project.objects.get(project_id=code)

    def _status(self, site):
        from projects.models import DesignAssignment
        a = DesignAssignment.objects.filter(project=site).first()
        return a.status if a else None

    def _expect(self, site, status):
        return lambda: self._status(site) == status

    @staticmethod
    def _cad_upload():
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('SEEDED-NO-FILE-layout.pdf', b'%PDF-1.4 walkthrough stub')
            archive.writestr('SEEDED-NO-FILE-layout.dwg', b'AC1027 walkthrough stub')
        return SimpleUploadedFile(stub_file_name('cad_zip', 'zip'), buffer.getvalue(),
                                  content_type='application/zip')

    @staticmethod
    def _pdf(kind):
        return SimpleUploadedFile(stub_file_name(kind, 'pdf'), b'%PDF-1.4 walkthrough stub',
                                  content_type='application/pdf')

    def _opex_masters(self):
        from projects.models import get_opex_boq_catalogue
        chosen = list(get_opex_boq_catalogue()[:OPEX_BOQ_ROWS])
        from projects.models import get_opex_mandatory_items
        for m in get_opex_mandatory_items():
            if m not in chosen:
                chosen.append(m)
        return chosen

    # ------------------------------------------------------------ design steps
    def _d(self, site, step, scale=1):
        """One step of the design route, as the person the product requires."""
        from projects.models import BOQ
        from projects import models as m
        pid = {'project_id': site.project_id}
        if step == 'survey':
            self._post('walk.designhead', 'design_survey_link_set', pid,
                       {'survey_folder_url':
                            f'https://drive.google.com/drive/folders/{site.project_id}'},
                       check=self._expect(site, m.DESIGN_AWAITING_ALLOCATION),
                       what=f'{site.project_id} survey link')
        elif step == 'allocate':
            self._post('walk.designhead', 'design_allocate', pid,
                       {'designer_id': self.p['walk.design'].pk},
                       check=self._expect(site, m.DESIGN_IN_DESIGN),
                       what=f'{site.project_id} allocate')
        elif step == 'arka':
            self._post('walk.design', 'design_arka_submit', pid,
                       {'capacity_kw': str(site.dc_capacity_kw),
                        'arka_link': f'https://example.invalid/arka/{site.project_id}',
                        'remarks': ''},
                       check=self._expect(site, m.DESIGN_ARKA_SUBMITTED),
                       what=f'{site.project_id} Arka submit')
        elif step == 'arka_qc':
            self._post('walk.designqc', 'design_arka_approve', pid,
                       check=self._expect(site, m.DESIGN_AWAITING_HEAD_ARKA),
                       what=f'{site.project_id} Arka gate 1')
        elif step == 'arka_head':
            self._post('walk.designhead', 'design_arka_head_approve', pid,
                       check=self._expect(site, m.DESIGN_ARKA_SUBMITTED),
                       what=f'{site.project_id} Arka gate 2')
        elif step == 'cad':
            self._post('walk.design', 'design_artifact_upload', pid,
                       {'kind': m.DESIGN_FILE_CAD_ZIP, 'artifact_file': self._cad_upload(),
                        'remarks': ''},
                       check=lambda: m.DesignFile.objects.filter(
                           attempt__assignment__project=site, is_current=True,
                           kind=m.DESIGN_FILE_CAD_ZIP).exists(),
                       what=f'{site.project_id} CAD upload')
        elif step == 'boq':
            masters = self._opex_masters()
            data = {'action': 'save_draft', 'item': [str(x.pk) for x in masters],
                    'keep_row': []}
            for position, master in enumerate(masters, start=1):
                data[f'qty_{master.pk}'] = str(position * 4 * scale)
            self._post('walk.design', 'opex_boq_entry', pid, data,
                       check=lambda: BOQ.objects.filter(project=site).exists(),
                       what=f'{site.project_id} BOQ entry')
            self._post('walk.design', 'design_boq_complete', pid, {'boq_remarks': ''},
                       check=self._expect(site, m.DESIGN_ARTIFACTS_UPLOADED),
                       what=f'{site.project_id} BOQ complete')
        elif step == 'qc_start':
            self._post('walk.designqc', 'design_qc_start', pid,
                       check=self._expect(site, m.DESIGN_IN_QC),
                       what=f'{site.project_id} QC start')
        elif step == 'qc_pass':
            self._post('walk.designqc', 'design_qc_pass', pid,
                       check=self._expect(site, m.DESIGN_AWAITING_HEAD_QC),
                       what=f'{site.project_id} QC pass')
        elif step == 'head_pass':
            self._post('walk.designhead', 'design_head_qc_pass', pid,
                       check=self._expect(site, m.DESIGN_AWAITING_PM_APPROVAL),
                       what=f'{site.project_id} Head pass')
        elif step == 'pm_approve':
            self._post('walk.pm', 'design_pm_approve', pid,
                       {'remark': 'Approved for procurement.'},
                       check=self._expect(site, m.DESIGN_RELEASED),
                       what=f'{site.project_id} PM approval')
        else:
            raise CommandError(f'unknown design step {step}')

    def _drive(self, site, upto, end_days_ago, steps=None, scale=1):
        """Walk the route from the start to `upto`, one simulated day per step, ending
        `end_days_ago` days before today."""
        steps = steps or DESIGN_ROUTE[:DESIGN_ROUTE.index(upto) + 1]
        start = end_days_ago + len(steps) - 1
        for offset, step in enumerate(steps):
            with self.clock.at(start - offset, history=site.project_id):
                self._d(site, step, scale=scale)

    def _released(self, program, code, released_days_ago, scale=1):
        site = self._site(program, code, released_days_ago + 14)
        self._drive(site, 'pm_approve', released_days_ago, scale=scale)
        site.refresh_from_db()
        return site

    # ================================================================= design
    def _area_design(self):
        """One site per design state a person can reach, on tender WALKDSN."""
        from projects import models as m
        program = self._program('design')
        s = {}

        def new(code, days):
            s[code] = self._site(program, code, days)
            return s[code]

        # D01 — no DesignAssignment at all: where every real walk starts.
        new('WALKD01', 20)
        # D02..D05, D09..D12 — the route, stopped at each step.
        for code, upto, end in [('WALKD02', 'survey', 3), ('WALKD03', 'allocate', 4),
                                ('WALKD04', 'arka', 5), ('WALKD05', 'arka_qc', 5),
                                ('WALKD09', 'boq', 6), ('WALKD10', 'qc_start', 4),
                                ('WALKD11', 'qc_pass', 3), ('WALKD12', 'head_pass', 2)]:
            self._drive(new(code, end + 14), upto, end)

        # D06 — Arka rejected at gate 1.
        site = new('WALKD06', 20)
        self._drive(site, 'arka', 6)
        with self.clock.at(5, history='WALKD06'):
            self._post('walk.designqc', 'design_arka_reject', {'project_id': 'WALKD06'},
                       {'rejection_reason': 'String sizing exceeds the inverter window.',
                        'error_category': m.ERR_ELECTRICAL_DESIGN},
                       check=self._expect(site, m.DESIGN_ARKA_REJECTED), what='WALKD06 reject')

        # D07 — on Design Hold (survey returned) from in_design.
        site = new('WALKD07', 20)
        self._drive(site, 'allocate', 6)
        with self.clock.at(4, history='WALKD07'):
            self._post('walk.design', 'design_mark_blocked', {'project_id': 'WALKD07'},
                       {'reason': 'Survey has no roof dimensions for the east block.'},
                       check=self._expect(site, m.DESIGN_SURVEY_RETURNED), what='WALKD07 hold')

        # D08 — held at arka_submitted, then lifted by a new survey link: restored (D18).
        site = new('WALKD08', 20)
        self._drive(site, 'arka', 8)
        with self.clock.at(7, history='WALKD08'):
            self._post('walk.design', 'design_mark_blocked', {'project_id': 'WALKD08'},
                       {'reason': 'Shadow survey missing for the water tank.'},
                       check=self._expect(site, m.DESIGN_SURVEY_RETURNED), what='WALKD08 hold')
        with self.clock.at(6, history='WALKD08'):
            self._post('walk.designhead', 'design_survey_link_set', {'project_id': 'WALKD08'},
                       {'survey_folder_url': 'https://drive.google.com/drive/folders/WALKD08-v2'},
                       check=self._expect(site, m.DESIGN_ARKA_SUBMITTED), what='WALKD08 restore')

        # D10 carries a NAMED QC reviewer instead of the open pool.
        # (Assigned before QC starts would be the real order; assignment is refused once
        # a gate-1 verdict exists, and D10's Arka gate 1 is already recorded — so the named
        # reviewer lives on D03, which has none yet.)
        with self.clock.at(3, history='WALKD03'):
            self._post('walk.designhead', 'design_assign_qc', {'project_id': 'WALKD03'},
                       {'qc_id': self.p['walk.designqc'].pk},
                       check=lambda: m.DesignAssignment.objects.filter(
                           project=s['WALKD03'], qc_assigned_to=self.p['walk.designqc']).exists(),
                       what='WALKD03 named QC reviewer')

        # D13 — PM rejected.
        site = new('WALKD13', 20)
        self._drive(site, 'head_pass', 4)
        with self.clock.at(3, history='WALKD13'):
            self._post('walk.pm', 'design_pm_reject', {'project_id': 'WALKD13'},
                       {'remark': 'Inverter make is not the one tendered.'},
                       check=self._expect(site, m.DESIGN_PM_REJECTED), what='WALKD13 PM reject')

        # D14 — PM rejected, then the Head sent it back to the designer: attempt 2.
        site = new('WALKD14', 24)
        self._drive(site, 'head_pass', 6)
        with self.clock.at(5, history='WALKD14'):
            self._post('walk.pm', 'design_pm_reject', {'project_id': 'WALKD14'},
                       {'remark': 'Layout blocks the fire path.'},
                       check=self._expect(site, m.DESIGN_PM_REJECTED), what='WALKD14 PM reject')
        with self.clock.at(4, history='WALKD14'):
            self._post('walk.designhead', 'design_head_send_back', {'project_id': 'WALKD14'},
                       {'error_category': m.ERR_LAYOUT,
                        'pm_rejection_remarks': 'Move the inverters clear of the fire path.',
                        'redo_scope_submitted': '1', 'redo': ['arka', 'cad', 'boq']},
                       check=self._expect(site, m.DESIGN_IN_DESIGN), what='WALKD14 send back')

        # D15 — failed QC once (BOQ redo), fixed, and released on attempt 2.
        site = new('WALKD15', 40)
        self._drive(site, 'qc_start', 20)
        with self.clock.at(19, history='WALKD15'):
            self._post('walk.designqc', 'design_qc_fail', {'project_id': 'WALKD15'},
                       {'qc_remarks': 'Cable schedule short by one run.',
                        'error_category': m.ERR_BOQ_QUANTITY,
                        'redo_scope_submitted': '1', 'redo': ['boq']},
                       check=lambda: m.DesignAssignment.objects.get(
                           project=site).current_attempt_number == 2,
                       what='WALKD15 QC fail')
        self._drive(site, None, 10, steps=['boq', 'qc_start', 'qc_pass', 'head_pass',
                                           'pm_approve'], scale=2)

        # D16 — released cleanly, and ACTIVATED, so its Design mirror task reads Done.
        site = new('WALKD16', 40)
        self._drive(site, 'pm_approve', 12)
        with self.clock.at(10, history='WALKD16'):
            self._post('walk.pm', 'opex_site_activate', {'project_id': 'WALKD16'},
                       check=lambda: m.Project.objects.get(project_id='WALKD16').status
                       == 'Active', what='WALKD16 activate')

    # ================================================================ changes
    def _area_changes(self):
        """Released sites, one per change-request verdict, on tender WALKCR."""
        from projects import models as m
        program = self._program('changes')
        s = {code: self._released(program, code, 12 + i)
             for i, code in enumerate(['WALKC01', 'WALKC02', 'WALKC03', 'WALKC04',
                                       'WALKC05', 'WALKC06', 'WALKC07', 'WALKC08'])}

        def cr(site):
            return (m.DesignChangeRequest.objects
                    .filter(attempt__assignment__project=site).order_by('-pk').first())

        def verdict(site, value):
            return lambda: (cr(site) is not None and cr(site).verdict == value)

        def raise_as(who, code, reason, expected, days):
            with self.clock.at(days, history=code):
                self._post(who, 'design_change_request', {'project_id': code},
                           {'reason': reason}, check=verdict(s[code], expected),
                           what=f'{code} raise')

        # C02 sits in a DRAFT procurement group first, so the acceptance visibly pulls it
        # out with the red "PM change request" reason. C08 sits in a LOCKED group, where
        # a change request is refused — the state is the demonstration.
        with self.clock.at(9, history='groups:WALKCR'):
            self._post('walk.scm', 'site_group_create', {'pk': program.pk},
                       {'name': 'WALK CR Batch - draft', 'project_ids': [s['WALKC02'].pk]},
                       check=lambda: m.SiteGroup.objects.filter(
                           name='WALK CR Batch - draft').exists(), what='CR draft group')
            self._post('walk.scm', 'site_group_create', {'pk': program.pk},
                       {'name': 'WALK CR Batch - locked', 'project_ids': [s['WALKC08'].pk]},
                       check=lambda: m.SiteGroup.objects.filter(
                           name='WALK CR Batch - locked').exists(), what='CR locked group')
            locked = m.SiteGroup.objects.get(name='WALK CR Batch - locked')
            self._post('walk.scm', 'site_group_lock', {'pk': locked.pk},
                       check=lambda: m.SiteGroup.objects.get(pk=locked.pk).status
                       == m.SITE_GROUP_LOCKED, what='CR group lock')

        raise_as('walk.pm', 'WALKC01', 'Client wants the array moved to the north shed.',
                 m.CHANGE_REQUEST_PENDING, 8)

        raise_as('walk.pm', 'WALKC02', 'DISCOM asked for a smaller inverter.',
                 m.CHANGE_REQUEST_PENDING, 8)
        with self.clock.at(7, history='WALKC02'):
            self._post('walk.designhead', 'design_change_request_accept',
                       {'pk': cr(s['WALKC02']).pk},
                       check=verdict(s['WALKC02'], m.CHANGE_REQUEST_ACCEPTED),
                       what='WALKC02 accept')

        raise_as('walk.pm', 'WALKC03', 'Customer asked for a different module brand.',
                 m.CHANGE_REQUEST_PENDING, 8)
        with self.clock.at(7, history='WALKC03'):
            self._post('walk.designhead', 'design_change_request_reject',
                       {'pk': cr(s['WALKC03']).pk},
                       {'rejection_reason': 'The tendered make stands.'},
                       check=verdict(s['WALKC03'], m.CHANGE_REQUEST_REJECTED),
                       what='WALKC03 reject')

        raise_as('walk.scm', 'WALKC04', 'Module count looks short for the roof area.',
                 m.CHANGE_REQUEST_WITH_PM, 6)

        raise_as('walk.scm', 'WALKC05', 'Cable run seems long.', m.CHANGE_REQUEST_WITH_PM, 8)
        with self.clock.at(7, history='WALKC05'):
            self._post('walk.pm', 'design_change_request_pm_reject',
                       {'pk': cr(s['WALKC05']).pk},
                       {'pm_note': 'Not needed - the run follows the parapet.'},
                       check=verdict(s['WALKC05'], m.CHANGE_REQUEST_PM_REJECTED),
                       what='WALKC05 PM reject')

        raise_as('walk.scm', 'WALKC06', 'Raised against the wrong site.',
                 m.CHANGE_REQUEST_WITH_PM, 8)
        with self.clock.at(7, history='WALKC06'):
            self._post('walk.scm', 'design_change_request_withdraw',
                       {'pk': cr(s['WALKC06']).pk},
                       {'withdrawal_note': 'Raised in error - withdrawing.'},
                       check=verdict(s['WALKC06'], m.CHANGE_REQUEST_WITHDRAWN),
                       what='WALKC06 withdraw')

        raise_as('walk.scm', 'WALKC07', 'Earthing pits look one short.',
                 m.CHANGE_REQUEST_WITH_PM, 9)
        with self.clock.at(8, history='WALKC07'):
            self._post('walk.pm', 'design_change_request_forward',
                       {'pk': cr(s['WALKC07']).pk},
                       {'pm_note': 'Quantities need the Head - forwarding.'},
                       check=verdict(s['WALKC07'], m.CHANGE_REQUEST_PENDING),
                       what='WALKC07 forward')
        with self.clock.at(7, history='WALKC07'):
            item = m.BOQItem.objects.filter(boq__project=s['WALKC07']).order_by('pk').first()
            before = m.BOQCorrection.objects.count()
            self._post('walk.designqc', 'boq_correct', {'project_id': 'WALKC07'},
                       {'action': 'set_quantity', 'item_id': item.pk,
                        'quantity': str(item.boq_quantity + 2)},
                       check=lambda: m.BOQCorrection.objects.count() == before + 1,
                       what='WALKC07 BOQ correction')
        with self.clock.at(6, history='WALKC07'):
            self._post('walk.designhead', 'design_change_request_correct',
                       {'pk': cr(s['WALKC07']).pk},
                       {'correction_note': 'Added the two missing earthing pits.'},
                       check=verdict(s['WALKC07'], m.CHANGE_REQUEST_CORRECTED),
                       what='WALKC07 corrected')

    # ============================================================ procurement
    def _area_procurement(self):
        """The post-QC pool, a draft and two locked groups, orders, and a payment in
        every status, on tender WALKPRC."""
        from projects import models as m
        program = self._program('procurement')
        ages = {'WALKP01': 30, 'WALKP02': 26, 'WALKP03': 21, 'WALKP04': 17,
                'WALKP05': 11, 'WALKP06': 6, 'WALKP07': 3}
        s = {code: self._released(program, code, age, scale=1 + i % 3)
             for i, (code, age) in enumerate(ages.items())}

        def group(name, codes, lock, days):
            with self.clock.at(days, history=f'group:{name}'):
                self._post('walk.scm', 'site_group_create', {'pk': program.pk},
                           {'name': name, 'project_ids': [s[c].pk for c in codes],
                            'notes': 'Walkthrough batch.'},
                           check=lambda: m.SiteGroup.objects.filter(name=name).exists(),
                           what=f'group {name}')
                g = m.SiteGroup.objects.get(name=name)
                if lock:
                    self._post('walk.scm', 'site_group_lock', {'pk': g.pk},
                               check=lambda: m.SiteGroup.objects.get(pk=g.pk).status
                               == m.SITE_GROUP_LOCKED, what=f'lock {name}')
            return g

        locked_a = group('WALK Batch A - Modules & Structure', ['WALKP01', 'WALKP02'], True, 20)
        group('WALK Batch B - BOS & Cabling', ['WALKP03'], False, 9)
        # WALKP04..P07 stay in the pool, released 17 to 3 days ago.

        # A site moved out of the draft group by SCM, with its reason: add, then remove.
        with self.clock.at(8, history='group:WALK Batch B - BOS & Cabling'):
            draft = m.SiteGroup.objects.get(name='WALK Batch B - BOS & Cabling')
            self._post('walk.scm', 'site_group_add_sites', {'pk': draft.pk},
                       {'project_ids': [s['WALKP04'].pk]},
                       check=lambda: draft.memberships.filter(project=s['WALKP04'],
                                                              removed_at__isnull=True).exists(),
                       what='add P04 to Batch B')
            membership = draft.memberships.get(project=s['WALKP04'], removed_at__isnull=True)
            self._post('walk.scm', 'site_group_remove_site', {'pk': draft.pk},
                       {'membership_id': membership.pk,
                        'reason': 'Lead time on cable drums - moved to the next batch'},
                       check=lambda: m.SiteGroupMembership.objects.get(
                           pk=membership.pk).removed_at is not None,
                       what='remove P04 from Batch B')

        vendors = {v.name: v for v in m.Vendor.objects.filter(name__startswith='WALK ')}
        modules_vendor = vendors['WALK Modules & Structure Supplies']
        cable_vendor = vendors['WALK Cable & BOS Supplies']

        def payments(order):
            return list(m.PaymentRequest.objects.filter(vendor_order=order).order_by('pk'))

        def status_is(pk, value):
            return lambda: m.PaymentRequest.objects.get(pk=pk).status == value

        # --- Order 1: the group raise against locked Batch A -----------------------
        with self.clock.at(18, history='order:group'):
            before = m.VendorOrder.objects.count()
            data = {'client_uuid': str(uuid.uuid4()), 'vendor_id': str(modules_vendor.pk),
                    'po_number': 'WALK-PO-0001', 'order_total': '1000000',
                    'payment_amount': '200000', 'payment_note': 'Advance',
                    'site': [str(s['WALKP01'].pk), str(s['WALKP02'].pk)],
                    'program': [str(program.pk)],
                    'doc_type_0': m.VENDOR_ORDER_DOC_PO, 'doc_file_0': self._pdf('po')}
            for code in ('WALKP01', 'WALKP02'):
                data[f'via_group_{s[code].pk}'] = str(locked_a.pk)
            self._post('walk.scm', 'vendor_order_create_group', data=data,
                       check=lambda: m.VendorOrder.objects.count() == before + 1,
                       what='group order')
            order1 = m.VendorOrder.objects.order_by('-pk').first()
        pay1 = payments(order1)[0]
        with self.clock.at(16, history='order:group'):
            self._post('walk.finance', 'payment_approve', {'payment_pk': pay1.pk},
                       {'approved_amount': '200000', 'remark': ''},
                       check=status_is(pay1.pk, m.PaymentRequest.APPROVED), what='approve 1')
        with self.clock.at(14, history='order:group'):
            today = self._today()
            self._post('walk.finpay', 'payment_mark_paid', {'payment_pk': pay1.pk},
                       {'payment_date': today, 'payment_reference': 'UTR-WALK-0001'},
                       check=status_is(pay1.pk, m.PaymentRequest.CONFIRMED), what='mark paid 1')
        with self.clock.at(12, history='order:group'):
            self._post('walk.scm', 'vendor_order_add_payment', {'order_pk': order1.pk},
                       {'client_uuid': str(uuid.uuid4()), 'payment_amount': '150000',
                        'payment_note': 'Second tranche'},
                       check=lambda: len(payments(order1)) == 2, what='payment 2')
            pay2 = payments(order1)[1]
        with self.clock.at(11, history='order:group'):
            self._post('walk.finance', 'payment_approve', {'payment_pk': pay2.pk},
                       {'approved_amount': '100000',
                        'remark': 'Only 8 of 12 pallets delivered so far.'},
                       check=status_is(pay2.pk, m.PaymentRequest.APPROVED),
                       what='partial approve 2')
        with self.clock.at(10, history='order:group'):
            self._post('walk.scm', 'vendor_order_add_payment', {'order_pk': order1.pk},
                       {'client_uuid': str(uuid.uuid4()), 'payment_amount': '100000',
                        'payment_note': 'Freight'},
                       check=lambda: len(payments(order1)) == 3, what='payment 3')
            pay3 = payments(order1)[2]
        with self.clock.at(9, history='order:group'):
            self._post('walk.finance', 'payment_hold', {'payment_pk': pay3.pk},
                       {'reason': 'Freight is included in the PO price.'},
                       check=status_is(pay3.pk, m.PaymentRequest.ON_HOLD), what='hold 3')
        with self.clock.at(8, history='order:group'):
            self._post('walk.finance', 'payment_reject', {'payment_pk': pay3.pk},
                       {'reason': 'Duplicate of the PO freight line.'},
                       check=status_is(pay3.pk, m.PaymentRequest.REJECTED), what='reject 3')
        with self.clock.at(7, history='order:group'):
            self._post('walk.scm', 'vendor_order_add_documents', {'order_pk': order1.pk},
                       {'doc_type_0': m.VENDOR_ORDER_DOC_INVOICE,
                        'doc_file_0': self._pdf('invoice'),
                        'doc_invoice_number_0': 'WALK-INV-0001',
                        'doc_invoice_amount_0': '200000'},
                       check=lambda: order1.documents.count() == 2,
                       what='add invoice to order 1')

        # --- Order 2: a purchases-workspace record, no site ------------------------
        with self.clock.at(15, history='order:purchases'):
            before = m.VendorOrder.objects.count()
            self._post('walk.scm', 'purchases_new', data={
                'client_uuid': str(uuid.uuid4()), 'vendor_id': str(cable_vendor.pk),
                'pi_number': 'WALK-PI-0002', 'order_total': '500000', 'site': [],
                'program': [], 'project_type': 'OPEX',
                'request_payment': '1', 'payment_amount': '100000',
                'payment_note': 'Advance against PI',
                'doc_type_0': m.VENDOR_ORDER_DOC_PI, 'doc_file_0': self._pdf('pi'),
            }, check=lambda: m.VendorOrder.objects.count() == before + 1,
                what='purchases record')
            order2 = m.VendorOrder.objects.order_by('-pk').first()
        # payment 1 stays pending_approval.
        for days, amount, note in ((13, '50000', 'Cable drums'), (12, '80000', 'Conduit'),
                                   (11, '30000', 'Lugs and glands')):
            with self.clock.at(days, history='order:purchases'):
                self._post('walk.scm', 'purchases_pay', data={
                    'order': str(order2.pk), 'client_uuid': str(uuid.uuid4()),
                    'payment_amount': amount, 'payment_note': note,
                }, check=lambda n=note: m.PaymentRequest.objects.filter(
                    vendor_order=order2, note=n).exists(), what=f'purchases pay {note}')
        p = payments(order2)
        with self.clock.at(10, history='order:purchases'):
            self._post('walk.finance', 'payment_hold', {'payment_pk': p[1].pk},
                       {'reason': 'Waiting for the GRN.'},
                       check=status_is(p[1].pk, m.PaymentRequest.ON_HOLD), what='hold (stays)')
            self._post('walk.finance', 'payment_approve', {'payment_pk': p[2].pk},
                       {'approved_amount': '80000', 'remark': ''},
                       check=status_is(p[2].pk, m.PaymentRequest.APPROVED), what='approve (stays)')
            self._post('walk.finance', 'payment_hold', {'payment_pk': p[3].pk},
                       {'reason': 'Which site is this for?'},
                       check=status_is(p[3].pk, m.PaymentRequest.ON_HOLD), what='hold (answered)')
        with self.clock.at(9, history='order:purchases'):
            self._post('walk.scm', 'payment_hold_respond', {'payment_pk': p[3].pk},
                       {'response': 'Common stock for the Batch A sites.'},
                       check=status_is(p[3].pk, m.PaymentRequest.PENDING_APPROVAL),
                       what='answer the hold')

    def _today(self):
        from django.utils import timezone
        return timezone.localdate().isoformat()

    # =============================================================== delivery
    def _activated(self, program, code, days_ago):
        from projects import models as m
        site = self._site(program, code, days_ago + 2)
        with self.clock.at(days_ago, history=code):
            self._post('walk.pm', 'opex_site_activate', {'project_id': code},
                       check=lambda: m.Project.objects.get(project_id=code).status == 'Active',
                       what=f'{code} activate')
        site.refresh_from_db()
        return site

    def _assign(self, site, task, who, days_ago):
        from projects.models import Task
        with self.clock.at(days_ago, history=site.project_id):
            self._post('walk.pm', 'task_assign',
                       {'project_id': site.project_id, 'task_id': task.pk},
                       {'assigned_to': self.p[who].pk},
                       check=lambda: Task.objects.get(pk=task.pk).assigned_to_id
                       == self.p[who].pk, what=f'{site.project_id} assign {task.task_name}')

    def _area_delivery(self):
        """Challans in every status, GRN by the site engineer and on-behalf by SCM, a
        delivery issue — on tender WALKDLV."""
        from projects import models as m
        program = self._program('delivery')
        site = self._activated(program, 'WALKL01', 12)
        for task in m.Task.objects.filter(phase__project=site, assigned_role='Site Engineer',
                                          is_mirror=False).order_by('pk')[:2]:
            self._assign(site, task, 'walk.se', 11)

        vendor = m.Vendor.objects.get(name='WALK Modules & Structure Supplies')
        warehouse = m.StockLocation.objects.get(code='WALK-WH-DEL')
        plan = [
            ('WALK-DC-0001', [('Solar Modules', 'Module 550Wp', 40, 'Nos')]),
            ('WALK-DC-0002', [('Structure', 'MMS set', 12, 'Set'),
                              ('BOS', 'DC cable 4 sqmm', 500, 'Mtr')]),
            ('WALK-DC-0003', [('Inverter', 'String inverter 50 kW', 2, 'Nos')]),
            ('WALK-DC-0004', [('BOS', 'ACDB panel', 1, 'Nos')]),
            ('WALK-DC-0005', [('Solar Modules', 'Module 550Wp (lot 2)', 20, 'Nos')]),
        ]
        dcs = {}
        for index, (number, lines) in enumerate(plan):
            with self.clock.at(9 - index, history=f'dc:{number}'):
                data = {'dc_number': number, 'dc_date': self._today(),
                        'expected_delivery_date': self._today(),
                        'vendor_id': str(vendor.pk), 'po_number': 'WALK-PO-0001',
                        'notes': 'Walkthrough challan.',
                        'issued_from_warehouse': str(warehouse.pk) if index == 0 else ''}
                for n, (cat, desc, qty, unit) in enumerate(lines):
                    data.update({f'line_item_category_{n}': cat,
                                 f'line_item_description_{n}': desc,
                                 f'line_item_qty_{n}': str(qty), f'line_item_unit_{n}': unit})
                self._post('walk.scm', 'create_delivery_challan', {'project_id': 'WALKL01'},
                           data, check=lambda n=number: m.DeliveryChallan.objects.filter(
                               dc_number=n).exists(), what=f'challan {number}')
                dcs[number] = m.DeliveryChallan.objects.get(dc_number=number)

        def grn(who, number, fn, extra=None, name='confirm_grn', expected=None):
            dc = dcs[number]
            data = dict(extra or {})
            for item in dc.line_items.order_by('pk'):
                received, damaged = fn(item)
                data[f'received_qty_{item.pk}'] = str(received)
                data[f'damaged_qty_{item.pk}'] = str(damaged)
                data[f'grn_notes_{item.pk}'] = 'Checked at site.'
            # GRN dates are written with date.today(), which no clock can move — so the
            # receipt is driven at today's date and nothing contradicts it.
            with self.clock.at(0, history=f'dc:{number}', hours=-0.5):
                self._post(who, name, {'project_id': 'WALKL01', 'dc_id': dc.pk}, data,
                           check=lambda: m.DeliveryChallan.objects.get(pk=dc.pk).status
                           == expected, what=f'GRN {number}')

        # 0001 stays Expected. 0002 partial, 0003 received, 0004 rejected (nothing came).
        grn('walk.se', 'WALK-DC-0002', lambda i: (int(i.ordered_quantity) // 2, 0),
            expected=m.DeliveryChallan.PARTIALLY_RECEIVED)
        grn('walk.se', 'WALK-DC-0003', lambda i: (int(i.ordered_quantity), 0),
            expected=m.DeliveryChallan.RECEIVED)
        grn('walk.se', 'WALK-DC-0004', lambda i: (0, 0),
            expected=m.DeliveryChallan.REJECTED)
        # 0005 — the site engineer was unreachable; SCM records it on their behalf.
        grn('walk.scm', 'WALK-DC-0005', lambda i: (int(i.ordered_quantity), 0),
            extra={'grn_on_behalf_reason': 'Site engineer unreachable; verified over call.'},
            name='override_grn', expected=m.DeliveryChallan.RECEIVED)

        with self.clock.at(0, history='dc:WALK-DC-0002', hours=-0.2):
            self._post('walk.pm', 'create_delivery_issue',
                       {'project_id': 'WALKL01', 'dc_id': dcs['WALK-DC-0002'].pk},
                       {'title': 'Short delivery on MMS sets', 'severity': 'High',
                        'description': 'Half the sets arrived; balance promised next week.',
                        'assigned_to': self.p['walk.scm'].pk},
                       check=lambda: m.Issue.objects.filter(
                           delivery_challan=dcs['WALK-DC-0002']).exists(),
                       what='delivery issue')

    # ============================================================== execution
    def _area_execution(self):
        """An activated OPEX site with tasks in every state, two-step completion, punch
        points, Not Applicable, rename/reorder/duplicate, checklist answers, and the
        issue lifecycle — on tender WALKEXE."""
        from projects import models as m
        program = self._program('execution')
        site = self._activated(program, 'WALKE01', 30)
        pid = site.project_id
        T = m.Task

        with self.clock.at(29, history=pid):
            self._post('walk.pm', 'assign_coordinators', {'project_id': pid},
                       {'coordinator_ids': [self.p['walk.coord'].pk]},
                       check=lambda: site.coordinators.filter(pk=self.p['walk.coord'].pk).exists(),
                       what='coordinator')

        def task(name):
            return T.objects.get(phase__project=site, task_name=name)

        se_tasks = list(T.objects.filter(phase__project=site, assigned_role='Site Engineer',
                                         is_mirror=False).order_by('phase__phase_order',
                                                                   'task_order', 'pk'))
        for t in se_tasks:
            who = 'walk.qaqc' if t.task_name == 'Net Meter Installation' else 'walk.se'
            self._assign(site, t, who, 28)
        self._assign(site, task('Completion Certificates (Paperwork)'), 'walk.coord', 28)

        tp = {'project_id': pid}

        def kw(t):
            return {'project_id': pid, 'task_id': t.pk}

        def start(t, who, days):
            with self.clock.at(days, history=f'task:{t.pk}'):
                due = self._plus_days(10)
                self._post(who, 'task_status_update', kw(t),
                           {'status': 'In Progress', 'due_date': due},
                           check=lambda: T.objects.get(pk=t.pk).status == T.IN_PROGRESS,
                           what=f'start {t.task_name}')

        def submit(t, who, days, remarks='Work complete, photos in the site folder.'):
            with self.clock.at(days, history=f'task:{t.pk}'):
                self._post(who, 'task_submit_for_approval', kw(t),
                           {'submission_remarks': remarks},
                           check=lambda: T.objects.get(pk=t.pk).submitted_at is not None,
                           what=f'submit {t.task_name}')

        def answer(t, who, days, answers):
            # The product's own resolver, so the items answered are the ones the task
            # detail page shows.
            from projects.views import _checklist_for_task
            checklist = _checklist_for_task(t, site)
            if checklist is None:
                raise CommandError(f'{t.task_name} has no active checklist — was '
                                   f'seed_opex_installation_checklists run?')
            checklist_items = list(checklist.items.order_by('order', 'pk')[:len(answers)])
            for item, (ans, remarks) in zip(checklist_items, answers):
                with self.clock.at(days, history=f'task:{t.pk}'):
                    self._post(who, 'checklist_item_complete',
                               {'project_id': pid, 'task_id': t.pk, 'item_id': item.pk},
                               {'answer': ans, 'remarks': remarks,
                                'witness_names': 'Walk Witness (client)'},
                               check=lambda i=item: m.ChecklistItemCompletion.objects.filter(
                                   task=t, item=i, is_checked=True).exists(),
                               what=f'checklist {t.task_name}')

        # Civil Work: checklist answered, submitted by the SE, approved by QA/QC -> Done.
        civil = task('Civil Work and MMS Installation')
        start(civil, 'walk.se', 25)
        answer(civil, 'walk.se', 22, [('yes', ''), ('yes', ''), ('na', ''),
                                      ('no', 'Two pedestals re-cast; retest passed.')])
        submit(civil, 'walk.se', 20)
        with self.clock.at(19, history=f'task:{civil.pk}'):
            self._post('walk.qaqc', 'task_approve', kw(civil),
                       {'approval_remarks': 'Pedestal levels verified on site.'},
                       check=lambda: T.objects.get(pk=civil.pk).status == T.DONE,
                       what='approve civil')

        # Module Installation: submitted, awaiting approval.
        module = task('Module Installation')
        start(module, 'walk.se', 18)
        answer(module, 'walk.se', 16, [('yes', ''), ('yes', '')])
        submit(module, 'walk.se', 5)

        # LA and Earthing: rejected -> open punch point, back in progress.
        la = task('LA and Earthing Installation')
        start(la, 'walk.se', 17)
        submit(la, 'walk.se', 9)
        with self.clock.at(8, history=f'task:{la.pk}'):
            self._post('walk.qaqc', 'task_reject', kw(la),
                       {'approval_remarks': 'Earth pit 3 resistance above 5 ohm.'},
                       check=lambda: m.PunchPoint.objects.filter(task=la).exists(),
                       what='reject LA')

        # DC Cable: rejected, then the PM waived the punch point.
        dc = task('DC Cable Laying with Conduit')
        start(dc, 'walk.se', 16)
        submit(dc, 'walk.se', 10)
        with self.clock.at(9, history=f'task:{dc.pk}'):
            self._post('walk.qaqc', 'task_reject', kw(dc),
                       {'approval_remarks': 'Conduit saddles missing on the east run.'},
                       check=lambda: m.PunchPoint.objects.filter(task=dc).exists(),
                       what='reject DC cable')
        punch = m.PunchPoint.objects.get(task=dc)
        with self.clock.at(7, history=f'task:{dc.pk}'):
            self._post('walk.pm', 'punch_point_waive',
                       {'project_id': pid, 'punch_point_id': punch.pk},
                       {'waiver_reason': 'Client accepted cable ties; signed off.'},
                       check=lambda: m.PunchPoint.objects.get(pk=punch.pk).status
                       == m.PunchPoint.WAIVED,
                       what='waive punch point')

        # DCDB/ACDB: in progress, checklist partly answered.
        dcdb = task('DCDB and ACDB Installation')
        start(dcdb, 'walk.se', 12)
        answer(dcdb, 'walk.se', 11, [('yes', ''), ('no', 'Gland plate hole oversized.'),
                                     ('na', '')])

        # Inverter Installation: blocked, with its issue.
        inverter = task('Inverter Installation')
        start(inverter, 'walk.se', 14)
        with self.clock.at(6, history=f'task:{inverter.pk}'):
            self._post('walk.se', 'task_status_update', kw(inverter),
                       {'status': 'Blocked', 'block_issue_title': 'Inverter plinth not cured',
                        'block_issue_description': 'Civil team to re-pour.',
                        'block_issue_severity': 'High'},
                       check=lambda: T.objects.get(pk=inverter.pk).status == T.BLOCKED,
                       what='block inverter')

        # Net Metering Approval (PM): submitted by the PM, approved by the coordinator.
        net = task('Net Metering Approval')
        start(net, 'walk.pm', 26)
        submit(net, 'walk.pm', 21, remarks='DISCOM approval letter received.')
        with self.clock.at(20, history=f'task:{net.pk}'):
            self._post('walk.coord', 'task_approve', kw(net),
                       {'approval_remarks': 'Letter checked against the application.'},
                       check=lambda: T.objects.get(pk=net.pk).status == T.DONE,
                       what='approve net metering')

        # RMS Installation: Not Applicable (the PM only).
        rms = task('RMS Installation')
        with self.clock.at(15, history=f'task:{rms.pk}'):
            self._post('walk.pm', 'task_set_not_applicable', kw(rms),
                       {'not_applicable': '1',
                        'not_applicable_reason': 'Client supplies their own SCADA.'},
                       check=lambda: T.objects.get(pk=rms.pk).is_not_applicable,
                       what='RMS not applicable')

        # Testing & Commissioning: due date set by the PM, not started.
        testing = task('Testing & Commissioning')
        with self.clock.at(15, history=f'task:{testing.pk}'):
            self._post('walk.pm', 'task_set_due_date', kw(testing),
                       {'due_date': self._plus_days(20)},
                       check=lambda: T.objects.get(pk=testing.pk).due_date is not None,
                       what='due date on testing')

        # Solar Generation Meter Installation: renamed by the PM.
        meter = task('Solar Generation Meter Installation')
        with self.clock.at(14, history=f'task:{meter.pk}'):
            self._post('walk.pm', 'task_rename_save', kw(meter),
                       {'value': 'Solar Generation Meter Installation (CT-operated)'},
                       hx=True, check=lambda: T.objects.get(pk=meter.pk).task_name
                       == 'Solar Generation Meter Installation (CT-operated)',
                       what='rename meter task')

        # AC Cable Laying: duplicated for two locations.
        ac = task('AC Cable Laying')
        with self.clock.at(13, history=f'task:{ac.pk}'):
            before = T.objects.filter(phase__project=site).count()
            self._post('walk.pm', 'task_duplicate_locations_create', kw(ac),
                       {'existing_locations': [], 'new_locations': 'Block A\nBlock B',
                        'assigned_to': self.p['walk.se'].pk},
                       check=lambda: T.objects.filter(phase__project=site).count() == before + 2,
                       what='duplicate AC cable')

        # Reorder the Installation phase: the last task moves to the top.
        phase = civil.phase
        with self.clock.at(12, history=f'phase:{phase.pk}'):
            order = list(T.objects.filter(phase=phase).order_by('task_order', 'pk')
                         .values_list('pk', flat=True))
            order = [order[-1]] + order[:-1]
            self._post('walk.pm', 'phase_tasks_reorder',
                       {'project_id': pid, 'phase_id': phase.pk},
                       {'order': ','.join(str(x) for x in order)}, hx=True,
                       check=lambda: list(T.objects.filter(phase=phase)
                                          .order_by('task_order', 'pk')
                                          .values_list('pk', flat=True)) == order,
                       what='reorder installation')

        # A task the template does not carry, added by the PM.
        closeout = task('Completion Certificates (Paperwork)').phase
        with self.clock.at(11, history=f'phase:{closeout.pk}'):
            self._post('walk.pm', 'task_add', tp, {
                'phase': closeout.pk, 'task_name': 'WALK Site cleaning and debris removal',
                'assigned_role': 'PM', 'assigned_to': self.p['walk.pm'].pk,
                'due_date': self._plus_days(12),
            }, hx=True, check=lambda: T.objects.filter(
                phase=closeout, task_name='WALK Site cleaning and debris removal').exists(),
                what='add task')

        # Issues: one in each state, plus one reopened.
        def issue(title, days):
            with self.clock.at(days, history=f'issue:{title}'):
                self._post('walk.pm', 'create_project_issue', tp,
                           {'title': title, 'description': 'Walkthrough issue.',
                            'severity': 'Medium', 'assigned_to': self.p['walk.se'].pk},
                           check=lambda: m.Issue.objects.filter(project=site, title=title).exists(),
                           what=f'issue {title}')
            return m.Issue.objects.get(project=site, title=title)

        def move(i, who, name, days, data=None, expected=None):
            with self.clock.at(days, history=f'issue:{i.title}'):
                self._post(who, name, {'issue_id': i.pk}, data or {},
                           check=lambda: m.Issue.objects.get(pk=i.pk).status == expected,
                           what=f'{name} {i.title}')

        issue('WALK Scaffolding permit pending', 10)
        i2 = issue('WALK Water supply for curing', 12)
        move(i2, 'walk.se', 'update_issue_status', 11, expected=m.Issue.IN_PROGRESS)
        i3 = issue('WALK Crane access road', 14)
        move(i3, 'walk.se', 'update_issue_status', 13, expected=m.Issue.IN_PROGRESS)
        move(i3, 'walk.se', 'resolve_issue', 12, {'resolution_note': 'Road levelled.'},
             expected=m.Issue.RESOLVED)
        i4 = issue('WALK Material theft report', 18)
        move(i4, 'walk.se', 'update_issue_status', 17, expected=m.Issue.IN_PROGRESS)
        move(i4, 'walk.se', 'resolve_issue', 16, {'resolution_note': 'FIR filed; replaced.'},
             expected=m.Issue.RESOLVED)
        move(i4, 'walk.pm', 'close_issue', 15, expected=m.Issue.CLOSED)
        i5 = issue('WALK Earthing strip corrosion', 16)
        move(i5, 'walk.se', 'update_issue_status', 15, expected=m.Issue.IN_PROGRESS)
        move(i5, 'walk.se', 'resolve_issue', 14, {'resolution_note': 'Strips painted.'},
             expected=m.Issue.RESOLVED)
        move(i5, 'walk.pm', 'reopen_issue', 13, expected=m.Issue.OPEN)

    def _plus_days(self, days):
        from datetime import timedelta
        from django.utils import timezone
        return (timezone.localdate() + timedelta(days=days)).isoformat()

    # ============================================================ residential
    def _area_residential(self):
        """Four Residential projects: Draft; and Active with the BOQ Submitted,
        Acknowledged and Revision Requested; milestones Pending/Invoiced/Received; a
        Residential vendor order; some tasks moved."""
        from projects import models as m

        def create(name, days):
            with self.clock.at(days, history=f'res:{name}'):
                self._post('walk.pm', 'project_create', data={
                    'customer_name': name, 'customer_phone': '9300000000',
                    'customer_email': f'{name.split()[-1].lower()}@{WALK_EMAIL_DOMAIN}',
                    'site_address': 'Synthetic residential address', 'city': 'Pune',
                    'state': 'Maharashtra', 'project_type': 'Residential',
                    'dc_capacity_kw': '8.50', 'contract_value': '450000.00',
                    'target_commissioning_date': self._plus_days(60),
                }, check=lambda: m.Project.objects.filter(customer_name=name).exists(),
                    what=f'create {name}')
            return m.Project.objects.get(customer_name=name)

        def activate(p, days):
            with self.clock.at(days, history=f'res:{p.customer_name}'):
                self._post('walk.pm', 'project_activate', {'project_id': p.project_id},
                           {'assigned_design_id': self.p['walk.design'].pk},
                           check=lambda: m.Project.objects.get(pk=p.pk).status == 'Active',
                           what=f'activate {p.customer_name}')

        def author_boq(p, days):
            with self.clock.at(days, history=f'res:{p.customer_name}'):
                self._get('walk.design', 'boq_detail', {'project_id': p.project_id})
                items = list(m.BOQItem.objects.filter(boq__project=p).order_by('pk')[:5])
                data = {'action': 'submit_design', 'notes': 'First pass.'}
                for n, item in enumerate(items, start=1):
                    data[f'boq_qty_{item.pk}'] = str(n * 2)
                self._post('walk.design', 'boq_detail', {'project_id': p.project_id}, data,
                           check=lambda: m.BOQ.objects.get(project=p).status == 'Submitted',
                           what=f'BOQ submit {p.customer_name}')

        create('WALK Residential Draft Sharma', 5)

        acked = create('WALK Residential Active Iyer', 40)
        activate(acked, 38)
        author_boq(acked, 35)
        with self.clock.at(33, history=f'res:{acked.customer_name}'):
            self._post('walk.scm', 'boq_acknowledge', {'project_id': acked.project_id},
                       check=lambda: m.BOQ.objects.get(project=acked).status == 'Acknowledged',
                       what='BOQ acknowledge')

        revised = create('WALK Residential Revision Khan', 30)
        activate(revised, 28)
        author_boq(revised, 25)
        with self.clock.at(22, history=f'res:{revised.customer_name}'):
            self._post('walk.pm', 'boq_request_revision', {'project_id': revised.project_id},
                       {'reason': 'Module wattage does not match the survey.'},
                       check=lambda: m.BOQ.objects.get(project=revised).status
                       == 'Revision Requested', what='BOQ revision')

        submitted = create('WALK Residential Submitted Das', 20)
        activate(submitted, 18)
        author_boq(submitted, 15)

        # Milestones on the acknowledged project: amounts, M1 received, M2 invoiced.
        with self.clock.at(37, history=f'res:{acked.customer_name}'):
            self._post('walk.pm', 'set_milestone_amounts', {'project_id': acked.project_id},
                       json='{"m1_amount": 150000, "m2_amount": 150000, "m3_amount": 150000}',
                       check=lambda: m.PaymentMilestone.objects.filter(
                           project=acked, amount__isnull=False).count() == 3,
                       what='milestone amounts')
        # milestone_invoice / milestone_receive write invoice_date and received_date with
        # date.today(), which no clock can move — so both are driven TODAY, minutes
        # apart, and a milestone's dates never disagree with its own ledger rows.
        ms = {x.milestone_name: x for x in m.PaymentMilestone.objects.filter(project=acked)}
        for name, invoice_at, receive_at in (('M1', -0.5, -0.4), ('M2', -0.3, None)):
            with self.clock.at(0, history=f'ms:{acked.pk}:{name}', hours=invoice_at):
                self._post('walk.finpay', 'milestone_invoice',
                           {'project_id': acked.project_id, 'milestone_pk': ms[name].pk},
                           check=lambda n=name: m.PaymentMilestone.objects.get(
                               pk=ms[n].pk).status == 'Invoiced', what=f'invoice {name}')
            if receive_at:
                with self.clock.at(0, history=f'ms:{acked.pk}:{name}', hours=receive_at):
                    self._post('walk.finpay', 'milestone_receive',
                               {'project_id': acked.project_id, 'milestone_pk': ms[name].pk},
                               {'amount_received': '150000'},
                               check=lambda n=name: m.PaymentMilestone.objects.get(
                                   pk=ms[n].pk).status == 'Received', what=f'receive {name}')

        # A Residential vendor order against the acknowledged BOQ, first payment pending.
        items = [i for i in m.BOQItem.objects.filter(boq__project=acked).order_by('pk')
                 if i.boq_quantity and i.boq_quantity > 0][:2]
        vendor = m.Vendor.objects.get(name='WALK Modules & Structure Supplies')
        with self.clock.at(30, history=f'res:{acked.customer_name}'):
            before = m.VendorOrder.objects.count()
            data = {'client_uuid': str(uuid.uuid4()), 'vendor_id': str(vendor.pk),
                    'po_number': 'WALK-PO-RES-0001', 'line': [str(i.pk) for i in items],
                    'doc_type_0': m.VENDOR_ORDER_DOC_PO, 'doc_file_0': self._pdf('po'),
                    'request_payment': '1', 'payment_amount': '40000',
                    'payment_note': 'Advance'}
            for i in items:
                data[f'qty_{i.pk}'] = str(int(i.boq_quantity))
                data[f'amount_{i.pk}'] = '60000'
            self._post('walk.scm', 'vendor_order_create', {'project_pk': acked.pk}, data,
                       check=lambda: m.VendorOrder.objects.count() == before + 1,
                       what='Residential order')

        # Tasks: the PM's first two tasks done, the third in progress.
        pm_tasks = list(m.Task.objects.filter(phase__project=acked, assigned_role='PM')
                        .order_by('phase__phase_order', 'task_order', 'pk')[:3])
        for n, t in enumerate(pm_tasks):
            with self.clock.at(36 - n, history=f'task:{t.pk}'):
                self._post('walk.pm', 'task_status_update',
                           {'project_id': acked.project_id, 'task_id': t.pk},
                           {'status': 'In Progress', 'due_date': self._plus_days(5)},
                           check=lambda t=t: m.Task.objects.get(pk=t.pk).status
                           == m.Task.IN_PROGRESS, what=f'start {t.task_name}')
            if n < 2:
                with self.clock.at(33 - n, history=f'task:{t.pk}'):
                    self._post('walk.pm', 'task_status_update',
                               {'project_id': acked.project_id, 'task_id': t.pk},
                               {'status': 'Done'},
                               check=lambda t=t: m.Task.objects.get(pk=t.pk).status
                               == m.Task.DONE, what=f'finish {t.task_name}')

    # ================================================================= report
    def _report(self, manifest, areas):
        self.stdout.write('')
        self.stdout.write('Rows recorded in the manifest, by area:')
        total = 0
        for area in areas:
            count = len(manifest.entries(area))
            total += count
            self.stdout.write(f'  {area:<12} {count}')
        self.stdout.write(f'  {"TOTAL":<12} {total}')
        self.stdout.write('')
        self.stdout.write('Logins: see docs/WALKTHROUGH_DATA.md (the password is not '
                          'printed here).')
