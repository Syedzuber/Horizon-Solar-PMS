"""List every stored date outside the typed-date range. Read-only.

    python manage.py list_implausible_dates

The range is the one forms.check_typed_date() enforces on input: TYPED_DATE_FLOOR to
five years from today. It covers every DateField on the models — the nine a view takes
from a person, the five only Django admin can type, and the eight the system or a
calculation writes (two of those, the effective_from stamps, are also editable in Django
admin) — because a bad typed date spreads: a cascade from a year-26 anchor writes
year-26 dates onto the tasks after it and year-26 rows into DueDateChangeLog.

IT CORRECTS NOTHING. The existing bad rows are fixed on screen by their owners.

READ-ONLY BY CONSTRUCTION, so it can run against a read-only connection: SELECTs only,
no get_or_create, no SystemSettings read (SystemSettings.get() creates its row when it
is missing), no logging to the database. It prints the database host and name first
and never the password.

THE CEILING MOVES WITH THE CALENDAR. A date that is in range today can fall outside
it later, and nothing re-checks stored rows, so the range used is printed with the
results.
"""
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q

from projects import models as M
from projects.forms import TYPED_DATE_FLOOR, typed_date_ceiling


# (model, field, path to Project.project_id or None). Every DateField in models.py.
FIELDS = [
    ('Project',          'survey_date',               'project_id'),
    ('Project',          'target_commissioning_date', 'project_id'),
    ('Project',          'commissioned_at',           'project_id'),
    ('Program',          'expected_completion_date',  None),
    ('Program',          'award_date',                None),
    ('Program',          'ppa_signed_date',           None),
    ('Task',             'due_date',                  'phase__project__project_id'),
    ('Milestone',        'due_date',                  'project__project_id'),
    ('Milestone',        'completed_date',            'project__project_id'),
    ('DueDateChangeLog', 'old_date',                  'task__phase__project__project_id'),
    ('DueDateChangeLog', 'new_date',                  'task__phase__project__project_id'),
    ('PaymentMilestone', 'due_date',                  'project__project_id'),
    ('PaymentMilestone', 'invoice_date',              'project__project_id'),
    ('PaymentMilestone', 'received_date',             'project__project_id'),
    ('Issue',            'due_date',                  'project__project_id'),
    ('DeliveryChallan',  'dc_date',                   'project__project_id'),
    ('DeliveryChallan',  'expected_delivery_date',    'project__project_id'),
    ('DCLineItem',       'grn_date',                  'challan__project__project_id'),
    ('PaymentRequest',   'payment_date',              'project__project_id'),
    ('TaskTemplate',     'effective_from',            None),
    ('Checklist',        'effective_from',            None),
    ('DueDateCommitment', 'proposed_date',            'assignment__project__project_id'),
]


class Command(BaseCommand):
    help = 'List stored dates outside the typed-date range (read-only).'

    def handle(self, *args, **options):
        db = settings.DATABASES['default']
        self.stdout.write(f"DB host: {db.get('HOST') or '(local socket)'}  name: {db.get('NAME')}")

        floor, ceiling = TYPED_DATE_FLOOR, typed_date_ceiling()
        self.stdout.write(f'Range: {floor.isoformat()} .. {ceiling.isoformat()} '
                          f'(the ceiling is today + 5 years and moves daily)')

        rows = []
        for model_name, field, project_path in FIELDS:
            model = getattr(M, model_name)
            # _base_manager: soft-deleted rows are listed too — a bad date on a deleted
            # project is still a bad date if the project is restored.
            qs = (model._base_manager
                  .filter(Q(**{f'{field}__lt': floor}) | Q(**{f'{field}__gt': ceiling}))
                  .order_by('pk'))
            values = ['pk', field] + ([project_path] if project_path else [])
            for rec in qs.values_list(*values):
                project_id = rec[2] if project_path else None
                rows.append((model_name, rec[0], project_id or '-', field, rec[1].isoformat()))

        self.stdout.write(f"{'model':<18} {'id':>7}  {'project_id':<22} {'field':<26} value")
        for model_name, pk, project_id, field, value in rows:
            self.stdout.write(f'{model_name:<18} {pk:>7}  {project_id:<22} {field:<26} {value}')
        self.stdout.write(f'{len(rows)} implausible date(s) across {len(FIELDS)} field(s).')
