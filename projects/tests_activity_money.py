"""
AF-1 — money lines in the activity feed, and the one money audience for a project page.

WHAT IS PINNED HERE
-------------------
  audience  user_can_see_project_money(): CEO, Admin, System Admin, Finance, SCM, and
            whoever manages the site (assigned PM, coordinators). Not a site engineer,
            the site's QA/QC engineer (even one whose role is PM — CL-I), Design or BD.
  card      the vendor-orders card on project_overview follows the helper, role by role
  feeds     outside the audience, no MONEY_ACTIVITY_CODES row on the Recent activity
            card or project_timeline, excluded in the query: the card still fills to 8
            and timeline page 1 is full with the paginator's count excluding them
  Q4        set_milestone_amounts writes 'milestone_amounts_set'; migration 0121 stamps
            the old blank-code rows (and reverses); a Residential SE sees neither
  tripwire  a static scan of every log_activity() call: a description carrying money
            must be written under a code in MONEY_ACTIVITY_CODES, resolvable statically,
            and every code in the set must still be written by some money call. Fails
            closed; the exception list is empty.
  queries   the helper costs nothing for a money portfolio role, one lookup otherwise

Run with:
    python manage.py test projects.tests_activity_money --settings=solarpms.test_settings
"""
import ast
import functools
import importlib
import json
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .activity_codes import MONEY_ACTIVITY_CODES
from .models import ActivityLog, Program, Project, ProjectPhase, SiteQaqcAssignment, Task
from .permissions import user_can_see_project_money

MIGRATION = importlib.import_module('projects.migrations.0121_activity_milestone_amounts_code')


# ---------------------------------------------------------------------------
# The tripwire: a static scan of every log_activity() call
# ---------------------------------------------------------------------------

#: What a money amount looks like in a description's SOURCE: the rupee sign, or a field
#: or word that carries one. Matched against the expression's source text, so an
#: f-string interpolating `pr.amount` or `cleaned['total']` is caught as surely as a
#: literal "₹".
MONEY_RE = re.compile(r'₹|\bamounts?\b|\btotal\b|\bpaid\b|effective_amount|approved_amount'
                      r'|\bprice\b', re.IGNORECASE)

#: Money call sites allowed to sit outside MONEY_ACTIVITY_CODES, each with a reason. AF-1
#: (Q4) requires it EMPTY; an entry here is a decision, not a fix.
EXEMPT = frozenset()

PROJECTS_DIR = Path(__file__).resolve().parent


def _project_sources():
    """{relative path: source} for every module the product runs: not tests, not
    migrations (which write no feed lines)."""
    sources = {}
    for path in sorted(PROJECTS_DIR.rglob('*.py')):
        if path.name.startswith('tests') or 'migrations' in path.parts:
            continue
        sources[str(path.relative_to(PROJECTS_DIR.parent))] = path.read_text(
            encoding='utf-8-sig')
    return sources


def _callee(node):
    return getattr(node.func, 'id', None) or getattr(node.func, 'attr', None)


def _params(fn):
    if fn is None:
        return []
    return [a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs]


def scan_money_log_calls(sources):
    """Every log_activity() call whose description carries money, as a list of
    (site, codes): `site` is 'path:line' of the call that supplies the money text, and
    `codes` the set of action codes it can be written under, or None when that cannot be
    resolved statically (which the caller treats as a failure — fail closed).

    Direct calls are judged on their own description. A function that passes one of its
    PARAMETERS into the description is a wrapper (_log_payment, punch_points._log, ...):
    its call sites are judged instead, with the wrapper's action_code resolved through the
    arguments they pass — a literal, a parameter bound to a literal, or a module-level
    dict such as PAYMENT_ACTION_CODES indexed by either. Wrappers of wrappers are followed
    the same way. A description built in a local variable is followed one assignment back.
    """
    modules = {}
    for name, src in sources.items():
        tree = ast.parse(src)
        parents = {child: node for node in ast.walk(tree)
                   for child in ast.iter_child_nodes(node)}
        dicts = {}
        for node in tree.body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Dict)):
                try:
                    dicts[node.targets[0].id] = ast.literal_eval(node.value)
                except ValueError:
                    pass
        modules[name] = {'src': src, 'tree': tree, 'parents': parents, 'dicts': dicts,
                         'lines': src.splitlines(),
                         'calls': [n for n in ast.walk(tree) if isinstance(n, ast.Call)]}
    # Every call, by the name it calls, so each wrapper's call sites are one lookup rather
    # than a walk of every module.
    calls_by_name = {}
    for name, mod in modules.items():
        for call in mod['calls']:
            calls_by_name.setdefault(_callee(call), []).append((name, mod, call))

    def enclosing(mod, node):
        while node in mod['parents']:
            node = mod['parents'][node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node
        return None

    def seg(mod, node):
        """ast.get_source_segment() without re-splitting the file on every call. Column
        offsets are UTF-8 byte offsets, hence the encode/decode."""
        if getattr(node, 'end_lineno', None) is None:
            return ''
        lines = [line.encode() for line in
                 mod['lines'][node.lineno - 1:node.end_lineno]]
        if len(lines) == 1:
            return lines[0][node.col_offset:node.end_col_offset].decode()
        lines[0] = lines[0][node.col_offset:]
        lines[-1] = lines[-1][:node.end_col_offset]
        return b'\n'.join(lines).decode()

    def is_money(mod, expr, fn):
        if expr is None:
            return False
        if MONEY_RE.search(seg(mod, expr)):
            return True
        # One assignment back, for a description built in a local variable.
        names = {n.id for n in ast.walk(expr) if isinstance(n, ast.Name)} - set(_params(fn))
        if fn is None or not names:
            return False
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                targets = [node.target.id]
            else:
                continue
            if names & set(targets) and MONEY_RE.search(seg(mod, node.value)):
                return True
        return False

    def value(mod, expr, env):
        """The set of strings `expr` can be, or None if it cannot be told statically."""
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return {expr.value}
        if isinstance(expr, ast.Name) and expr.id in env:
            return env[expr.id]()
        if (isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Name)
                and expr.value.id in mod['dicts']):
            table = mod['dicts'][expr.value.id]
            keys = value(mod, expr.slice, env)
            if keys is None:
                # Any key: every value of the table must then pass.
                return {v for v in table.values() if isinstance(v, str)} or None
            if not keys <= set(table):
                return None
            return {table[k] for k in keys}
        return None

    def bind(call, params):
        bound = dict(zip(params, call.args))
        bound.update({k.arg: k.value for k in call.keywords if k.arg})
        return bound

    def site(name, node):
        return f'{name}:{node.lineno}'

    findings = {}
    sinks = []   # (function name, params, desc params, code resolver)
    for name, mod, call in calls_by_name.get('log_activity', []):
        desc = (call.args[2] if len(call.args) > 2
                else next((k.value for k in call.keywords if k.arg == 'action'), None))
        code = next((k.value for k in call.keywords if k.arg == 'action_code'),
                    call.args[5] if len(call.args) > 5 else None)
        fn = enclosing(mod, call)
        if is_money(mod, desc, fn):
            findings[site(name, call)] = (
                value(mod, code, {}) if code is not None else None)
        # Every enclosing function whose parameter flows into the description is a
        # wrapper — the nearest one and, for a closure, the outer one too.
        outer = fn
        while outer is not None:
            forwarded = ({n.id for n in ast.walk(desc) if isinstance(n, ast.Name)}
                         & set(_params(outer))) if desc is not None else set()
            if forwarded:
                sinks.append((outer.name, _params(outer), forwarded,
                              lambda env, m=mod, c=code: (
                                  value(m, c, env) if c is not None else None)))
            outer = enclosing(mod, outer)

    seen = set()
    depth = 0
    while sinks and depth < 6:
        depth += 1
        next_sinks = []
        for fname, params, desc_params, resolve in sinks:
            if (fname, frozenset(desc_params)) in seen:
                continue
            seen.add((fname, frozenset(desc_params)))
            for name, mod, call in calls_by_name.get(fname, []):
                bound = bind(call, params)
                caller = enclosing(mod, call)
                env = {p: (lambda e=expr, m=mod: value(m, e, {}))
                       for p, expr in bound.items()}
                args = [bound[p] for p in desc_params if p in bound]
                if any(is_money(mod, a, caller) for a in args):
                    findings[site(name, call)] = resolve(env)
                forwarded = {n.id for a in args for n in ast.walk(a)
                             if isinstance(n, ast.Name)} & set(_params(caller))
                if forwarded:
                    def resolve_up(env_up, bound=bound, mod=mod, resolve=resolve):
                        env2 = {p: (lambda e=expr: value(mod, e, env_up))
                                for p, expr in bound.items()}
                        return resolve(env2)
                    next_sinks.append((caller.name, _params(caller), forwarded,
                                       resolve_up))
        sinks = next_sinks
    return sorted(findings.items())


@functools.lru_cache(maxsize=1)
def _real_findings():
    """The scan of the real codebase, once per test run."""
    return tuple(scan_money_log_calls(_project_sources()))


def tripwire_failures(findings, codes=MONEY_ACTIVITY_CODES, exempt=EXEMPT):
    """(failures, written): the money sites whose code is unresolved or outside `codes`,
    and the union of codes the resolved ones are written under."""
    failures, written = [], set()
    for where, found in findings:
        if where in exempt:
            continue
        if found is None:
            failures.append(f'{where}: money description, action_code cannot be resolved')
        elif not found <= codes:
            failures.append(f'{where}: money description under {sorted(found - codes)}, '
                            f'not in MONEY_ACTIVITY_CODES')
        else:
            written |= found
    return failures, written


class MoneyCodeTripwireTests(SimpleTestCase):

    def test_exception_list_is_empty(self):
        self.assertEqual(EXEMPT, frozenset())

    def test_every_money_line_is_under_a_money_code(self):
        findings = _real_findings()
        failures, _written = tripwire_failures(findings)
        self.assertEqual(failures, [], '\n'.join(failures))
        # Non-vacuous: the scan really reaches the known money writers, including the
        # four that go through _log_payment and the bill payment in approval_views.
        self.assertGreaterEqual(len(findings), 12)
        sites = ' '.join(where for where, _ in findings)
        for module in ('order_views.py', 'payments.py', 'approval_views.py', 'views.py'):
            self.assertIn(module, sites)

    def test_every_money_code_is_still_written(self):
        _failures, written = tripwire_failures(_real_findings())
        self.assertEqual(MONEY_ACTIVITY_CODES - written, set(),
                         'codes in MONEY_ACTIVITY_CODES that no money call writes')

    # -- the scanner itself fails closed ------------------------------------------

    def _scan(self, src):
        return tripwire_failures(scan_money_log_calls({'fake.py': src}))[0]

    def test_unknown_code_fails(self):
        self.assertTrue(self._scan(
            "def v(p, u, x):\n"
            "    log_activity(p, u, f'Paid ₹{x.amount}', action_code='brand_new_money')\n"))

    def test_missing_code_fails(self):
        self.assertTrue(self._scan(
            "def v(p, u, x):\n    log_activity(p, u, f'Order total {x.total}')\n"))

    def test_money_through_a_wrapper_with_a_computed_code_fails(self):
        self.assertTrue(self._scan(
            "def _w(p, u, text, code):\n"
            "    log_activity(p, u, text, action_code=code)\n"
            "def v(p, u, x, c):\n"
            "    _w(p, u, f'Approved ₹{x.amount}', c)\n"))

    def test_money_through_a_wrapper_with_a_listed_code_passes(self):
        self.assertEqual(self._scan(
            "CODES = {'ok': 'payment_request_paid'}\n"
            "def _w(p, u, text, key):\n"
            "    log_activity(p, u, text, action_code=CODES[key])\n"
            "def v(p, u, x):\n"
            "    _w(p, u, f'Paid ₹{x.amount}', 'ok')\n"), [])

    def test_money_in_a_local_variable_is_followed(self):
        self.assertTrue(self._scan(
            "def v(p, u, x):\n"
            "    text = f'Paid ₹{x.amount}'\n"
            "    log_activity(p, u, text, action_code='task_done')\n"))

    def test_a_line_without_money_is_ignored(self):
        self.assertEqual(self._scan(
            "def v(p, u, t):\n"
            "    log_activity(p, u, f'Task {t.name} done', action_code='task_done')\n"), [])


# ---------------------------------------------------------------------------
# The audience, the card and the feeds
# ---------------------------------------------------------------------------

def _profile(username, role):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title(), last_name='Test')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.save()
    return profile


def _client(profile):
    client = Client(SERVER_NAME='localhost')
    client.force_login(profile.user)
    return client


class MoneyAudienceBase(TestCase):
    """One OPEX site and one Residential site, and a viewer of every kind.

    site: pm (assigned), coord (Project Coordinator, a coordinator), se (holds a task),
    qaqc_pm (PM role, the site's QA/QC engineer — sees it, does not manage it), designer
    (assigned_design), and the portfolio roles. res: pm, res_se (holds a task).
    """

    def setUp(self):
        self.pm       = _profile('af_pm', 'PM')
        self.coord    = _profile('af_coord', 'Project Coordinator')
        self.se       = _profile('af_se', 'Site Engineer')
        self.qaqc_pm  = _profile('af_qaqc_pm', 'PM')
        self.designer = _profile('af_design', 'Design')
        self.bd       = _profile('af_bd', 'BD')
        self.ceo      = _profile('af_ceo', 'CEO')
        self.sysadmin = _profile('af_sysadmin', 'System Admin')
        self.admin    = _profile('af_admin', 'Admin')
        self.finance  = _profile('af_fin', 'Finance')
        self.scm      = _profile('af_scm', 'SCM')
        self.res_se   = _profile('af_res_se', 'Site Engineer')
        program = Program.objects.create(name='Test-AF1', program_type='OPEX',
                                         client_name='AFClient', status='Active',
                                         short_tender_code='AF1')
        self.site = Project.objects.create(
            project_id='AF1-A', customer_name='AFClient', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX', program=program,
            site_code='AF1-A', dc_capacity_kw=Decimal('100.00'), status='Active',
            assigned_pm=self.pm, assigned_design=self.designer)
        self.site.coordinators.add(self.coord)
        self._task(self.site, self.se)
        SiteQaqcAssignment.objects.create(project=self.site, engineer=self.qaqc_pm,
                                          assigned_by=self.pm)
        self.res = Project.objects.create(
            project_id='AF1-R', customer_name='AFRes', customer_phone='9876543210',
            site_address='2 Sun Rd', city='Delhi', project_type='Residential',
            dc_capacity_kw=Decimal('5.00'), status='Active', assigned_pm=self.pm)
        self._task(self.res, self.res_se)

    def _task(self, site, profile):
        phase = ProjectPhase.objects.create(project=site, phase_name='Install',
                                            phase_order=1)
        Task.objects.create(phase=phase, task_name='Mount structure', task_order=1,
                            assigned_role=Task.SITE_ENGINEER, assigned_to=profile)

    def _row(self, project, code, action, minutes_ago):
        row = ActivityLog.objects.create(project=project, actor=self.pm, action=action,
                                         action_code=code)
        # auto_now_add cannot be set on create; the order is what these tests are about.
        ActivityLog.objects.filter(pk=row.pk).update(
            timestamp=timezone.now() - timedelta(minutes=minutes_ago))
        return row

    def _seed_feed(self, others=25):
        """One row per money code, all NEWER than `others` plain rows."""
        ActivityLog.objects.filter(project=self.site).delete()
        for i, code in enumerate(sorted(MONEY_ACTIVITY_CODES)):
            self._row(self.site, code, f'Money line ₹{1000 + i} ({code})', minutes_ago=i)
        for i in range(others):
            self._row(self.site, 'task_status_done', f'Plain event {i:02d}',
                      minutes_ago=100 + i)

    def _overview(self, profile, project=None):
        return _client(profile).get(
            reverse('project_overview', args=[(project or self.site).project_id]))

    def _timeline(self, profile, page=1):
        return _client(profile).get(
            reverse('project_timeline', args=[self.site.project_id]), {'page': page})


SEES_MONEY = ('pm', 'coord', 'ceo', 'sysadmin', 'admin', 'finance', 'scm')
NO_MONEY   = ('se', 'qaqc_pm', 'designer', 'bd')

#: The vendor-orders card before AF-1 (the inline rule it replaced), for the report's
#: before/after table: Finance, SCM, Admin, or the PM role managing the site.
CARD_BEFORE = {'pm': True, 'coord': False, 'ceo': False, 'sysadmin': False, 'admin': True,
               'finance': True, 'scm': True, 'se': False, 'qaqc_pm': False,
               'designer': False, 'bd': False}


class AudienceTests(MoneyAudienceBase):

    def test_the_helper_role_by_role(self):
        for who in SEES_MONEY:
            self.assertTrue(user_can_see_project_money(getattr(self, who).user, self.site),
                            who)
        for who in NO_MONEY:
            self.assertFalse(user_can_see_project_money(getattr(self, who).user, self.site),
                             who)

    def test_vendor_orders_card_follows_the_helper_for_every_role(self):
        changed = {}
        for who in SEES_MONEY + NO_MONEY:
            response = self._overview(getattr(self, who))
            self.assertEqual(response.status_code, 200, who)
            after = response.context['show_vendor_orders']
            self.assertEqual(after, who in SEES_MONEY, who)
            if after != CARD_BEFORE[who]:
                changed[who] = (CARD_BEFORE[who], after)
        # Exactly the three AF-1 widened the card to; nobody lost it.
        self.assertEqual(changed, {'coord': (False, True), 'ceo': (False, True),
                                   'sysadmin': (False, True)})

    def test_newly_admitted_roles_see_the_card_and_the_money_lines(self):
        self._seed_feed()
        for who in ('ceo', 'sysadmin', 'coord'):
            response = self._overview(getattr(self, who))
            self.assertContains(response, 'id="vendor-orders-card"')
            codes = {e.action_code for e in response.context['recent_activity']}
            self.assertEqual(codes, MONEY_ACTIVITY_CODES, who)

    def test_helper_cost(self):
        with CaptureQueriesContext(connection) as ctx:
            user_can_see_project_money(self.finance.user, self.site)
        self.assertEqual(len(ctx.captured_queries), 0)
        site = Project.objects.select_related('assigned_pm').get(pk=self.site.pk)
        with CaptureQueriesContext(connection) as ctx:
            user_can_see_project_money(self.se.user, site)
        self.assertEqual(len(ctx.captured_queries), 1)   # the coordinator lookup


class FeedTests(MoneyAudienceBase):

    def test_outside_the_audience_the_card_shows_eight_other_rows(self):
        self._seed_feed()
        for who in NO_MONEY:
            feed = self._overview(getattr(self, who)).context['recent_activity']
            self.assertEqual(len(feed), 8, who)
            self.assertEqual([e.action for e in feed],
                             [f'Plain event {i:02d}' for i in range(8)], who)
            self.assertFalse(any('₹' in e.action for e in feed), who)

    def test_inside_the_audience_the_money_lines_lead_the_card(self):
        self._seed_feed()
        for who in ('pm', 'finance', 'scm', 'admin'):
            feed = self._overview(getattr(self, who)).context['recent_activity']
            self.assertEqual({e.action_code for e in feed}, MONEY_ACTIVITY_CODES, who)

    def test_timeline_excludes_money_and_page_one_is_full(self):
        self._seed_feed(others=25)
        for who in NO_MONEY:
            page = self._timeline(getattr(self, who)).context['page_obj']
            self.assertEqual(page.paginator.count, 25, who)
            self.assertEqual(len(page.object_list), 20, who)
            self.assertFalse(any(e.action_code in MONEY_ACTIVITY_CODES
                                 for e in page.object_list), who)
            last = self._timeline(getattr(self, who), page=2).context['page_obj']
            self.assertEqual(len(last.object_list), 5, who)

    def test_timeline_inside_the_audience_lists_everything(self):
        self._seed_feed(others=25)
        for who in ('pm', 'coord', 'ceo', 'finance'):
            page = self._timeline(getattr(self, who)).context['page_obj']
            self.assertEqual(page.paginator.count, 25 + len(MONEY_ACTIVITY_CODES), who)
            self.assertEqual({e.action_code for e in page.object_list[:8]},
                             MONEY_ACTIVITY_CODES, who)

    def test_the_rendered_pages_carry_no_rupee_for_a_site_engineer(self):
        self._seed_feed()
        self.assertNotContains(self._overview(self.se), 'Money line')
        self.assertNotContains(self._timeline(self.se), 'Money line')


class MilestoneLineTests(MoneyAudienceBase):

    def test_the_view_writes_the_money_code(self):
        response = _client(self.pm).post(
            reverse('set_milestone_amounts', args=[self.res.project_id]),
            data=json.dumps({'m2_amount': 124000}), content_type='application/json')
        self.assertTrue(response.json()['success'], response.content)
        row = ActivityLog.objects.get(project=self.res, entity_type='Milestone')
        self.assertTrue(row.action.startswith('Set milestone amounts:'))
        self.assertEqual(row.action_code, 'milestone_amounts_set')
        feed = self._overview(self.res_se, self.res).context['recent_activity']
        self.assertNotIn(row.pk, [e.pk for e in feed])
        self.assertIn(row.pk, [e.pk for e in
                               self._overview(self.pm, self.res).context['recent_activity']])

    def test_migration_stamps_old_rows_narrowly_and_reverses(self):
        old = self._row(self.res, '', 'Set milestone amounts: M2=₹124000', minutes_ago=5)
        coded = self._row(self.res, 'something_else', 'Set milestone amounts: M1=₹1',
                          minutes_ago=6)
        lookalike = self._row(self.res, '', 'Milestone amounts were discussed',
                              minutes_ago=7)
        feed = self._overview(self.res_se, self.res).context['recent_activity']
        self.assertIn(old.pk, [e.pk for e in feed])   # visible until stamped

        MIGRATION.stamp(django_apps, None)
        codes = dict(ActivityLog.objects.filter(project=self.res)
                     .values_list('pk', 'action_code'))
        self.assertEqual(codes[old.pk], 'milestone_amounts_set')
        self.assertEqual(codes[coded.pk], 'something_else')
        self.assertEqual(codes[lookalike.pk], '')
        feed = self._overview(self.res_se, self.res).context['recent_activity']
        self.assertNotIn(old.pk, [e.pk for e in feed])

        MIGRATION.unstamp(django_apps, None)
        self.assertEqual(ActivityLog.objects.get(pk=old.pk).action_code, '')
        self.assertEqual(ActivityLog.objects.get(pk=coded.pk).action_code, 'something_else')
