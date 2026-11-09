"""SCHED-2c — the two meter blocks for the OPEX Execution v1 template (spec "Meter blocks
(9 Nov 2026)").

    6  RMS & Gen meter  RMS_INSTALLATION + SOLAR_GENERATION_METER_INSTALLATION  0.5 / 0.5 / 0.5
    7  Testing          (moved here from 6, unchanged)
    8  Net meter        NET_METER_INSTALLATION                                  0.5 / 1 / 1

A NEW MIGRATION, NOT AN EDIT OF 0138: 0138 may already have run on production, and an
edited migration that has already been applied never runs again.

LINKED BY TEMPLATE TASK CODE, NEVER BY NAME, for the reasons in 0138 (labels change
between versions; CAPEX v1 carries the same codes, so the template is pinned too).

FAILS LOUDLY. Every precondition is checked before the first write: the template exists,
Testing sits at sequence 6 and nothing is at 7 or 8, and each code matches exactly one
non-mirror template task that is in no block yet. Anything else raises and the migration
rolls back, because a half-applied reorder would put Testing in the wrong place on every
RESCO site with nothing saying so.

WRITES NO TASK DATES. Sites pick up the new blocks the next time they are triggered or
when `schedule_sites --apply` is run; a backfill here would bypass the activity log the
scheduler writes for every change.

ORDER OF STEPS: (template, sequence) is unique, so Testing leaves 6 before block 6 is
created, and the reverse deletes 6 and 8 before Testing moves back.
"""
from decimal import Decimal

from django.db import migrations

OPEX_TEMPLATE_CODE = 'OPEX'
OPEX_TEMPLATE_VERSION = 1

TESTING_CODE = 'TESTING_COMMISSIONING'
TESTING_OLD_SEQUENCE = 6
TESTING_NEW_SEQUENCE = 7

# (sequence, name, task codes, days <=50, days 51-100, days 101+)
METER_BLOCKS = [
    (6, 'RMS & Gen meter', ['RMS_INSTALLATION', 'SOLAR_GENERATION_METER_INSTALLATION'],
     '0.5', '0.5', '0.5'),
    (8, 'Net meter', ['NET_METER_INSTALLATION'], '0.5', '1', '1'),
]


def _template(apps):
    TaskTemplate = apps.get_model('projects', 'TaskTemplate')
    try:
        return TaskTemplate.objects.get(code=OPEX_TEMPLATE_CODE, version_no=OPEX_TEMPLATE_VERSION)
    except TaskTemplate.DoesNotExist:
        raise RuntimeError(
            f'[0139] Template {OPEX_TEMPLATE_CODE} v{OPEX_TEMPLATE_VERSION} not found; '
            f'cannot add the meter blocks.')


def _testing_block(ScheduleBlock, template, sequence):
    """The Testing block at `sequence`, checked to be the one 0138 seeded (linked to
    TESTING_COMMISSIONING only), so a hand-edited table is refused rather than reordered."""
    block = ScheduleBlock.objects.filter(template=template, sequence=sequence).first()
    codes = sorted(block.block_tasks.values_list('template_task__code', flat=True)) if block else []
    if codes != [TESTING_CODE]:
        raise RuntimeError(
            f'[0139] Expected the Testing block ({TESTING_CODE} only) at sequence {sequence}; '
            f'found {codes or "no block"}.')
    return block


def add_meter_blocks(apps, schema_editor):
    TaskTemplateTask = apps.get_model('projects', 'TaskTemplateTask')
    ScheduleBlock = apps.get_model('projects', 'ScheduleBlock')
    ScheduleBlockTask = apps.get_model('projects', 'ScheduleBlockTask')

    template = _template(apps)
    testing = _testing_block(ScheduleBlock, template, TESTING_OLD_SEQUENCE)
    taken = list(ScheduleBlock.objects.filter(
        template=template, sequence__in=[TESTING_NEW_SEQUENCE, 8]).values_list('sequence', flat=True))
    if taken:
        raise RuntimeError(f'[0139] Sequence(s) {sorted(taken)} already in use; expected free.')

    # Resolve every code BEFORE the first write, so a bad code leaves nothing half done.
    resolved = []
    for sequence, name, codes, le_50, mid, plus in METER_BLOCKS:
        template_tasks = []
        for code in codes:
            matches = list(TaskTemplateTask.objects.filter(phase__template=template, code=code))
            if len(matches) != 1:
                raise RuntimeError(
                    f'[0139] Block {sequence} ({name}): template task code {code!r} matched '
                    f'{len(matches)} rows in {OPEX_TEMPLATE_CODE} v{OPEX_TEMPLATE_VERSION}; '
                    f'expected exactly 1.')
            task = matches[0]
            # The scheduler never writes a mirror, so a mirror in a block would be a block
            # that silently schedules nothing.
            if task.is_mirror:
                raise RuntimeError(f'[0139] Block {sequence} ({name}): {code!r} is a mirror task.')
            if ScheduleBlockTask.objects.filter(template_task=task).exists():
                raise RuntimeError(f'[0139] Block {sequence} ({name}): {code!r} is already in a block.')
            template_tasks.append(task)
        resolved.append((sequence, name, le_50, mid, plus, template_tasks))

    testing.sequence = TESTING_NEW_SEQUENCE
    testing.save(update_fields=['sequence'])

    for sequence, name, le_50, mid, plus, template_tasks in resolved:
        block = ScheduleBlock.objects.create(
            template=template, sequence=sequence, name=name,
            days_le_50=Decimal(le_50), days_51_100=Decimal(mid), days_101_plus=Decimal(plus),
        )
        for template_task in template_tasks:
            ScheduleBlockTask.objects.create(block=block, template_task=template_task)

    print(f'  [0139] Testing moved to block {TESTING_NEW_SEQUENCE}; added blocks '
          f'{", ".join(f"{s} ({n})" for s, n, *_ in METER_BLOCKS)} for '
          f'{OPEX_TEMPLATE_CODE} v{OPEX_TEMPLATE_VERSION}.')


def remove_meter_blocks(apps, schema_editor):
    ScheduleBlock = apps.get_model('projects', 'ScheduleBlock')

    template = _template(apps)
    testing = _testing_block(ScheduleBlock, template, TESTING_NEW_SEQUENCE)
    # Delete by sequence AND name, so a block someone added at 6 or 8 by hand is not
    # silently taken out; it then blocks the Testing move below with a unique error.
    for sequence, name, *_ in METER_BLOCKS:
        ScheduleBlock.objects.filter(template=template, sequence=sequence, name=name).delete()
        # cascades to the ScheduleBlockTask links
    testing.sequence = TESTING_OLD_SEQUENCE
    testing.save(update_fields=['sequence'])


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0138_sched2_seed_schedule_blocks'),
    ]

    operations = [
        migrations.RunPython(add_meter_blocks, remove_meter_blocks),
    ]
