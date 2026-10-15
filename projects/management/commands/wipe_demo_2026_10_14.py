"""ONE-OFF, written 14 Oct 2026. Wipe the 14 Oct demo (streams S1-S4) from production.

    python manage.py wipe_demo_2026_10_14                                   # dry run
    python manage.py wipe_demo_2026_10_14 --execute --confirm-host <host>   # the real run

THE RULING
----------
Ruled by Zuber, 14 Oct 2026: the demo activity S1-S4 of 14 Oct 13:00-15:40 IST is
wiped from the system entirely, bypassing the append-only guards ONCE. That is the
single sanctioned exception to R-4 ("never UPDATE and never DELETE a StatusTransition
row") and to docs/demo-data.md's "do not generalise". It is recorded in
docs/execution-model.md, section 12 (decision log), 14 Oct.

  S1  MB0218 (214): activation, coordinator, QA/QC engineer + visit + punch point,
      task 2132, delivery challans 11 and 12 and their GRNs, mirror 2128
  S2  MB0233 (225): activation, added task 2171, QA/QC engineer
  S3  2512   (464): activation, coordinator, QA/QC engineers (one replaced), visit,
      punch point, tasks 2179 and 2180 with their checklist answers and submissions,
      delivery challans 13 and 14 and their GRNs, mirror 2175
  S4  2511   (463): activation, coordinator, QA/QC engineer + visit + punch point,
      task 2202 with its checklist answers

NEVER REUSE THIS, NEVER GENERALISE IT
-------------------------------------
This command BYPASSES MODEL-LEVEL APPEND-ONLY GUARDS ON PURPOSE. StatusTransition,
QaqcVisit, QaqcVisitAnswer and SiteQaqcAssignment refuse delete() on the instance;
QuerySet.delete() runs in SQL and never calls it, and that is the route taken here.
The ruling covers these rows on this date and nothing else. If another demo ever needs
undoing, it needs its own ruling and its own one-off; copying this file is not one.

THE IDS ARE PRODUCTION IDS, HARD-CODED FROM THE 14 OCT AUDIT
------------------------------------------------------------
Every row deleted or changed is named below by primary key, with the project, task,
visit or challan the audit found it on. No query decides WHAT to delete by date, actor
or pattern. Queries do run to CHECK, and every check aborts the whole run:

  a. every listed row exists and still belongs where the audit found it, and each
     project's phases and tasks are exactly the listed ones;
  b. no row outside the lists points at a listed row (any time), and none points at
     one of the 4 projects with a timestamp after 15:40 IST (real work since the audit);
  c. no design, approval, bill, payment, COD or ProjectDocument row points at the
     listed tasks or phases, and no COD is recorded on the 4 sites.

Check (b) is STRICTER than "after 15:40" for listed rows other than the projects: any
unlisted row pointing at a listed row aborts, whatever its time, because deleting the
listed row would cascade it away, null its pointer, or hit a PROTECT. Only the projects
themselves are referenced by years of real history, so only for them is the cut-off
time what separates real work from demo.

WHAT ACTIVATION WROTE, AND SO WHAT IS RESTORED
----------------------------------------------
opex_site_activate() sets status='Active' and activated_at, then project.save(),
record_transition(), attach_opex_template() (phases, tasks, the PM pre-assignment of
the non-mirror PM tasks through assign_tasks_to(), and sync_design_mirror() when the
site holds a DesignAssignment), then log_activity(). On the Project row only `status`
and `activated_at` change; save() rewrites the other columns with the values they
already held. The 13 Oct production dump agrees: against it the 4 rows differ in
status, activated_at and, on 463/464 only, assigned_pm. RESTORE below puts back the
dump's values for the first two.

assigned_pm IS NOT RESTORED. 463 and 464 were handed from anand to mehtab (profile 16)
at 15:30 in the 23-site handover (S5), which stands: assigned_pm stays 16 and
SitePmAssignment spans 224-246 / 250-272 are not touched.

NOT TOUCHED: users 69-72 and every profile edit, the S5 handover rows, tasks 1895-1902
(arash), 1918-1919, 2083/2102/2106/2112/2115, StatusTransitions 623-630, logins and
logouts, Programs, and every S5-S9 ActivityLog, Notification and NotificationLog row.
The post-checks compare all of them before and after, field by field.

ORDER, AND THE ONE TRANSACTION
------------------------------
Checks, deletes and post-checks run in one transaction.atomic(); any failure rolls
everything back. Deletes run children first, because the PROTECT foreign keys
(QaqcVisitAnswer -> PunchPoint/Task/QaqcVisit, QaqcVisit -> SiteQaqcAssignment) refuse
the reverse: answers, punch points, visits, QA/QC assignments, checklist answers, DC
lines, challans, tasks, phases; then the coordinator rows, ledger, activity and
notification rows; then the project restore. Each delete's per-model count must equal
the list exactly, so a cascade nobody listed rolls the run back.

STORAGE runs only after the commit: the 5 QA/QC photos, through
qaqc_views.discard_unrecorded_visit_photo(), which refuses any path a row still
records, any bucket but the bills bucket, and anything outside qaqc-visit/. Each
result is printed. A failure is reported and never retried; remove that object by hand.

THERE IS NO REVERSE. The rows are gone. Take a backup before --execute.
"""
import datetime

from django.apps import apps
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import models, transaction

from projects.management.commands._demo_support import database_host, print_db_banner
from projects.models import (
    ActivityLog, ChecklistItemCompletion, CodRecord, DCLineItem, DeliveryChallan,
    Notification, NotificationLog, Project, ProjectPhase, PunchPoint, QaqcVisit,
    QaqcVisitAnswer, SitePmAssignment, SiteQaqcAssignment, StatusTransition, Task,
    UserProfile,
)
from projects.qaqc_views import discard_unrecorded_visit_photo
from projects.bill_storage import bills_bucket


IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def _ist(*args):
    return datetime.datetime(*args, tzinfo=IST)


#: Check (b)'s line: a row pointing at one of the 4 projects with a timestamp after this
#: is work done since the audit, and stops the run.
CUTOFF = _ist(2026, 10, 14, 15, 40)

# ---------------------------------------------------------------------------
# The projects: restored to Draft, never deleted.
# ---------------------------------------------------------------------------
PROJECTS = {214: 'MB0218', 225: 'MB0233', 463: '2511', 464: '2512'}

#: What the audit read on each project. assigned_pm is checked, never written.
PROJECT_AT_AUDIT = {
    214: {'activated_at': _ist(2026, 10, 14, 13, 42, 2, 455836), 'assigned_pm_id': 15},
    225: {'activated_at': _ist(2026, 10, 14, 14, 4, 28, 173369), 'assigned_pm_id': 15},
    463: {'activated_at': _ist(2026, 10, 14, 15, 19, 15, 415865), 'assigned_pm_id': 16},
    464: {'activated_at': _ist(2026, 10, 14, 14, 52, 18, 913121), 'assigned_pm_id': 16},
}

#: The 13 Oct 16:27 dump's value of every Project column activation changes.
RESTORE = {'status': 'Draft', 'activated_at': None}

# ---------------------------------------------------------------------------
# Rows deleted, each with where the audit found it.
# ---------------------------------------------------------------------------
# phase id -> project
PHASES = {
    **{pk: 214 for pk in range(377, 384)},
    **{pk: 225 for pk in range(384, 391)},
    **{pk: 464 for pk in range(391, 398)},
    **{pk: 463 for pk in range(398, 405)},
}

# task id -> project. 2171 is the task added by hand on MB0233 at 14:05, after its
# activation; the other 92 are the template's.
TASKS = {
    **{pk: 214 for pk in range(2125, 2148)},
    **{pk: 225 for pk in range(2148, 2172)},
    **{pk: 464 for pk in range(2172, 2195)},
    **{pk: 463 for pk in range(2195, 2218)},
}

# id -> (subject_type, subject_id, project). 623-630 (arash, Residential) are not here.
STATUS_TRANSITIONS = {
    615: ('project', 214, 214),
    616: ('task', 2132, 214),
    617: ('delivery_challan', 11, 214),
    618: ('task', 2128, 214),
    619: ('delivery_challan', 12, 214),
    620: ('delivery_challan', 12, 214),
    621: ('delivery_challan', 11, 214),
    622: ('project', 225, 225),
    631: ('project', 464, 464),
    632: ('task', 2179, 464),
    633: ('delivery_challan', 13, 464),
    634: ('task', 2175, 464),
    635: ('delivery_challan', 14, 464),
    636: ('delivery_challan', 14, 464),
    637: ('delivery_challan', 13, 464),
    638: ('task', 2180, 464),
    639: ('project', 463, 463),
    640: ('task', 2202, 463),
}

# id -> task
CHECKLIST_COMPLETIONS = {
    1: 2179, 2: 2179, 3: 2179, 4: 2179,
    5: 2180, 6: 2180, 7: 2180, 8: 2180,
    9: 2202, 10: 2202, 11: 2202,
}

PHOTO_BUCKET = 'Horizon-PMS-Bills'

# id -> (visit, task, punch point raised, photo path in PHOTO_BUCKET)
QAQC_ANSWERS = {
    1: (1, 2132, 1, 'MB0218/qaqc-visit/e6eaeaf11a6e471a985898eae2ab5226.jpg'),
    2: (2, 2179, None, '2512/qaqc-visit/3183b62041c544778af6cd3f7b7f568d.jpg'),
    3: (2, 2179, 2, '2512/qaqc-visit/8dbad96f5d2e4989a2dc186394aa7c80.jpg'),
    4: (3, 2202, None, '2511/qaqc-visit/931727cbb8ab4a13adba5d66c3ee703b.jpg'),
    5: (3, 2202, 3, '2511/qaqc-visit/5b63f3938e77434cb1eb6d0db061323a.jpg'),
}

# id -> (project, site QA/QC assignment); all three were submitted at the audit
QAQC_VISITS = {1: (214, 1), 2: (464, 3), 3: (463, 5)}

# id -> task; all three were Open at the audit
PUNCH_POINTS = {1: 2132, 2: 2179, 3: 2202}

# id -> project. 3 is rohit's assignment on 2512, ended when avishek (4) replaced it.
SITE_QAQC_ASSIGNMENTS = {1: 214, 2: 225, 3: 464, 4: 464, 5: 463}

# id -> project. Challan numbers Demo-001-01, Demo-005, Demo-001-01, Demo-003.
DELIVERY_CHALLANS = {11: 214, 12: 214, 13: 464, 14: 464}

# id -> challan
DC_LINES = {25: 11, 26: 12, 27: 13, 28: 14}

# Project.coordinators through-row id -> (project, coordinator profile)
COORDINATOR_ROWS = {27: (214, 34), 28: (464, 58), 29: (463, 42)}

# id -> (entity_type, entity_id, project). The S1-S4 rows of 3737-3905 whose entity is
# a listed object or one of the 4 projects. 3901/3902 (S5 handover, entity 463/464)
# and every login/logout stay.
ACTIVITY_LOG = {
    3757: ('Project', 214, 214),                    # nirankar
    3758: ('SiteQaqcAssignment', 1, 214),           # nirankar
    3759: ('Project', 214, 214),                    # nirankar
    3760: ('QaqcVisit', 1, 214),                    # gaurav
    3762: ('Task', 2132, 214),                      # nirankar
    3763: ('Task', 2132, 214),                      # nirankar
    3764: ('Task', 2132, 214),                      # nirankar
    3765: ('DeliveryChallan', 11, 214),             # mahesh
    3766: ('DeliveryChallan', 12, 214),             # mahesh
    3768: ('DeliveryChallan', 12, 214),             # anurag
    3769: ('DeliveryChallan', 12, 214),             # anurag
    3770: ('DeliveryChallan', 11, 214),             # anurag
    3773: ('QaqcVisit', 1, 214),                    # gaurav
    3774: ('Project', 225, 225),                    # nirankar
    3775: ('Task', 2171, 225),                      # nirankar
    3777: ('SiteQaqcAssignment', 2, 225),           # nirankar
    3820: ('Project', 464, 464),                    # anand
    3821: ('SiteQaqcAssignment', 3, 464),           # anand
    3822: ('Project', 464, 464),                    # anand
    3823: ('Task', 2179, 464),                      # anand
    3826: ('Task', 2179, 464),                      # preetam
    3827: ('Task', 2179, 464),                      # preetam
    3828: ('Task', 2179, 464),                      # preetam
    3829: ('ChecklistItemCompletion', 1, 464),      # preetam
    3830: ('ChecklistItemCompletion', 2, 464),      # preetam
    3831: ('ChecklistItemCompletion', 3, 464),      # preetam
    3832: ('ChecklistItemCompletion', 4, 464),      # preetam
    3833: ('Task', 2179, 464),                      # preetam
    3836: ('QaqcVisit', 2, 464),                    # rohit
    3837: ('QaqcVisit', 2, 464),                    # rohit
    3838: ('DeliveryChallan', 13, 464),             # mahesh
    3839: ('DeliveryChallan', 14, 464),             # mahesh
    3840: ('DeliveryChallan', 14, 464),             # preetam
    3841: ('DeliveryChallan', 13, 464),             # preetam
    3849: ('Task', 2180, 464),                      # anand
    3852: ('Task', 2180, 464),                      # yash
    3853: ('Task', 2180, 464),                      # yash
    3855: ('ChecklistItemCompletion', 5, 464),      # yash
    3856: ('ChecklistItemCompletion', 6, 464),      # yash
    3857: ('ChecklistItemCompletion', 7, 464),      # yash
    3858: ('ChecklistItemCompletion', 8, 464),      # yash
    3859: ('Task', 2180, 464),                      # yash
    3860: ('SiteQaqcAssignment', 4, 464),           # anand
    3865: ('Project', 463, 463),                    # anand
    3866: ('SiteQaqcAssignment', 5, 463),           # anand
    3867: ('Project', 463, 463),                    # anand
    3868: ('Task', 2202, 463),                      # anand
    3869: ('Task', 2202, 463),                      # anand
    3870: ('Task', 2202, 463),                      # anand
    3871: ('QaqcVisit', 3, 463),                    # avishek
    3872: ('ChecklistItemCompletion', 9, 463),      # anand
    3873: ('ChecklistItemCompletion', 10, 463),     # anand
    3874: ('ChecklistItemCompletion', 11, 463),     # anand
    3877: ('QaqcVisit', 3, 463),                    # avishek
}

# id -> related project. 7179-7193 (S8 reassignments) and 7225-7228 (S5 handover)
# stay. The whatsapp and email rows record messages that really went out; deleting
# the row does not unsend them.
NOTIFICATION_LOG = {
    7162: 214,  # nirankar -> chandan, in_app, coordinator added
    7163: 214,  # nirankar -> gaurav, in_app, site_qaqc_assigned
    7164: 214,  # nirankar -> gaurav, email, site_qaqc_assigned
    7165: 214,  # nirankar -> anurag, in_app, assign_task
    7166: 214,  # nirankar -> anurag, whatsapp, assign_task
    7167: 214,  # nirankar -> anurag, email, assign_task
    7168: 214,  # gaurav -> nirankar, in_app, qaqc_visit_submitted
    7169: 214,  # gaurav -> nirankar, email, qaqc_visit_submitted
    7170: 214,  # gaurav -> chandan, in_app, qaqc_visit_submitted
    7171: 214,  # gaurav -> chandan, email, qaqc_visit_submitted
    7172: 214,  # gaurav -> anurag, in_app, punch_point_raised_by_visit
    7173: 214,  # gaurav -> anurag, email, punch_point_raised_by_visit
    7174: 225,  # nirankar -> priyanka, in_app, assign_task
    7175: 225,  # nirankar -> priyanka, whatsapp, assign_task
    7176: 225,  # nirankar -> priyanka, email, assign_task
    7177: 225,  # nirankar -> gaurav, in_app, site_qaqc_assigned
    7178: 225,  # nirankar -> gaurav, email, site_qaqc_assigned
    7194: 464,  # anand -> akhil, in_app, coordinator added
    7195: 464,  # anand -> rohit, in_app, site_qaqc_assigned
    7196: 464,  # anand -> rohit, email, site_qaqc_assigned
    7197: 464,  # anand -> preetam, in_app, assign_task
    7198: 464,  # anand -> preetam, whatsapp, assign_task
    7199: 464,  # anand -> preetam, email, assign_task
    7200: 464,  # rohit -> anand, in_app, qaqc_visit_submitted
    7201: 464,  # rohit -> anand, email, qaqc_visit_submitted
    7202: 464,  # rohit -> akhil, in_app, qaqc_visit_submitted
    7203: 464,  # rohit -> akhil, email, qaqc_visit_submitted
    7204: 464,  # rohit -> preetam, in_app, punch_point_raised_by_visit
    7205: 464,  # rohit -> preetam, email, punch_point_raised_by_visit
    7206: 464,  # anand -> yash, in_app, assign_task
    7207: 464,  # anand -> yash, whatsapp, assign_task
    7208: 464,  # anand -> yash, email, assign_task
    7209: 464,  # anand -> avishek, in_app, site_qaqc_assigned
    7210: 464,  # anand -> avishek, email, site_qaqc_assigned
    7211: 464,  # anand -> rohit, in_app, site_qaqc_removed
    7212: 464,  # anand -> rohit, email, site_qaqc_removed
    7213: 463,  # anand -> narendra, in_app, coordinator added
    7214: 463,  # anand -> avishek, in_app, site_qaqc_assigned
    7215: 463,  # anand -> avishek, email, site_qaqc_assigned
    7216: 463,  # anand -> vijay, in_app, assign_task
    7217: 463,  # anand -> vijay, whatsapp, assign_task
    7218: 463,  # anand -> vijay, email, assign_task
    7219: 463,  # avishek -> anand, in_app, qaqc_visit_submitted
    7220: 463,  # avishek -> anand, email, qaqc_visit_submitted
    7221: 463,  # avishek -> narendra, in_app, qaqc_visit_submitted
    7222: 463,  # avishek -> narendra, email, qaqc_visit_submitted
    7223: 463,  # avishek -> vijay, in_app, punch_point_raised_by_visit
    7224: 463,  # avishek -> vijay, email, punch_point_raised_by_visit
}

# id -> project code its link opens. Notification has no FK to anything but its
# recipient, so the link is what ties it to a site. 1496-1500 (S8) and 1516-1517 (S5)
# stay.
NOTIFICATIONS = {
    1488: 'MB0218', 1489: 'MB0218', 1490: 'MB0218', 1491: 'MB0218', 1492: 'MB0218',
    1493: 'MB0218', 1494: 'MB0233', 1495: 'MB0233',
    1501: '2512', 1502: '2512', 1503: '2512', 1504: '2512', 1505: '2512', 1506: '2512',
    1507: '2512', 1508: '2512', 1509: '2512',
    1510: '2511', 1511: '2511', 1512: '2511', 1513: '2511', 1514: '2511', 1515: '2511',
}

# ---------------------------------------------------------------------------
# Rows that must come through untouched: S5-S9 and arash. Compared field by field
# before the first delete and after the last write; any difference rolls back.
# ---------------------------------------------------------------------------
_S5_SPANS = list(range(224, 247)) + list(range(250, 273))
KEEP = [
    (StatusTransition, list(range(623, 631)), ()),
    (Task, list(range(1895, 1903)) + [1918, 1919, 2083, 2102, 2106, 2112, 2115], ()),
    (User, [69, 70, 71, 72], ('last_login',)),           # a login is not a change
    (UserProfile, [69, 70, 71, 72], ()),
    (SitePmAssignment, _S5_SPANS, ()),
    (Project, list(range(442, 463)), ()),                # S5's other 21 sites
    (ActivityLog, [pk for pk in range(3737, 3906) if pk not in ACTIVITY_LOG], ()),
    (Notification, [1496, 1497, 1498, 1499, 1500, 1516, 1517], ('is_read',)),
    (NotificationLog, list(range(7179, 7194)) + [7225, 7226, 7227, 7228],
     ('delivery_status',)),                              # Interakt webhooks update it
]

_COORDINATORS = Project.coordinators.through

#: Children first. Each entry is deleted with QuerySet.delete(), whose per-model count
#: must equal this list exactly.
DELETE_ORDER = [
    ('QA/QC visit answers', QaqcVisitAnswer, QAQC_ANSWERS),
    ('punch points', PunchPoint, PUNCH_POINTS),
    ('QA/QC visits', QaqcVisit, QAQC_VISITS),
    ('site QA/QC assignments', SiteQaqcAssignment, SITE_QAQC_ASSIGNMENTS),
    ('checklist answers', ChecklistItemCompletion, CHECKLIST_COMPLETIONS),
    ('delivery challan lines', DCLineItem, DC_LINES),
    ('delivery challans', DeliveryChallan, DELIVERY_CHALLANS),
    ('tasks', Task, TASKS),
    ('phases', ProjectPhase, PHASES),
    ('coordinator rows', _COORDINATORS, COORDINATOR_ROWS),
    ('status transitions', StatusTransition, STATUS_TRANSITIONS),
    ('activity log rows', ActivityLog, ACTIVITY_LOG),
    ('notification log rows', NotificationLog, NOTIFICATION_LOG),
    ('in-app notifications', Notification, NOTIFICATIONS),
]

#: Models whose rows are deleted here, with their ids, so check (b) can tell a listed
#: row from an unlisted one pointing at the same target.
LISTED = {model: set(ids) for _label, model, ids in DELETE_ORDER}

#: Targets of check (b): a pointer at any of these from an unlisted row matters.
TARGETS = {model: set(ids) for _label, model, ids in DELETE_ORDER
           if model not in (StatusTransition, ActivityLog, NotificationLog, Notification,
                            _COORDINATORS)}
TARGETS[Project] = set(PROJECTS)

#: Check (c): model names whose pointers at the listed tasks or phases abort the run.
CHECK_C_MODELS = {
    'DesignAssignment', 'DesignAttempt', 'ArkaSubmission', 'DesignFile',
    'DesignChangeRequest', 'DueDateCommitment', 'DesignSubmission',
    'ApprovalRequest', 'ApprovalStep', 'ApprovalAttachment', 'ApprovalRoundSnapshot',
    'ApprovalOrderLink', 'MaterialApprovalDetail', 'MaterialApprovalLine',
    'ContractorBillDetail', 'ContractorBillTask', 'PaymentRequest', 'PaymentRequestHold',
    'CodRecord', 'ProjectDocument',
}

# StatusTransition.subject_type and ActivityLog.entity_type for each listed model.
_SUBJECT_TYPES = {'project': Project, 'task': Task, 'delivery_challan': DeliveryChallan}


class Command(BaseCommand):
    help = ('ONE-OFF: wipe the 14 Oct 2026 demo (S1-S4) by hard-coded id. Dry run '
            'unless --execute --confirm-host HOST.')

    def add_arguments(self, parser):
        parser.add_argument('--execute', action='store_true',
                            help='Delete and restore. Without it nothing is written.')
        parser.add_argument('--confirm-host', default=None,
                            help='Required with --execute: the database host printed '
                                 'on the first line, typed back.')

    def handle(self, *args, **options):
        print_db_banner(self)
        execute = options['execute']
        if execute:
            host = database_host()
            if not host or options['confirm_host'] != host:
                raise CommandError(
                    f'REFUSING --execute: --confirm-host must equal the database host '
                    f'printed above ({host or "(none - local socket)"}). Got '
                    f'{options["confirm_host"]!r}. Nothing was written.')

        self._print_plan()

        if not execute:
            failures = self._prechecks()
            self._report_prechecks(failures)
            if failures:
                raise CommandError('Pre-checks failed (dry run). Nothing was written.')
            self.stdout.write(self.style.SUCCESS(
                '\nDRY RUN: every pre-check passed. Nothing was written. Re-run with '
                '--execute --confirm-host <host> to delete and restore.'))
            return

        with transaction.atomic():
            # Locks the 4 projects for the length of the run, so an activation, a
            # handover or an edit on one of them waits for this transaction instead of
            # interleaving with it. Rows on other tables are guarded by the post-check
            # comparison, which rolls back if anything moved underneath.
            list(Project.objects.select_for_update().filter(pk__in=PROJECTS))

            failures = self._prechecks()
            self._report_prechecks(failures)
            if failures:
                raise CommandError('Pre-checks failed. Nothing was written.')

            kept_before = self._snapshot_keep()
            self._delete_all()
            self._restore_projects()

            problems = self._postchecks(kept_before)
            if problems:
                for line in problems:
                    self.stderr.write(f'  POST-CHECK FAILED: {line}')
                # Raising inside atomic() is the rollback.
                raise CommandError('Post-checks failed. Everything was rolled back.')

        self.stdout.write(self.style.SUCCESS(
            '\nCOMMITTED: rows deleted, 4 projects back to Draft, post-checks passed.'))
        self._delete_photos()

    # ------------------------------------------------------------------ plan
    def _print_plan(self):
        self.stdout.write('\nPROJECTS restored (never deleted). Columns written: '
                          + ', '.join(f'{k}={v!r}' for k, v in RESTORE.items())
                          + '. assigned_pm is not written.')
        for pk, code in PROJECTS.items():
            audit = PROJECT_AT_AUDIT[pk]
            self.stdout.write(f'  Project {pk} {code}: Active -> Draft; activated_at '
                              f'{audit["activated_at"].isoformat()} -> None; '
                              f'assigned_pm stays {audit["assigned_pm_id"]}')
        total = 0
        for label, model, rows in DELETE_ORDER:
            self.stdout.write(f'\nDELETE {model._meta.label} - {label} ({len(rows)})')
            for pk, where in rows.items():
                self.stdout.write(f'  {pk}: {where}')
            total += len(rows)
        self.stdout.write(f'\nDELETE total: {total} rows; 4 projects restored.')
        self.stdout.write(f'STORAGE after commit ({len(QAQC_ANSWERS)} objects, bucket '
                          f'{PHOTO_BUCKET}; bills bucket setting here is '
                          f'{bills_bucket()!r}):')
        for _visit, _task, _pp, path in QAQC_ANSWERS.values():
            self.stdout.write(f'  {PHOTO_BUCKET}/{path}')
        if bills_bucket() != PHOTO_BUCKET:
            self.stdout.write(self.style.WARNING(
                f'  WARNING: the bills bucket setting is not {PHOTO_BUCKET!r}, so the '
                f'photo cleanup will refuse every object and report it.'))

    # ------------------------------------------------------------------ checks
    def _report_prechecks(self, failures):
        self.stdout.write('\nPRE-CHECKS')
        if not failures:
            self.stdout.write('  a, b, c: all passed')
            return
        for line in failures:
            self.stderr.write(f'  FAILED: {line}')

    def _prechecks(self):
        return self._check_a() + self._check_b() + self._check_c()

    def _check_a(self):
        """Every listed row exists and belongs where the audit found it."""
        failures = []

        projects = {p.pk: p for p in Project.objects.filter(pk__in=PROJECTS)}
        for pk, code in PROJECTS.items():
            p = projects.get(pk)
            if p is None:
                failures.append(f'a: Project {pk} {code} does not exist')
                continue
            audit = PROJECT_AT_AUDIT[pk]
            if (p.project_id, p.project_type, p.status, p.is_deleted) != (code, 'OPEX', 'Active', False):
                failures.append(f'a: Project {pk} is {p.project_id}/{p.project_type}/'
                                f'{p.status}/deleted={p.is_deleted}, expected {code}/OPEX/Active/False')
            if p.activated_at != audit['activated_at']:
                failures.append(f'a: Project {pk} activated_at {p.activated_at}, '
                                f'expected {audit["activated_at"]}')
            if p.assigned_pm_id != audit['assigned_pm_id']:
                failures.append(f'a: Project {pk} assigned_pm {p.assigned_pm_id}, '
                                f'expected {audit["assigned_pm_id"]}')

        # Every phase and task on the 4 projects, listed or not: the sets must match the
        # lists exactly, so a phase or task added since the audit stops the run.
        actual_phases = dict(ProjectPhase.objects.filter(project_id__in=PROJECTS)
                             .values_list('pk', 'project_id'))
        failures += _diff('a: ProjectPhase (id -> project)', PHASES, actual_phases)
        actual_tasks = dict(Task.objects.filter(phase__project_id__in=PROJECTS)
                            .values_list('pk', 'phase__project_id'))
        failures += _diff('a: Task (id -> project)', TASKS, actual_tasks)
        stray = Task.objects.filter(pk__in=TASKS).exclude(phase_id__in=PHASES)
        failures += [f'a: Task {t.pk} is not under a listed phase' for t in stray]

        failures += _diff('a: StatusTransition', STATUS_TRANSITIONS, {
            pk: (st, sid, proj) for pk, st, sid, proj in StatusTransition.objects
            .filter(pk__in=STATUS_TRANSITIONS)
            .values_list('pk', 'subject_type', 'subject_id', 'project_id')})
        failures += _diff('a: ChecklistItemCompletion (id -> task)', CHECKLIST_COMPLETIONS,
                          dict(ChecklistItemCompletion.objects.filter(pk__in=CHECKLIST_COMPLETIONS)
                               .values_list('pk', 'task_id')))
        failures += _diff('a: QaqcVisitAnswer', QAQC_ANSWERS, {
            pk: (visit, task, pp, path) for pk, visit, task, pp, bucket, path in
            QaqcVisitAnswer.objects.filter(pk__in=QAQC_ANSWERS).values_list(
                'pk', 'visit_id', 'task_id', 'punch_point_id', 'photo_bucket', 'photo_path')
            if bucket == PHOTO_BUCKET})
        failures += _diff('a: QaqcVisit (id -> project, assignment)', QAQC_VISITS, {
            pk: (proj, asg) for pk, proj, asg in QaqcVisit.objects
            .filter(pk__in=QAQC_VISITS, status='submitted')
            .values_list('pk', 'project_id', 'assignment_id')})
        failures += _diff('a: PunchPoint (id -> task, Open)', PUNCH_POINTS, dict(
            PunchPoint.objects.filter(pk__in=PUNCH_POINTS, status='Open')
            .values_list('pk', 'task_id')))
        failures += _diff('a: SiteQaqcAssignment (id -> project)', SITE_QAQC_ASSIGNMENTS, dict(
            SiteQaqcAssignment.objects.filter(pk__in=SITE_QAQC_ASSIGNMENTS)
            .values_list('pk', 'project_id')))
        failures += _diff('a: DeliveryChallan (id -> project)', DELIVERY_CHALLANS, dict(
            DeliveryChallan.objects.filter(pk__in=DELIVERY_CHALLANS)
            .values_list('pk', 'project_id')))
        failures += _diff('a: DCLineItem (id -> challan)', DC_LINES, dict(
            DCLineItem.objects.filter(pk__in=DC_LINES).values_list('pk', 'challan_id')))
        failures += _diff('a: coordinator row (id -> project, profile)', COORDINATOR_ROWS, {
            pk: (proj, prof) for pk, proj, prof in _COORDINATORS.objects
            .filter(pk__in=COORDINATOR_ROWS)
            .values_list('pk', 'project_id', 'userprofile_id')})
        failures += _diff('a: ActivityLog', ACTIVITY_LOG, {
            pk: (et, eid, proj) for pk, et, eid, proj in ActivityLog.objects
            .filter(pk__in=ACTIVITY_LOG)
            .values_list('pk', 'entity_type', 'entity_id', 'project_id')})
        failures += _diff('a: NotificationLog (id -> related project)', NOTIFICATION_LOG, dict(
            NotificationLog.objects.filter(pk__in=NOTIFICATION_LOG)
            .values_list('pk', 'related_project_id')))
        failures += _diff('a: Notification (id -> project in link)', NOTIFICATIONS, {
            pk: _code_in_link(link) for pk, link in Notification.objects
            .filter(pk__in=NOTIFICATIONS).values_list('pk', 'link')})
        return failures

    def _check_b(self):
        """No unlisted row points at a listed row; none points at the 4 projects with
        a timestamp after the cut-off. Discovers rows to REPORT, never to delete."""
        failures = []
        for model, field, target in _pointers_into(TARGETS):
            qs = model._base_manager.filter(**{f'{field.attname}__in': TARGETS[target]})
            if model in LISTED:
                qs = qs.exclude(pk__in=LISTED[model])
            if target is Project:
                after = _after_cutoff_q(model)
                if after is None:
                    continue    # no timestamp to tell real work from old history
                qs = qs.filter(after)
            for row in qs[:50]:
                failures.append(f'b: {model._meta.label} {row.pk} -> {field.name}='
                                f'{getattr(row, field.attname)} ({target.__name__})')

        # The two polymorphic pointers: (subject_type, subject_id) on the ledger,
        # (entity_type, entity_id) on the activity log. Same rule as the FKs above.
        for subject_type, model in _SUBJECT_TYPES.items():
            qs = (StatusTransition.objects
                  .filter(subject_type=subject_type, subject_id__in=TARGETS[model])
                  .exclude(pk__in=STATUS_TRANSITIONS))
            if model is Project:
                qs = qs.filter(occurred_at__gt=CUTOFF)
            failures += [f'b: StatusTransition {st.pk} on {subject_type} {st.subject_id} '
                         f'at {st.occurred_at}' for st in qs[:50]]
        for model, ids in TARGETS.items():
            qs = (ActivityLog.objects
                  .filter(entity_type=model.__name__, entity_id__in=ids)
                  .exclude(pk__in=ACTIVITY_LOG))
            if model is Project:
                qs = qs.filter(timestamp__gt=CUTOFF)
            failures += [f'b: ActivityLog {a.pk} on {a.entity_type} {a.entity_id} at '
                         f'{a.timestamp}: {a.action[:60]}' for a in qs[:50]]

        # Notification has no FK to a site, only a link; a link to one of the 4 sites
        # created after the cut-off is a notice about real work since the audit.
        for code in PROJECTS.values():
            qs = (Notification.objects
                  .filter(link__startswith=f'/projects/{code}/', created_at__gt=CUTOFF)
                  .exclude(pk__in=NOTIFICATIONS))
            failures += [f'b: Notification {n.pk} links {n.link} at {n.created_at}'
                         for n in qs[:50]]
        return failures

    def _check_c(self):
        """No design, approval, bill, payment, COD or document row points at the listed
        tasks or phases, and no COD is recorded on the 4 sites."""
        failures = []
        targets = {Task: TARGETS[Task], ProjectPhase: TARGETS[ProjectPhase]}
        for model, field, target in _pointers_into(targets):
            owner = model._meta.auto_created or model
            if owner.__name__ not in CHECK_C_MODELS:
                continue
            for row in model._base_manager.filter(**{f'{field.attname}__in': targets[target]})[:50]:
                failures.append(f'c: {model._meta.label} {row.pk} -> {target.__name__} '
                                f'{getattr(row, field.attname)}')
        failures += [f'c: CodRecord {c.pk} on project {c.project_id}'
                     for c in CodRecord.objects.filter(project_id__in=PROJECTS)]
        return failures

    # ------------------------------------------------------------------ writes
    def _delete_all(self):
        """Children first; each QuerySet.delete() must remove exactly its list.

        QuerySet.delete() runs in SQL through the deletion collector and never calls a
        model's delete() override. That is the append-only bypass the ruling allows,
        and the reason no instance is ever loaded and deleted here."""
        self.stdout.write('\nDELETING')
        for label, model, rows in DELETE_ORDER:
            _total, per_model = model._base_manager.filter(pk__in=rows).delete()
            per_model = {k: v for k, v in per_model.items() if v}
            if per_model != {model._meta.label: len(rows)}:
                raise CommandError(
                    f'{label}: deleted {per_model}, expected exactly '
                    f'{{{model._meta.label!r}: {len(rows)}}}. A cascade nobody listed, or '
                    f'a missing row. Everything was rolled back.')
            self.stdout.write(f'  {model._meta.label}: {len(rows)}')

    def _restore_projects(self):
        # Race: the 4 rows are locked by select_for_update() at the top of the
        # transaction, and the status/activated_at filter re-states what the pre-check
        # read, so a row that changed anyway updates nothing and the count check below
        # rolls the run back.
        restored = 0
        for pk, audit in PROJECT_AT_AUDIT.items():
            restored += Project.objects.filter(
                pk=pk, status='Active', activated_at=audit['activated_at'],
            ).update(**RESTORE)
        if restored != len(PROJECTS):
            raise CommandError(f'Restored {restored} of {len(PROJECTS)} projects. '
                               f'Everything was rolled back.')
        self.stdout.write(f'  Project restored to Draft: {restored}')

    # ------------------------------------------------------------------ post-checks
    def _snapshot_keep(self):
        snap = {}
        for model, ids, ignore in KEEP:
            rows = {}
            for values in model._base_manager.filter(pk__in=ids).values():
                for col in ignore:
                    values.pop(col, None)
                rows[values['id']] = values
            snap[model._meta.label] = rows
        return snap

    def _postchecks(self, kept_before):
        problems = []
        for _label, model, rows in DELETE_ORDER:
            left = model._base_manager.filter(pk__in=rows).count()
            if left:
                problems.append(f'{model._meta.label}: {left} listed rows still exist')
        if ProjectPhase.objects.filter(project_id__in=PROJECTS).exists():
            problems.append('a phase is still on one of the 4 projects')
        if Task.objects.filter(phase__project_id__in=PROJECTS).exists():
            problems.append('a task is still on one of the 4 projects')
        for p in Project.objects.filter(pk__in=PROJECTS):
            want_pm = PROJECT_AT_AUDIT[p.pk]['assigned_pm_id']
            if (p.status, p.activated_at, p.assigned_pm_id) != ('Draft', None, want_pm):
                problems.append(f'Project {p.pk} reads {p.status}/{p.activated_at}/'
                                f'pm {p.assigned_pm_id}, expected Draft/None/pm {want_pm}')

        kept_after = self._snapshot_keep()
        for model, ids, _ignore in KEEP:
            label = model._meta.label
            before, after = kept_before[label], kept_after[label]
            if len(before) != len(ids):
                problems.append(f'{label}: {len(ids) - len(before)} kept rows were '
                                f'already missing before the run')
            if before != after:
                changed = sorted(pk for pk in set(before) | set(after)
                                 if before.get(pk) != after.get(pk))
                problems.append(f'{label}: kept rows changed: {changed[:20]}')
        return problems

    # ------------------------------------------------------------------ storage
    def _delete_photos(self):
        """After the commit only. One attempt per object; a failure is reported for a
        human to remove, never retried."""
        self.stdout.write(f'\nSTORAGE ({PHOTO_BUCKET})')
        failed = 0
        for _visit, _task, _pp, path in QAQC_ANSWERS.values():
            removed = discard_unrecorded_visit_photo({'bucket': PHOTO_BUCKET, 'path': path})
            if removed:
                self.stdout.write(f'  removed  {path}')
            else:
                failed += 1
                self.stderr.write(f'  NOT REMOVED  {path}  (the helper refused it or '
                                  f'storage failed; see the server log; remove by hand)')
        if failed:
            self.stderr.write(f'{failed} of {len(QAQC_ANSWERS)} photos were not removed. '
                              f'The database wipe is committed regardless.')


def _diff(label, expected, actual):
    """Lines for every id whose actual value differs from the audit, or is missing."""
    out = []
    for pk in sorted(set(expected) | set(actual)):
        if pk not in actual:
            out.append(f'{label}: {pk} missing or no longer as audited '
                       f'(expected {expected[pk]})')
        elif pk not in expected:
            out.append(f'{label}: {pk} -> {actual[pk]} exists but is not listed')
        elif expected[pk] != actual[pk]:
            out.append(f'{label}: {pk} is {actual[pk]}, expected {expected[pk]}')
    return out


def _code_in_link(link):
    """'MB0218' from '/projects/MB0218/tasks/2132/'; '' for any other link."""
    parts = (link or '').split('/')
    return parts[2] if len(parts) > 2 and parts[1] == 'projects' else ''


def _pointers_into(targets):
    """(model, field, target) for every FK / one-to-one column, in every model
    including auto-created M2M through tables, that points at a target model."""
    for model in apps.get_models(include_auto_created=True):
        for field in model._meta.concrete_fields:
            if field.is_relation and field.related_model in targets:
                yield model, field, field.related_model


def _after_cutoff_q(model):
    """OR of `<datetime column> > CUTOFF` over the model's timestamp columns, or None."""
    q = None
    for field in model._meta.concrete_fields:
        if isinstance(field, models.DateTimeField):
            part = models.Q(**{f'{field.attname}__gt': CUTOFF})
            q = part if q is None else q | part
    return q
