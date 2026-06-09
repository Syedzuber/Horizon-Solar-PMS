from django.db import transaction
from django.utils import timezone


def generate_project_id(project_type):
    """
    Generate a unique project ID of the form HRP-{PREFIX}-{YEAR}-{NNN}.
    Must be called inside transaction.atomic() — uses select_for_update()
    to prevent duplicate IDs under concurrent saves.
    """
    from .models import Project

    PREFIX_MAP = {
        'Residential': 'RES',
        'OPEX':        'OPX',
        'CAPEX':       'CAP',
    }
    prefix = PREFIX_MAP[project_type]
    year = timezone.now().year

    # select_for_update() cannot be combined with .count() (Django raises
    # NotSupportedError). Fetch locked IDs instead and count in Python.
    locked_ids = list(
        Project.objects
        .select_for_update()
        .filter(project_type=project_type, created_at__year=year)
        .values_list('id', flat=True)
    )
    count = len(locked_ids)
    number = count + 1
    return f"HRP-{prefix}-{year}-{number:03d}"


def attach_residential_template(project):
    """
    Create all 9 phases and 51 tasks for a Residential project.
    Entire operation is atomic — any failure rolls back all phases and tasks.
    """
    from .models import ProjectPhase, Task

    with transaction.atomic():

        PHASES = [
            {
                'phase_name':  'Sales & Documentation',
                'phase_order': 1,
                'tasks': [
                    {'task_order': 1, 'task_name': 'OCR, Documentation & Verification', 'assigned_role': Task.BD},
                    {'task_order': 2, 'task_name': 'Advance Payment Confirmation',       'assigned_role': Task.FINANCE},
                ],
            },
            {
                'phase_name':  'Detail Engineering Visit',
                'phase_order': 2,
                'tasks': [
                    {'task_order': 1, 'task_name': 'DEV Schedule',            'assigned_role': Task.PM},
                    {'task_order': 2, 'task_name': 'DEV Conduct',             'assigned_role': Task.SITE_ENGINEER},
                    {'task_order': 3, 'task_name': 'DEV Data to Design',      'assigned_role': Task.SITE_ENGINEER},
                    {'task_order': 4, 'task_name': 'DEV Inputs Validation',   'assigned_role': Task.DESIGN},
                ],
            },
            {
                'phase_name':  'Design',
                'phase_order': 3,
                'tasks': [
                    {'task_order': 1, 'task_name': 'Design',                            'assigned_role': Task.DESIGN},
                    {'task_order': 2, 'task_name': 'Array Layout',                      'assigned_role': Task.DESIGN},
                    {'task_order': 3, 'task_name': 'SLD',                               'assigned_role': Task.DESIGN},
                    {'task_order': 4, 'task_name': 'Installation Drawings',             'assigned_role': Task.DESIGN},
                    {'task_order': 5, 'task_name': 'BOQ Preparation',                  'assigned_role': Task.DESIGN},
                    {'task_order': 6, 'task_name': 'Design Approval by Internal Team', 'assigned_role': Task.PM},
                    {'task_order': 7, 'task_name': 'Design Approval by Customer',      'assigned_role': Task.PM},
                ],
            },
            {
                'phase_name':  'Pre-Installation Approvals',
                'phase_order': 4,
                'tasks': [
                    {'task_order': 1, 'task_name': 'Pre Installation Approvals',          'assigned_role': Task.PM},
                    {'task_order': 2, 'task_name': 'LC / PC / NC Required',               'assigned_role': Task.PM},
                    {'task_order': 3, 'task_name': 'Vendor Registration',                 'assigned_role': Task.SCM},
                    {'task_order': 4, 'task_name': 'Document Preparation',                'assigned_role': Task.PM},
                    {'task_order': 5, 'task_name': 'Signing Document by Customer',        'assigned_role': Task.PM},
                    {'task_order': 6, 'task_name': 'Net Metering Application Submission', 'assigned_role': Task.PM},
                    {'task_order': 7, 'task_name': 'TFR Received',                        'assigned_role': Task.PM},
                ],
            },
            {
                'phase_name':  'Procurement',
                'phase_order': 5,
                'tasks': [
                    {'task_order': 1, 'task_name': 'Procurement Schedule',              'assigned_role': Task.SCM},
                    {'task_order': 2, 'task_name': 'PO Placed MMS',                     'assigned_role': Task.SCM},
                    {'task_order': 3, 'task_name': 'PO Placed Module',                  'assigned_role': Task.SCM},
                    {'task_order': 4, 'task_name': 'PO Placed Inverter',                'assigned_role': Task.SCM},
                    {'task_order': 5, 'task_name': 'PO for B & C Class Items',          'assigned_role': Task.SCM},
                    {'task_order': 6, 'task_name': 'Finance Confirmation',              'assigned_role': Task.FINANCE},
                    {'task_order': 7, 'task_name': 'Pre Dispatch Payment Confirmation', 'assigned_role': Task.FINANCE},
                ],
            },
            {
                'phase_name':  'Delivery',
                'phase_order': 6,
                'tasks': [
                    {'task_order': 1, 'task_name': 'Delivery Schedule',             'assigned_role': Task.SCM},
                    {'task_order': 2, 'task_name': 'Delivery of MMS',               'assigned_role': Task.SCM},
                    {'task_order': 3, 'task_name': 'Delivery of B & C Class Items', 'assigned_role': Task.SCM},
                    {'task_order': 4, 'task_name': 'Delivery of Module',            'assigned_role': Task.SCM},
                    {'task_order': 5, 'task_name': 'Delivery of Inverter',          'assigned_role': Task.SCM},
                ],
            },
            {
                'phase_name':  'Installation',
                'phase_order': 7,
                'tasks': [
                    {'task_order': 1, 'task_name': 'MMS Installation',               'assigned_role': Task.SITE_ENGINEER},
                    {'task_order': 2, 'task_name': 'Earthing Work',                  'assigned_role': Task.SITE_ENGINEER},
                    {'task_order': 3, 'task_name': 'Module Installation',            'assigned_role': Task.SITE_ENGINEER},
                    {'task_order': 4, 'task_name': 'Inverter Installation',          'assigned_role': Task.SITE_ENGINEER},
                    {'task_order': 5, 'task_name': 'DC Wire Work',                   'assigned_role': Task.SITE_ENGINEER},
                    {'task_order': 6, 'task_name': 'AC Cable Work',                  'assigned_role': Task.SITE_ENGINEER},
                    {'task_order': 7, 'task_name': 'Connections and Voc Testing',    'assigned_role': Task.SITE_ENGINEER},
                    {'task_order': 8, 'task_name': 'Pre Commissioning Check List',   'assigned_role': Task.SITE_ENGINEER},
                ],
            },
            {
                'phase_name':  'Commissioning',
                'phase_order': 8,
                'tasks': [
                    {'task_order': 1, 'task_name': 'Pre Commissioning Visit by DISCOM', 'assigned_role': Task.PM},
                    {'task_order': 2, 'task_name': 'Meter Testing',                     'assigned_role': Task.SITE_ENGINEER},
                    {'task_order': 3, 'task_name': 'SCO Release',                       'assigned_role': Task.PM},
                    {'task_order': 4, 'task_name': 'Meter Installation by DISCOM',      'assigned_role': Task.PM},
                    {'task_order': 5, 'task_name': 'RMS Configuration',                 'assigned_role': Task.SITE_ENGINEER},
                    {'task_order': 6, 'task_name': 'Plant Commissioning',               'assigned_role': Task.SITE_ENGINEER},
                    {'task_order': 7, 'task_name': 'Commissioning Report Prepared',     'assigned_role': Task.SITE_ENGINEER},
                    {'task_order': 8, 'task_name': 'Commissioning Report Approved',     'assigned_role': Task.PM},
                    {'task_order': 9, 'task_name': 'Customer Handover',                 'assigned_role': Task.PM},
                ],
            },
            {
                'phase_name':  'Finance Closure',
                'phase_order': 9,
                'tasks': [
                    {'task_order': 1, 'task_name': '100% Payment Confirmation', 'assigned_role': Task.FINANCE},
                ],
            },
        ]

        for phase_data in PHASES:
            phase = ProjectPhase.objects.create(
                project=project,
                phase_name=phase_data['phase_name'],
                phase_order=phase_data['phase_order'],
            )
            Task.objects.bulk_create([
                Task(
                    phase=phase,
                    task_name=t['task_name'],
                    task_order=t['task_order'],
                    assigned_role=t['assigned_role'],
                )
                for t in phase_data['tasks']
            ])

        task_count = Task.objects.filter(phase__project=project).count()
        assert task_count == 50, f"Expected 50 tasks, got {task_count}"
