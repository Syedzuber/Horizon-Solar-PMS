"""
Contractor bills 4a-1 — what SCM is warned about when raising a bill.

WARNINGS ARE NEVER BLOCKS. Every function here is pure: it reads, returns a list of
messages for SCM (empty when there is nothing to say), and writes nothing. The raise page
(4a-2) shows them; SCM may raise the bill anyway. What REFUSES a bill is
approvals.create_approval_request(), and nothing here is consulted by it.

  task_complete_for_bill()        D-A33 — the one completion rule for a bill's tasks
  incomplete_task_warnings()      tasks not complete by that rule, and N/A tasks
  other_bill_warnings()           D-A34 — a task already on another counting bill
  repeated_bill_number_warnings() D-A41 — the same contractor's bill number, any time
  site_engineer_warnings()        D-A35 — the Site Engineer holds no task on the site
  pm_warnings()                   the PM named is not the site's assigned PM
  site_engineer_choices()         D-A35 — the Site Engineer list, the site's own first

4a-2 adds three helpers the screens and emails share, none of which changes the above:

  bill_warnings()                 every warning above for one bill, in one list
  task_status_label()             a task's status as a bill reader needs it
  format_bill_amount()            "₹12,500.00" — Indian grouping, paise kept

NOTHING HERE CHECKS THE AMOUNT (B-16). There is no rate contract to check it against.
"""
from decimal import Decimal, InvalidOperation

from .models import (
    ContractorBillDetail, ContractorBillTask, Task, UserProfile,
    APPROVAL_APPROVED, APPROVAL_CHANGES_REQUESTED, APPROVAL_OPEN,
)

#: The bill statuses that count as "another bill" (D-A34) and as an earlier use of a bill
#: number (D-A41): a bill that is still being decided, is waiting for SCM's changes, or
#: was approved. A rejected or withdrawn bill will never be paid, so it is ignored.
COUNTING_BILL_STATUSES = frozenset({
    APPROVAL_OPEN, APPROVAL_CHANGES_REQUESTED, APPROVAL_APPROVED,
})


def _task_label(task):
    """A task as SCM reads it: its name, and where on the site when it is one of several
    per-location copies ("Module Installation — Block A")."""
    if task.location_label:
        return f'{task.task_name} — {task.location_label}'
    return task.task_name


def _person(profile):
    return profile.user.get_full_name() or profile.user.username


def task_complete_for_bill(task, project_type):
    """Is `task` finished, for a contractor bill's warning (D-A33)? True or False.

    OPEX: Done AND approved — an OPEX task is finished by two people (2.1), and Done
    alone is not reachable without the approval, but the approval is what the bill is
    really waiting on. Residential and CAPEX: Done.

    KNOWN, RECORDED, NOT FIXED HERE: reopening an OPEX task (Done -> Blocked) does not
    clear `approved_at`. While it is reopened it is not Done, so this says False; once it
    is taken back to Done it says True on the OLD approval, because the Done gate itself
    passes on that stale stamp (views._apply_task_status_change, rung 1).
    """
    if task.status != Task.DONE:
        return False
    if project_type == 'OPEX':
        return task.approved_at is not None
    return True


def incomplete_task_warnings(project, tasks):
    """A message for each of `tasks` that is marked Not Applicable, or is not complete by
    task_complete_for_bill(). No query: the tasks and the project are already in hand."""
    messages = []
    for task in tasks:
        label = _task_label(task)
        if task.is_not_applicable:
            messages.append(f"'{label}' is marked Not Applicable.")
        elif task.status == Task.DONE and not task_complete_for_bill(task, project.project_type):
            messages.append(f"'{label}' is Done but not yet approved.")
        elif task.status != Task.DONE:
            messages.append(f"'{label}' is not complete — it is {task.status}.")
    return messages


def other_bill_warnings(tasks, exclude=None):
    """A message for each counting bill (COUNTING_BILL_STATUSES) that already names one
    of `tasks` (D-A34). `exclude` is a request to leave out — the bill itself, when 4b
    re-reads a resubmit. One query."""
    # Every link from one of these tasks to a bill still in play, with the bill's detail
    # and request joined in for the message, oldest bill first.
    links = (ContractorBillTask.objects
             .filter(task__in=[task.pk for task in tasks],
                     detail__request__status__in=sorted(COUNTING_BILL_STATUSES))
             .select_related('task', 'detail__request')
             .order_by('detail__request__raised_at', 'pk'))
    if exclude is not None:
        links = links.exclude(detail__request=exclude)
    messages = []
    for link in links:
        request = link.detail.request
        messages.append(
            f"'{_task_label(link.task)}' is also on bill {link.detail.bill_number} — "
            f'"{request.title}" ({request.get_status_display().lower()}).')
    return messages


def repeated_bill_number_warnings(vendor, bill_number, exclude=None):
    """A message for each counting bill from the same contractor carrying the same bill
    number, at any time, whoever raised it (D-A41). Case and surrounding spaces are
    ignored; a blank number never matches. One query."""
    number = (bill_number or '').strip()
    if vendor is None or not number:
        return []
    # The same contractor's bills with this number, still in play, oldest first.
    rows = (ContractorBillDetail.objects
            .filter(request__vendor=vendor, bill_number__iexact=number,
                    request__status__in=sorted(COUNTING_BILL_STATUSES))
            .select_related('request')
            .order_by('request__raised_at', 'pk'))
    if exclude is not None:
        rows = rows.exclude(request=exclude)
    return [f'{vendor.name} already has bill number {row.bill_number} on '
            f'"{row.request.title}" ({row.request.get_status_display().lower()}).'
            for row in rows]


def _site_engineer_ids_on(project):
    """The pks of every profile holding a task on `project` — any status, any phase."""
    return set(Task.objects.filter(phase__project=project, assigned_to__isnull=False)
               .values_list('assigned_to_id', flat=True))


def site_engineer_warnings(project, site_engineer):
    """A message when `site_engineer` holds no task on `project` (D-A35): allowed, but
    they may not have seen the work. One query."""
    if site_engineer is None:
        return []
    if Task.objects.filter(phase__project=project, assigned_to=site_engineer).exists():
        return []
    return [f'{_person(site_engineer)} holds no task on {project.project_id}.']


def pm_warnings(project, pm):
    """A message when `pm` is not the project's assigned PM. No query beyond the
    project already in hand."""
    if pm is None or project.assigned_pm_id == pm.pk:
        return []
    return [f'{_person(pm)} is not the assigned PM on {project.project_id}.']


def site_engineer_choices(project):
    """Every active Site Engineer, those holding a task on `project` first, each group by
    name (D-A35). Returns [(profile, holds_task)]. Two queries."""
    on_site = _site_engineer_ids_on(project)
    # Active Site Engineers, the same set profile_can_be_approval_assignee() admits.
    engineers = (UserProfile.objects.filter(is_active=True, role='Site Engineer')
                 .select_related('user'))
    return sorted(((p, p.pk in on_site) for p in engineers),
                  key=lambda pair: (not pair[1], _person(pair[0]).lower()))


def bill_warnings(project, tasks, vendor, bill_number, site_engineer, pm, exclude=None):
    """Every warning for one bill, in the order SCM reads them: the tasks, other bills
    naming them, the bill number, then the two people. `exclude` is the bill itself when
    its own detail page re-reads them, so it is never "another bill". Three queries."""
    return (incomplete_task_warnings(project, tasks)
            + other_bill_warnings(tasks, exclude=exclude)
            + repeated_bill_number_warnings(vendor, bill_number, exclude=exclude)
            + site_engineer_warnings(project, site_engineer)
            + pm_warnings(project, pm))


def task_status_label(task, project_type):
    """A task's status as a bill's reader needs it: Not Applicable first, then an OPEX
    task's approval beside Done — Done alone does not finish an OPEX task for a bill
    (task_complete_for_bill). No query."""
    if task.is_not_applicable:
        return 'Not Applicable'
    if task.status == Task.DONE and project_type == 'OPEX':
        return 'Done — approved' if task.approved_at is not None else 'Done — not yet approved'
    return task.get_status_display()


def format_bill_amount(amount):
    """A bill amount as people read it: '₹12,500.00', '₹1,23,45,678.50'. Indian grouping
    (lakhs and crores), and the paise KEPT — views._format_inr rounds to whole rupees,
    which is right for a dashboard total and wrong for what a contractor billed. Takes a
    Decimal or a snapshot's string ("12500.00"); anything unreadable is shown as given."""
    try:
        value = Decimal(str(amount)).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError):
        return f'₹{amount}'
    sign = '-' if value < 0 else ''
    rupees, paise = f'{abs(value):.2f}'.split('.')
    head, tail = rupees[:-3], rupees[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return f"{sign}₹{','.join(groups + [tail])}.{paise}"
