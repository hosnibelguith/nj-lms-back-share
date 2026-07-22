from django.core.management.base import BaseCommand, CommandError

from accounts.models import Customer, User
from loans.models import Loan
from loans.services import LoanService


class Command(BaseCommand):
    help = "Approve an Arrive test loan by customer email (triggers Arrive decision webhook)."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--confirm", required=True, help='Must be "APPROVE"')
        parser.add_argument("--notes", default="Arrive joint QA approval")

    def handle(self, *args, **options):
        if options["confirm"] != "APPROVE":
            raise CommandError('Pass --confirm APPROVE to proceed.')

        email = options["email"].strip()
        customers = list(Customer.objects.filter(email__iexact=email))
        if not customers:
            raise CommandError(f"No customer for {email}")

        for customer in customers:
            self.stdout.write(
                f"CUSTOMER {customer.id} {customer.email} "
                f"banking={customer.banking_verified} contract={customer.contract_completed} "
                f"stage={customer.onboarding_stage} arrive_app={customer.arrive_application_id}"
            )
            loans = list(customer.loans.order_by("-created_at"))
            if not loans:
                self.stdout.write("NO_LOANS")
                continue

            loan = loans[0]
            self.stdout.write(f"LOAN_BEFORE {loan.id} status={loan.status} amount={loan.amount}")

            staff = (
                User.objects.filter(user_type="staff", is_active=True).order_by("date_joined").first()
                or User.objects.filter(is_staff=True, is_active=True).order_by("date_joined").first()
            )
            self.stdout.write(f"APPROVER {getattr(staff, 'id', None)} {getattr(staff, 'email', None)}")

            try:
                loan = LoanService.approve_loan(
                    loan,
                    approved_by=staff,
                    notes=options["notes"],
                    source="human",
                )
            except Exception as exc:
                raise CommandError(f"Approve failed: {exc}") from exc

            loan.refresh_from_db()
            self.stdout.write(
                f"LOAN_AFTER {loan.id} status={loan.status} approved_at={loan.approved_at}"
            )

        self.stdout.write(self.style.SUCCESS("DONE"))
