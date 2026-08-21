"""Align remaining scheduled PAD rows after staff-recorded Interac/manual payments.

Completed etransfer/manual rows already reduced loan.balance. This command
re-trims or restores scheduled installments so they match that remaining
balance. NSF/failed amounts are never changed.

Default is dry-run. Pass --apply to write.

Examples:
  python manage.py heal_recorded_payment_schedules
  python manage.py heal_recorded_payment_schedules --apply
  python manage.py heal_recorded_payment_schedules --loan-id <uuid> --apply
"""

from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from loans.models import Loan
from loans.services import LoanService


class Command(BaseCommand):
    help = (
        "Re-align scheduled installments to remaining balance for loans that "
        "have completed Interac/manual payments. Does not change missed amounts."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--loan-id",
            default=None,
            help="Heal a single loan UUID. Default: all matching loans.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist changes. Without this flag the command only simulates.",
        )

    def handle(self, *args, **options):
        queryset = (
            Loan.objects.filter(
                payments__type__in=("manual", "etransfer"),
                payments__status="completed",
            )
            .filter(status__in=("active", "defaulted", "paid_off"))
            .distinct()
            .order_by("created_at")
        )
        loan_id = options["loan_id"]
        if loan_id:
            queryset = queryset.filter(pk=loan_id)
            if not queryset.exists():
                raise CommandError(f"No matching loan with recorded payments: {loan_id}")

        apply_changes = options["apply"]
        money = LoanService.money
        healed = 0
        skipped = 0

        for loan in queryset:
            scheduled = list(loan.payments.filter(status="scheduled").order_by("scheduled_date", "id"))
            current = money(
                sum((money(row.amount or Decimal("0.00")) for row in scheduled), Decimal("0.00"))
            )
            target = money(loan.balance or Decimal("0.00"))
            extra = money(current - target)
            customer = getattr(loan, "customer", None)
            label = (
                f"{getattr(customer, 'first_name', '')} {getattr(customer, 'last_name', '')}".strip()
                or str(loan.id)
            )
            if extra == 0:
                skipped += 1
                self.stdout.write(
                    f"OK {loan.id} {label}: scheduled {current} already matches balance {target}"
                )
                continue

            self.stdout.write(
                f"{'APPLY' if apply_changes else 'DRY'} {loan.id} {label}: "
                f"scheduled {current} → {target} (delta {extra})"
            )
            if apply_changes:
                LoanService._align_scheduled_payments_to_balance(loan)
                healed += 1
            else:
                healed += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"{'Updated' if apply_changes else 'Would update'} {healed} loan(s); "
                f"{skipped} already aligned."
            )
        )
