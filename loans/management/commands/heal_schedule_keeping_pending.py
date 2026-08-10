"""Heal upcoming schedule rows without touching in-flight (Pending) payments.

Default is dry-run (simulate only). Pass --apply to write.

Examples:
  python manage.py heal_schedule_keeping_pending \\
      --loan-id <uuid> --payment-amount 147.18 --frequency bi-weekly

  python manage.py heal_schedule_keeping_pending \\
      --loan-id <uuid> --payment-amount 147.18 --frequency bi-weekly --apply
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_date

from loans.models import Loan
from loans.services import LoanService


class Command(BaseCommand):
    help = (
        "Simulate or apply a rebuild of Scheduled/failed/nsf installments while "
        "keeping Pending / in-flight collection payments untouched."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--loan-id",
            required=True,
            help="Loan UUID to heal.",
        )
        parser.add_argument(
            "--payment-amount",
            default=None,
            help="Target installment amount (payment_amount mode).",
        )
        parser.add_argument(
            "--number-of-payments",
            type=int,
            default=None,
            help="Number of new scheduled installments (number_of_payments mode).",
        )
        parser.add_argument(
            "--frequency",
            choices=["weekly", "bi-weekly", "monthly"],
            default="bi-weekly",
        )
        parser.add_argument(
            "--start-date",
            default=None,
            help="First new scheduled date (YYYY-MM-DD). Default: after last Pending.",
        )
        parser.add_argument(
            "--reprice",
            action="store_true",
            help=(
                "Recompute loan.total_amount with Adjust Schedule interest math, "
                "then schedule only the remainder after completed + Pending."
            ),
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist changes. Without this flag the command only simulates.",
        )
        parser.add_argument(
            "--notes",
            default="",
            help="Optional note stored on the activity log when applying.",
        )

    def handle(self, *args, **options):
        loan_id = options["loan_id"]
        try:
            loan = Loan.objects.select_related("customer", "formula").get(pk=loan_id)
        except Loan.DoesNotExist as exc:
            raise CommandError(f"Loan not found: {loan_id}") from exc

        payment_amount = None
        if options["payment_amount"] is not None:
            try:
                payment_amount = Decimal(str(options["payment_amount"]))
            except (InvalidOperation, TypeError) as exc:
                raise CommandError("Invalid --payment-amount") from exc

        number_of_payments = options["number_of_payments"]
        if payment_amount is not None and number_of_payments is not None:
            raise CommandError("Use either --payment-amount or --number-of-payments, not both.")
        if payment_amount is None and number_of_payments is None:
            # Allow auto-detect from existing scheduled amounts inside the service.
            calculation_mode = "payment_amount"
        elif number_of_payments is not None:
            calculation_mode = "number_of_payments"
        else:
            calculation_mode = "payment_amount"

        start_date = None
        if options["start_date"]:
            start_date = parse_date(options["start_date"])
            if start_date is None:
                try:
                    start_date = datetime.strptime(options["start_date"], "%Y-%m-%d").date()
                except ValueError as exc:
                    raise CommandError("Invalid --start-date; use YYYY-MM-DD") from exc

        dry_run = not options["apply"]
        try:
            plan = LoanService.heal_upcoming_schedule_keeping_pending(
                loan,
                calculation_mode=calculation_mode,
                payment_amount=payment_amount,
                number_of_payments=number_of_payments,
                frequency=options["frequency"],
                start_date=start_date,
                reprice=bool(options["reprice"]),
                dry_run=dry_run,
                user=None,
                notes=options["notes"] or "",
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        mode = "DRY-RUN (simulate only)" if plan["dry_run"] else "APPLIED"
        self.stdout.write(self.style.NOTICE(f"=== {mode} ==="))
        self.stdout.write(
            f"loan={plan['loan_id']} status={plan['loan_status']} "
            f"frequency={plan['frequency']} start={plan['start_date']} "
            f"reprice={plan['reprice']}"
        )
        self.stdout.write(
            f"totals: before={plan['total_amount_before']} "
            f"after={plan['total_amount_after']} | "
            f"balance before={plan['balance_before']} after={plan['balance_after']}"
        )
        self.stdout.write(
            f"reserved: completed={plan['completed_sum']} "
            f"protected(pending)={plan['protected_sum']} | "
            f"new schedule_total={plan['schedule_total']} "
            f"open_sum_after={plan['open_sum_after']}"
        )

        self.stdout.write("\nKEEP (untouched):")
        if not plan["protected"]:
            self.stdout.write("  (none)")
        for row in plan["protected"]:
            self.stdout.write(
                f"  {row['scheduled_date']}  ${row['amount']}  {row['status']}  id={row['id']}"
            )

        self.stdout.write("\nDELETE:")
        if not plan["will_delete"]:
            self.stdout.write("  (none)")
        for row in plan["will_delete"]:
            self.stdout.write(
                f"  {row['scheduled_date']}  ${row['amount']}  {row['status']}  id={row['id']}"
            )

        self.stdout.write("\nCREATE (proposed upcoming schedule):")
        if not plan["proposed"]:
            self.stdout.write("  (none — nothing left to schedule)")
        for row in plan["proposed"]:
            self.stdout.write(
                f"  {row['scheduled_date']}  ${row['amount']}  {row['status']}"
            )

        if plan["dry_run"]:
            self.stdout.write(
                self.style.WARNING(
                    "\nSimulation only. Re-run with --apply to persist these changes."
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS("\nSchedule heal applied."))
