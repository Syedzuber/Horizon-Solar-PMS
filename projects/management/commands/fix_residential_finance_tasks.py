"""FIN-M-B9b (R13), written 2026-11-12. Correct the Finance confirmation tasks on the
older Residential projects that still carry the legacy 'Finance Confirmation' task.

    python manage.py fix_residential_finance_tasks                              # dry run
    python manage.py fix_residential_finance_tasks --apply --actor <username>

WHAT IS WRONG
-------------
Residential projects created before 21 Jul 2026 (commit e9a75ae) were built from a
template with TWO Procurement-phase Finance tasks: 'Finance Confirmation' (order 6),
which carried is_payment_milestone, and 'Pre Dispatch Payment Confirmation' (order 7),
which did not. On 21 Jul the template dropped 'Finance Confirmation' and handed its M2
role to 'Pre Dispatch', moving both the flag and the M2 entry of the task -> milestone
name map. Projects that already existed kept the old flags, so on them:

  * completing 'Pre Dispatch' records M2 (it is in the name map) but alerts nobody
    (no flag), and
  * completing 'Finance Confirmation' alerts Finance, the PMs, BD and the CEO (flag) but
    records nothing (not in the map).

Since late July Finance has been ticking both, seconds apart, so the two halves happened
to add up. This command makes the flags agree with the current template.

WHAT IT CHANGES, PER PROJECT (rulings, Zuber, 12 Nov 2026)
-----------------------------------------------------------
  * 'Pre Dispatch Payment Confirmation': is_payment_milestone off -> on.
  * 'Finance Confirmation':              is_payment_milestone on  -> off.
  * 'Finance Confirmation', when not Done: marked Not Applicable with NA_REASON. A Done
    one keeps its status and history (completed work is not out of scope - the same
    refusal the N/A screen makes).
No task status, no milestone record, no task name and no template is touched.

WHICH PROJECTS
--------------
Every NON-DELETED Residential project (R3, R13) with a task named 'Finance
Confirmation', chosen by rule rather than by id so the dry run on a fresh production
copy shows the true count. A project that does not fit the one known shape - exactly
one 'Finance Confirmation' and exactly one 'Pre Dispatch Payment Confirmation' - is
listed as an OUTLIER and nothing is written to it.

DELIBERATE ONE-OFF SECOND WRITER OF NOT APPLICABLE
---------------------------------------------------
`task_set_not_applicable()` in views.py is the only writer of the N/A flag, and only the
project's PM may use it. This command is a deliberate, ruled one-off second writer
(Zuber, 12 Nov 2026): it makes the same four field writes (flag, appended reason entry
in the same "[dd Mon YYYY HH:MM] Marked not applicable: ..." form, marked_by, marked_at)
and writes the same `task_marked_not_applicable` activity line. It does not call the
view's SCHED-2 reschedule: a 'Finance Confirmation' task has no template link, so it can
never be in a schedule block. Do not copy this into a general N/A tool.

LOGGING
-------
One ActivityLog line per changed field per task, under the --actor:
  * flag changes:  action_code 'task_payment_flag_corrected' (plain, not a money code)
  * N/A marks:     action_code 'task_marked_not_applicable' (the screen's code)

IDEMPOTENT: every change is computed from the row's current value, so a second --apply
finds nothing to do and writes nothing.

REVERSING IT BY HAND
--------------------
The ActivityLog rows with those two codes and "FIN-M-B9b" (flags) or NA_REASON (N/A)
name every task and every old value. Set the flags back, and un-mark N/A through the
task screen (it asks for a reason and appends it).
"""
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from projects.models import ActivityLog, Project, Task, UserProfile, log_activity


LEGACY_TASK = 'Finance Confirmation'
M2_TASK = 'Pre Dispatch Payment Confirmation'
NA_REASON = ('Replaced by Pre Dispatch Payment Confirmation when the Residential '
             'template changed on 21 Jul 2026.')
FLAG_CODE = 'task_payment_flag_corrected'
NA_CODE = 'task_marked_not_applicable'


def _on_off(value):
    return 'on' if value else 'off'


class Command(BaseCommand):
    help = ("FIN-M-B9b: on older non-deleted Residential projects, move the M2 payment "
            "flag from 'Finance Confirmation' to 'Pre Dispatch Payment Confirmation' and "
            "mark open 'Finance Confirmation' tasks Not Applicable. Dry run unless --apply.")

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write the changes. Without it nothing is written.')
        parser.add_argument('--actor', metavar='USERNAME',
                            help='The person the ActivityLog rows name. Required with --apply.')

    def handle(self, *args, **options):
        apply = options['apply']
        db = settings.DATABASES['default']
        self.stdout.write(f"Database: {db.get('HOST') or '(local)'} / {db.get('NAME')}")
        self.stdout.write('MODE: APPLY' if apply else 'MODE: DRY RUN (nothing will be written)')

        if apply and not options['actor']:
            raise CommandError('--apply requires --actor <username>. Nothing was written.')
        actor = self._actor(options['actor']) if options['actor'] else None

        if not apply:
            plans, outliers = self._plan(lock=False)
            self._print(plans, outliers, 'WOULD CHANGE')
            return

        baseline = ActivityLog.objects.order_by('-pk').values_list('pk', flat=True).first() or 0
        # One transaction for the whole run, re-planned under row locks so a task edited
        # between the dry run and here is planned from its value now, not then.
        with transaction.atomic():
            plans, outliers = self._plan(lock=True)
            changed_task_ids = set()
            expected_lines = 0
            now = timezone.now()
            for project, changes in plans:
                for task, field, old, new in changes:
                    self._write(project, task, field, new, actor, now)
                    changed_task_ids.add(task.pk)
                    expected_lines += 1
            self._print(plans, outliers, 'CHANGED')

        logged = ActivityLog.objects.filter(
            pk__gt=baseline, action_code__in=[FLAG_CODE, NA_CODE],
            entity_type='Task', entity_id__in=changed_task_ids,
        ).count()
        self.stdout.write(f'ActivityLog rows written: {logged} (expected {expected_lines}).')
        if logged != expected_lines:
            self.stdout.write(self.style.WARNING(
                f'WARNING: {expected_lines} changes made but {logged} ActivityLog rows '
                f'found. The changes stand; the audit trail is incomplete. Check the '
                f'application log for "ActivityLog failed".'))

    # -- planning -------------------------------------------------------------

    def _plan(self, lock):
        """Return ([(project, [(task, field, old, new), ...]), ...], [outlier text]).

        Projects with nothing left to change are included with an empty list, so the
        totals can say how many were already correct."""
        # Non-deleted Residential projects holding the legacy task (R3, R13). Tasks and
        # phases have no soft-delete of their own; the project's flag is the only one.
        projects = (Project.objects
                    .filter(project_type='Residential', is_deleted=False,
                            phases__tasks__task_name=LEGACY_TASK)
                    .distinct().order_by('project_id'))
        plans, outliers = [], []
        for project in projects:
            # The two tasks this command may touch on this project, locked on --apply
            # so nobody completes or re-flags one between the plan and the write.
            tasks = Task.objects.filter(phase__project=project,
                                        task_name__in=[LEGACY_TASK, M2_TASK])
            if lock:
                tasks = tasks.select_for_update()
            tasks = list(tasks.order_by('pk'))
            legacy = [t for t in tasks if t.task_name == LEGACY_TASK]
            m2 = [t for t in tasks if t.task_name == M2_TASK]
            if len(legacy) != 1 or len(m2) != 1:
                outliers.append(f"{project.project_id}: {len(legacy)} x '{LEGACY_TASK}', "
                                f"{len(m2)} x '{M2_TASK}' (expected 1 of each)")
                continue
            legacy, m2 = legacy[0], m2[0]
            changes = []
            if not m2.is_payment_milestone:
                changes.append((m2, 'is_payment_milestone', False, True))
            if legacy.is_payment_milestone:
                changes.append((legacy, 'is_payment_milestone', True, False))
            if legacy.status != Task.DONE and not legacy.is_not_applicable:
                changes.append((legacy, 'is_not_applicable', False, True))
            plans.append((project, changes))
        return plans, outliers

    # -- writing --------------------------------------------------------------

    def _write(self, project, task, field, new, actor, now):
        if field == 'is_payment_milestone':
            task.is_payment_milestone = new
            task.save(update_fields=['is_payment_milestone'])
            log_activity(
                project, actor,
                f"Payment-milestone flag on '{task.task_name}': {_on_off(not new)} → "
                f"{_on_off(new)} (FIN-M-B9b)",
                entity_type='Task', entity_id=task.pk, action_code=FLAG_CODE,
            )
            return
        # Not Applicable: the same four fields task_set_not_applicable() writes, and its
        # reason format, appended to any earlier history rather than replacing it.
        entry = f"[{now:%d %b %Y %H:%M}] Marked not applicable: {NA_REASON}"
        history = task.not_applicable_reason
        task.is_not_applicable = True
        task.not_applicable_reason = f"{history}\n{entry}".strip() if history else entry
        task.not_applicable_marked_by = actor
        task.not_applicable_marked_at = now
        task.save(update_fields=['is_not_applicable', 'not_applicable_reason',
                                 'not_applicable_marked_by', 'not_applicable_marked_at'])
        log_activity(
            project, actor,
            f"Marked task Not Applicable: {task.task_name} — {NA_REASON}"[:255],
            entity_type='Task', entity_id=task.pk, action_code=NA_CODE,
        )

    # -- output ---------------------------------------------------------------

    def _print(self, plans, outliers, verb):
        # Console output is plain ASCII ('->', not an arrow): a Windows console in
        # cp1252 cannot encode U+2192 and the run would crash mid-report. The
        # ActivityLog text keeps the arrow, as the FIN-M-L milestone lines do.
        changed_projects = flag_changes = na_marks = 0
        for project, changes in plans:
            if not changes:
                continue
            changed_projects += 1
            for task, field, old, new in changes:
                if field == 'is_payment_milestone':
                    flag_changes += 1
                    label = f'payment flag: {_on_off(old)} -> {_on_off(new)}'
                else:
                    na_marks += 1
                    label = f'not applicable: no -> yes (status stays {task.status})'
                self.stdout.write(f'  {project.project_id} | {task.task_name} | {label}')
        for text in outliers:
            self.stdout.write(self.style.WARNING(f'  OUTLIER, not touched: {text}'))
        already = sum(1 for _project, changes in plans if not changes)
        self.stdout.write(
            f'{verb}: {changed_projects} projects, {flag_changes} flag changes, '
            f'{na_marks} Not Applicable marks. Already correct: {already} projects. '
            f'Outliers: {len(outliers)}. Projects checked: {len(plans) + len(outliers)}.')
        if verb == 'WOULD CHANGE':
            self.stdout.write('Dry run: nothing written.')

    # -- validation -----------------------------------------------------------

    def _actor(self, username):
        user = User.objects.filter(username=username).first()
        if user is None:
            raise CommandError(f'--actor {username!r}: no such user. Nothing was written.')
        if not user.is_active:
            raise CommandError(f'--actor {username!r} is inactive. Nothing was written.')
        try:
            profile = user.profile
        except UserProfile.DoesNotExist:
            raise CommandError(f'--actor {username!r} has no UserProfile. Nothing was written.')
        if not profile.is_active:
            raise CommandError(f'--actor {username!r} has an inactive profile. '
                               f'Nothing was written.')
        return profile
