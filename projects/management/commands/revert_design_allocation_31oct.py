"""ONE-OFF, MPUVNL (program 2) ONLY, written 2026-10-31. Undo the bulk design allocation
of 31 Oct 13:28:41 IST that handed 56 unassigned sites to Anil Gupta.

    python manage.py revert_design_allocation_31oct --dry-run --expect 56
    python manage.py revert_design_allocation_31oct --apply   --expect 56 --actor <username>

WHAT HAPPENED
-------------
At 13:28:41.63-42.26 IST the Design Head posted ONE bulk allocation of 57 sites to Anil
Gupta (UserProfile 31). One of them, MB0003, was the intended reassignment from Sudhanshu
Rawat. The other 56 were every site on the tender still awaiting allocation, ticked in the
browser by mistake. The incident audit (railway_31-oct.dump, taken 13:54) recovered their
before-state completely, and it is the same for all 56: no designer, `awaiting_allocation`,
no due-date commitment, Project.assigned_design empty. Allocation never touches the QC
reviewer, so there is nothing to restore there.

WHAT THIS WRITES, PER SITE
--------------------------
  * the status back to `awaiting_allocation` and the three allocation stamps back to NULL,
    through apply_design_status() — so the StatusTransition row (reason
    `revert_erroneous_allocation`, remark REMARK), the ActivityLog row and the Design
    mirror (In Progress -> Not Started) come from the one chokepoint, exactly as the
    allocation's own rows did
  * DELETES the auto-approved commitment the event created. Retiring it instead would
    leave a fabricated agreement in the history, inflating the "N revision(s)" count and
    the design analytics when the site is properly allocated later (ruled 31 Oct).
  * Project.assigned_design back to NULL

MB0003 IS NEVER TOUCHED. Its reassignment to Anil was intended. It is listed in
PROTECTED_SITES and refused at import time and again at run time.

THE IDS ARE PRODUCTION IDS, HARDCODED ON PURPOSE
------------------------------------------------
Site codes, commitment ids and profile ids were read off the 13:54 dump. Nothing is
looked up by name, the same rule as oneoff_ukru001_link_location_tasks. The tests patch
the constants below.

ABORT ON DRIFT
--------------
Every site is checked before anything is written, and ONE failure aborts the whole run
with no writes. A site that is no longer exactly as the event left it — reallocated, moved
on in the workflow, given an extension or a revised date, or carrying any design attempt —
means someone has acted on it since, and blindly restoring it would undo that work. Those
sites need a person to look at them, not this command. All problems are listed together
so one dry run shows everything that needs attention.

--dry-run IS READ-ONLY. It runs every check and prints the per-site diff, and writes
nothing — not even a rolled-back write, so it holds no locks on production.

REVERSING IT BY HAND
--------------------
Re-allocate the sites from the Design screen. The reverted rows are findable by
ActivityLog.action_code = ACTION_CODE and StatusTransition.reason_code = REASON_CODE.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from projects.design_views import (
    _design_mirror_task, apply_design_status, derive_design_mirror_state,
)
from projects.models import (
    ActivityLog, DesignAssignment, DesignAttempt, DesignSubmission, DueDateCommitment,
    Project, StatusTransition, UserProfile,
    DESIGN_AWAITING_ALLOCATION, DESIGN_IN_DESIGN, SUBJECT_DESIGN_ASSIGNMENT,
)
from projects.permissions import user_has_design_head_authority


IST = ZoneInfo('Asia/Kolkata')

# The event, to the second. The 57 ActivityLog rows fall between 13:28:41.630 and
# 13:28:42.259; the window is padded to whole seconds and to nothing more, so a later
# hand allocation of any of these sites cannot be mistaken for part of the event.
EVENT_START = datetime(2026, 10, 31, 13, 28, 41, tzinfo=IST)
EVENT_END   = datetime(2026, 10, 31, 13, 28, 43, tzinfo=IST)
EVENT_DUE   = date(2026, 11, 2)     # the due date the event wrote on every site

PROGRAM_ID          = 2    # MPUVNL
DESIGNER_PROFILE_ID = 31   # Anil Gupta, the designer the event named
ACTOR_PROFILE_ID    = 17   # Praveen Kethunia, the Design Head who posted it

# site code -> the DueDateCommitment id the event created on it. Production values.
SITES = {
    'MB0023': 56,  'MB0045': 57,  'MB0046': 58,  'MB0053': 59,  'MB0061': 60,
    'MB0062': 61,  'MB0063': 62,  'MB0066': 63,  'MB0069': 64,  'MB0079': 65,
    'MB0088': 66,  'MB0097': 67,  'MB0100': 68,  'MB0108': 69,  'MB0118': 70,
    'MB0119': 71,  'MB0124': 72,  'MB0126': 73,  'MB0127': 74,  'MB0132': 75,
    'MB0133': 76,  'MB0134': 77,  'MB0137': 78,  'MB0138': 79,  'MB0142': 80,
    'MB0144': 81,  'MB0147': 82,  'MB0148': 83,  'MB0154': 84,  'MB0157': 85,
    'MB0158': 86,  'MB0166': 87,  'MB0169': 88,  'MB0174': 89,  'MB0175': 90,
    'MB0177': 91,  'MB0190': 92,  'MB0192': 93,  'MB0196': 94,  'MB0197': 95,
    'MB0198': 96,  'MB0199': 97,  'MB0200': 98,  'MB0201': 99,  'MB0204': 100,
    'MB0205': 101, 'MB0213': 102, 'MB0216': 103, 'MB0218': 104, 'MB0224': 105,
    'MB0225': 106, 'MB0226': 107, 'MB0228': 108, 'MB0231': 109, 'MB0233': 110,
    'MB0238': 111,
}

# In the same POST, and intended. Never reverted.
PROTECTED_SITES = {'MB0003'}

REMARK      = 'Revert erroneous bulk allocation 31-Oct'
REASON_CODE = 'revert_erroneous_allocation'
ACTION_CODE = 'design_allocation_reverted'
ALLOCATION_CODES = ('design_allocated', 'design_reallocated')

assert len(SITES) == 56, 'the event allocated exactly 56 previously unassigned sites'
assert not PROTECTED_SITES & set(SITES), 'MB0003 must never be reverted'


def _name(profile):
    if profile is None:
        return 'none'
    return profile.user.get_full_name() or profile.user.username


class Command(BaseCommand):
    help = ('ONE-OFF for MPUVNL: undo the 31-Oct 13:28 bulk allocation of 56 sites to '
            'Anil Gupta. MB0003 is left alone. Choose --dry-run or --apply.')

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument('--dry-run', action='store_true',
                          help='Check every site and print the diff. Writes nothing.')
        mode.add_argument('--apply', action='store_true',
                          help='Write the revert. Aborts with no writes on any drift.')
        parser.add_argument('--expect', type=int, required=True,
                            help='How many sites you approved reverting. Must equal the '
                                 'number this command would revert.')
        parser.add_argument('--actor', metavar='USERNAME',
                            help='The Design Head the rows name. Required with --apply.')

    def handle(self, *args, **options):
        if options['expect'] != len(SITES):
            raise CommandError(
                f'--expect {options["expect"]} does not match the {len(SITES)} sites this '
                f'command reverts. Nothing was written.')
        # Checked again at run time because the module-level assert can be bypassed by
        # patching SITES, and MB0003 must be impossible to revert whatever the input.
        protected = PROTECTED_SITES & set(SITES)
        if protected:
            raise CommandError(
                f'{", ".join(sorted(protected))} is protected and must never be reverted. '
                f'Nothing was written.')

        actor = None
        if options['apply']:
            actor = self._resolve_actor(options.get('actor'))

        with transaction.atomic():
            plans, problems = self._check(lock=options['apply'])
            if problems:
                raise CommandError(
                    'Drift found - nothing was written. These sites changed after the '
                    'event and need a person to look at them:\n  '
                    + '\n  '.join(problems))

            for plan in plans:
                self._print_diff(plan)

            if options['dry_run']:
                self.stdout.write(self.style.WARNING(
                    f'DRY RUN - {len(plans)} site(s) checked, nothing written.'))
                return

            for plan in plans:
                self._revert(plan, actor)

        self.stdout.write(self.style.SUCCESS(f'Reverted {len(plans)} site(s).'))

    def _resolve_actor(self, username):
        # Only the Design Head (or his deputy) may run the write: allocation is a
        # Head-only action, and every row this writes names the actor as the person who
        # undid a Head decision. Anyone else would be recorded doing what they cannot do
        # from the screen.
        if not username:
            raise CommandError('--actor is required with --apply.')
        try:
            user = User.objects.select_related('profile').get(username=username)
        except User.DoesNotExist:
            raise CommandError(f'No user named {username!r}.')
        if not user.profile.is_active or not user_has_design_head_authority(user):
            raise CommandError(f'{username} is not an active Design Head or deputy.')
        return user.profile

    def _check(self, lock):
        """Return (plans, problems). A plan is everything the revert and the diff need
        for one site; any problem anywhere aborts the whole run."""
        problems = []

        # THE EVENT, FROM THE LEDGER: every allocation row the Head wrote in the window
        # on this tender. The plain allocations must be exactly SITES and the one
        # reallocation exactly the protected site — anything else means this command's
        # constants do not describe the database it is running against.
        event = (ActivityLog.objects
                 .filter(action_code__in=ALLOCATION_CODES, actor_id=ACTOR_PROFILE_ID,
                         project__program_id=PROGRAM_ID,
                         timestamp__gte=EVENT_START, timestamp__lt=EVENT_END)
                 .values_list('action_code', 'project__project_id'))
        allocated   = {code for kind, code in event if kind == 'design_allocated'}
        reallocated = {code for kind, code in event if kind == 'design_reallocated'}
        if allocated != set(SITES):
            missing, extra = set(SITES) - allocated, allocated - set(SITES)
            problems.append(
                f'event mismatch: the ledger shows {len(allocated)} allocation(s) in the '
                f'window, expected {len(SITES)}'
                + (f'; not in the event: {", ".join(sorted(missing))}' if missing else '')
                + (f'; in the event but not listed: {", ".join(sorted(extra))}' if extra else ''))
        if reallocated != PROTECTED_SITES:
            problems.append(
                f'event mismatch: expected the reallocation of '
                f'{", ".join(sorted(PROTECTED_SITES))} in the window, found '
                f'{", ".join(sorted(reallocated)) or "none"}')

        # One query for all 56 assignments, locked on --apply so nobody can act on a
        # site between its check and its write. The dry run takes no lock: it must not
        # hold rows on production while someone reads its output.
        qs = (DesignAssignment.objects
              .filter(project__project_id__in=SITES)
              .select_related('project', 'project__assigned_design__user',
                              'assigned_to__user', 'assigned_by__user', 'qc_assigned_to__user'))
        if lock:
            qs = qs.select_for_update(of=('self',))
        assignments = {a.project.project_id: a for a in qs}

        plans = []
        for code, commitment_id in sorted(SITES.items()):
            a = assignments.get(code)
            if a is None:
                problems.append(f'{code}: no design assignment')
                continue
            site_problems = self._site_problems(code, a, commitment_id)
            problems.extend(site_problems)
            if not site_problems:
                plans.append({'code': code, 'assignment': a, 'commitment_id': commitment_id,
                              'mirror': _design_mirror_task(a.project)})
        return plans, problems

    def _site_problems(self, code, a, commitment_id):
        """Every way this site can differ from the state the event left it in."""
        out = []
        p = a.project
        if p.program_id != PROGRAM_ID or p.project_type != 'OPEX' or p.is_deleted:
            out.append(f'{code}: not a live OPEX site on program {PROGRAM_ID}')
        if a.status != DESIGN_IN_DESIGN:
            out.append(f'{code}: status is {a.status}, expected {DESIGN_IN_DESIGN}')
        if a.assigned_to_id != DESIGNER_PROFILE_ID:
            out.append(f'{code}: designer is {_name(a.assigned_to)}, expected profile '
                       f'{DESIGNER_PROFILE_ID}')
        if a.assigned_by_id != ACTOR_PROFILE_ID or not (
                a.assigned_at and EVENT_START <= a.assigned_at < EVENT_END):
            out.append(f'{code}: allocation stamps are not the event\'s '
                       f'(by {_name(a.assigned_by)} at {a.assigned_at})')
        if p.assigned_design_id != DESIGNER_PROFILE_ID:
            out.append(f'{code}: Project.assigned_design is {_name(p.assigned_design)}, '
                       f'expected profile {DESIGNER_PROFILE_ID}')

        # The event's commitment must be the ONLY one. A second row is an extension
        # request or a revised date made after the event; an earlier one would mean the
        # site had a due date before, so its before-state is not "none".
        rows = list(a.due_date_commitments.values_list(
            'pk', 'proposed_date', 'proposed_by_id', 'approved_by_id', 'is_current',
            'proposed_at'))
        expected_row = [r for r in rows if r[0] == commitment_id]
        if len(rows) != 1 or not expected_row:
            out.append(f'{code}: {len(rows)} due-date commitment(s), expected only #{commitment_id}')
        else:
            _pk, due, by, approver, current, at = expected_row[0]
            if not (due == EVENT_DUE and by == ACTOR_PROFILE_ID and approver == ACTOR_PROFILE_ID
                    and current and EVENT_START <= at < EVENT_END):
                out.append(f'{code}: commitment #{commitment_id} is not the event\'s '
                           f'auto-approved {EVENT_DUE} row')

        # The ledger: the event's own transition must be there, nothing may follow it, and
        # no allocation may precede it (that would mean a designer before the event).
        transitions = StatusTransition.objects.filter(
            subject_type=SUBJECT_DESIGN_ASSIGNMENT, subject_id=a.pk)
        if not transitions.filter(from_status=DESIGN_AWAITING_ALLOCATION,
                                  to_status=DESIGN_IN_DESIGN,
                                  occurred_at__gte=EVENT_START,
                                  occurred_at__lt=EVENT_END).exists():
            out.append(f'{code}: the event\'s awaiting_allocation -> in_design transition is missing')
        if transitions.filter(occurred_at__gte=EVENT_END).exists():
            out.append(f'{code}: status moved after the event')
        if ActivityLog.objects.filter(project=p, action_code__in=ALLOCATION_CODES,
                                      timestamp__lt=EVENT_START).exists():
            out.append(f'{code}: allocated before the event, so its before-state is not "unassigned"')
        if ActivityLog.objects.filter(project=p, action_code__in=ALLOCATION_CODES,
                                      timestamp__gte=EVENT_END).exists():
            out.append(f'{code}: allocated again after the event')

        # Design work started since. Neither exists on a site that has never had a
        # designer, so either one is work this command must not throw away.
        if DesignAttempt.objects.filter(assignment=a).exists():
            out.append(f'{code}: has a design attempt')
        if DesignSubmission.objects.filter(project=p).exists():
            out.append(f'{code}: has a design submission')
        return out

    def _print_diff(self, plan):
        a = plan['assignment']
        mirror = plan['mirror']
        mirror_diff = (f'{mirror.status} -> {derive_design_mirror_state(DESIGN_AWAITING_ALLOCATION)}'
                       if mirror else 'no mirror task (site not activated)')
        qc = _name(a.qc_assigned_to) if a.qc_assigned_to_id else 'open pool'
        self.stdout.write(
            f'{plan["code"]}: status {a.status} -> {DESIGN_AWAITING_ALLOCATION} | '
            f'designer {_name(a.assigned_to)} -> none | '
            f'assigned by {_name(a.assigned_by)} at '
            f'{a.assigned_at.astimezone(IST):%Y-%m-%d %H:%M:%S} -> none | '
            f'due {EVENT_DUE} agreed (#{plan["commitment_id"]}) -> deleted | '
            f'Project.assigned_design {_name(a.project.assigned_design)} -> none | '
            f'mirror {mirror_diff} | QC {qc} (unchanged)')

    def _revert(self, plan, actor):
        a = plan['assignment']
        code = plan['code']
        designer = _name(a.assigned_to)
        apply_design_status(
            a, DESIGN_AWAITING_ALLOCATION, actor,
            f'{REMARK}: designer {designer} removed, back to awaiting allocation',
            ACTION_CODE,
            extra_fields={'assigned_to': None, 'assigned_by': None, 'assigned_at': None},
            reason_code=REASON_CODE, remark=REMARK)

        # Race-condition: filtered on the assignment as well as the pk, and the row count
        # checked, so a commitment that changed hands between check and write aborts the
        # run instead of deleting the wrong row. The assignment row is already locked.
        deleted, _ = DueDateCommitment.objects.filter(
            pk=plan['commitment_id'], assignment=a).delete()
        if deleted != 1:
            raise CommandError(f'{code}: commitment #{plan["commitment_id"]} vanished '
                               f'mid-run. Nothing was written.')

        # Race-condition: filter().update() conditioned on the value the check saw, so a
        # concurrent change to assigned_design is detected (0 rows) rather than
        # overwritten, and the whole run rolls back.
        updated = Project.objects.filter(
            pk=a.project_id, assigned_design_id=DESIGNER_PROFILE_ID,
        ).update(assigned_design=None)
        if updated != 1:
            raise CommandError(f'{code}: Project.assigned_design changed mid-run. '
                               f'Nothing was written.')
