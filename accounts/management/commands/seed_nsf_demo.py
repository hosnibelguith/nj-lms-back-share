from datetime import date
from decimal import Decimal
import uuid

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import Customer, Lender, User
from loans.models import Loan, LoanFormula, Payment


PASSWORD = "TestPass123!"
STAFF_EMAIL = "sandbox.staff@example.com"


CASES = [
    {
        "email": "sandbox.failed.one@example.com",
        "first": "Mohawk",
        "last": "Failed One",
        "phone": "+14165558101",
        "reason": "EftFailedAccountClosed",
        "balance": Decimal("853.45"),
        "fee": Decimal("433.09"),
        "total": Decimal("933.09"),
        "payments": [
            ("2026-08-31", "147.18", "completed", ""),
            ("2026-09-15", "147.18", "failed", "EftFailedAccountClosed"),
            ("2026-09-30", "147.18", "unscheduled", ""),
            ("2026-10-15", "147.18", "unscheduled", ""),
            ("2026-10-30", "147.18", "unscheduled", ""),
            ("2026-11-13", "147.19", "unscheduled", ""),
            ("2026-11-27", "147.18", "unscheduled", ""),
            ("2026-12-11", "67.54", "unscheduled", ""),
            ("2026-12-25", "50.00", "unscheduled", "fee"),
        ],
    },
    {
        "email": "sandbox.two.nsf@example.com",
        "first": "Mohawk",
        "last": "Two NSF",
        "phone": "+14165558102",
        "reason": "EftFailedInsufficientFunds",
        "balance": Decimal("1003.45"),
        "fee": Decimal("533.09"),
        "total": Decimal("1033.09"),
        "payments": [
            ("2026-08-31", "147.18", "completed", ""),
            ("2026-09-15", "147.18", "nsf", "EftFailedInsufficientFunds"),
            ("2026-09-30", "147.18", "nsf", "EftFailedInsufficientFunds"),
            ("2026-10-15", "147.18", "unscheduled", ""),
            ("2026-10-30", "147.18", "unscheduled", ""),
            ("2026-11-13", "147.19", "unscheduled", ""),
            ("2026-11-27", "147.18", "unscheduled", ""),
            ("2026-12-11", "147.18", "unscheduled", ""),
            ("2026-12-25", "214.72", "unscheduled", ""),
            ("2027-01-08", "50.00", "unscheduled", "fee"),
            ("2027-01-22", "50.00", "unscheduled", "fee"),
        ],
    },
    {
        "email": "sandbox.stop.payment@example.com",
        "first": "Mohawk",
        "last": "Stop Payment",
        "phone": "+14165558103",
        "reason": "EftFailedStopPayment",
        "balance": Decimal("903.45"),
        "fee": Decimal("483.09"),
        "total": Decimal("983.09"),
        "payments": [
            ("2026-08-31", "147.18", "completed", ""),
            ("2026-09-15", "147.18", "failed", "EftFailedStopPayment"),
            ("2026-09-30", "147.18", "unscheduled", ""),
            ("2026-10-15", "147.18", "unscheduled", ""),
            ("2026-10-30", "147.18", "unscheduled", ""),
            ("2026-11-13", "147.19", "unscheduled", ""),
            ("2026-11-27", "147.18", "unscheduled", ""),
            ("2026-12-11", "117.54", "unscheduled", ""),
            ("2026-12-25", "50.00", "unscheduled", "fee"),
        ],
    },
]


class Command(BaseCommand):
    help = "Seed local NSF balance-after demo customers."

    def handle(self, *args, **options):
        lender = Lender.default()
        staff = self._user(
            email=STAFF_EMAIL,
            full_name="Sandbox Staff",
            phone="+14165558000",
            lender=lender,
            user_type="staff",
            is_staff=True,
            is_superuser=True,
            permission_level=5,
        )

        formula, _ = LoanFormula.objects.update_or_create(
            lender=lender,
            name="Mohawk Sandbox 500 NSF Demo",
            defaults={
                "principal_amount": Decimal("500.00"),
                "brokerage_percent": Decimal("70.00"),
                "repayment_percent": Decimal("35.00"),
                "default_number_of_payments": 7,
                "default_frequency_days": 14,
                "is_active": True,
            },
        )

        self.stdout.write(self.style.SUCCESS("Staff login"))
        self.stdout.write(f"  {staff.email} / {PASSWORD}")
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Demo customers"))

        for case in CASES:
            customer = self._customer(case, lender)
            loan = self._loan(customer, formula, case)
            self.stdout.write(
                f"  {customer.email} / {PASSWORD} | {customer.full_name} | loan={loan.id}"
            )

    def _user(self, **kwargs):
        email = kwargs.pop("email")
        user, _ = User.objects.update_or_create(email=email, defaults=kwargs)
        user.set_password(PASSWORD)
        user.save(update_fields=["password"])
        return user

    def _customer(self, case, lender):
        portal = self._user(
            email=case["email"],
            lender=lender,
            full_name=f"{case['first']} {case['last']}",
            phone=case["phone"],
            phone_normalized=case["phone"],
            user_type="customer",
            permission_level=1,
            is_staff=False,
            is_superuser=False,
            is_active=True,
        )
        existing = Customer.objects.filter(email=case["email"]).first()
        if existing:
            existing.loans.all().delete()
        customer, _ = Customer.objects.update_or_create(
            email=case["email"],
            defaults={
                "lender": lender,
                "portal_user": portal,
                "first_name": case["first"],
                "last_name": case["last"],
                "phone": case["phone"],
                "phone_normalized": case["phone"],
                "province": "ON",
                "city": "Toronto",
                "status": "active",
                "onboarding_stage": "portal_active",
                "banking_verified": True,
                "contract_completed": True,
                "requested_loan_amount": Decimal("500.00"),
                "phone_verified": True,
                "phone_verified_at": timezone.now(),
            },
        )
        return customer

    def _loan(self, customer, formula, case):
        loan = Loan.objects.create(
            customer=customer,
            formula=formula,
            type="nojuice",
            principal=Decimal("500.00"),
            fee=case["fee"],
            total_amount=case["total"],
            balance=case["balance"],
            status="defaulted",
            is_active=False,
            funded_at=timezone.make_aware(timezone.datetime(2026, 8, 15, 12, 0)),
        )
        for payment_date, amount, status, marker in case["payments"]:
            notes = ""
            failure_reason = marker
            if marker == "fee":
                failure_reason = ""
                notes = (
                    "Collection failure fee\n"
                    f"Collection failure id: {uuid.uuid4()}\n"
                    f"Reason: {case['reason']}\n"
                    "NSF fee: $50.00\n"
                    "Extension interest: $0.00"
                )
            Payment.objects.create(
                loan=loan,
                amount=Decimal(amount),
                type="scheduled",
                status=status,
                scheduled_date=date.fromisoformat(payment_date),
                processed_at=timezone.now() if status == "completed" else None,
                failure_reason=failure_reason,
                notes=notes,
            )
        return loan
