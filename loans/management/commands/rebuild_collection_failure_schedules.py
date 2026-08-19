"""Rebuild old collection-failure schedule rows into capped fee buckets.

Default is dry-run. Pass --apply to persist changes.
"""

from django.core.management.base import BaseCommand, CommandError

from loans.models import Loan
from loans.services import LoanService


class Command(BaseCommand):
    help = (
        "Simulate or apply a rebuild of generated collection-failure recovery, "
        "fee, and interest rows using the current capped fee-bucket logic."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--loan-id",
            default=None,
            help="Only rebuild one loan UUID. Default scans loans with generated failure rows.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist changes. Without this flag the command only simulates.",
        )

    def handle(self, *args, **options):
        dry_run = not options["apply"]
        loan_id = options["loan_id"]
        if loan_id:
            try:
                loans = [Loan.objects.get(pk=loan_id)]
            except Loan.DoesNotExist as exc:
                raise CommandError(f"Loan not found: {loan_id}") from exc
        else:
            loans = list(
                Loan.objects.filter(
                    payments__notes__startswith=LoanService.COLLECTION_FAILURE_RECOVERY_NOTE
                )
                .distinct()
                .order_by("created_at", "id")
            )

        mode = "DRY-RUN" if dry_run else "APPLIED"
        self.stdout.write(self.style.NOTICE(f"=== {mode} ==="))
        self.stdout.write(f"loans_found={len(loans)}")

        changed = 0
        for loan in loans:
            plan = LoanService.rebuild_collection_failure_schedule(
                loan,
                dry_run=dry_run,
            )
            if plan["collections_count"] == 0:
                continue

            changed += 1
            self.stdout.write("")
            self.stdout.write(
                f"loan={plan['loan_id']} collections={plan['collections_count']} "
                f"delete={plan['delete_count']} create={plan['create_count']} "
                f"frequency_days={plan['frequency_days']}"
            )
            self.stdout.write(
                f"generated_total {plan['existing_generated_total']} -> "
                f"{plan['new_generated_total']} | balance "
                f"{plan['balance_before']} -> {plan['balance_after']} | total "
                f"{plan['total_amount_before']} -> {plan['total_amount_after']}"
            )

            self.stdout.write("  DELETE:")
            if not plan["will_delete"]:
                self.stdout.write("    (none)")
            for row in plan["will_delete"]:
                self.stdout.write(
                    f"    {row['scheduled_date']}  ${row['amount']}  "
                    f"{row['status']}  {row['notes']}  id={row['id']}"
                )

            self.stdout.write("  CREATE:")
            if not plan["proposed"]:
                self.stdout.write("    (none)")
            for row in plan["proposed"]:
                self.stdout.write(
                    f"    {row['scheduled_date']}  ${row['amount']}  "
                    f"{row['status']}  {row['kind']}"
                )

        self.stdout.write("")
        self.stdout.write(f"loans_changed={changed}")
        if dry_run:
            self.stdout.write(
                self.style.WARNING("Simulation only. Re-run with --apply to persist.")
            )
        else:
            self.stdout.write(self.style.SUCCESS("Collection-failure schedules rebuilt."))
