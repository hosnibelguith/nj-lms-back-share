from decimal import Decimal
from django.core.management.base import BaseCommand
from loans.models import Loan

class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--loan-id", required=True)
        parser.add_argument("--principal", required=True)
        parser.add_argument("--confirm", required=True)

    def handle(self, *args, **options):
        if options["confirm"] != "FIX":
            raise SystemExit("need --confirm FIX")
        loan = Loan.objects.get(id=options["loan_id"])
        principal = Decimal(str(options["principal"]))
        self.stdout.write(f"BEFORE principal={loan.principal} fee={loan.fee} total={loan.total_amount} balance={loan.balance} status={loan.status}")
        loan.principal = principal
        loan.fee = Decimal("0.00")
        loan.total_amount = principal
        loan.balance = principal
        loan.save(update_fields=["principal", "fee", "total_amount", "balance", "updated_at"])
        loan.refresh_from_db()
        self.stdout.write(f"AFTER principal={loan.principal} fee={loan.fee} total={loan.total_amount} balance={loan.balance} status={loan.status}")
        self.stdout.write("DONE")
