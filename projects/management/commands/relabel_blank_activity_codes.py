"""RPT-C1 (decision C5, Zuber's ruling 4), written 2026-11-28. Give old blank-code
ActivityLog rows the action code their call site writes now.

    python manage.py relabel_blank_activity_codes            # dry run: counts + samples
    python manage.py relabel_blank_activity_codes --apply    # write action_code

WHAT IT DOES
------------
RPT-C1 made every call site that wrote a blank `action_code` pass one. The rows those
sites wrote BEFORE then are still blank, so the Reports "What" column labels them by
entity type ("Task update"). This command matches each blank row against a fixed list of
EXACT patterns, one per call site's text (an entity type plus a regex that must match
the whole `action`), and sets the code that call site writes today. Two patterns are
historic: due-date edits before SCHED-1 ("Updated due date for task: X", now
`due_date_changed`) and the pre-FIN-M-L milestone task sync ("Milestone Mx auto-marked
Received via ...", now `milestone_updated`).

NO FUZZY MATCHING. A row that matches no pattern stays blank and is listed by its
leading words: on the 28 Nov 2026 restore that is 284 rows, all before 28 Jul 2026
(legacy sign-ins and sign-outs, old status and issue lines). They MUST stay blank: a
login row relabelled `user_login` would leave human activity and change July's figures
(decision C2). A row matching patterns that disagree on the code is a refusal: nothing
is written and the conflict is printed.

WHAT IT WRITES. `action_code` only, and only on rows whose code is still '' at the moment
of the write. Every new code passes site_dashboard.human_activity_q() exactly as the
blank row did (tests_activity_codes_c1 pins it), so "Active today", latest activity,
Gone quiet and every report figure stay the same for the same rows; only the kind
labels change. A second dry run after --apply shows 0 to relabel.

CONSOLE OUTPUT IS ASCII. A piped Windows console writes cp1252, which has no "→" or
"₹" (the B-0b trap); samples are printed with those replaced. The database is untouched.
"""
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from django.conf import settings
from django.core.management.base import BaseCommand

from projects.models import ActivityLog

SAMPLES = 5
CHUNK = 1000


@dataclass(frozen=True)
class Pattern:
    key: str
    entity_type: str
    regex: str        # must match the WHOLE action text (re.fullmatch, DOTALL)
    code: str
    writer: str       # the call site that writes this text (and now this code)


PATTERNS = (
    Pattern('P01', 'Project', r"Activated project: \S+", 'project_activated',
            'views.project_activate / opex_site_activate, seed_opex_test_data'),
    Pattern('P02', 'Project', r"Edited project fields: .+", 'project_fields_edited',
            'views.project_field_edit'),
    Pattern('P03', 'Project', r"Updated project coordinators \(added \d+, removed \d+\)",
            'project_coordinators_updated', 'views.assign_coordinators'),
    Pattern('P04', 'Project', r"Cascading scheduling enabled for project '[^']+'",
            'cascade_scheduling_enabled', 'views.enable_cascade_scheduling'),
    Pattern('P05', 'Project', r"Project '[^']+' created via Zoho CRM webhook",
            'project_created_from_zoho', 'views.zoho_deal_closed_webhook'),
    Pattern('B01', 'BOQ', r"BOQ Acknowledged for project: \S+", 'boq_acknowledged',
            'views._apply_boq_acknowledgement'),
    Pattern('B02', 'BOQ', r"Submitted BOQ for project: \S+", 'boq_submitted', 'views.boq_submit'),
    Pattern('B03', 'BOQ', r"BOQ Revision Requested for project: \S+", 'boq_revision_requested',
            'views.boq_request_revision'),
    Pattern('F01', 'File', r"Uploaded \d+ file\(s\) to project", 'project_file_uploaded',
            'views.upload_project_document'),
    Pattern('F02', 'File', r"Uploaded \d+ file\(s\) to task: .+", 'task_file_uploaded',
            'views.upload_task_attachment'),
    # The two delete views write the same text, hence one shared code (ruling 2).
    Pattern('F03', 'File', r"Deleted file: .+", 'file_deleted',
            'views.delete_project_document / delete_task_attachment'),
    Pattern('K01', 'ChecklistItemCompletion', r"Answered '.+' on checklist item '.+' on task: .+",
            'checklist_item_answered', 'views.checklist_item_complete'),
    Pattern('C01', 'Comment', r"Commented on task: .+", 'task_commented',
            'views.create_task_comment'),
    Pattern('C02', 'Comment', r"Replied to comment on task: .+", 'task_comment_replied',
            'views.create_task_comment'),
    Pattern('C03', 'Comment', r"Commented on issue: .+", 'issue_commented',
            'views.create_issue_comment / grn_damage.record_damage'),
    Pattern('C04', 'Comment', r"Replied to comment on issue: .+", 'issue_comment_replied',
            'views.create_issue_comment'),
    Pattern('C05', 'Comment', r"Deleted comment on task: .+", 'task_comment_deleted',
            'views.delete_comment'),
    Pattern('C06', 'Comment', r"Deleted comment on issue: .+", 'issue_comment_deleted',
            'views.delete_comment'),
    Pattern('I01', 'Issue', r"Moved issue to In Progress: .+", 'issue_in_progress',
            'views.update_issue_status'),
    Pattern('I02', 'Issue', r"Assigned issue to .+", 'issue_assigned', 'views.assign_issue'),
    Pattern('I03', 'Issue', r"Unassigned issue: .+", 'issue_unassigned', 'views.assign_issue'),
    Pattern('D01', 'DeliveryChallan', r"SCM created Delivery Challan .+ for .+", 'dc_created',
            'views.create_delivery_challan, seed_opex_test_data'),
    Pattern('D02', 'DeliveryChallan', r"SE confirmed GRN for DC .+ — .+", 'grn_confirmed',
            'views.confirm_grn'),
    Pattern('D03', 'DeliveryChallan', r"SCM overrode GRN for DC .+ — override by .+",
            'grn_overridden', 'views.override_grn'),
    Pattern('S01', 'System', r"Auto-schedule working-day switch set to .+",
            'system_setting_changed', 'views.admin_master_switches'),
    Pattern('S02', 'System', r"Master switch '[^']+' set to (ON|OFF)",
            'system_setting_changed', 'views.admin_master_switches'),
    Pattern('S03', 'System', r"Cascading scheduling feature gate set to (ON|OFF)",
            'system_setting_changed', 'views.admin_master_switches'),
    Pattern('S04', 'System',
            r"(Client Gantt buffer \(days\)|Gantt external min bar width \(days\)) set to \d+",
            'system_setting_changed', 'views.admin_master_switches'),
    Pattern('U01', 'User', r"User '.+' \([^()]+\) deactivated", 'user_deactivated',
            'views.admin_user_management / admin_departments'),
    Pattern('U02', 'User', r"User '.+' \([^()]+\) reactivated", 'user_reactivated',
            'views.admin_user_management / admin_departments'),
    Pattern('U03', 'User', r"User '.+' role changed from '[^']*' to '[^']*'", 'user_role_changed',
            'views.admin_user_management / admin_departments'),
    Pattern('U04', 'User', r"User '.+' role changed from '[^']*' to '[^']*' by System Admin",
            'user_role_changed', 'views.subadmin_departments'),
    Pattern('U05', 'User', r"User '.+' profile updated by admin", 'user_profile_updated',
            'views.admin_user_edit'),
    Pattern('U06', 'User', r"User '.+' details edited by System Admin( \(role: .+ → .+\))?",
            'user_profile_updated', 'views.subadmin_departments'),
    Pattern('U07', 'User', r"Password reset for user '.+' by admin", 'user_password_reset',
            'views.admin_user_edit'),
    Pattern('U08', 'User', r"Admin reset password for '.+'", 'user_password_reset',
            'views.admin_reset_password'),
    Pattern('U09', 'User', r"User '.+' deactivated by System Admin", 'user_deactivated',
            'views.subadmin_departments'),
    Pattern('U10', 'User', r"User '.+' reactivated by System Admin", 'user_reactivated',
            'views.subadmin_departments'),
    Pattern('U11', 'User', r"User '.+' \([^()]+\) created by System Admin", 'user_created',
            'views._subadmin_create_user'),
    Pattern('N01', 'Notification', r"Notification pref updated for '.+': .+ set to (ON|OFF)",
            'notification_pref_changed', 'views.admin_notification_prefs'),
    Pattern('N02', 'NotificationEventSetting', r"Notification '.+' \(\w+\) switched \w+: .*",
            'notify_event_switched', 'notify_views.admin_notification_set_state'),
    Pattern('N03', 'NotificationTemplate', r"Notification '.+' \(\w+\) version \d+ activated",
            'notify_template_activated', 'notify_views.admin_notification_draft_activate'),
    Pattern('N04', 'NotificationTemplate', r"Notification '.+' \(\w+\) draft version \d+ \w+",
            'notify_template_draft_discarded', 'notify_views.admin_notification_draft_discard'),
    Pattern('N05', 'NotificationTemplate',
            r"Notification '.+' \(\w+\) version \d+ restored as draft version \d+",
            'notify_template_restored', 'notify_views.admin_notification_version_restore'),
    Pattern('L01', 'Checklist', r"Created checklist '.+' as \S+ v1 \(draft\)", 'checklist_created',
            'views.admin_checklist_create'),
    Pattern('L02', 'Checklist', r"Published checklist '.+' \S+ v\d+.*", 'checklist_published',
            'views.admin_checklist_update'),
    Pattern('L03', 'Checklist', r"Archived checklist '.+' \S+ v\d+", 'checklist_archived',
            'views.admin_checklist_update'),
    Pattern('L04', 'Checklist', r"Renamed checklist \S+ v\d+ to '.+'", 'checklist_renamed',
            'views.admin_checklist_update'),
    Pattern('L05', 'Checklist',
            r"Deleted draft checklist '.+' \(its items and links; it held no answers\)",
            'checklist_deleted', 'views.admin_checklist_delete'),
    Pattern('L06', 'Checklist', r"Added item '.+' to checklist '.+'", 'checklist_item_added',
            'views.admin_checklist_item_add'),
    Pattern('L07', 'Checklist', r"Edited item to '.+' on checklist '.+'", 'checklist_item_edited',
            'views.admin_checklist_item_edit'),
    Pattern('L08', 'Checklist', r"Deleted item '.+' from checklist '.+'", 'checklist_item_deleted',
            'views.admin_checklist_item_delete'),
    Pattern('L09', 'Checklist', r"Reordered item '.+' (up|down) on checklist '.+'",
            'checklist_item_moved', 'views.admin_checklist_item_move'),
    Pattern('L10', 'Checklist', r"Assigned checklist '.+' to task '.+' \(.+\)", 'checklist_linked',
            'views.admin_checklist_link_add'),
    Pattern('L11', 'Checklist', r"Unassigned checklist '.+' from task '.+' \(.+\)",
            'checklist_unlinked', 'views.admin_checklist_link_delete'),
    Pattern('W01', 'StockLocation', r"Created warehouse .+", 'stock_location_created',
            'views.stock_location_create'),
    Pattern('W02', 'StockLocation', r"Updated warehouse .+ \(keeper=.+\)", 'stock_location_updated',
            'views.stock_location_edit'),
    Pattern('W03', 'StockLocation', r"Activated warehouse .+", 'stock_location_activated',
            'views.stock_location_toggle'),
    Pattern('W04', 'StockLocation', r"Deactivated warehouse .+", 'stock_location_deactivated',
            'views.stock_location_toggle'),
    Pattern('G01', 'BOQItemMaster', r"Created BOQ catalogue item '[^']+': .*",
            'boq_catalogue_item_created', 'views admin BOQ catalogue (log_catalogue_change)'),
    Pattern('G02', 'BOQItemMaster', r"Updated BOQ catalogue item '[^']+': .*",
            'boq_catalogue_item_updated', 'views admin BOQ catalogue (log_catalogue_change)'),
    Pattern('G03', 'BOQItemMaster', r"Activated BOQ catalogue item '[^']+': .*",
            'boq_catalogue_item_activated', 'views admin BOQ catalogue (log_catalogue_change)'),
    Pattern('G04', 'BOQItemMaster', r"Deactivated BOQ catalogue item '[^']+': .*",
            'boq_catalogue_item_deactivated', 'views admin BOQ catalogue (log_catalogue_change)'),
    Pattern('G05', 'BOQItemMaster',
            r"normalise_catalogue_units \(system\) updated (RESCO )?BOQ catalogue item '[^']+': .*",
            'boq_catalogue_unit_fixed', 'normalise_catalogue_units'),
    # Historic: these writers now write the code themselves.
    Pattern('H01', 'Task', r"Updated due date for task: .+", 'due_date_changed',
            'task due-date edit before SCHED-1 (now scheduling._log_manual_change)'),
    Pattern('H02', 'Milestone',
            r"Milestone M[123] auto-marked Received via completion of Finance task "
            r"'[^']+' by .+ \([^()]+\)",
            'milestone_updated', 'Finance-task milestone sync before FIN-M-L'),
)
COMPILED = tuple((p, re.compile(p.regex, re.S)) for p in PATTERNS)
_ASCII = str.maketrans({'→': '->', '—': '-', '–': '-', '₹': 'Rs', '…': '...'})


def match(entity_type, action):
    """Every pattern the row matches (entity type equal, regex over the whole text)."""
    return [p for p, rx in COMPILED if p.entity_type == entity_type and rx.fullmatch(action)]


def plan():
    """Read every blank-code row once and sort it: {pattern key: [(pk, ts, et, action)]},
    the unmatched rows by leading words, and the rows whose matches disagree on a code.
    Matching runs in Python because it is per-pattern regex over the whole text; one pass
    over the blank rows (about 2300 on production) is cheap."""
    matched, unmatched, conflicts = defaultdict(list), defaultdict(list), []
    rows = (ActivityLog.objects.filter(action_code='').order_by('timestamp', 'pk')
            .values_list('pk', 'timestamp', 'entity_type', 'action'))
    for row in rows.iterator():
        hits = match(row[2], row[3])
        if not hits:
            unmatched[(row[2], _lead(row[3]))].append(row)
        elif len({p.code for p in hits}) > 1:
            conflicts.append((row, [p.key for p in hits]))
        else:
            matched[hits[0].key].append(row)
    return matched, unmatched, conflicts


def _lead(text):
    """The leading fixed words of a text: up to a colon, quote, bracket or digit."""
    return ' '.join(re.split(r"[:'\"(]|\d", text, maxsplit=1)[0].split()[:5])


def _ascii(text):
    return text.translate(_ASCII).encode('ascii', 'replace').decode('ascii')


class Command(BaseCommand):
    help = ('RPT-C1: give old blank-code ActivityLog rows the code their call site writes '
            'now, by exact text patterns. Dry run unless --apply.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write action_code. Without it nothing is written.')

    def handle(self, *args, **options):
        apply = options['apply']
        db = settings.DATABASES['default']
        self.stdout.write(f"Database: {db.get('HOST') or '(local)'} / {db.get('NAME')}")
        self.stdout.write('MODE: APPLY' if apply else 'MODE: DRY RUN (nothing will be written)')

        matched, unmatched, conflicts = plan()
        total = sum(len(v) for v in matched.values())
        self.stdout.write(f'Blank-code rows: {total + sum(len(v) for v in unmatched.values()) + len(conflicts)}')
        self.stdout.write('')
        for p in PATTERNS:
            rows = matched.get(p.key, [])
            self.stdout.write(f'{p.key}  {len(rows):5}  {p.entity_type} -> {p.code}  [{p.writer}]')
            for pk, ts, _et, action in rows[:SAMPLES]:
                self.stdout.write(f'        #{pk} {ts:%Y-%m-%d %H:%M} {_ascii(action[:100])}')
        self.stdout.write('')
        self.stdout.write(f'TO RELABEL: {total}')
        left = sum(len(v) for v in unmatched.values())
        self.stdout.write(f'UNMATCHED (stay blank): {left}')
        for (et, lead), rows in sorted(unmatched.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            last = max(r[1] for r in rows)
            self.stdout.write(f'  {len(rows):5}  {et or "(none)"}: {_ascii(lead)!r}  '
                              f'last {last:%Y-%m-%d}')

        if conflicts:
            # A row two patterns claim for different codes means a pattern is wrong;
            # writing any of them would be a guess, so the whole run writes nothing.
            self.stdout.write(self.style.ERROR(f'CONFLICTS: {len(conflicts)}. Nothing written.'))
            for (pk, _ts, et, action), keys in conflicts[:20]:
                self.stdout.write(f'  #{pk} {et} {keys} {_ascii(action[:100])}')
            return
        if not apply:
            return

        by_code = defaultdict(list)
        for key, rows in matched.items():
            by_code[next(p.code for p in PATTERNS if p.key == key)].extend(r[0] for r in rows)
        written = Counter()
        for code, pks in sorted(by_code.items()):
            for i in range(0, len(pks), CHUNK):
                # Race: the update re-checks action_code='' in its own WHERE, so a row
                # given a code by anyone between plan() and here is left as it is; the
                # statement touches action_code alone, so nothing else on the row moves.
                written[code] += (ActivityLog.objects
                                  .filter(pk__in=pks[i:i + CHUNK], action_code='')
                                  .update(action_code=code))
        self.stdout.write('')
        for code, n in sorted(written.items()):
            self.stdout.write(f'  wrote {n:5}  {code}')
        self.stdout.write(f'RELABELLED: {sum(written.values())} (planned {total})')
