"""ONE-OFF, UKRU001 ONLY, written 2026-09-22. Set location_label on the 18 location
tasks that oneoff_ukru001_link_location_tasks linked to their template tasks.

    python manage.py oneoff_ukru001_set_location_labels                            # dry-run
    python manage.py oneoff_ukru001_set_location_labels --apply --actor <username>

WHAT IS WRONG
-------------
Production project UKRU001 has 18 hand-added location tasks, one per location for the
Civil/MMS and Module installation work. They predate location_label (0090), so the
location lives only in the task name, as the suffix after the template label
("Module Installation Food Court"). This copies that suffix into location_label on
exactly those 18 rows. The name is left as it is.

THE IDS ARE PRODUCTION IDS, HARDCODED ON PURPOSE
------------------------------------------------
Task ids 2099-2116 and template task ids 60/61 were read off production, and so were
the labels. Nothing is parsed out of a name at runtime: the label is written from
LABELS below and the name is only checked against it. The ids do not exist locally, so
the tests patch the constants. Every row is checked before anything is written: the
task must be on UKRU001, linked to its expected template task (which must carry the
expected code), named exactly template label + " " + location label, and have an empty
location_label. One failure aborts the whole run with no writes.

Because an already-set label fails the empty check, a second --apply aborts rather
than skipping. That is deliberate: this runs once.

DO NOT GENERALISE THIS. If another project needs the same repair, write another
one-off with that project's ids.

REVERSING IT BY HAND
--------------------
Set location_label back to '' on the task ids listed in the ActivityLog rows with
action_code='task_location_label_set'. Nothing else was changed.
"""
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from projects.models import ActivityLog, Task, TaskTemplateTask, UserProfile, log_activity


PROJECT_ID = 'UKRU001'

_LOCATIONS = ['Ticket Plaza 1', 'Ticket Plaza 2', 'Toilet Block 4', 'Parking-3',
              'Toilet Block 5', 'Toilet Block 6', 'ESS-2', 'Food Court', 'Library Block']

# task id -> (template task id, location label). Production values; see the docstring.
LABELS = {
    **{2099 + i: (60, loc) for i, loc in enumerate(_LOCATIONS)},   # Civil Work and MMS Installation
    **{2108 + i: (61, loc) for i, loc in enumerate(_LOCATIONS)},   # Module Installation
}

# template task id -> the code it must carry. Same rows as the link one-off.
EXPECTED_CODES = {
    60: 'CIVIL_WORK_AND_MMS_INSTALLATION',
    61: 'MODULE_INSTALLATION',
}

ACTION_CODE = 'task_location_label_set'

assert len(LABELS) == 18, 'UKRU001 has exactly 18 location tasks to label'
assert set(LABELS) == set(range(2099, 2117))
assert {tt for tt, _ in LABELS.values()} == set(EXPECTED_CODES)


class Command(BaseCommand):
    help = ('ONE-OFF for UKRU001: set location_label on 18 location tasks '
            '(2099-2116). Dry-run unless --apply.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write the labels. Without it nothing is written.')
        parser.add_argument('--actor', metavar='USERNAME',
                            help='The person the ActivityLog rows name. Required with --apply.')

    def handle(self, *args, **options):
        apply = options['apply']
        db = settings.DATABASES['default']
        self.stdout.write(f"Database: {db.get('HOST') or '(local)'} / {db.get('NAME')}")
        self.stdout.write('MODE: APPLY' if apply else 'MODE: DRY-RUN (nothing will be written)')

        if apply and not options['actor']:
            raise CommandError('--apply requires --actor <username>. Nothing was written.')
        actor = self._actor(options['actor']) if options['actor'] else None

        if not apply:
            plan = self._validate(lock=False)
            self._print_plan(plan, 'WOULD SET')
            self.stdout.write(f'Dry-run: would set {len(plan)} of {len(LABELS)}. '
                              f'Nothing written.')
            return

        baseline = ActivityLog.objects.order_by('-pk').values_list('pk', flat=True).first() or 0
        with transaction.atomic():
            plan = self._validate(lock=True)
            for task, label in plan:
                task.location_label = label
                task.save(update_fields=['location_label'])
                # log_activity swallows its own errors. On Postgres a failed INSERT would
                # still poison this transaction and take the labels down with it, so each
                # row gets its own savepoint: a lost log line must not cost a label.
                with transaction.atomic():
                    log_activity(
                        task.phase.project, actor,
                        f"Location label set to {label!r}; was ''",
                        entity_type='Task', entity_id=task.pk, action_code=ACTION_CODE,
                    )
            self._print_plan(plan, 'SET')

        set_ids = [task.pk for task, _ in plan]
        logged = ActivityLog.objects.filter(
            pk__gt=baseline, action_code=ACTION_CODE,
            entity_type='Task', entity_id__in=set_ids,
        ).count()
        self.stdout.write(f'Applied: set {len(set_ids)} of {len(LABELS)}. '
                          f'ActivityLog rows written: {logged}.')
        if logged != len(set_ids):
            self.stdout.write(self.style.WARNING(
                f'WARNING: {len(set_ids)} labels set but {logged} ActivityLog rows found. '
                f'The labels stand; the audit trail is incomplete. Check the application '
                f'log for "ActivityLog failed".'))

    # -- validation -------------------------------------------------------------

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

    def _validate(self, lock):
        """Check every row. Returns [(task, label)] or raises CommandError listing every
        failure, having written nothing."""
        errors = []

        targets = {}
        for tt_id, code in EXPECTED_CODES.items():
            tt = TaskTemplateTask.objects.filter(pk=tt_id).first()
            if tt is None:
                errors.append(f'template task {tt_id}: does not exist')
                continue
            if tt.code != code:
                errors.append(f'template task {tt_id}: code is {tt.code!r}, expected {code!r}')
            targets[tt_id] = tt

        tasks = Task.objects.select_related('phase__project').filter(pk__in=LABELS)
        if lock:
            tasks = tasks.select_for_update(of=('self',))
        tasks = {t.pk: t for t in tasks}

        plan = []
        for task_id, (tt_id, label) in sorted(LABELS.items()):
            task = tasks.get(task_id)
            if task is None:
                errors.append(f'task {task_id}: does not exist')
                continue
            project = task.phase.project
            if project.project_id != PROJECT_ID:
                errors.append(f'task {task_id}: on project {project.project_id}, not {PROJECT_ID}')
            if task.template_task_id != tt_id:
                errors.append(f'task {task_id}: template task is {task.template_task_id}, '
                              f'expected {tt_id}')
            target = targets.get(tt_id)
            if target is not None:
                expected_name = f'{target.label} {label}'
                if task.task_name != expected_name:
                    errors.append(f'task {task_id}: name {task.task_name!r}, '
                                  f'expected {expected_name!r}')
            if task.location_label != '':
                errors.append(f'task {task_id}: location_label is already '
                              f'{task.location_label!r}')
            plan.append((task, label))

        if errors:
            for line in errors:
                self.stderr.write(f'  FAIL {line}')
            raise CommandError(f'{len(errors)} check(s) failed. Nothing was written.')
        return plan

    def _print_plan(self, plan, verb):
        for task, label in plan:
            self.stdout.write(f'  {verb:<9} task {task.pk} {task.task_name!r} -> '
                              f'location_label {label!r}')
