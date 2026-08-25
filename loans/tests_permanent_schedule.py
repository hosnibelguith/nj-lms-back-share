from datetime import date, datetime, timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Customer, User
from loans.models import Loan, LoanFormula, Payment
from loans.services import LoanService


class PermanentFailedPaymentScheduleTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email="permanent-schedule@example.com",
            password="password123",
            full_name="Schedule Agent",
            user_type="staff",
            is_staff=True,
            permission_level=4,
        )
        self.customer = Customer.objects.create(
            first_name="Permanent",
            last_name="Client",
            email="permanent.client@example.com",
            phone="4165550177",
            province="ON",
            status="active",
        )
        self.formula = LoanFormula.objects.create(
            name="Permanent Schedule 500",
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
            fee=Decimal("433.09"),
            total_amount=Decimal("933.09"),
            balance=Decimal("933.09"),
            status="active",
            formula=self.formula,
            is_active=True,
            funded_at=timezone.make_aware(datetime(2026, 8, 8, 12, 0)),
        )
        self.missed = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("147.18"),
            scheduled_date=date(2026, 8, 15),
            status="nsf",
            type="scheduled",
            failure_reason="EftFailedNsf",
        )
        self.fee = Payment.objects.create(
            loan=self.loan,
            amount=LoanService.COLLECTION_FAILURE_FEE_AMOUNT,
            scheduled_date=timezone.localdate() + timedelta(days=28),
            status="scheduled",
            type="scheduled",
            notes=(
                f"{LoanService.COLLECTION_FAILURE_FEE_NOTE}\n"
                "NSF fee: $50.00\n"
                "Extension interest: $0.00"
            ),
        )
        self.pad = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("883.09"),
            scheduled_date=timezone.localdate() + timedelta(days=14),
            status="scheduled",
            type="scheduled",
        )
        self.client.force_authenticate(user=self.staff)

    def test_schedule_payload_flags_collection_failure_extra(self):
        response = self.client.get(f"/api/customers/{self.customer.id}/loans/")
        self.assertEqual(response.status_code, 200, response.data)
        loan_payload = next(
            item for item in response.data if item["id"] == str(self.loan.id)
        )
        rows = {row["id"]: row for row in loan_payload["paymentSchedule"]}
        self.assertTrue(rows[str(self.fee.id)]["is_collection_failure_extra"])
        self.assertFalse(rows[str(self.pad.id)]["is_collection_failure_extra"])
        self.assertFalse(rows[str(self.missed.id)]["is_collection_failure_extra"])

    def test_edit_and_defer_reject_collection_failure_fee(self):
        with self.assertRaises(ValueError):
            LoanService.update_scheduled_payment(
                self.fee,
                amount=Decimal("40.00"),
                user=self.staff,
            )
        with self.assertRaises(ValueError):
            LoanService.defer_scheduled_payment(self.fee, user=self.staff)
        self.fee.refresh_from_db()
        self.assertEqual(self.fee.amount, LoanService.COLLECTION_FAILURE_FEE_AMOUNT)

    def test_api_rejects_edit_defer_and_delete_of_fee_and_nsf(self):
        edit = self.client.patch(
            f"/api/payments/{self.fee.id}/",
            {"amount": "40.00"},
            format="json",
        )
        self.assertEqual(edit.status_code, 400, edit.data)
        defer = self.client.post(f"/api/payments/{self.fee.id}/defer/")
        self.assertEqual(defer.status_code, 400, defer.data)
        delete_fee = self.client.delete(f"/api/payments/{self.fee.id}/")
        self.assertEqual(delete_fee.status_code, 400, delete_fee.data)
        delete_nsf = self.client.delete(f"/api/payments/{self.missed.id}/")
        self.assertEqual(delete_nsf.status_code, 400, delete_nsf.data)
        self.assertTrue(Payment.objects.filter(pk=self.fee.pk).exists())
        self.assertTrue(Payment.objects.filter(pk=self.missed.pk).exists())

    def test_adjust_schedule_keeps_nsf_history_and_fee_row(self):
        LoanService.adjust_payment_schedule(
            self.loan,
            payment_amount=Decimal("50.00"),
            frequency="bi-weekly",
            start_date=timezone.localdate() + timedelta(days=7),
            user=self.staff,
        )
        self.loan.refresh_from_db()
        self.missed.refresh_from_db()
        self.fee.refresh_from_db()
        self.assertEqual(self.missed.status, "nsf")
        self.assertEqual(self.missed.amount, Decimal("147.18"))
        self.assertEqual(self.fee.status, "scheduled")
        self.assertEqual(self.fee.amount, LoanService.COLLECTION_FAILURE_FEE_AMOUNT)
        self.assertTrue(
            (self.fee.notes or "").startswith(LoanService.COLLECTION_FAILURE_FEE_NOTE)
        )
        scheduled_total = sum(
            (row.amount for row in self.loan.payments.filter(status="scheduled")),
            Decimal("0.00"),
        )
        self.assertEqual(scheduled_total, self.loan.balance)
        self.assertGreaterEqual(self.loan.fee, LoanService.COLLECTION_FAILURE_FEE_AMOUNT)

    def test_heal_does_not_delete_nsf_or_fee_rows(self):
        plan = LoanService.heal_upcoming_schedule_keeping_pending(
            self.loan,
            payment_amount=Decimal("147.18"),
            frequency="bi-weekly",
            dry_run=False,
            user=self.staff,
        )
        self.assertTrue(Payment.objects.filter(pk=self.missed.pk, status="nsf").exists())
        self.assertTrue(
            Payment.objects.filter(
                pk=self.fee.pk,
                notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE,
            ).exists()
        )
        deleted_ids = {row["id"] for row in plan["will_delete"]}
        self.assertNotIn(str(self.missed.id), deleted_ids)
        self.assertNotIn(str(self.fee.id), deleted_ids)
