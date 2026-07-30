from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import Customer, User
from banking.models import BankAccount, BankConnection, FinancialAnalysisReport
from contracts.models import Contract
from contracts.services import AGREEMENT_VERSION, render_loan_agreement
from loans.models import Loan
from loans.services import LoanService
from loans.zumrails import account_snapshot


PASSWORD = "TestPass123!"
STAFF_EMAIL = "qa.staff@example.com"


CUSTOMERS = [
    {
        "email": "qa.signature@example.com",
        "first_name": "Quinn",
        "last_name": "Signature",
        "phone": "+14165550101",
        "phone_normalized": "+14165550101",
        "province": "ON",
        "requested_amount": Decimal("500.00"),
        "banking_verified": True,
        "references_completed": False,
        "contract_completed": False,
        "onboarding_stage": "contract",
        "loan_status": "pending_funding",
        "ai_decision": "approved",
        "description": "Approved by staff but blocked until customer signs.",
    },
    {
        "email": "qa.review@example.com",
        "first_name": "Riley",
        "last_name": "Review",
        "phone": "+14165550102",
        "phone_normalized": "+14165550102",
        "province": "BC",
        "requested_amount": Decimal("750.00"),
        "banking_verified": True,
        "references_completed": True,
        "contract_completed": True,
        "onboarding_stage": "portal_active",
        "loan_status": "pending",
        "ai_decision": "review_required",
        "description": "Signed and ready for human approval, then funding.",
    },
    {
        "email": "qa.banking@example.com",
        "first_name": "Casey",
        "last_name": "Banking",
        "phone": "+14165550103",
        "phone_normalized": "+14165550103",
        "province": "AB",
        "requested_amount": Decimal("300.00"),
        "banking_verified": False,
        "references_completed": False,
        "contract_completed": False,
        "onboarding_stage": "banking_verification",
        "loan_status": "ibv_pending",
        "ai_decision": None,
        "description": "Banking not complete; contract/signing should be blocked.",
    },
]


class Command(BaseCommand):
    help = "Create deterministic QA users for customer, signature, approval, and banking workflow testing."

    def handle(self, *args, **options):
        with transaction.atomic():
            staff = self._upsert_user(
                email=STAFF_EMAIL,
                full_name="QA Staff Admin",
                phone="+14165550000",
                user_type="staff",
                permission_level=5,
                is_staff=True,
                is_superuser=True,
            )

            self.stdout.write(self.style.SUCCESS("Staff login"))
            self.stdout.write(f"  {staff.email} / {PASSWORD}")

            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("Customer logins"))

            for index, spec in enumerate(CUSTOMERS, start=1):
                customer = self._seed_customer(index, spec)
                loan = customer.loans.order_by("-created_at").first()
                self.stdout.write(
                    f"  {customer.email} / {PASSWORD} | "
                    f"{customer.full_name} | customer={customer.id} | "
                    f"loan={loan.id} | status={loan.status} | {spec['description']}"
                )

    def _upsert_user(
        self,
        *,
        email,
        full_name,
        phone,
        user_type,
        permission_level,
        is_staff=False,
        is_superuser=False,
    ):
        user, _ = User.objects.update_or_create(
            email=email,
            defaults={
                "full_name": full_name,
                "phone": phone,
                "phone_normalized": phone,
                "user_type": user_type,
                "permission_level": permission_level,
                "is_staff": is_staff,
                "is_superuser": is_superuser,
                "is_active": True,
            },
        )
        user.set_password(PASSWORD)
        user.save(update_fields=["password"])
        return user

    def _seed_customer(self, index, spec):
        full_name = f"{spec['first_name']} {spec['last_name']}"
        user = self._upsert_user(
            email=spec["email"],
            full_name=full_name,
            phone=spec["phone"],
            user_type="customer",
            permission_level=1,
        )

        existing = Customer.objects.filter(email=spec["email"]).first()
        if existing:
            existing.contracts.all().delete()
            existing.loans.all().delete()
            existing.bank_connections.all().delete()

        customer, _ = Customer.objects.update_or_create(
            email=spec["email"],
            defaults={
                "portal_user": user,
                "first_name": spec["first_name"],
                "last_name": spec["last_name"],
                "phone": spec["phone"],
                "phone_normalized": spec["phone_normalized"],
                "date_of_birth": date(1990 + index, index, min(index + 10, 28)),
                "address_line_1": f"{100 + index} QA Workflow Street",
                "city": "Toronto",
                "province": spec["province"],
                "postal_code": f"M{index}A {index}A{index}",
                "status": "active",
                "requested_loan_amount": spec["requested_amount"],
                "banking_verified": spec["banking_verified"],
                "references_completed": spec["references_completed"],
                "contract_completed": spec["contract_completed"],
                "onboarding_stage": spec["onboarding_stage"],
                "phone_verified": True,
                "phone_verified_at": timezone.now(),
                "job_place_name": "QA Payroll Inc." if spec["references_completed"] else None,
                "supervisor_name": "Morgan Manager" if spec["references_completed"] else None,
                "supervisor_phone": "+14165550900" if spec["references_completed"] else None,
                "reference_1_name": "Alex Reference" if spec["references_completed"] else None,
                "reference_1_phone": "+14165550901" if spec["references_completed"] else None,
                "reference_2_name": "Jordan Reference" if spec["references_completed"] else None,
                "reference_2_phone": "+14165550902" if spec["references_completed"] else None,
            },
        )

        bank_account = self._seed_banking(customer, index) if spec["banking_verified"] else None
        loan = LoanService.create_initial_application(customer)
        loan.status = spec["loan_status"]
        loan.ai_decision = spec["ai_decision"]
        loan.bank_account = bank_account
        loan.collections_account = bank_account
        if bank_account:
            loan.funding_destination = {
                "emt": {
                    "email": customer.email,
                    "source": "application",
                },
                "eft": {
                    "bank_account_id": str(bank_account.id),
                    "account": account_snapshot(bank_account),
                },
            }
        if spec["loan_status"] == "pending_funding":
            loan.approved_at = timezone.now()
            loan.approved_by = User.objects.get(email=STAFF_EMAIL)
        loan.save()

        if spec["contract_completed"]:
            signed_at = timezone.now()
            loan.contract_signed_at = signed_at
            loan.save(update_fields=["contract_signed_at", "updated_at"])
            Contract.objects.create(
                customer=customer,
                loan=loan,
                status="signed",
                agreement_version=AGREEMENT_VERSION,
                agreement_text=render_loan_agreement(customer, loan),
                typed_name=customer.full_name,
                signer_email=customer.email,
                signed_date=signed_at,
                accepted_terms=True,
                accepted_credit_check=True,
                accepted_banking_review=True,
                accepted_electronic_signature=True,
            )
        elif spec["banking_verified"]:
            Contract.objects.create(
                customer=customer,
                loan=loan,
                status="draft",
                agreement_version=AGREEMENT_VERSION,
                agreement_text=render_loan_agreement(customer, loan),
            )

        return customer

    def _seed_banking(self, customer, index):
        connection = BankConnection.objects.create(
            customer=customer,
            login_id=f"qa-workflow-login-{index}",
            provider="flinks",
            sync_status="synced",
            last_synced_at=timezone.now(),
        )
        account = BankAccount.objects.create(
            connection=connection,
            customer=customer,
            external_id=f"qa-workflow-account-{index}",
            name="QA Primary Chequing",
            type="checking",
            currency="CAD",
            balance=Decimal("2400.00") + Decimal(index * 250),
            institution_number="001",
            transit_number=f"1000{index}",
            account_number=f"900000{index}",
            is_primary=True,
            use_for_eft_funding=True,
            use_for_eft_collections=True,
        )
        FinancialAnalysisReport.objects.create(
            customer=customer,
            report_data={
                "avg_monthly_income": "3600.00",
                "risk_score": 25 + index,
                "detected_employer": "QA Payroll Inc.",
            },
        )
        return account
