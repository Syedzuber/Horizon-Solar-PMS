"""
NTF-DCG Session 3b — telling the site's Site Engineers about a delivery challan.

Two registry events, both in-app + WhatsApp (notify_registry, ruling D2):

  dc_created   fired by create_delivery_challan, once, inside its transaction (D4);
  dc_updated   fired by dc_edit's save, inside its transaction, only when the edit
               changed a line or the expected delivery date and the challan is not
               Received or Rejected (D5).

Both go through notify(), so nothing is sent unless the transaction commits, the
WhatsApp leg is deferred to the background pool, and it stays paused until Admin
switches it on after the W7 phone test (Session 3a).

WHAT IS HERE: the recipients rule (D3), the pure value builders (D6-D8) and the two
fire functions. NOT an authority check: who may SEE the challan is still
user_can_view_project(); this only decides who is TOLD, so it does not live in
permissions.py.

NO PRICE EVER. The material summary reads three line columns and nothing else
(SUMMARY_FIELDS); a DCLineItem has no rate or amount column today, and the summary must
not start reading one if it ever gets one — Site Engineers receive this.

Imports: models, notify, utils and whatsapp_params. views is imported lazily (see
site_engineers_for_delivery) because views imports this module.
"""

from decimal import Decimal

from django.urls import reverse

from .models import DeliveryChallan, Task
from .notify import notify
from .permissions import site_assigned_pm
from .utils import applicable_tasks_q
from .whatsapp_params import BODY_VALUE_LIMIT, clean_value

#: The only DCLineItem columns the material summary reads (D6). Never a rate, price or
#: amount: tests_delivery_notify pins this tuple and the SQL the summary's query runs.
SUMMARY_FIELDS = ('item_description', 'ordered_quantity', 'unit')

#: Lines named in the summary before the rest become "+N more items" (D6).
SUMMARY_MAX_LINES = 5

#: The statuses after which an edit is not worth a message: the material has arrived or
#: been refused, so "be ready to receive" would be noise (D5).
QUIET_STATUSES = frozenset({DeliveryChallan.RECEIVED, DeliveryChallan.REJECTED})

DATE_FORMAT = '%d %b %Y'   # "08 Nov 2026"


# ---------------------------------------------------------------------------
# Recipients (D3)
# ---------------------------------------------------------------------------

def site_engineers_for_delivery(project):
    """Who is told about a challan on `project`: every active Site Engineer holding at
    least one OPEN task there — not Done, not Not Applicable (applicable_tasks_q) — and,
    when there is none, the site's assigned PM alone (coordinators are not included).
    A list of UserProfile, de-duplicated (a queryset of profiles cannot repeat one).

    The Site Engineer half is task_assignee_candidates(Task.SITE_ENGINEER, project): the
    one rule for who may hold a Site Engineer task, so the role is matched through the
    task-to-profile mapping and never a second role string here. That rule also leaves
    out the site's QA/QC engineer, who cannot confirm a GRN on the site (CL-A v2.5), so
    they are not told to get ready to receive. Inactive logins are dropped too, so a
    site whose only engineer has left falls back to the PM. Two or three queries.
    """
    # Lazy import to avoid a circular import: views imports this module at load.
    from .views import task_assignee_candidates

    # Profiles holding an open task on this site, as a subquery. Task has no soft
    # delete of its own; the project's is checked by every caller that reached it.
    open_task_holders = (Task.objects
                         .filter(phase__project=project, assigned_to__isnull=False)
                         .filter(applicable_tasks_q())
                         .exclude(status=Task.DONE)
                         .values('assigned_to_id'))
    engineers = list(task_assignee_candidates(Task.SITE_ENGINEER, project)
                     .filter(pk__in=open_task_holders, user__is_active=True))
    if engineers:
        return engineers
    pm = site_assigned_pm(project)
    return [pm] if pm is not None else []


# ---------------------------------------------------------------------------
# Values (D6-D8) — pure
# ---------------------------------------------------------------------------

def quantity_text(quantity):
    """A quantity without trailing zeros: 2000.00 -> "2000", 12.50 -> "12.5". Accepts a
    Decimal or the snapshot's string ("12.00")."""
    value = Decimal(str(quantity)).normalize()
    # normalize() writes 2000 as 2E+3; the 'f' format writes it out in full.
    return format(value, 'f')


def material_summary(lines):
    """D6: `lines` (each (description, quantity, unit), in challan pk order) as one line
    for the message: "<description> <qty> <unit>" joined "; ", at most five, then
    "+N more items" ("+1 more item").

    Q7: the summary never relies on whatsapp_params' mid-value cut. While it would be
    longer than BODY_VALUE_LIMIT, the last named line moves into the "+N more" count.
    Only a single first line that is itself too long is left for that cut (the W4 cap
    stays as the backstop). Parts are measured as sanitise_params() will clean them.
    """
    parts = [clean_value(f'{description} {quantity_text(quantity)} {unit}')
             for description, quantity, unit in lines]

    def text(shown):
        rest = len(parts) - shown
        named = parts[:shown]
        if rest > 0:
            named = named + [f'+{rest} more item{"s" if rest != 1 else ""}']
        return '; '.join(named)

    shown = min(len(parts), SUMMARY_MAX_LINES)
    while shown > 1 and len(text(shown)) > BODY_VALUE_LIMIT:
        shown -= 1
    return text(shown)


def delivery_text(expected_delivery_date, dc_date):
    """D7: "08 Nov 2026"; with no expected date, "Not given (dispatched 04 Nov 2026)"
    from the DC date; with neither, "Not given"."""
    if expected_delivery_date:
        return expected_delivery_date.strftime(DATE_FORMAT)
    if dc_date:
        return f'Not given (dispatched {dc_date.strftime(DATE_FORMAT)})'
    return 'Not given'


def site_code(project):
    """D8: the project ID. On a tender site it IS the site code (OpexSiteForm); a
    Residential project has no site_code, and its ID is what every screen shows."""
    return project.project_id


def site_display_name(project):
    """D8 + Q6: the site's name for the message.

    site_name when set. Otherwise a Residential project is named by its customer (the
    customer's home is the site), and a tender site (OPEX/CAPEX) by "customer, city" —
    customer_name there is the tender's client, shared by every site, so the city is
    what tells one site from another. Blank all round, the project ID.
    """
    name = (project.site_name or '').strip()
    if name:
        return name
    customer = (project.customer_name or '').strip()
    if project.project_type == 'Residential':
        return customer or project.project_id
    parts = [part for part in (customer, (project.city or '').strip()) if part]
    return ', '.join(parts) if parts else project.project_id


def challan_values(project, challan, lines):
    """The registry values of both events for `challan`, its material from `lines`
    ((description, quantity, unit) in pk order). recipient_first_name and link are
    supplied by notify() per person."""
    return {
        'site_code': site_code(project),
        'site_name': site_display_name(project),
        'dc_number': challan.dc_number,
        'material_summary': material_summary(lines),
        'delivery_text': delivery_text(challan.expected_delivery_date, challan.dc_date),
    }


def challan_link(project, challan):
    """The challan page, relative; notify() makes it absolute on APP_BASE_URL (the one
    link-host setting). A Site Engineer with a task on the site may open it."""
    return reverse('delivery_challan_detail', args=[project.project_id, challan.pk])


def lines_from_db(challan):
    """The challan's lines as (description, quantity, unit), pk order (Q5: the order SCM
    entered them, the edit history's order — not Meta's category order). One query,
    reading SUMMARY_FIELDS only."""
    return list(challan.line_items.order_by('pk').values_list(*SUMMARY_FIELDS))


def lines_from_snapshot(snapshot):
    """The same from a DeliveryChallanRevision snapshot (dc_edit schema 1), whose lines
    are already in pk order. No query."""
    return [(line['item_description'], line['ordered_quantity'], line['unit'])
            for line in snapshot['lines']]


# ---------------------------------------------------------------------------
# What counts as a change (D5)
# ---------------------------------------------------------------------------

#: The line fields a Site Engineer receives against; has_receipt is not one of them.
_LINE_FIELDS = ('boq_category', 'item_description', 'ordered_quantity', 'unit')


def is_material_change(before, after):
    """D5: True when a revision's snapshots differ in the lines (one added or removed,
    or a category, description, ordered quantity or unit changed) or in the expected
    delivery date. Header-only edits — vendor, DC number, DC date, warehouse, PO,
    notes, documents — are not. Read from the snapshots, never from the form."""
    if before['header'].get('expected_delivery_date') != after['header'].get('expected_delivery_date'):
        return True
    old = {line['id']: tuple(line[f] for f in _LINE_FIELDS) for line in before['lines']}
    new = {line['id']: tuple(line[f] for f in _LINE_FIELDS) for line in after['lines']}
    return old != new


# ---------------------------------------------------------------------------
# The two fire functions — each called from one place, inside its transaction
# ---------------------------------------------------------------------------

def notify_dc_created(project, challan, actor):
    """D4: dc_created for a challan just written, from create_delivery_challan's
    transaction. notify() sends after commit, so a rolled-back create sends nothing.
    Fired whether or not the site's design is released: R1's warning is about design,
    not delivery."""
    notify('dc_created', recipients=site_engineers_for_delivery(project),
           values=challan_values(project, challan, lines_from_db(challan)),
           actor=actor, related_project=project, link=challan_link(project, challan))


def notify_dc_updated(project, revision, actor):
    """D5: dc_updated for one saved edit, from dc_edit's transaction, or nothing.
    Returns True when the event was fired.

    Silent for a header-only edit and once the challan is Received or Rejected. The
    status is the challan's as the edit left it (revision.challan is the locked row
    recalculate_dc_status() just saved)."""
    challan = revision.challan
    if challan.status in QUIET_STATUSES:
        return False
    if not is_material_change(revision.before, revision.after):
        return False
    notify('dc_updated', recipients=site_engineers_for_delivery(project),
           values=challan_values(project, challan, lines_from_snapshot(revision.after)),
           actor=actor, related_project=project, link=challan_link(project, challan))
    return True
