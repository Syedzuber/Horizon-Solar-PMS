"""
Static display constants for the Residential Gantt feature (Phase 1.5).

TEMPORARY HARDCODE — the task/phase names and their order below mirror
utils.build_residential_phases() exactly. When the Project Templates initiative
ships (admin-editable task lists), these maps must be replaced with template-driven
lookups, or they will silently desync the moment a task/phase is renamed or reordered.
The Gantt desync guard test asserts every key here still exists in the template.

Keys are the EXACT internal strings; any unmapped key falls back to the internal
name at render time (never a blank label). Tone: third person.
"""

# Fallback only — runtime reads SystemSettings.gantt_client_buffer_days (admin-editable).
GANTT_PHASE_BUFFER_DAYS = 3

# Internal ProjectPhase.phase_name -> client-facing band label.
GANTT_PHASE_DISPLAY_NAME_MAP = {
    'Sales & Documentation':       'Order & Documentation',
    'Detail Engineering Visit':    'Site Survey',
    'Design':                      'System Design',
    'Pre-Installation Approvals':  'Approvals & Permits',
    'Procurement':                 'Procurement',
    'Delivery':                    'Material Delivery',
    'Installation':                'Installation',
    'Commissioning':               'Commissioning & Handover',
    'Finance Closure':             'Project Closure',
}

# Distinct bar colour per phase, keyed by phase_order (1-based). Cycles if more phases appear.
GANTT_PHASE_COLORS = {
    1: '#f0a829',  # gold
    2: '#e07a5f',  # terracotta
    3: '#3d7ea6',  # blue
    4: '#8e6cb0',  # violet
    5: '#1a7a4a',  # brand green
    6: '#4a9c8c',  # teal
    7: '#c0562b',  # rust
    8: '#5a7d9a',  # slate blue
    9: '#6b8e23',  # olive
}

# Internal Task.task_name -> client-facing label.
# ACRONYM ASSUMPTIONS TO VERIFY (may differ for this DISCOM/process): LC/PC/NC = electricity-board
# clearances; TFR = Technical Feasibility Report/approval; SCO = utility Sanction/Supply Connection
# Order; "B & C Class Items" = Balance of System (cables/connectors/minor items).
GANTT_TASK_DISPLAY_NAME_MAP = {
    # Phase 1 — Sales & Documentation
    'OCR, Documentation & Verification':    'Order Confirmation & Documentation',
    'Send Invoice - Advance Payment':       'Advance Payment Invoice',
    'Advance Payment Confirmation':         'Advance Payment Received',

    # Phase 2 — Detail Engineering Visit
    'DEV Schedule':                         'Site Survey Scheduled',
    'DEV Conduct':                          'Site Survey Visit',
    'DEV Data to Design':                   'Site Data Handover to Design',
    'DEV Inputs Validation':                'Site Data Validation',

    # Phase 3 — Design
    'Design':                               'System Design',
    'Array Layout':                         'Solar Panel Layout',
    'SLD':                                  'Electrical Design (Single-Line Diagram)',
    'Installation Drawings':                'Installation Drawings',
    'BOQ Preparation':                      'Material & Equipment List',
    'Design Approval by Internal Team':     'Design Quality Review',
    'Design Approval by Customer':          'Design Sign-off (Customer)',            # EXT

    # Phase 4 — Pre-Installation Approvals
    'Pre Installation Approvals':           'Pre-Installation Approvals',
    'LC / PC / NC Required':                'Electricity Board Clearances',          # EXT
    'Vendor Registration':                  'Utility Vendor Registration',           # EXT
    'Document Preparation':                 'Application Document Preparation',
    'Signing Document by Customer':         'Document Signing (Customer)',           # EXT
    'Net Metering Application Submission':  'Net Metering Application',
    'TFR Received':                         'Technical Feasibility Approval (TFR)',  # EXT

    # Phase 5 — Procurement
    'Procurement Schedule':                 'Procurement Planning',
    'PO Placed MMS':                        'Mounting Structure Ordered',
    'PO Placed Module':                     'Solar Panels Ordered',
    'PO Placed Inverter':                   'Inverter Ordered',
    'PO for B & C Class Items':             'Balance of System Ordered',
    'Send Invoice - Material Supply':       'Material Supply Invoice',
    'Pre Dispatch Payment Confirmation':    'Pre-Dispatch Payment Received',

    # Phase 6 — Delivery
    'Delivery Schedule':                    'Delivery Planning',
    'Delivery of MMS':                      'Mounting Structure Delivered',
    'Delivery of B & C Class Items':        'Balance of System Delivered',
    'Delivery of Module':                   'Solar Panels Delivered',
    'Delivery of Inverter':                 'Inverter Delivered',

    # Phase 7 — Installation
    'MMS Installation':                     'Mounting Structure Installation',
    'Earthing Work':                        'Earthing & Safety Work',
    'Module Installation':                  'Solar Panel Installation',
    'Inverter Installation':                'Inverter Installation',
    'DC Wire Work':                         'DC Wiring',
    'AC Cable Work':                        'AC Cabling',
    'Connections and Voc Testing':          'Electrical Connections & Testing',
    'Pre Commissioning Check List':         'Pre-Commissioning Inspection',          # milestone

    # Phase 8 — Commissioning
    'Pre Commissioning Visit by DISCOM':    'Utility Inspection Visit',              # EXT
    'Meter Testing':                        'Meter Testing',
    'SCO Release':                          'Utility Sanction (SCO)',                # EXT
    'Meter Installation by DISCOM':         'Net Meter Installation (Utility)',      # EXT
    'RMS Configuration':                    'Remote Monitoring Setup',
    'Plant Commissioning':                  'System Commissioning',
    'Commissioning Report Prepared':        'Commissioning Report',
    'Commissioning Report Approved':        'Commissioning Sign-off',                # milestone
    'Customer Handover':                    'Handover to Customer',                  # milestone
    'Send Invoice - Final Payment':         'Final Payment Invoice',

    # Phase 9 — Finance Closure
    '100% Payment Confirmation':            'Final Payment Received',
}
