"""ONE-OFF, UKRU001 ONLY, written 2026-09-22. Link 18 hand-added location tasks to
their template task so their installation checklist attaches.

    python manage.py oneoff_ukru001_link_location_tasks                            # dry-run
    python manage.py oneoff_ukru001_link_location_tasks --apply --actor <username>

WHAT IS WRONG
-------------
Production project UKRU001 has 18 tasks that were added by hand, one per location, for
the Civil/MMS and Module installation work. Hand-added tasks carry template_task = NULL,
and a checklist is keyed on the template task (ChecklistTaskLink, 0077), so none of the
18 shows its checklist. This sets template_task on exactly those 18 rows and on nothing
else.

THE IDS ARE PRODUCTION IDS, HARDCODED ON PURPOSE
------------------------------------------------
Task ids 2099-2116 and template task ids 60/61 were read off production. Nothing is
looked up by name at runtime, because a name lookup is how the wrong row gets linked
quietly. The ids do not exist locally (there the same template tasks are 110/111), so
the tests patch the constants below. Every id is still checked before anything is
written: the task must be on UKRU001 (OPEX), unlinked, not a mirror or payment
milestone, and named after its target; the target must carry the expected code and
sit on the ACTIVE OPEX template. One failure aborts the whole run with no writes.

Task 2093 (Net Metering approval ESS-1) is also hand-added and is deliberately NOT
linked.

DO NOT GENERALISE THIS. It is not a "link orphan tasks" tool. A general version would
need a matching rule, and a matching rule is exactly the decision this command avoids
making. If another project needs the same repair, write another one-off with that
project's ids.

REVERSING IT BY HAND
--------------------
Set template_task_id back to NULL on the task ids listed in the ActivityLog rows with
action_code='task_template_linked'. Nothing else was changed.
"""
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from projects.models import ActivityLog, Task, TaskTemplate, TaskTemplateTask, UserProfile, log_activity


PROJECT_ID = 'UKRU001'

# task id -> template task id. Production values; see the module docstring.
LINKS = {
    **{task_id: 60 for task_id in range(2099, 2108)},   # Civil Work and MMS Installation
    **{task_id: 61 for task_id in range(2108, 2117)},   # Module Installation
}

# template task id -> the code it must carry. Confirmed on production by direct SQL.
EXPECTED_CODES = {
    60: 'CIVIL_WORK_AND_MMS_INSTALLATION',
    61: 'MODULE_INSTALLATION',
}

# Hand-added on UKRU001 too, and deliberately left alone.
NEVER_LINK = {2093}

ACTION_CODE = 'task_template_linked'

assert len(LINKS) == 18, 'UKRU001 has exactly 18 location tasks to link'
assert not NEVER_LINK & set(LINKS), 'task 2093 must never be linked'
assert set(LINKS.values()) == set(EXPECTED_CODES)


class Command(BaseCommand):
    help = ('ONE-OFF for UKRU001: link 18 hand-added location tasks to template tasks '
            '60/61. Dry-run unless --apply.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write the links. Without it nothing is written.')
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
            self._print_plan(plan, 'WOULD LINK')
            linked = sum(1 for _, _, state in plan if state == 'link')
            self.stdout.write(f'Dry-run: would link {linked}, already linked '
                              f'{len(plan) - linked}, of {len(LINKS)}. Nothing written.')
            return

        baseline = ActivityLog.objects.order_by('-pk').values_list('pk', flat=True).first() or 0
        with transaction.atomic():
            plan = self._validate(lock=True)
            linked_ids = []
            for task, target, state in plan:
                if state != 'link':
                    continue
                old_id = task.template_task_id
                task.template_task = target
                task.save(update_fields=['template_task'])
                linked_ids.append(task.pk)
                # log_activity swallows its own errors. On Postgres a failed INSERT would
                # still poison this transaction and take the links down with it, so each
                # row gets its own savepoint: a lost log line must not cost a link.
                with transaction.atomic():
                    log_activity(
                        task.phase.project, actor,
                        f"Linked to template task {target.pk} ({target.code}); "
                        f"was {old_id or 'none'}",
                        entity_type='Task', entity_id=task.pk, action_code=ACTION_CODE,
                    )
            self._print_plan(plan, 'LINKED')

        logged = ActivityLog.objects.filter(
            pk__gt=baseline, action_code=ACTION_CODE,
            entity_type='Task', entity_id__in=linked_ids,
        ).count()
        self.stdout.write(f'Applied: linked {len(linked_ids)}, already linked '
                          f'{len(plan) - len(linked_ids)}, of {len(LINKS)}. '
                          f'ActivityLog rows written: {logged}.')
        if logged != len(linked_ids):
            self.stdout.write(self.style.WARNING(
                f'WARNING: {len(linked_ids)} tasks linked but {logged} ActivityLog rows '
                f'found. The links stand; the audit trail is incomplete. Check the '
                f'application log for "ActivityLog failed".'))

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
        """Check every row. Returns [(task, target, 'link' | 'already')] or raises
        CommandError listing every failure, having written nothing."""
        errors = []

        targets = {}
        for tt_id, code in EXPECTED_CODES.items():
            tt = TaskTemplateTask.objects.select_related('phase__template').filter(pk=tt_id).first()
            if tt is None:
                errors.append(f'template task {tt_id}: does not exist')
                continue
            template = tt.phase.template
            if tt.code != code:
                errors.append(f'template task {tt_id}: code is {tt.code!r}, expected {code!r}')
            if template.project_type != 'OPEX' or template.status != TaskTemplate.ACTIVE:
                errors.append(f'template task {tt_id}: on template {template} '
                              f'({template.project_type}), not the active OPEX template')
            targets[tt_id] = tt

        tasks = Task.objects.select_related('phase__project', 'template_task').filter(pk__in=LINKS)
        if lock:
            tasks = tasks.select_for_update(of=('self',))
        tasks = {t.pk: t for t in tasks}

        plan = []
        for task_id, tt_id in sorted(LINKS.items()):
            task = tasks.get(task_id)
            if task is None:
                errors.append(f'task {task_id}: does not exist')
                continue
            project = task.phase.project
            if project.project_id != PROJECT_ID:
                errors.append(f'task {task_id}: on project {project.project_id}, not {PROJECT_ID}')
            if project.project_type != 'OPEX':
                errors.append(f'task {task_id}: project type is {project.project_type!r}, not OPEX')
            if task.is_mirror:
                errors.append(f'task {task_id}: is a mirror task')
            if task.is_payment_milestone:
                errors.append(f'task {task_id}: is a payment milestone')

            target = targets.get(tt_id)
            if target is not None and not task.task_name.startswith(target.label):
                errors.append(f'task {task_id}: name {task.task_name!r} does not start with '
                              f'{target.label!r}')

            if task.template_task_id is None:
                state = 'link'
            elif task.template_task_id == tt_id:
                state = 'already'
            else:
                errors.append(f'task {task_id}: already linked to template task '
                              f'{task.template_task_id}, not {tt_id}')
                continue
            plan.append((task, target, state))

        if errors:
            for line in errors:
                self.stderr.write(f'  FAIL {line}')
            raise CommandError(f'{len(errors)} check(s) failed. Nothing was written.')
        return plan

    def _print_plan(self, plan, verb):
        for task, target, state in plan:
            label = verb if state == 'link' else 'ALREADY LINKED'
            self.stdout.write(f'  {label:<14} task {task.pk} {task.task_name!r} -> '
                              f'template task {target.pk} ({target.code})')
