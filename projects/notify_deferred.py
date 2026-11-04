"""
Deferred external sends (NTF-DCG Session 2, ruling N9).

For an event registered with deferred=True, notify()'s email and WhatsApp legs (the
latter from Session 3a) do not call the provider in the request. _send_to_one() renders
the email, or builds and sanitises the WhatsApp values, then calls defer(), which
writes a 'pending' NotificationLog row and hands the send to a small background pool. The job calls send_notification() with log_row_id, so the master
switch, the person's preference and the provider call all run exactly as for a
synchronous send, and their outcome finishes that same row: sent, failed or skipped.
send_notification() stays the only thing that talks to Interakt or ZeptoMail.

WHY. notify() already sends after commit, but on the request's own thread, so a slow
ZeptoMail holds the user's page for up to 10 s per recipient. A deferred send costs the
request one INSERT.

ORDERING (S6). defer() runs inside notify()'s on_commit callback, so a rolled-back
action never reaches it: no row, no send. The hand-off to the pool is registered with
on_commit too, which in that callback runs at once. It matters only for notify_now()
called inside an open transaction: the job then cannot start before the row it updates
is committed, and a rollback removes the row and the job together.

THE POOL (S4). One ThreadPoolExecutor per process, MAX_WORKERS threads, created at
import. Threads start on the first submit, so gunicorn's fork happens before any
exist. Django's connections are per thread; each job closes its own when done, because
CONN_MAX_AGE is 0 and no request cycle will close it.

NO RETRY (S7). A job lost to a worker kill or restart leaves its row 'pending'. It is
never retried automatically, because a WhatsApp may already have gone out before the
crash. list_stuck_notifications prints such rows.

TESTS (S8). settings.NOTIFY_DEFERRED_SYNC (True in solarpms.test_settings) runs the job
inline at hand-off, still after commit, and does not close the connection, which would
destroy an in-memory test database. Real threads are exercised by notify_deferred_smoke
on local Postgres.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.db import close_old_connections, connection, transaction

from .models import NotificationLog, UserProfile
from .notifications import finish_pending, send_notification

logger = logging.getLogger(__name__)

# PER PROCESS, and small on purpose: every gunicorn worker has its own pool, so the
# site's concurrent provider traffic is MAX_WORKERS x the worker count, and every thread
# holds a database connection while it works. Production runs (or will run) 2 workers,
# so 2 here keeps the total at 4 (NTF-DCG S3a, W6). The worker count is set by the
# GUNICORN_CMD_ARGS Railway variable, not by the Procfile, which passes no --workers:
# raising it there multiplies this.
MAX_WORKERS = 2

_POOL = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix='notify-deferred')


def defer(*, recipient, channel, row_fields, send_kwargs):
    """Write the 'pending' row for one deferred send and hand it off. Returns the row pk.

    recipient   : UserProfile; the job loads it again by pk on its own connection.
    channel     : 'email' or 'whatsapp', the row's channel.
    row_fields  : the row's fixed fields, as notify._log_base builds them, plus
                  event_code and template_version.
    send_kwargs : send_notification()'s arguments other than recipient and log_row_id;
                  `channels` must be [channel].

    Raises if the row cannot be written: _send_to_one() then logs 'failed' as it does
    for any other fault, and nothing is handed off, because a send without a row
    could not be seen or finished.
    """
    row = NotificationLog.objects.create(**row_fields, channel=channel, status='pending')
    # A lambda, as in notify(): robust on_commit names the callback in its log line.
    transaction.on_commit(lambda: _submit(row.pk, recipient.pk, send_kwargs), robust=True)
    return row.pk


def _submit(row_id, recipient_id, send_kwargs):
    if getattr(settings, 'NOTIFY_DEFERRED_SYNC', False):
        _run(row_id, recipient_id, send_kwargs, close_connection=False)
        return
    try:
        _POOL.submit(_run, row_id, recipient_id, send_kwargs, True)
    except Exception as exc:
        # The pool refuses new work once the interpreter has begun shutting down
        # (RuntimeError); the row is finished as failed rather than left pending.
        logger.exception('notify_deferred: could not hand off row %s', row_id)
        finish_pending(row_id, 'failed', f'not handed off: {type(exc).__name__}: {exc}')


def _run(row_id, recipient_id, send_kwargs, close_connection):
    """The job: one send_notification() call that finishes row `row_id`. Never raises;
    any fault finishes the row as failed."""
    if close_connection:
        close_old_connections()
    try:
        # Loaded fresh on this thread's own connection, with its user, so the
        # preference and address read are the ones current at send time.
        recipient = UserProfile.objects.select_related('user').get(pk=recipient_id)
        send_notification(recipient=recipient, log_row_id=row_id, **send_kwargs)
    except Exception as exc:
        logger.exception('notify_deferred: job for row %s failed', row_id)
        finish_pending(row_id, 'failed', f'{type(exc).__name__}: {exc}')
    finally:
        if close_connection:
            connection.close()
