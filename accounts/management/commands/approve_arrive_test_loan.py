from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from accounts.models import Customer, User
from loans.services import LoanService


class Command(BaseCommand):
    help = "Approve an Arrive test loan by customer email (triggers Arrive decision webhook)."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--confirm", required=True, help='Must be "APPROVE"')
        parser.add_argument("--notes", default="Arrive joint QA approval")
        parser.add_argument(
            "--approved-amount",
            default=None,
            help="Optional override of loan.principal sent as approved_amount (e.g. 500).",
        )

    def handle(self, *args, **options):
        if options["confirm"] != "APPROVE":
            raise CommandError('Pass --confirm APPROVE to proceed.')

        email = options["email"].strip()
        customers = list(Customer.objects.filter(email__iexact=email))
        if not customers:
            # fallback common typo used in Arrive UI
            customers = list(Customer.objects.filter(email__icontains="samuel"))
        if not customers:
            raise CommandError(f"No customer for {email}")

        for customer in customers:
            self.stdout.write(
                f"CUSTOMER {customer.id} {customer.email} "
                f"banking={customer.banking_verified} contract={customer.contract_completed} "
                f"stage={customer.onboarding_stage} arrive_app={customer.arrive_application_id} "
                f"requested={customer.requested_loan_amount}"
            )
            loans = list(customer.loans.order_by("-created_at"))
            if not loans:
                self.stdout.write("NO_LOANS")
                continue

            loan = loans[0]
            self.stdout.write(
                f"LOAN_BEFORE {loan.id} status={loan.status} "
                f"principal={loan.principal} signed_at={loan.contract_signed_at}"
            )

            if loan.status in ["human_approved", "pending_funding", "active", "human_declined", "ai_declined"]:
                raise CommandError(f"Loan already decided/funded: status={loan.status}")

            if options["approved_amount"] is not None:
                amount = Decimal(str(options["approved_amount"]))
                loan.principal = amount
                loan.save(update_fields=["principal", "updated_at"])
                # Reprice fee/total/calendar from LoanFormula (brokerage + daily interest).
                # Keep principal as the Arrive approved_amount / card fund amount.
                LoanService.rebuild_payment_schedule(loan, reprice=True)
                loan.refresh_from_db()
                self.stdout.write(
                    f"SET_APPROVED_AMOUNT principal={loan.principal} "
                    f"fee={loan.fee} total={loan.total_amount}"
                )

            # Contract-signed apps land in pending; still allow pending_signature by moving to pending.
            if loan.status == "pending_signature":
                if not loan.contract_signed_at and not customer.contract_completed:
                    raise CommandError("Contract not signed yet; cannot approve.")
                loan.status = "pending"
                loan.save(update_fields=["status", "updated_at"])
                self.stdout.write("NORMALIZED_STATUS pending")

            staff = (
                User.objects.filter(user_type="staff", is_active=True).order_by("date_joined").first()
                or User.objects.filter(is_staff=True, is_active=True).order_by("date_joined").first()
            )
            self.stdout.write(f"APPROVER {getattr(staff, 'id', None)} {getattr(staff, 'email', None)}")

            try:
                loan = LoanService.approve_loan(
                    loan,
                    approved_by=staff,
                    notes=options["notes"]
                    + (f" (approved_amount={options['approved_amount']})" if options["approved_amount"] else ""),
                    source="human",
                )
            except Exception as exc:
                raise CommandError(f"Approve failed: {exc}") from exc

            loan.refresh_from_db()
            self.stdout.write(
                f"LOAN_AFTER {loan.id} status={loan.status} "
                f"principal={loan.principal} approved_at={loan.approved_at}"
            )

        self.stdout.write(self.style.SUCCESS("DONE"))
