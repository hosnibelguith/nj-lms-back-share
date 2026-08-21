from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Customer, User
from loans.collection_policy import (
    AUTO_STOP_MODE_AFTER_MISSED,
    AUTO_STOP_MODE_MANUAL,
    classify_failure_reason,
    is_problematic_counts,
    save_settings,
    should_auto_stop_loan,
)
from loans.models import CollectionPayment, Loan, Payment
from loans.services import LoanService


class CollectionPolicyTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email="collection-policy@example.com",
            password="password123",
            full_name="Policy Admin",
            user_type="staff",
            is_staff=True,
            permission_level=4,
        )
        self.agent = User.objects.create_user(
            email="collection-agent@example.com",
            password="password123",
            full_name="Policy Agent",
            user_type="staff",
            is_staff=True,
            permission_level=2,
        )
        self.customer = Customer.objects.create(
            first_name="Policy",
            last_name="Customer",
            email="policy-customer@example.com",
            phone="4165550199",
            province="ON",
            status="active",
        )
        self.loan = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("500.00"),
            fee=Decimal("100.00"),
            total_amount=Decimal("600.00"),
            balance=Decimal("600.00"),
            status="active",
            is_active=True,
        )
        self.client.force_authenticate(user=self.staff)

    def _failed_collection(self, reason):
        payment = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("147.18"),
            status="failed",
            scheduled_date=timezone.localdate(),
            failure_reason=reason,
        )
        return CollectionPayment.objects.create(
            loan=self.loan,
            payment=payment,
            amount=payment.amount,
            status="failed",
            failure_reason=reason,
        )

    def test_classifies_stop_payment_and_nsf_reasons(self):
        self.assertEqual(classify_failure_reason("EftFailedStopPayment"), "stop_payment")
        self.assertEqual(classify_failure_reason("EftFailedInsufficientFunds"), "nsf")
        self.assertEqual(classify_failure_reason("EftFailedAccountClosed"), "account_closed")
        self.assertEqual(classify_failure_reason("EftFailedNoDebitAllowed"), "account_closed")
        self.assertEqual(classify_failure_reason("EftFailedFrozenAccount"), "account_closed")

    def test_account_closed_always_auto_stops(self):
        save_settings(mode=AUTO_STOP_MODE_MANUAL, missed_count=5)
        self._failed_collection("EftFailedAccountClosed")
        self.assertTrue(should_auto_stop_loan(self.loan, "EftFailedAccountClosed"))

    def test_nsf_does_not_stop_on_first_failure_when_threshold_is_three(self):
        save_settings(mode=AUTO_STOP_MODE_AFTER_MISSED, missed_count=3)
        upcoming = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("147.18"),
            status="scheduled",
            scheduled_date=timezone.localdate() + timedelta(days=14),
        )
        collection = self._failed_collection("EftFailedInsufficientFunds")
        LoanService.apply_collection_failure_fee(
            collection,
            reason="EftFailedInsufficientFunds",
        )
        self.loan.refresh_from_db()
        upcoming.refresh_from_db()
        self.assertEqual(self.loan.status, "defaulted")
        self.assertFalse(self.loan.is_active)
        self.assertEqual(upcoming.status, "scheduled")

    def test_two_nsfs_stay_in_collections_and_keep_future_payments_scheduled(self):
        save_settings(mode=AUTO_STOP_MODE_AFTER_MISSED, missed_count=3)
        upcoming = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("147.18"),
            status="scheduled",
            scheduled_date=timezone.localdate() + timedelta(days=14),
        )
        first_collection = self._failed_collection("EftFailedInsufficientFunds")
        LoanService.apply_collection_failure_fee(
            first_collection,
            reason="EftFailedInsufficientFunds",
        )
        collection = self._failed_collection("EftFailedInsufficientFunds")
        LoanService.apply_collection_failure_fee(
            collection,
            reason="EftFailedInsufficientFunds",
        )
        self.loan.refresh_from_db()
        upcoming.refresh_from_db()
        self.assertEqual(self.loan.status, "defaulted")
        self.assertEqual(upcoming.status, "scheduled")

    def test_third_nsf_auto_stops_when_mode_is_after_missed(self):
        save_settings(mode=AUTO_STOP_MODE_AFTER_MISSED, missed_count=3)
        upcoming = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("147.18"),
            status="scheduled",
            scheduled_date=timezone.localdate() + timedelta(days=14),
        )
        self._failed_collection("EftFailedInsufficientFunds")
        self._failed_collection("EftFailedInsufficientFunds")
        collection = self._failed_collection("EftFailedInsufficientFunds")
        LoanService.apply_collection_failure_fee(
            collection,
            reason="EftFailedInsufficientFunds",
        )
        self.loan.refresh_from_db()
        upcoming.refresh_from_db()
        self.assertEqual(self.loan.status, "stopped")
        self.assertFalse(self.loan.is_active)
        self.assertEqual(upcoming.status, "unscheduled")

    def test_account_closed_stops_and_unschedules_immediately(self):
        save_settings(mode=AUTO_STOP_MODE_AFTER_MISSED, missed_count=3)
        upcoming = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("147.18"),
            status="scheduled",
            scheduled_date=timezone.localdate() + timedelta(days=14),
        )
        collection = self._failed_collection("EftFailedAccountClosed")
        LoanService.apply_collection_failure_fee(
            collection,
            reason="EftFailedAccountClosed",
        )
        self.loan.refresh_from_db()
        upcoming.refresh_from_db()
        self.assertEqual(self.loan.status, "stopped")
        self.assertEqual(upcoming.status, "unscheduled")

    def test_no_debit_allowed_stops_and_unschedules_immediately(self):
        save_settings(mode=AUTO_STOP_MODE_AFTER_MISSED, missed_count=3)
        upcoming = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("147.18"),
            status="scheduled",
            scheduled_date=timezone.localdate() + timedelta(days=14),
        )
        collection = self._failed_collection("EftFailedNoDebitAllowed")
        LoanService.apply_collection_failure_fee(
            collection,
            reason="EftFailedNoDebitAllowed",
        )
        self.loan.refresh_from_db()
        upcoming.refresh_from_db()
        self.assertEqual(self.loan.status, "stopped")
        self.assertEqual(upcoming.status, "unscheduled")

    def test_manual_mode_leaves_stop_payment_in_collections(self):
        save_settings(mode=AUTO_STOP_MODE_MANUAL, missed_count=1)
        collection = self._failed_collection("EftFailedStopPayment")
        LoanService.apply_collection_failure_fee(
            collection,
            reason="EftFailedStopPayment",
        )
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "defaulted")

    def test_three_nsf_or_two_stop_payments_are_problematic(self):
        self.assertTrue(
            is_problematic_counts(
                {
                    "nsf": 3,
                    "stop_payment": 0,
                    "account_closed": 0,
                    "other": 0,
                    "total": 3,
                }
            )
        )
        self.assertTrue(
            is_problematic_counts(
                {
                    "nsf": 0,
                    "stop_payment": 2,
                    "account_closed": 0,
                    "other": 0,
                    "total": 2,
                }
            )
        )
        self.assertFalse(
            is_problematic_counts(
                {
                    "nsf": 1,
                    "stop_payment": 1,
                    "account_closed": 0,
                    "other": 0,
                    "total": 2,
                }
            )
        )

    def test_collection_settings_api_requires_manager_to_update(self):
        response = self.client.get("/api/loans/collection-settings/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn(
            response.data["mode"],
            [AUTO_STOP_MODE_MANUAL, AUTO_STOP_MODE_AFTER_MISSED],
        )

        updated = self.client.patch(
            "/api/loans/collection-settings/",
            {"mode": "manual", "missed_count": 4},
            format="json",
        )
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertEqual(updated.data["mode"], "manual")
        self.assertEqual(updated.data["missed_count"], 4)

        self.client.force_authenticate(user=self.agent)
        denied = self.client.patch(
            "/api/loans/collection-settings/",
            {"mode": "after_missed", "missed_count": 2},
            format="json",
        )
        self.assertEqual(denied.status_code, 403)

    def test_problematic_collections_lists_active_risk_loans(self):
        save_settings(mode=AUTO_STOP_MODE_MANUAL, missed_count=3)
        for _ in range(3):
            self._failed_collection("EftFailedInsufficientFunds")
        response = self.client.get("/api/loans/problematic-collections/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(str(response.data["results"][0]["loan_id"]), str(self.loan.id))
        self.assertIn(
            "problematic account",
            response.data["results"][0]["risk_label"].lower(),
        )
        listed = self.client.get("/api/loans/returned-collections/")
        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertTrue(listed.data["results"][0]["is_problematic"])
