"""
GRN-V0 — THROWAWAY unboxing-video prototype. Delete this module, its templates, its
routes, tests_video_proto.py, delete_video_proto and the three VIDEO_PROTO_* settings
together once the one-week field test on SE phones is over.

WHAT IT PROVES
  A tester records a short clip on their phone inside the browser (or picks a gallery
  video) and the browser PUTs it straight to Supabase through a signed upload URL. The
  page shows the hard numbers (format, resolution, size, duration, upload time) and plays
  the stored file back. Nothing here touches GRNs, challans, receipts or notifications.

THE SERVER NEVER RECEIVES VIDEO BYTES
  A 30 MB upload at 1 Mbps takes about four minutes, far past the sync worker's timeout
  (GRN-PV-A). So the server only signs: one small JSON POST for an upload URL, one for a
  read URL, one for the log line. Every POST endpoint refuses any body that is not JSON.

AN EXPLICIT, DOCUMENTED EXCEPTION TO "NO XHR FROM ALPINE"
  The recording page is vanilla JS (not Alpine) that calls fetch/XHR. This is the only
  page allowed to, and only because it is a prototype that is deleted after the test.

GATE, IN THIS ORDER, ON EVERY URL (_prototype_view)
  1) VIDEO_PROTO_ENABLED off           -> 404, for everyone, anonymous included
  2) not logged in                     -> /login/
  3) not the Admin role and not listed in VIDEO_PROTO_TESTERS -> 403
  The results page has the same audience (Zuber's Q1 ruling): Admin plus the listed
  testers, so SCM and Finance users are added to the list for the week to test playback.

STORAGE
  A bucket of its own (VIDEO_PROTO_BUCKET), never the delivery bucket: _bucket_name()
  treats an empty name, or one equal to any other SUPABASE_*_BUCKET, as not configured.
  Paths are video-proto/<username>/<uuid hex>.<ext>, chosen by the SERVER; a tester can
  sign a read URL only for their own folder. The Supabase client is called directly here
  (V0-2 forbids touching the existing storage helpers).

WHAT IS STORED
  No model, no migration. One ActivityLog line per try (action_code video_proto_upload or
  video_proto_upload_failed), its numbers written into `action` as key=value tokens that
  results_rows() reads back. The lines outlive the cleanup command; they are harmless.
"""
import json
import logging
import re
import uuid
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from .models import ActivityLog, log_activity

logger = logging.getLogger(__name__)

PREFIX = 'video-proto'

#: Content type the browser sends -> file extension the server stores it under. The same
#: four the production bucket's MIME list allows (Q4); the page accepts the same four.
ACCEPTED_TYPES = {
    'video/mp4': 'mp4',
    'video/webm': 'webm',
    'video/quicktime': 'mov',
    'video/3gpp': '3gp',
}

#: The page refuses to upload anything larger (V0-4). Decimal MB, so the page is never
#: looser than the bucket's own 30 MB limit, whichever way Supabase counts a megabyte.
MAX_BYTES = 30 * 1000 * 1000

#: Seconds a signed read link lives (V0-6).
READ_LINK_SECONDS = 300

#: Where a failed try stopped (Q3). The page reports exactly one of these.
STAGES = ('permission_denied', 'recorder_error', 'too_large', 'sign_failed',
          'upload_failed', 'playback_failed')

#: Which control produced the file: the default recorder, the 720p comparison recorder,
#: or a gallery file.
SOURCES = ('rec', 'rec720', 'file')

ACTION_OK = 'video_proto_upload'
ACTION_FAILED = 'video_proto_upload_failed'

#: Recording presets drawn by the page (V0-3). Kept here so the page and the tests read
#: one copy.
PRESETS = {
    'rec': {'width': 640, 'height': 480, 'fps': 24, 'video_bps': 1_000_000,
            'audio_bps': 32_000, 'label': 'Record (640×480, 1 Mbps)', 'order': 'webm'},
    'rec720': {'width': 1280, 'height': 720, 'fps': 24, 'video_bps': 2_000_000,
               'audio_bps': 32_000, 'label': 'Record 720p (2 Mbps)', 'order': 'mp4'},
}
MAX_SECONDS = 90

#: The recorder formats tried, in order; the first one the browser supports wins. Each
#: preset names its order, and every log line records it as ord=webm|mp4.
#:   webm  the default button: Chrome wrote audio-only MP4s in the local check (SECONDARY_
#:         FINDINGS, GRN-V0), and every WebM had a picture, so WebM is tried first.
#:   mp4   the 720p button keeps V0-3's original MP4-first order on purpose, to measure how
#:         often real phones hit the audio-only MP4 (their playback_failed rows).
RECORDER_ORDERS = {
    'webm': ('video/webm;codecs=vp9,opus', 'video/webm;codecs=vp8,opus', 'video/webm',
             'video/mp4;codecs=avc1,mp4a.40.2'),
    'mp4': ('video/mp4;codecs=avc1,mp4a.40.2', 'video/webm;codecs=vp9,opus',
            'video/webm;codecs=vp8,opus', 'video/webm'),
}

#: Bucket settings the prototype bucket must never equal (V0-2: never the delivery bucket,
#: and by the same reasoning never any other bucket holding real files).
OTHER_BUCKET_SETTINGS = ('SUPABASE_BUCKET', 'SUPABASE_DESIGN_BUCKET',
                         'SUPABASE_BILLS_BUCKET', 'SUPABASE_DELIVERY_BUCKET')

#: A username that can sit in a storage path as it is. All 73 users on the 9 Nov restore
#: match; a tester who does not is refused a signed URL rather than given a mangled path.
_USERNAME_RE = re.compile(r'[A-Za-z0-9_.-]+')

#: Any prototype object path: the server-built shape, nothing else (no "..", no nesting).
_PATH_RE = re.compile(
    r'video-proto/(?P<username>[A-Za-z0-9_.-]+)/[0-9a-f]{32}\.(?:mp4|webm|mov|3gp)')

_MIME_RE = re.compile(r'[a-z0-9/;=,.+-]{1,80}')

# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def user_may_use_video_proto(user):
    """True for the Admin role (Q2: Admin role only, not System Admin) and for any username
    listed in VIDEO_PROTO_TESTERS. Everyone else is kept out: the page is a hidden field
    test, and its results table shows every tester's numbers and videos."""
    if not user.is_authenticated:
        return False
    if getattr(getattr(user, 'profile', None), 'role', '') == 'Admin':
        return True
    return user.username in {name.strip() for name in settings.VIDEO_PROTO_TESTERS}


def _prototype_view(json_errors=False):
    """The GRN-V0 gate (module docstring), outermost on every prototype view so the flag's
    404 comes before login, method checks and everything else. json_errors makes the 403
    a JSON body for the page's fetch calls."""
    def decorate(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            # Flag off: the prototype does not exist, for anyone (V0-1).
            if not settings.VIDEO_PROTO_ENABLED:
                raise Http404
            if not request.user.is_authenticated:
                return redirect('/login/')
            # Admin role or a named tester only (V0-1, Q1, Q2); see user_may_use_video_proto.
            if not user_may_use_video_proto(request.user):
                if json_errors:
                    return JsonResponse({'error': 'Not allowed.'}, status=403)
                raise PermissionDenied
            return view(request, *args, **kwargs)
        return wrapper
    return decorate


def path_belongs_to(path, username):
    """True when `path` is a server-shaped prototype path in `username`'s own folder."""
    match = _PATH_RE.fullmatch(path or '')
    return bool(match) and match.group('username') == username


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _bucket_name():
    """VIDEO_PROTO_BUCKET, or '' when it is unset or names a bucket that holds real files
    (V0-2). '' means every signing refuses."""
    name = (settings.VIDEO_PROTO_BUCKET or '').strip()
    others = {(getattr(settings, s, '') or '').strip() for s in OTHER_BUCKET_SETTINGS} - {''}
    if not name or name in others:
        return ''
    return name


def bucket_api():
    """The Supabase storage API for the prototype bucket, or None when storage is not
    configured (no URL/key, or _bucket_name() is empty). Tests patch this function."""
    bucket = _bucket_name()
    if not bucket or not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        return None
    # import inside function — not a circular-import guard: it keeps the supabase package
    # out of module import, as supabase_storage.get_supabase_client does.
    from supabase import create_client
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY).storage.from_(bucket)


def _read_json(request):
    """The request's JSON object, or None. Anything that is not JSON (a multipart file
    upload above all) is refused, so no video can reach the server by mistake."""
    if request.content_type != 'application/json':
        return None
    try:
        data = json.loads(request.body or b'{}')
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


@_prototype_view()
@require_GET
def video_proto_page(request):
    """The hidden recording page (V0-3..V0-6). Access: Admin role or a named tester, only
    while VIDEO_PROTO_ENABLED is on. No menu links here."""
    return render(request, 'projects/video_proto/record.html', {
        'presets': PRESETS,
        'max_seconds': MAX_SECONDS,
        'max_bytes': MAX_BYTES,
        'accepted_types': list(ACCEPTED_TYPES),
        'recorder_orders': {name: list(types) for name, types in RECORDER_ORDERS.items()},
        'stages': list(STAGES),
        'storage_ready': bucket_api() is not None,
    })


@_prototype_view(json_errors=True)
@require_POST
def video_proto_sign_upload(request):
    """Sign one upload URL for the caller's own folder (V0-5). Access: Admin role or a
    named tester. POST JSON {content_type, bytes}; answers {path, signed_url}.

    The SERVER picks the path, so a client can never choose where it writes: any `path`
    in the body is ignored."""
    data = _read_json(request)
    if data is None:
        return JsonResponse({'error': 'Send JSON.'}, status=400)
    content_type = str(data.get('content_type') or '').split(';')[0].strip().lower()
    ext = ACCEPTED_TYPES.get(content_type)
    if ext is None:
        return JsonResponse({'error': 'Only MP4, WebM, MOV or 3GP videos.'}, status=400)
    try:
        size = int(data.get('bytes') or 0)
    except (TypeError, ValueError):
        size = 0
    # A cheap echo of the page's own check; the bucket's 30 MB limit is the real guard,
    # because the browser could send a different file to the signed URL than it declared.
    if size <= 0 or size > MAX_BYTES:
        return JsonResponse({'error': 'The file must be between 1 byte and 30 MB.'},
                            status=400)
    username = request.user.username
    if not _USERNAME_RE.fullmatch(username):
        return JsonResponse({'error': 'This username cannot be used in a storage path.'},
                            status=400)
    api = bucket_api()
    if api is None:
        return JsonResponse({'error': 'Video storage is not configured.'}, status=503)
    path = f'{PREFIX}/{username}/{uuid.uuid4().hex}.{ext}'
    try:
        signed = api.create_signed_upload_url(path)
    except Exception:
        logger.exception('video_proto: signing an upload URL failed for %s', path)
        return JsonResponse({'error': 'Could not get an upload link.'}, status=502)
    return JsonResponse({'path': path, 'signed_url': signed['signed_url']})


@_prototype_view(json_errors=True)
@require_POST
def video_proto_sign_read(request):
    """Sign a 300 s read URL for one of the caller's OWN uploads, for the playback check
    (V0-6). Access: Admin role or a named tester. POST JSON {path}; answers {url}."""
    data = _read_json(request)
    if data is None:
        return JsonResponse({'error': 'Send JSON.'}, status=400)
    path = str(data.get('path') or '')
    # Own folder only, even for Admin: the playback check is for the file just uploaded.
    # Other people's videos are opened from the results page (video_proto_play).
    if not path_belongs_to(path, request.user.username):
        return JsonResponse({'error': 'Not your upload.'}, status=403)
    api = bucket_api()
    if api is None:
        return JsonResponse({'error': 'Video storage is not configured.'}, status=503)
    try:
        url = api.create_signed_url(path, READ_LINK_SECONDS)['signedURL']
    except Exception:
        logger.exception('video_proto: signing a read URL failed for %s', path)
        return JsonResponse({'error': 'Could not get a playback link.'}, status=502)
    return JsonResponse({'url': url})


def _number(value):
    """A finite, non-negative float from the page, or None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number < 0 or number > 1e12:   # NaN, negative, absurd
        return None
    return number


def _fmt(number, digits):
    return f'{number:.{digits}f}' if number is not None else None


def build_log_text(data, username):
    """The ActivityLog `action` text for one try, or raises ValueError with a message.

    Shape: '<sentence>: k=v k=v ... err=<text>'. Values never hold spaces, so results_rows()
    splits on them; the error text is last because log_activity cuts an over-long line at
    255 characters and only the error may be cut."""
    outcome = data.get('outcome')
    if outcome not in ('ok', 'failed'):
        raise ValueError('outcome must be ok or failed')
    stage = data.get('stage') or ''
    if outcome == 'failed' and stage not in STAGES:
        raise ValueError('unknown stage')
    if outcome == 'ok':
        stage = ''
    source = data.get('source')
    if source not in SOURCES:
        raise ValueError('unknown source')
    # Which recorder-format order the page used (RECORDER_ORDERS); none for a gallery file.
    order = data.get('order') or ''
    if order and order not in RECORDER_ORDERS:
        raise ValueError('unknown order')
    path = str(data.get('path') or '')
    # A path, when given, must be the caller's own server-built one; a failure before
    # signing has none.
    if path and not path_belongs_to(path, username):
        raise ValueError('not your upload')
    if outcome == 'ok' and not path:
        raise ValueError('an upload needs its path')
    mime = re.sub(r'\s+', '', str(data.get('mime') or '')).lower()
    if mime and not _MIME_RE.fullmatch(mime):
        mime = 'unreadable'

    width, height = _number(data.get('width')), _number(data.get('height'))
    # The stored file's own picture size, read back at playback: proves it has a video
    # track, and shows its orientation (the camera's res= can differ on a portrait phone).
    played_w, played_h = _number(data.get('played_width')), _number(data.get('played_height'))
    sent = _number(data.get('bytes_sent'))
    tokens = [
        ('src', source),
        ('ord', order or None),
        ('stage', stage or None),
        ('mime', mime or None),
        ('res', f'{int(width)}x{int(height)}' if width and height else None),
        ('fps', _fmt(_number(data.get('fps')), 0)),
        ('dur', _fmt(_number(data.get('duration')), 1)),
        ('bytes', _fmt(_number(data.get('bytes')), 0)),
        ('up', _fmt(_number(data.get('upload_seconds')), 2)),   # Mbps is computed from it
        # Only when the upload stopped short: equal to bytes= it says nothing new.
        ('sent', _fmt(sent, 0) if sent is not None and sent != _number(data.get('bytes')) else None),
        ('el', _fmt(_number(data.get('elapsed')), 1)),
        ('played', f'{int(played_w)}x{int(played_h)}' if played_w and played_h else None),
        # The file name only: the folder is always video-proto/<the actor's username>/,
        # and the 255-character column is better spent on the error text.
        ('file', path.rsplit('/', 1)[1] if path else None),
    ]
    sentence = 'Video test' + (' ok' if outcome == 'ok' else ' failed')
    text = sentence + ': ' + ' '.join(f'{k}={v}' for k, v in tokens if v is not None)
    error = ' '.join(str(data.get('error') or '').split())
    if error:
        text += ' err=' + error
    return text, (ACTION_OK if outcome == 'ok' else ACTION_FAILED)


@_prototype_view(json_errors=True)
@require_POST
def video_proto_log(request):
    """Write one ActivityLog line for one try, success or failure (V0-2, V0-6, Q3).
    Access: Admin role or a named tester. POST JSON from the page; answers {logged: true}."""
    data = _read_json(request)
    if data is None:
        return JsonResponse({'error': 'Send JSON.'}, status=400)
    try:
        text, code = build_log_text(data, request.user.username)
    except ValueError as refusal:
        return JsonResponse({'error': str(refusal)}, status=400)
    log_activity(None, getattr(request.user, 'profile', None), text,
                 entity_type='VideoProto', action_code=code)
    return JsonResponse({'logged': True})


def _parse_line(action):
    """{key: value} from a build_log_text() line; 'err' holds the error text."""
    _, _, rest = action.partition(': ')
    rest, _, error = rest.partition(' err=')
    values = dict(token.split('=', 1) for token in rest.split() if '=' in token)
    if error:
        values['err'] = error
    return values


def _float(values, key):
    return _number(values.get(key))


def results_rows(limit=500):
    """(rows, stage_counts, ok_count) for the results page: newest first."""
    # Every prototype line, newest first (pk breaks a same-instant tie), with the tester's
    # username in one join; capped because the prototype runs for a week and the page has
    # no paging.
    logs = (ActivityLog.objects
            .filter(action_code__in=(ACTION_OK, ACTION_FAILED))
            .select_related('actor__user')
            .order_by('-timestamp', '-pk')[:limit])
    rows, stage_counts, ok_count = [], {stage: 0 for stage in STAGES}, 0
    for log in logs:
        values = _parse_line(log.action)
        size, duration = _float(values, 'bytes'), _float(values, 'dur')
        upload, sent = _float(values, 'up'), _float(values, 'sent')
        ok =log.action_code == ACTION_OK
        if ok:
            ok_count += 1
        elif values.get('stage') in stage_counts:
            stage_counts[values['stage']] += 1
        mb = size / 1e6 if size else None
        per_minute = mb / (duration / 60) if mb and duration else None
        tester = log.actor.user.username if log.actor and log.actor.user else ''
        path = f"{PREFIX}/{tester}/{values.get('file', '')}"
        rows.append({
            'when': log.timestamp,
            'tester': tester or '—',
            'ok': ok,
            'stage': values.get('stage', ''),
            'source': values.get('src', ''),
            'order': values.get('ord', ''),
            'mime': values.get('mime', ''),
            'res': values.get('res', ''),
            'played': values.get('played', ''),
            'fps': values.get('fps', ''),
            'duration': duration,
            'mb': mb,
            'mb_per_min': per_minute,
            'over_budget': per_minute is not None and per_minute > 10,   # field-test bar
            'upload_seconds': upload,
            'mbps': (size * 8 / 1e6 / upload) if size and upload else None,
            'sent_mb': sent / 1e6 if sent is not None else None,
            'elapsed': _float(values, 'el'),
            'error': values.get('err', ''),
            'path': path if _PATH_RE.fullmatch(path) else '',   # no file, or no tester
        })
    return rows, stage_counts, ok_count


@_prototype_view()
@require_GET
def video_proto_results(request):
    """Every prototype log line as one table, successes and failures together, with a count
    per failure stage and a play link per stored video (V0-7, Q1, Q3). Access: Admin role
    or a named tester. Read-only."""
    rows, stage_counts, ok_count = results_rows()
    return render(request, 'projects/video_proto/results.html', {
        'rows': rows,
        'stage_counts': stage_counts,
        'ok_count': ok_count,
        'failed_count': sum(stage_counts.values()),
    })


@_prototype_view()
@require_GET
def video_proto_play(request):
    """Sign a 300 s read link for one stored prototype video and redirect to it, so a
    results-page link never goes stale (V0-7). Access: Admin role or a named tester; any
    tester's video, as the results page lists them all. GET ?path=."""
    path = request.GET.get('path', '')
    if not _PATH_RE.fullmatch(path):
        raise Http404
    api = bucket_api()
    if api is None:
        messages.error(request, 'Video storage is not configured.')
        return redirect('video_proto_results')
    try:
        url = api.create_signed_url(path, READ_LINK_SECONDS)['signedURL']
    except Exception:
        logger.exception('video_proto: signing a play link failed for %s', path)
        messages.error(request, 'That video could not be opened. Try again.')
        return redirect('video_proto_results')
    return redirect(url)
