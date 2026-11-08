"""SCHED-2 — seed the RESCO duration table for the OPEX Execution v1 template.

Six blocks from the PMs' table (spec Layer 2; block 6 = Testing & Commissioning only,
by Zuber's pre-flight ruling: Net Meter Installation stays unscheduled).

LINKED BY TEMPLATE TASK CODE, NEVER BY NAME. A label can be reworded in a later version;
the code is the cross-version identity (TaskTemplateTask.code). The lookup also pins
the template's code and version, because CAPEX v1 (0128) carries the SAME task codes.

FAILS LOUDLY. A missing template or a missing / duplicated code raises and the whole
migration rolls back: a silently skipped block would make every RESCO site's schedule
shorter than the PMs' table with nothing saying so. On a fresh database 0075 has seeded
the template by the time this runs, so the only way to fail is a real data problem.

Reverse deletes exactly these six blocks (their task links cascade).
"""
from decimal import Decimal

from django.db import migrations

OPEX_TEMPLATE_CODE = 'OPEX'
OPEX_TEMPLATE_VERSION = 1

# (sequence, name, task codes, days <=50, days 51-100, days 101+)
SCHEDULE_BLOCKS = [
    (1, 'Civil & MMS', ['CIVIL_WORK_AND_MMS_INSTALLATION'], '3.5', '7', '11'),
    (2, 'Modules', ['MODULE_INSTALLATION'], '1.5', '4', '5'),
    (3, 'Earthing & DC', ['LA_AND_EARTHING_INSTALLATION', 'DC_CABLE_LAYING_WITH_CONDUIT'],
     '1.5', '2.5', '5'),
    (4, 'Inverter & DBs', ['DCDB_AND_ACDB_INSTALLATION', 'INVERTER_INSTALLATION'], '1', '1', '1'),
    (5, 'AC cable', ['AC_CABLE_LAYING'], '0.5', '0.5', '2'),
    (6, 'Testing', ['TESTING_COMMISSIONING'], '0.5', '1', '1'),
]


def seed_schedule_blocks(apps, schema_editor):
    TaskTemplate = apps.get_model('projects', 'TaskTemplate')
    TaskTemplateTask = apps.get_model('projects', 'TaskTemplateTask')
    ScheduleBlock = apps.get_model('projects', 'ScheduleBlock')
    ScheduleBlockTask = apps.get_model('projects', 'ScheduleBlockTask')

    try:
        template = TaskTemplate.objects.get(code=OPEX_TEMPLATE_CODE,
                                            version_no=OPEX_TEMPLATE_VERSION)
    except TaskTemplate.DoesNotExist:
        raise RuntimeError(
            f'[0138] Template {OPEX_TEMPLATE_CODE} v{OPEX_TEMPLATE_VERSION} not found; '
            f'cannot seed the schedule blocks.')

    for sequence, name, codes, le_50, mid, plus in SCHEDULE_BLOCKS:
        # Resolve every code BEFORE creating the block, so a bad code never leaves a
        # half-built block behind even inside the migration's transaction.
        template_tasks = []
        for code in codes:
            matches = list(TaskTemplateTask.objects.filter(phase__template=template, code=code))
            if len(matches) != 1:
                raise RuntimeError(
                    f'[0138] Block {sequence} ({name}): template task code {code!r} matched '
                    f'{len(matches)} rows in {OPEX_TEMPLATE_CODE} v{OPEX_TEMPLATE_VERSION}; '
                    f'expected exactly 1.')
            template_tasks.append(matches[0])
        block = ScheduleBlock.objects.create(
            template=template, sequence=sequence, name=name,
            days_le_50=Decimal(le_50), days_51_100=Decimal(mid), days_101_plus=Decimal(plus),
        )
        for template_task in template_tasks:
            ScheduleBlockTask.objects.create(block=block, template_task=template_task)

    print(f'  [0138] Seeded {len(SCHEDULE_BLOCKS)} schedule blocks for '
          f'{OPEX_TEMPLATE_CODE} v{OPEX_TEMPLATE_VERSION}.')


def unseed_schedule_blocks(apps, schema_editor):
    TaskTemplate = apps.get_model('projects', 'TaskTemplate')
    ScheduleBlock = apps.get_model('projects', 'ScheduleBlock')
    template = TaskTemplate.objects.filter(code=OPEX_TEMPLATE_CODE,
                                           version_no=OPEX_TEMPLATE_VERSION).first()
    if template is None:
        return
    ScheduleBlock.objects.filter(
        template=template, sequence__in=[row[0] for row in SCHEDULE_BLOCKS],
    ).delete()   # cascades to the ScheduleBlockTask links


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0137_sched2_schedule_blocks'),
    ]

    operations = [
        migrations.RunPython(seed_schedule_blocks, unseed_schedule_blocks),
    ]
