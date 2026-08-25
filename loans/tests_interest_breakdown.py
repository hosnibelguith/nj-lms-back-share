from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Customer, User
from loans.models import Loan, LoanFormula, Payment
from loans.services import LoanService


class InterestBreakdownFailedPaymentTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email="interest-breakdown@example.com",
            password="password123",
            full_name="Interest Agent",
            user_type="staff",
            is_staff=True,
            permission_level=4,
        )
        self.customer = Customer.objects.create(
            first_name="Interest",
            last_name="Client",
            email="interest.client@example.com",
            phone="4165550199",
            province="ON",
            status="active",
        )
        self.formula = LoanFormula.objects.create(
            name="Interest Breakdown 500",
            principal_amount=Decimal("500.00"),
            brokerage_percent=Decimal("70.00"),
            repayment_percent=Decimal("35.00"),
            default_number_of_payments=6,
            default_frequency_days=14,
            is_active=True,
        )
        self.loan = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("500.00"),
            fee=Decimal("383.09"),
            total_amount=Decimal("883.09"),
            balance=Decimal("883.09"),
            status="active",
            formula=self.formula,
            is_active=True,
            funded_at=timezone.make_aware(datetime(2026, 8, 8, 12, 0)),
        )
        self.client.force_authenticate(user=self.staff)

    def _breakdown(self, **kwargs):
        return LoanService.get_interest_breakdown(self.loan, **kwargs)

    def test_clean_loan_omits_failed_payment_section(self):
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("147.18"),
            scheduled_date=date(2026, 8, 22),
            status="scheduled",
            type="scheduled",
        )
        data = self._breakdown(include_timeline=False)
        self.assertFalse(data["has_failed_payments"])
        self.assertIsNone(data["collection_failure_fees"])
        self.assertIsNone(data["updated_interest"])
        self.assertEqual(data["capital"], "500.00")
        self.assertEqual(data["brokerage_fee"], "350.00")
        self.assertEqual(data["planned_interest"], "33.09")
        self.assertEqual(data["total_amount"], "883.09")

    def test_nsf_loan_includes_fees_and_updated_interest(self):
        collection_id = str(uuid4())
        extra_interest = Decimal("9.13")
        nsf_fee = LoanService.COLLECTION_FAILURE_FEE_AMOUNT
        self.loan.fee = Decimal("442.22")
        self.loan.total_amount = Decimal("942.22")
        self.loan.balance = Decimal("942.22")
        self.loan.save(update_fields=["fee", "total_amount", "balance", "updated_at"])
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("147.18"),
            scheduled_date=date(2026, 8, 15),
            status="nsf",
            type="scheduled",
            failure_reason="EftFailedNsf",
        )
        Payment.objects.create(
            loan=self.loan,
            amount=nsf_fee + extra_interest,
            scheduled_date=date(2026, 8, 29),
            status="scheduled",
            type="scheduled",
            notes=(
                f"{LoanService.COLLECTION_FAILURE_FEE_NOTE}\n"
                f"Collection failure id: {collection_id}\n"
                "Reason: EftFailedNsf\n"
                f"NSF fee: ${nsf_fee}\n"
                f"Extension interest: ${extra_interest}"
            ),
        )

        data = self._breakdown(include_timeline=False)
        self.assertTrue(data["has_failed_payments"])
        self.assertEqual(data["collection_failure_fees"], "50.00")
        self.assertEqual(data["updated_interest"], "42.22")
        self.assertEqual(data["planned_interest"], "92.22")
        self.assertEqual(data["brokerage_fee"], "350.00")
        self.assertEqual(data["capital"], "500.00")
        payoff, unused = LoanService.payoff_today_amount(self.loan)
        self.assertEqual(data["payoff_today"], str(payoff))
        self.assertEqual(data["unused_daily_interest"], str(unused))

    def test_failed_payment_without_applied_fee_still_shows_section(self):
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("147.18"),
            scheduled_date=date(2026, 8, 15),
            status="failed",
            type="scheduled",
            failure_reason="EftFailedAccountClosed",
        )
        data = self._breakdown(include_timeline=False)
        self.assertTrue(data["has_failed_payments"])
        self.assertEqual(data["collection_failure_fees"], "0.00")
        self.assertEqual(data["updated_interest"], "33.09")
        self.assertEqual(data["planned_interest"], "33.09")

    def test_two_collection_failures_sum_fees_once_per_id(self):
        first_id = str(uuid4())
        second_id = str(uuid4())
        extra = Decimal("59.13")
        self.loan.fee = Decimal("383.09") + extra + extra
        self.loan.total_amount = Decimal("883.09") + extra + extra
        self.loan.balance = self.loan.total_amount
        self.loan.save(update_fields=["fee", "total_amount", "balance", "updated_at"])
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("147.18"),
            scheduled_date=date(2026, 8, 15),
            status="nsf",
            type="scheduled",
        )
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("147.18"),
            scheduled_date=date(2026, 8, 22),
            status="failed",
            type="scheduled",
        )
        Payment.objects.create(
            loan=self.loan,
            amount=extra + extra,
            scheduled_date=date(2026, 9, 5),
            status="scheduled",
            type="scheduled",
            notes=(
                f"{LoanService.COLLECTION_FAILURE_FEE_NOTE}\n"
                f"Collection failure id: {first_id}\n"
                "NSF fee: $50.00\n"
                "Extension interest: $9.13\n"
                f"Collection failure id: {second_id}\n"
                "NSF fee: $50.00\n"
                "Extension interest: $9.13"
            ),
        )
        data = self._breakdown(include_timeline=False)
        self.assertTrue(data["has_failed_payments"])
        self.assertEqual(data["collection_failure_fees"], "100.00")
        self.assertEqual(data["updated_interest"], "51.35")

    def test_interest_breakdown_api_returns_failed_payment_section(self):
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("147.18"),
            scheduled_date=timezone.localdate() - timedelta(days=7),
            status="nsf",
            type="scheduled",
            notes=(
                f"{LoanService.COLLECTION_FAILURE_FEE_NOTE}\n"
                "NSF fee: $50.00"
            ),
        )
        self.loan.fee = Decimal("433.09")
        self.loan.total_amount = Decimal("933.09")
        self.loan.balance = Decimal("933.09")
        self.loan.save(update_fields=["fee", "total_amount", "balance", "updated_at"])

        response = self.client.get(f"/api/loans/{self.loan.id}/interest-breakdown/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["has_failed_payments"])
        self.assertEqual(response.data["collection_failure_fees"], "50.00")
        self.assertEqual(response.data["updated_interest"], "33.09")
        self.assertEqual(response.data["planned_interest"], "83.09")
        self.assertIn("payoff_today", response.data)
        self.assertIn("timeline", response.data)

    def test_interest_breakdown_api_hides_section_without_failed_payment(self):
        response = self.client.get(f"/api/loans/{self.loan.id}/interest-breakdown/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["has_failed_payments"])
        self.assertIsNone(response.data["collection_failure_fees"])
        self.assertIsNone(response.data["updated_interest"])
        self.assertEqual(response.data["planned_interest"], "33.09")
