# PART 4.6 — the Design Head triages PM change requests.
#
# THE BACKFILL IS THE PART THAT NEEDS EXPLAINING.
#
# Under Part 4 a raised change request opened attempt N+1 in the same transaction that
# created the row, so `resulting_attempt` was set on every request the UI could produce
# and null meant only "made outside the UI". That is exactly the classification this
# migration needs, and it is unambiguous:
#
#     resulting_attempt IS NOT NULL  ->  verdict='accepted'
#
# An attempt exists because this request caused it. Nobody can now argue the Head might
# have rejected it — the rework was already done. `decided_by` and `decided_at` are left
# NULL and that is deliberate: naming a decider would manufacture a triage that never
# happened, and stamping `requested_at` as the decision time would put a Head verdict in
# the record with nobody behind it. Null is the honest statement "pre-amendment row —
# accepted by the old automatic rule, not by a person."
#
# Rows with a NULL resulting_attempt keep the `pending` default. Under Part 4 those could
# only come from Django admin or an import (deferred finding G6), and pending is the
# correct thing to say about them: nobody has ruled.
#
# ORDER MATTERS. The RunPython runs BEFORE AddConstraint, because the partial unique
# constraint allows one `pending` row per attempt and every row starts at the `pending`
# default. Two pre-amendment accepted requests on one attempt — legal under Part 4, since
# nothing stopped a PM raising a second request against an attempt that had already
# spawned its successor — would collide on the way in if the constraint existed first.
#
# The reverse is a genuine inverse: it clears the verdict back to the default before the
# AddField reversals drop the columns, so `migrate projects 0057` then `migrate projects
# 0058` returns the same rows. No data is lost either way — `resulting_attempt`, which is
# what the forward pass READS, is never written by this migration.

import django.db.models.deletion
from django.db import migrations, models


def mark_pre_amendment_requests_accepted(apps, schema_editor):
    """Every Part 4 request that opened an attempt was accepted. See the note above."""
    DesignChangeRequest = apps.get_model('projects', 'DesignChangeRequest')

    # One UPDATE regardless of row count; the source column is the only thing read.
    migrated = (DesignChangeRequest.objects
                .filter(resulting_attempt__isnull=False)
                .update(verdict='accepted'))
    print(f'    Part 4.6: {migrated} pre-amendment change request(s) marked accepted.')


def unmark_pre_amendment_requests(apps, schema_editor):
    """Reverse. The verdict column is dropped by the AddField reversal immediately after
    this runs, so this exists to make the RunPython reversible in its own right rather
    than to leave a particular state behind."""
    DesignChangeRequest = apps.get_model('projects', 'DesignChangeRequest')
    DesignChangeRequest.objects.update(verdict='pending')


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0057_boqitemmaster_project_type_opex_catalogue'),
    ]

    operations = [
        migrations.AddField(
            model_name='designchangerequest',
            name='verdict',
            field=models.CharField(
                choices=[('pending', 'Pending'), ('accepted', 'Accepted'),
                         ('rejected', 'Rejected')],
                default='pending', max_length=10),
        ),
        migrations.AddField(
            model_name='designchangerequest',
            name='decided_by',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='triaged_design_change_requests',
                to='projects.userprofile'),
        ),
        migrations.AddField(
            model_name='designchangerequest',
            name='decided_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='designchangerequest',
            name='rejection_reason',
            field=models.TextField(blank=True, default=''),
        ),
        # Runs BEFORE the constraints — see the ordering note at the top of this file.
        migrations.RunPython(
            mark_pre_amendment_requests_accepted,
            unmark_pre_amendment_requests,
        ),
        migrations.AddConstraint(
            model_name='designchangerequest',
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(('verdict', 'rejected'), _negated=True),
                    models.Q(('rejection_reason', ''), _negated=True),
                    _connector='OR'),
                name='cr_rejection_reason_required_when_rejected'),
        ),
        migrations.AddConstraint(
            model_name='designchangerequest',
            constraint=models.UniqueConstraint(
                condition=models.Q(('verdict', 'pending')),
                fields=('attempt',),
                name='uniq_pending_change_request_per_attempt'),
        ),
    ]
