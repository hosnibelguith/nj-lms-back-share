from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Customer, Lender, User
from loans.models import CollectionPayment, Loan, LoanFormula, Payment
from loans.services import LoanService


class LendingSettingsTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            lender=self.lender,
            email="admin@example.com",
            password="pass",
            full_name="Admin User",
            user_type="staff",
            permission_level=5,
        )
        self.agent = User.objects.create_user(
            lender=self.lender,
            email="agent@example.com",
            password="pass",
            full_name="Agent User",
            user_type="staff",
            permission_level=2,
        )
        self.customer = Customer.objects.create(
            lender=self.lender,
            first_name="Jane",
            last_name="Borrower",
            email="jane@example.com",
            phone="4165550101",
            phone_normalized="4165550101",
            requested_loan_amount=Decimal("500.00"),
            banking_verified=True,
        )
        self.formula = LoanFormula.objects.create(
            lender=self.lender,
            name="Default 500",
            principal_amount=Decimal("500.00"),
            brokerage_percent=Decimal("70.00"),
            repayment_percent=Decimal("35.00"),
            default_number_of_payments=4,
            default_frequency_days=14,
            is_active=True,
            is_default=True,
        )

    @classmethod
    def setUpTestData(cls):
        cls.lender = Lender.objects.create(
            name="MohawkLoans",
            slug="mohawk-test",
            primary_domain="mohawk-test.local",
        )

    def test_manager_can_save_global_lending_settings(self):
        self.client.force_authenticate(self.admin)
        response = self.client.patch(
            "/api/loans/lending-settings/",
            {
                "nsf_fee_amount": "75.00",
                "brokerage_percent": "65.00",
                "interest_percent": "31.50",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["nsf_fee_amount"], Decimal("75.00"))
        self.assertEqual(response.data["brokerage_percent"], Decimal("65.00"))
        self.assertEqual(response.data["interest_percent"], Decimal("31.50"))
        self.lender.refresh_from_db()
        self.assertEqual(self.lender.nsf_fee_amount, Decimal("75.00"))
        self.assertEqual(self.lender.brokerage_percent, Decimal("65.00"))
        self.assertEqual(self.lender.interest_percent, Decimal("31.50"))
        self.formula.refresh_from_db()
        self.assertEqual(self.formula.brokerage_percent, Decimal("70.00"))
        self.assertEqual(self.formula.repayment_percent, Decimal("35.00"))

    def test_lending_settings_are_scoped_to_current_lender(self):
        other_lender = Lender.objects.create(
            name="NovaLoans",
            slug="novaloans-test",
            primary_domain="novaloans-test.local",
            nsf_fee_amount=Decimal("25.00"),
            brokerage_percent=Decimal("40.00"),
            interest_percent=Decimal("15.00"),
        )
        other_formula = LoanFormula.objects.create(
            lender=other_lender,
            name="Nova 500",
            principal_amount=Decimal("500.00"),
            brokerage_percent=Decimal("40.00"),
            repayment_percent=Decimal("15.00"),
            default_number_of_payments=4,
            default_frequency_days=14,
            is_active=True,
            is_default=True,
        )

        self.client.force_authenticate(self.admin)
        response = self.client.patch(
            "/api/loans/lending-settings/",
            {
                "nsf_fee_amount": "75.00",
                "brokerage_percent": "65.00",
                "interest_percent": "31.50",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        other_lender.refresh_from_db()
        other_formula.refresh_from_db()
        self.assertEqual(other_lender.nsf_fee_amount, Decimal("25.00"))
        self.assertEqual(other_lender.brokerage_percent, Decimal("40.00"))
        self.assertEqual(other_lender.interest_percent, Decimal("15.00"))
        self.assertEqual(other_formula.brokerage_percent, Decimal("40.00"))
        self.assertEqual(other_formula.repayment_percent, Decimal("15.00"))

    def test_agent_cannot_save_global_lending_settings(self):
        self.client.force_authenticate(self.agent)
        response = self.client.patch(
            "/api/loans/lending-settings/",
            {
                "nsf_fee_amount": "75.00",
                "brokerage_percent": "65.00",
                "interest_percent": "31.50",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 403, response.data)

    def test_new_application_uses_global_brokerage_and_interest_settings(self):
        self.lender.brokerage_percent = Decimal("60.00")
        self.lender.interest_percent = Decimal("20.00")
        self.lender.save(update_fields=["brokerage_percent", "interest_percent"])

        loan = LoanService.create_initial_application(self.customer)

        self.assertEqual(loan.pricing_brokerage_percent, Decimal("60.00"))
        self.assertEqual(loan.pricing_interest_percent, Decimal("20.00"))
        self.assertEqual(loan.total_amount, Decimal("815.34"))
        self.formula.refresh_from_db()
        self.assertEqual(self.formula.brokerage_percent, Decimal("70.00"))
        self.assertEqual(self.formula.repayment_percent, Decimal("35.00"))

    def test_existing_loan_interest_breakdown_uses_pricing_snapshot(self):
        loan = Loan.objects.create(
            customer=self.customer,
            formula=self.formula,
            principal=Decimal("500.00"),
            fee=Decimal("421.83"),
            total_amount=Decimal("921.83"),
            balance=Decimal("921.83"),
            status="active",
            pricing_brokerage_percent=Decimal("70.00"),
            pricing_interest_percent=Decimal("35.00"),
            pricing_number_of_payments=5,
            pricing_frequency_days=7,
            funded_at=timezone.now(),
        )

        self.client.force_authenticate(self.admin)
        response = self.client.patch(
            "/api/loans/lending-settings/",
            {
                "nsf_fee_amount": "75.00",
                "brokerage_percent": "60.00",
                "interest_percent": "20.00",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        breakdown = LoanService.get_interest_breakdown(loan, include_timeline=False)
        self.assertEqual(breakdown["brokerage_fee"], "350.00")
        self.assertEqual(breakdown["planned_interest"], "71.83")

    def test_collection_failure_uses_global_nsf_fee(self):
        self.lender.nsf_fee_amount = Decimal("75.00")
        self.lender.save(update_fields=["nsf_fee_amount"])
        loan = Loan.objects.create(
            customer=self.customer,
            formula=self.formula,
            principal=Decimal("500.00"),
            fee=Decimal("96.00"),
            total_amount=Decimal("596.00"),
            balance=Decimal("596.00"),
            status="active",
        )
        payment = Payment.objects.create(
            loan=loan,
            amount=Decimal("149.00"),
            type="scheduled",
            status="nsf",
            scheduled_date=timezone.localdate(),
            failure_reason="Non-sufficient funds",
        )
        collection = CollectionPayment.objects.create(
            loan=loan,
            payment=payment,
            amount=payment.amount,
            status="failed",
            failure_reason="Non-sufficient funds",
            returned_at=timezone.now(),
        )

        fee_payment = LoanService.apply_collection_failure_fee(collection)

        self.assertIsNotNone(fee_payment)
        self.assertTrue(
            loan.payments.filter(notes__contains="NSF fee: $75.00").exists()
        )
        loan.refresh_from_db()
        self.assertGreater(loan.total_amount, Decimal("596.00") + Decimal("75.00"))
