from django.core.management.base import BaseCommand

from projects.models import NotificationLog


class Command(BaseCommand):
    help = "Print the last 50 NotificationLog entries (template, status, error, recipient, created_at)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=50,
            help="Number of entries to show (default: 50)",
        )
        parser.add_argument(
            "--channel", type=str, default="",
            help="Filter by channel: whatsapp | email | in_app",
        )
        parser.add_argument(
            "--status", type=str, default="",
            help="Filter by status: sent | failed | skipped",
        )

    def handle(self, *args, **options):
        limit   = options["limit"]
        channel = options["channel"]
        status  = options["status"]

        qs = NotificationLog.objects.select_related("recipient__user").order_by("-created_at")
        if channel:
            qs = qs.filter(channel=channel)
        if status:
            qs = qs.filter(status=status)
        entries = qs[:limit]

        count = entries.count()
        self.stdout.write(self.style.SUCCESS(
            f"\n{'─'*90}\n  Last {count} NotificationLog entries"
            + (f"  [channel={channel}]" if channel else "")
            + (f"  [status={status}]" if status else "")
            + f"\n{'─'*90}"
        ))

        COL = "{:<28}  {:<8}  {:<10}  {:<28}  {}"
        self.stdout.write(COL.format(
            "template_name", "channel", "status", "recipient", "created_at"
        ))
        self.stdout.write("─" * 90)

        for log in entries:
            recipient_str = str(log.recipient) if log.recipient_id else "(none)"
            template_str  = (log.template_name or "(none)")[:28]
            created_str   = log.created_at.strftime("%Y-%m-%d %H:%M:%S") if log.created_at else ""

            status_label = log.status
            if log.status == "failed":
                status_label = self.style.ERROR(log.status)
            elif log.status == "sent":
                status_label = self.style.SUCCESS(log.status)

            self.stdout.write(COL.format(
                template_str,
                log.channel or "",
                log.status or "",
                recipient_str[:28],
                created_str,
            ))

            if log.error_detail:
                self.stdout.write(
                    self.style.WARNING(f"    ERROR: {log.error_detail[:200]}")
                )

        self.stdout.write("─" * 90)
        self.stdout.write(f"Total shown: {count}\n")
