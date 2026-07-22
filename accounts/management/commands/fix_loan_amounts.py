from decimal import Decimal

from django.core.management.base import BaseCommand

from loans.models import Loan
from loans.services import LoanService


class Command(BaseCommand):
    help = "Set loan principal/total and rebuild the payment calendar to match."

    def add_arguments(self, parser):
        parser.add_argument("--loan-id", required=True)
        parser.add_argument("--principal", required=True)
        parser.add_argument("--confirm", required=True)

    def handle(self, *args, **options):
        if options["confirm"] != "FIX":
            raise SystemExit('need --confirm FIX')
        loan = Loan.objects.get(id=options["loan_id"])
        principal = Decimal(str(options["principal"]))
        before_payments = list(
            loan.payments.order_by("scheduled_date").values_list("amount", "status")
        )
        self.stdout.write(
            f"BEFORE principal={loan.principal} fee={loan.fee} total={loan.total_amount} "
            f"balance={loan.balance} status={loan.status} payments={before_payments}"
        )
        loan.principal = principal
        loan.fee = Decimal("0.00")
        loan.total_amount = principal
        loan.balance = principal
        loan.save(update_fields=["principal", "fee", "total_amount", "balance", "updated_at"])

        payments = LoanService.rebuild_payment_schedule(loan)
        loan.refresh_from_db()
        after_payments = [(str(p.amount), p.status) for p in payments]
        self.stdout.write(
            f"AFTER principal={loan.principal} fee={loan.fee} total={loan.total_amount} "
            f"balance={loan.balance} status={loan.status} payments={after_payments}"
        )
        self.stdout.write("DONE")
