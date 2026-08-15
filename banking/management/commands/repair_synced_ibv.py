"""Restore existing synced IBV data for stuck pending/expired applications.

Default is dry-run. Pass --apply only after reviewing the output.

Examples:
  python manage.py repair_synced_ibv
  python manage.py repair_synced_ibv --status ibv_pending --status expired --source arrive --apply
"""

from django.core.management.base import BaseCommand, CommandError

from accounts.models import Customer
from banking.repair import (
    REPAIRABLE_LOAN_STATUSES,
    apply_synced_ibv_repair,
    find_repairable_synced_ibv,
)


class Command(BaseCommand):
    help = (
        "Find pending/expired applications that already have synced IBV data "
        "on an older connection, and optionally move them to pending signature."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--status",
            action="append",
            choices=REPAIRABLE_LOAN_STATUSES,
            help=(
                "Loan status to include. Can be passed multiple times. "
                "Defaults to ibv_pending and expired."
            ),
        )
        parser.add_argument(
            "--source",
            choices=["arrive", "organic", "all"],
            default="arrive",
            help="Customer source to include. Defaults to arrive.",
        )
        parser.add_argument(
            "--customer-id",
            default=None,
            help="Limit to one customer UUID.",
        )
        parser.add_argument(
            "--email",
            default=None,
            help="Limit to one customer email.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Max number of repairs to apply after matching.",
        )
        parser.add_argument(
            "--allow-no-transactions",
            action="store_true",
            help="Allow synced connections with accounts but no transactions.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply repairs. Without this flag only print the plan.",
        )

    def handle(self, *args, **options):
        statuses = tuple(options["status"] or REPAIRABLE_LOAN_STATUSES)
        limit = options["limit"]
        if limit is not None and limit < 1:
            raise CommandError("--limit must be >= 1")

        customers = Customer.objects.filter(loans__status__in=statuses).distinct()
        if options["source"] != "all":
            customers = customers.filter(source=options["source"])
        if options["customer_id"]:
            customers = customers.filter(id=options["customer_id"])
        if options["email"]:
            customers = customers.filter(email__iexact=options["email"])

        plans = []
        skipped = 0
        for customer in customers.order_by("email", "id").iterator():
            plan = find_repairable_synced_ibv(
                customer,
                statuses=statuses,
                require_transactions=not options["allow_no_transactions"],
            )
            if plan is None:
                skipped += 1
                continue
            plans.append(plan)
            if limit is not None and len(plans) >= limit:
                break

        mode = "APPLY" if options["apply"] else "DRY-RUN"
        self.stdout.write(
            f"{mode}: {len(plans)} repairable customer(s); skipped {skipped}."
        )

        repaired = 0
        for plan in plans:
            line = (
                f"customer={plan.customer.email} loan={plan.loan.id} "
                f"status={plan.loan.status} restore_connection={plan.restore_connection.id} "
                f"deactivate={','.join(str(c.id) for c in plan.deactivate_connections) or '-'} "
                f"bank_account={plan.bank_account.id if plan.bank_account else '-'}"
            )
            if not options["apply"]:
                self.stdout.write(f"WOULD REPAIR {line}")
                continue

            result = apply_synced_ibv_repair(plan)
            repaired += 1
            self.stdout.write(
                "REPAIRED "
                f"customer={result.customer_email} loan={result.loan_id} "
                f"{result.previous_loan_status}->{result.new_loan_status} "
                f"connection={result.restored_connection_id}"
            )

        if options["apply"]:
            self.stdout.write(self.style.SUCCESS(f"Repaired {repaired} customer(s)."))
        else:
            self.stdout.write("No records changed. Pass --apply to repair these customers.")
