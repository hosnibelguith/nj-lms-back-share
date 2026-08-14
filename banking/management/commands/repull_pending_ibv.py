"""Queue GetAccountsDetail re-pull for stuck / pending IBV.

Default is dry-run. Pass --apply to enqueue Celery tasks.

Examples:
  python manage.py repull_pending_ibv
  python manage.py repull_pending_ibv --since 2026-08-14 --apply
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_date, parse_datetime

from banking.tasks import pending_ibv_repull_targets, queue_flinks_gad_repull


class Command(BaseCommand):
    help = (
        "Re-queue Flinks GetAccountsDetail for customers still pending IBV "
        "(failed / pending / inactive LoginId). Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--since",
            default=None,
            help="Only connections updated on/after this date (YYYY-MM-DD or ISO datetime).",
        )
        parser.add_argument(
            "--customer-id",
            default=None,
            help="Limit to one customer UUID.",
        )
        parser.add_argument(
            "--include-syncing",
            action="store_true",
            help="Also re-queue connections currently marked syncing.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Max customers to queue (after latest-per-customer).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Enqueue re-pulls. Without this flag only print the plan.",
        )

    def handle(self, *args, **options):
        since = self._parse_since(options["since"])
        targets = pending_ibv_repull_targets(
            since=since,
            customer_id=options["customer_id"],
            include_syncing=options["include_syncing"],
        )
        limit = options["limit"]
        if limit is not None:
            if limit < 1:
                raise CommandError("--limit must be >= 1")
            targets = targets[:limit]

        mode = "APPLY" if options["apply"] else "DRY-RUN"
        self.stdout.write(f"{mode}: {len(targets)} pending IBV connection(s).")
        queued = 0
        skipped = 0
        for connection in targets:
            customer = connection.customer
            line = (
                f"  customer={customer.email} connection={connection.id} "
                f"status={connection.sync_status} active={connection.is_active}"
            )
            if not options["apply"]:
                self.stdout.write(f"WOULD REPULL{line}")
                continue
            try:
                queue_flinks_gad_repull(connection, user=None)
            except ValueError as exc:
                skipped += 1
                self.stdout.write(f"SKIP{line} reason={exc}")
                continue
            queued += 1
            self.stdout.write(f"QUEUED{line}")

        if options["apply"]:
            self.stdout.write(
                self.style.SUCCESS(f"Queued {queued} IBV re-pull(s); skipped {skipped}.")
            )
        else:
            self.stdout.write("No tasks queued (dry-run). Pass --apply to enqueue.")

    @staticmethod
    def _parse_since(raw):
        if not raw:
            return None
        parsed = parse_datetime(raw) or parse_date(raw)
        if parsed is None:
            raise CommandError(f"Invalid --since value: {raw}")
        return parsed
