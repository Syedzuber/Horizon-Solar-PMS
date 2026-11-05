"""
READ-ONLY export of real site data for the Site Dashboard / Tenders Gantt prototype.

    python manage.py export_dashboard_prototype --out dashboard_data.json

Writes nothing to the database. Produces one JSON file in the shape the standalone
HTML prototype loads ("Load export" button), so the UX can be judged on real sites
before anything is retrofitted into Django.

PERSONAL DATA IS LEFT OUT ON PURPOSE: no customer name, phone, email or address.
Residential sites are identified by project_id + city only. Contract values are not
exported (CEO Tenders rule, 28 Sep 2026) — only open payment-request amounts.

Excluded sites: is_deleted, is_test, Cancelled.

"Latest activity" = newest ActivityLog row for the site, ignoring pure reads
(anything *_opened, logins/logouts) and system noise listed in IGNORED_CODES.
Contractor attendance never writes ActivityLog, so it is excluded by construction;
it is exported separately as "logged today or not".
"""
import json
from collections import defaultdict
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Min, Prefetch, Sum
from django.utils import timezone

from projects.models import (
    ActivityLog, ContractorAttendance, DeliveryChallan, Issue, PaymentRequest,
    Program, Project, StatusTransition, Task,
)
from projects.utils import current_phase, is_applicable, is_human_owned

IGNORED_CODES = {
    'user_login', 'user_logout', 'notify_template_seeded', 'cad_pdf_extraction_failed',
}
FEED_LEN = 5
NEXT_LEN = 3


def _name(profile):
    if profile is None:
        return 'System'
    u = profile.user
    return (u.get_full_name() or u.username).strip()


def _iso(dt):
    return dt.isoformat() if dt else None


def _is_update(row):
    code = row.action_code or ''
    return not (code in IGNORED_CODES or code.endswith('_opened'))


class Command(BaseCommand):
    help = 'Read-only JSON export of real site data for the dashboard prototype.'

    def add_arguments(self, parser):
        parser.add_argument('--out', default='dashboard_data.json')

    def handle(self, *args, **opts):
        today = timezone.localdate()
        now = timezone.now()

        sites = (
            Project.objects
            .filter(is_deleted=False, is_test=False)
            .exclude(status='Cancelled')
            .select_related('program', 'assigned_pm__user')
            .prefetch_related(Prefetch(
                'phases__tasks', queryset=Task.objects.order_by('task_order'),
            ))
        )
        site_ids = [s.id for s in sites]

        # Actual task start = first transition into In Progress or Done.
        task_start = dict(
            StatusTransition.objects
            .filter(subject_type='task', project_id__in=site_ids,
                    to_status__in=[Task.IN_PROGRESS, Task.DONE])
            .values_list('subject_id')
            .annotate(first=Min('occurred_at'))
        )

        open_issues = defaultdict(int)
        for pid in (Issue.objects.filter(project_id__in=site_ids,
                                         status__in=[Issue.OPEN, Issue.IN_PROGRESS])
                    .values_list('project_id', flat=True)):
            open_issues[pid] += 1

        dc = defaultdict(lambda: {'expected': 0, 'partial': 0, 'received': 0})
        dc_key = {DeliveryChallan.EXPECTED: 'expected',
                  DeliveryChallan.PARTIALLY_RECEIVED: 'partial',
                  DeliveryChallan.RECEIVED: 'received'}
        for pid, st in DeliveryChallan.objects.filter(project_id__in=site_ids).values_list('project_id', 'status'):
            if st in dc_key:
                dc[pid][dc_key[st]] += 1

        # PaymentRequest.project is a display anchor (lowest site on the order), so this
        # figure is indicative per site, not an exact attribution.
        pay = dict(
            PaymentRequest.objects
            .filter(project_id__in=site_ids, status__in=[
                PaymentRequest.PENDING_APPROVAL, PaymentRequest.APPROVED, PaymentRequest.ON_HOLD])
            .values_list('project')
            .annotate(total=Sum('amount'))
        )

        att = dict(
            ContractorAttendance.objects
            .filter(project_id__in=site_ids, date=today, is_voided=False)
            .values_list('project')
            .annotate(total=Sum('headcount'))
        )

        feed = defaultdict(list)
        logs = (ActivityLog.objects.filter(project_id__in=site_ids)
                .select_related('actor__user').order_by('-timestamp'))
        for row in logs.iterator(chunk_size=2000):
            lst = feed[row.project_id]
            if len(lst) >= FEED_LEN or not _is_update(row):
                continue
            lst.append({'at': _iso(row.timestamp), 'who': _name(row.actor), 'what': row.action})

        out_sites = []
        for s in sites:
            phases, total_all, done_all, overdue, blocked, upcoming = [], 0, 0, 0, 0, []
            for ph in s.phases.all():
                tasks = [t for t in ph.tasks.all() if is_applicable(t)]
                human = [t for t in tasks if is_human_owned(t)]
                done = [t for t in tasks if t.status == Task.DONE]
                starts = [task_start[t.id] for t in tasks if t.id in task_start]
                starts += [t.completed_at for t in done if t.completed_at]
                dues = [t.due_date for t in tasks if t.due_date]
                plan_starts = [t.due_date - timedelta(days=t.duration_days or 1) for t in tasks if t.due_date]
                ph_over = sum(1 for t in human if t.status != Task.DONE and t.due_date and t.due_date < today)
                ph_blk = sum(1 for t in human if t.status == Task.BLOCKED)
                all_done = bool(tasks) and len(done) == len(tasks)
                phases.append({
                    'name': ph.phase_name,
                    'total': len(tasks), 'done': len(done),
                    'start': _iso(min(starts)) if starts else None,
                    'end': _iso(max(t.completed_at for t in done if t.completed_at)) if all_done and any(t.completed_at for t in done) else None,
                    'plan_start': min(plan_starts).isoformat() if plan_starts else None,
                    'plan_end': max(dues).isoformat() if dues else None,
                    'overdue': ph_over, 'blocked': ph_blk,
                })
                total_all += len(tasks); done_all += len(done)
                overdue += ph_over; blocked += ph_blk
                upcoming += [t for t in human if t.status != Task.DONE and t.due_date]
            upcoming.sort(key=lambda t: t.due_date)
            cur = current_phase(s) if s.status != 'Draft' else None
            pm = s.assigned_pm
            f = feed.get(s.id, [])
            out_sites.append({
                'id': s.id,
                'project_id': s.project_id,
                'code': s.site_code or s.project_id,
                'name': (s.site_name or '') if s.project_type != 'Residential' else '',
                'city': s.city,
                'type': s.project_type,
                'status': s.status,
                'program_id': s.program_id,
                'pm': _name(pm) if pm else None,
                'kwp': float(s.dc_capacity_kw) if s.dc_capacity_kw is not None else None,
                'created': _iso(s.created_at),
                'activated': _iso(s.activated_at),
                'target': s.target_commissioning_date.isoformat() if s.target_commissioning_date else None,
                'progress': round(done_all * 100 / total_all) if total_all else 0,
                'overdue': overdue, 'blocked': blocked,
                'issues': open_issues.get(s.id, 0),
                'pay_pending': float(pay.get(s.id) or 0),
                'dc': dc.get(s.id, {'expected': 0, 'partial': 0, 'received': 0}),
                'att_today': att.get(s.id),          # null = not logged today
                'current_phase': cur.phase_name if cur else None,
                'phases': phases,
                'last': f[0] if f else None,
                'feed': f,
                'next': [{'name': t.task_name, 'due': t.due_date.isoformat()} for t in upcoming[:NEXT_LEN]],
            })

        programs = [{'id': p.id, 'name': p.name, 'code': p.short_tender_code or p.name,
                     'type': p.program_type}
                    for p in Program.objects.filter(id__in={s['program_id'] for s in out_sites if s['program_id']})]

        payload = {'generated_at': _iso(now), 'today': today.isoformat(),
                   'programs': programs, 'sites': out_sites}
        with open(opts['out'], 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, ensure_ascii=False)
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {len(out_sites)} sites, {len(programs)} programs to {opts['out']}"))
