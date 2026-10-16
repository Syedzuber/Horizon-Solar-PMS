"""Checklist PDFs — the shared renderer and the task checklist document (docs/CHECKLIST_PDF_SPEC.md).

TWO HALVES, AND ONLY THE FIRST IS SHARED.

  render_pdf()  — HTML template in, PDF bytes out, or an exception. It knows nothing about
                  checklists. Part 2 (the QA/QC visit PDF) and the step 9 dossier call it
                  with their own templates, built from the same partials under
                  templates/projects/pdf/ (see base_document.html for the list).

  task_checklist_document() and its helpers — the task checklist PDF's content: which
                  rows, which columns, which header lines, FINAL or NOT FINAL. Each later
                  document writes its own builder of the same shape and reuses the
                  partials, not this builder.

READ-ONLY, BOTH HALVES. Nothing here writes a row, a file or a session. The one thing
it writes is a log line: the F-7 warning when a character had to be replaced. A
download is a view (ruling 6), and the view's test asserts it issues no write query.

THE FONT IS BUNDLED (F-7). DejaVu Sans, regular and bold, lives in projects/fonts/dejavu/
with its licence, and base_document.html registers it with @font-face. It draws Ω, ₹,
≤ and most of what a site engineer types; it does not draw Devanagari. A character it
cannot draw would come out BLANK, so replace_undrawable() swaps it for a visible "□"
before rendering — a reader can see something was there.
"""

import functools
import io
import logging
import re
from pathlib import Path

from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.text import slugify

from .models import ChecklistItemCompletion, Task

logger = logging.getLogger(__name__)

# The bundled font's directory: the ONLY place a PDF render may read a file from (see
# render_pdf). Server-side only — not under static/, so collectstatic never copies it.
FONT_DIR = Path(__file__).resolve().parent / 'fonts' / 'dejavu'
FONT_FILES = ('DejaVuSans.ttf', 'DejaVuSans-Bold.ttf')

UNDRAWABLE = '□'   # "□" — F-7: what an undrawable character prints as

# Line breaks and tabs are layout, not glyphs: linebreaksbr turns newlines into <br>, so
# they are never looked up in the font and never replaced.
_LAYOUT_CHARS = frozenset('\n\r\t')


class PdfRenderError(Exception):
    """The PDF could not be rendered completely. Raised instead of returning bytes, so
    no caller can hand a person a truncated document that looks whole."""


@functools.lru_cache(maxsize=1)
def drawable_codepoints():
    """The code points BOTH bundled faces can draw, read once per process from the TTF
    character maps. The intersection, because text can land in either face: a bold
    heading or table header uses the bold face, which has slightly fewer glyphs."""
    # Imported here, not at module level, and NOT for a circular import: reportlab is
    # only needed when a PDF is actually rendered, and views.py imports this module.
    from reportlab.pdfbase.ttfonts import TTFont
    sets = [set(TTFont(name, str(FONT_DIR / name)).face.charToGlyph) for name in FONT_FILES]
    return frozenset.intersection(*map(frozenset, sets))


def replace_undrawable(value, found):
    """`value` with every character the bundled font cannot draw replaced by "□" (F-7).

    Walks strings inside dicts, lists and tuples, so a whole document context can be
    passed at once; anything else (numbers, booleans, None) is returned untouched. Each
    replaced character is added to the set `found`, so the caller can log once per
    document rather than once per cell."""
    if isinstance(value, str):
        drawable = drawable_codepoints()
        out = []
        for ch in value:
            if ch in _LAYOUT_CHARS or ord(ch) in drawable:
                out.append(ch)
            else:
                found.add(ch)
                out.append(UNDRAWABLE)
        return ''.join(out)
    if isinstance(value, dict):
        return {key: replace_undrawable(item, found) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(replace_undrawable(item, found) for item in value)
    return value


def render_pdf(template_name, context):
    """Render `template_name` with `context` and convert the HTML to PDF bytes.

    RAISES PdfRenderError ON ANY FAILURE, NEVER RETURNS PARTIAL BYTES. xhtml2pdf has two
    ways of failing: it raises (its default), or it records an error, counts it in
    `result.err` and still writes whatever it managed to build. The second is the
    dangerous one — the bytes are a valid PDF with something missing — so a non-zero
    `err` is treated exactly like an exception and the buffer is thrown away. A failure
    rendering the HTML template itself is the same failure and raises the same error.

    Characters the font cannot draw are the caller's to replace before this is called
    (replace_undrawable); xhtml2pdf would only warn about them and print a blank.

    Read-only: renders a template and converts it in memory; writes nothing anywhere.
    """
    # Imported here, not at module level, and NOT for a circular import: importing
    # xhtml2pdf pulls in pyHanko, aiohttp and cryptography eagerly (~1.8 s measured on
    # 15 Oct 2026). views.py imports this module, so a top-level import would add that to
    # every gunicorn worker boot and every manage.py command, for a page few people open.
    from xhtml2pdf import pisa
    from xhtml2pdf.config.resources import ResourceAccessPolicy

    # What a render may read: the bundled fonts and nothing else. No network at all, and
    # no local file outside FONT_DIR, whatever text ends up in the document. `path` names
    # a (non-existent) document inside FONT_DIR so the template's relative font URLs
    # resolve there.
    policy = ResourceAccessPolicy.server(base_dir=FONT_DIR, allow_remote=False)
    try:
        html = render_to_string(template_name, context)
        out = io.BytesIO()
        result = pisa.CreatePDF(html, dest=out, encoding='utf-8',
                                path=str(FONT_DIR / 'document.html'), resource_policy=policy)
    except Exception as exc:
        raise PdfRenderError(f'PDF rendering failed for {template_name}: {exc}') from exc
    if result.err:
        # result.log holds (level, line, message, fragment) tuples; the messages are what
        # a person debugging this needs, and the fragment can be most of the document.
        messages = '; '.join(str(entry[2]) for entry in result.log)
        raise PdfRenderError(
            f'PDF rendering reported {result.err} error(s) for {template_name}: {messages}')
    pdf = out.getvalue()
    if not pdf.startswith(b'%PDF'):
        raise PdfRenderError(f'PDF rendering produced no document for {template_name}.')
    return pdf


# ---------------------------------------------------------------------------
# Task checklist PDF (part 1)
# ---------------------------------------------------------------------------

TASK_CHECKLIST_TEMPLATE = 'projects/pdf/task_checklist.html'

NOT_CHECKED = 'Not checked'   # F-3: the status of an item with no answer on this task

# F-4's six columns. Widths in percent of the 18 cm content width; Checking point takes
# what is left because item labels reach 238 characters and must wrap, not clip. Status
# and Date are sized for their longest fixed value in DejaVu Sans 8.5pt ("Not Applicable",
# "07 Sep 2026") so neither breaks across two lines.
TASK_CHECKLIST_COLUMNS = [
    {'label': 'Checking point',   'width': 32},
    {'label': 'Status',           'width': 15},
    {'label': 'Witness',          'width': 14},
    {'label': 'Photo proof',      'width': 8},
    {'label': 'Date of checking', 'width': 12},
    {'label': 'Remark',           'width': 19},
]

# Ruling 1's grid. Approved By names PM/Coordinator because that is who signs it on paper;
# it says nothing about which authority approved in the system (ruling 4).
SIGNOFF_HEADINGS = ['Checked/Tested By', 'Witnessed By', 'Witnessed By',
                    'Approved By (PM/Coordinator)']

# Ruling 2, verbatim, printed once.
WITNESS_FOOTNOTE = ('Witness names are as entered by the person who recorded the check; '
                    'they are not signatures.')


def _person(profile):
    """A UserProfile as a person reads it on paper: full name, else username."""
    return profile.user.get_full_name() or profile.user.username


def _ist_date(value):
    """An aware datetime as its IST calendar date, the way the rest of the app writes it."""
    return timezone.localtime(value).strftime('%d %b %Y')


def task_checklist_is_final(task):
    """True when the task checklist PDF may be marked FINAL (F-1); else it prints NOT FINAL.

    THE RULE, ALL THREE REQUIRED:
      1. task.status is Done;
      2. task.approved_at is set;
      3. no ChecklistItemCompletion on this task has checked_at later than approved_at.

    Condition 3 is the stale-approval case (G63): a task reopened after approval keeps
    its old approved_at, answers reopen (checklist_answers_open ignores approved_at), and
    an answer recorded then was never seen by the approver. It reads EVERY completion on
    the task, not only those on the pinned version, because an answer is an answer
    whichever version holds it. One query, and only when 1 and 2 already hold.
    """
    if task.status != Task.DONE or task.approved_at is None:
        return False
    return not ChecklistItemCompletion.objects.filter(
        task=task, checked_at__gt=task.approved_at).exists()


def _status(completion):
    """F-4's Status cell. Not checked unless the row records an answer.

    is_checked is the "was it answered" flag (see ChecklistItemCompletion). A checked row
    with a blank answer cannot be written by the completion view and 0081 gave every
    older row "yes"; should one exist anyway it reads "Checked", the model's own word for
    it in __str__, rather than inventing an answer it never recorded.
    """
    if completion is None or not completion.is_checked:
        return NOT_CHECKED
    return completion.get_answer_display() or 'Checked'


def _row_cells(row):
    """One item row's six cells, from a `_checklist_context()` row dict.

    The Checking point cell is `row['label']`, which _checklist_context() already chose by
    F-5 (the snapshot when answered, the pinned version's label when not) — taken from
    there so the PDF and the task page cannot disagree on a wording. The item's number in
    the whole checklist leads the cell, so a printed line can be matched to the screen
    and to the paper form without a seventh column F-4 does not list.
    """
    completion = row['completion']
    answered = completion is not None and completion.is_checked
    has_photo = bool(completion and (completion.photo_url or completion.photo_supabase_path))
    return [
        f"{row['number']}. {row['label']}",
        _status(completion),
        completion.witness_names if answered else '',
        'Yes' if has_photo else 'No',   # ruling 9: recorded or not; never the photo itself
        _ist_date(completion.checked_at) if answered and completion.checked_at else '',
        completion.remarks if answered else '',
    ]


def _safe_filename_part(value):
    """Keep a filename component to letters, digits, dot, dash and underscore, so a code
    can never break the Content-Disposition header or name a path."""
    return re.sub(r'[^A-Za-z0-9._-]+', '-', str(value)).strip('-') or 'x'


# A cap on the task-name part alone, so the whole filename stays far inside Windows'
# 255-character limit whatever the task is called.
TASK_SLUG_MAX = 80


def _task_slug(task):
    """The task name as a filename part (F-8): slugify() keeps lowercase ASCII letters,
    digits, hyphens and underscores, which no Windows filename rule forbids, and drops
    everything else. A name with no ASCII letter or digit at all slugifies to nothing, and
    reads "task" rather than leaving an empty part between two underscores."""
    return slugify(task.task_name)[:TASK_SLUG_MAX].strip('-_') or 'task'


def task_checklist_filename(project, task, checklist, generated_at):
    """<site code>_<task name>_<checklist code>_v<version>_<YYYYMMDD>.pdf, dated by the IST
    generation day (F-8). For OPEX the site code is project_id (site_code IS the project
    ID). The task name is what tells per-location copies of one task apart (G74): four
    Civil Work tasks on one site otherwise download under one filename."""
    return '_'.join([
        _safe_filename_part(project.project_id),
        _task_slug(task),
        _safe_filename_part(checklist.code),
        f'v{checklist.version_no}',
        timezone.localtime(generated_at).strftime('%Y%m%d'),
    ]) + '.pdf'


def task_checklist_document(project, task, checklist, sections, generated_at):
    """The template context for one task's checklist PDF.

    `checklist` and `sections` are `_checklist_context()`'s `checklist` and
    `checklist_sections`: the version _checklist_for_task() pins, and EVERY item of it in
    order, grouped by section, answered or not (F-3). Nothing is filtered out here.

    Header (F-6): only fields the system holds. Job No, Drawing and Spec No are not
    printed at all, and neither is the source form reference, which is not stored on
    Checklist (F-6 v1.1, G73). The task name is printed (F-8). Beside FINAL / NOT FINAL
    the header says "N of M points checked" (F-1 v1.1): M is every item of the pinned
    version, N the items on it this task has answered.

    F-7: every string in the returned context has already had undrawable characters
    replaced by "□", and one warning naming the task is logged when any were.
    """
    is_final = task_checklist_is_final(task)
    local_now = timezone.localtime(generated_at)
    generated_text = local_now.strftime('%d %b %Y %H:%M IST')

    rows = [row for section in sections for row in section['rows']]
    answered = sum(1 for row in rows if row['completion'] is not None
                   and row['completion'].is_checked)

    header_rows = [('Site code', project.project_id)]
    if project.customer_name:
        header_rows.append(('Client', project.customer_name))
    if project.program_id:
        header_rows.append(('Tender', project.program.reference_display))
    header_rows.append(('Task', task.task_name))
    header_rows.append(('Checklist', f'{checklist.name} — version {checklist.version_no}'))
    # Ruling 4: the recorded approver and date, and the self-certified flag; no capacity.
    if task.approved_by_id and task.approved_at:
        approval = f'Approved by {_person(task.approved_by)} on {_ist_date(task.approved_at)}'
        if task.approval_self_certified:
            approval += ' (self-certified)'
        header_rows.append(('Approval', approval))
    header_rows.append(('Generated', generated_text))

    approver_name = ''
    if task.approved_by_id:
        approver_name = _person(task.approved_by)
        if task.approval_self_certified:
            approver_name += ' (self-certified)'
    signoff_names = [
        _person(task.submitted_by) if task.submitted_by_id else '',
        '', '',   # ruling 1: witness names stay in each item's row, not in the grid
        approver_name,
    ]

    document = {
        'pdf_title':        f'Checklist — {checklist.name}',
        'pdf_is_final':     is_final,
        'pdf_progress':     f'{answered} of {len(rows)} points checked',
        'pdf_generated_at': generated_text,
        'pdf_header_rows':  header_rows,
        'pdf_footer_left':  f'{project.project_id} · {checklist.code} v{checklist.version_no}',
        'pdf_columns':      TASK_CHECKLIST_COLUMNS,
        'pdf_sections':     [
            {'name': section['name'],
             'rows': [{'cells': _row_cells(row)} for row in section['rows']]}
            for section in sections
        ],
        'pdf_signoff':      [{'heading': heading, 'name': name}
                             for heading, name in zip(SIGNOFF_HEADINGS, signoff_names)],
        'pdf_footnotes':    [WITNESS_FOOTNOTE],
    }
    # F-7 over the WHOLE context, not only the fields people type into: remarks, witness
    # names and person names are where it bites, but a task or client name is typed by
    # someone too, and a blank anywhere reads as "nothing was there".
    found = set()
    document = replace_undrawable(document, found)
    if found:
        logger.warning(
            "Checklist PDF for task %s ('%s') on %s: %d character(s) the bundled font "
            "cannot draw were printed as %s: %s",
            task.pk, task.task_name, project.project_id, len(found), UNDRAWABLE,
            ' '.join(f'U+{ord(ch):04X}' for ch in sorted(found)))
    return document
