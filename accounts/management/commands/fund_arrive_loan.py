from django.core.management.base import BaseCommand, CommandError

from loans.models import Loan
from loans.services import LoanService


class Command(BaseCommand):
    help = (
        "Mark an Arrive loan as funded/active without re-sending the decision webhook. "
        "Use after card funding already happened on Arrive."
    )

    def add_arguments(self, parser):
        parser.add_argument("--loan-id", required=True)
        parser.add_argument("--confirm", required=True, help='Must be "FUND"')
        parser.add_argument(
            "--reference",
            default="ARRIVE-BACKFILL-NO-WEBHOOK",
            help="Funding reference stored on the loan.",
        )

    def handle(self, *args, **options):
        if options["confirm"] != "FUND":
            raise CommandError('Pass --confirm FUND to proceed.')

        try:
            loan = Loan.objects.select_related("customer").get(id=options["loan_id"])
        except Loan.DoesNotExist as exc:
            raise CommandError(f"Loan not found: {options['loan_id']}") from exc

        self.stdout.write(
            f"BEFORE status={loan.status} funded_at={loan.funded_at} "
            f"total={loan.total_amount} payments={loan.payments.count()}"
        )

        if loan.status == "active" and loan.funded_at:
            self.stdout.write(self.style.WARNING("ALREADY_FUNDED"))
            return

        if loan.status == "human_approved":
            loan.status = "pending_funding"
            loan.save(update_fields=["status", "updated_at"])
            self.stdout.write("NORMALIZED_STATUS pending_funding")

        if loan.status != "pending_funding":
            raise CommandError(f"Cannot fund loan in status={loan.status}")

        loan = LoanService.fund_loan(
            loan,
            method="arrive_card",
            reference=options["reference"],
        )
        loan.refresh_from_db()
        self.stdout.write(
            f"AFTER status={loan.status} funded_at={loan.funded_at} "
            f"total={loan.total_amount} payments={loan.payments.count()}"
        )
        self.stdout.write(self.style.SUCCESS("DONE"))
